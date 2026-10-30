"""采集 / 选品 / 导出流水线。"""
from __future__ import annotations

import datetime as _dt
import os
from typing import Any, Iterable

from .api import ApiError, GouVip
from .scoring import DEFAULT_CONFIG, merge_config, rank
from .store import Store, now


# ------------------------------------------------------------------ 行业榜单
def sync_industry(api: GouVip, store: Store, cat_tab_id: int, with_subs: bool = True,
                  progress=None) -> dict:
    """拉取一个一级类目下的「子榜单目录 + 榜单条目」，并做新旧对比。"""
    run_id = store.start_run("industry", {"cat": cat_tab_id})
    cats = api.industry_cats()
    store.upsert_cat_top(cats)
    cats_map = {c["tab_id"]: c["tab_name"] for c in cats}
    cat_name = cats_map.get(cat_tab_id, str(cat_tab_id))

    subs = api.industry_subs(cat_tab_id) if with_subs else []
    if subs:
        store.upsert_cat_sub(cat_tab_id, subs)

    targets: list[dict] = []
    # 子榜单目录（如 精选/柑桔橙柚/...) 每个下面还有一层榜单条目
    for s in subs or [{"tab_id": cat_tab_id, "tab_name": cat_name}]:
        try:
            res = api.industry_rankings(s["tab_id"])
        except ApiError as e:
            if progress:
                progress(f"  ! 子榜 {s.get('tab_name')} 拉取失败：{e}")
            continue
        for it in res["items"]:
            it.setdefault("superior_tab_id", s["tab_id"])
            targets.append(it)
        if progress:
            progress(f"  · {s.get('tab_name')}: {len(res['items'])} 条榜单")

    diff = store.upsert_rankings(run_id, targets)
    store.finish_run(run_id, len(targets), f"cat={cat_name}")
    return {
        "run_id": run_id,
        "cat": cat_name,
        "subs": len(subs),
        "rankings": len(targets),
        "new": diff["new"],
        "total_in_db": diff["total"],
    }


def sync_rankings_by_keyword(api: GouVip, store: Store, keyword: str) -> dict:
    """跨类目按关键词找榜单（type=44），命中即为该细分行业下的所有榜单。"""
    run_id = store.start_run("rank_search", {"keyword": keyword})
    items = api.search_rankings(keyword)
    diff = store.upsert_rankings(run_id, items)
    store.finish_run(run_id, len(items), f"keyword={keyword}")
    return {"run_id": run_id, "keyword": keyword, "rankings": len(items),
            "new": diff["new"], "items": items}


# ------------------------------------------------------------------ 商品池
def sync_goods(api: GouVip, store: Store, keywords: Iterable[str], source: str = "today",
               date: str | None = None, pages: int = 2, size: int = 100,
               sort: int = 0, cat: int | str = 0, progress=None) -> dict:
    """按关键词/类目翻页拉热销商品池，逐轮快照，供差异追踪。"""
    date = date or _dt.date.today().isoformat()
    kws = [k for k in (keywords or [""])]
    run_id = store.start_run("goods", {"keywords": kws, "source": source, "date": date,
                                      "pages": pages, "size": size, "cat": cat})
    total_rows, total_hint = 0, None
    for kw in kws:
        for p in range(1, max(1, pages) + 1):
            try:
                res = api.hot_goods(date=date, page=p, size=size, search=kw,
                                    sort=sort, cat=cat, source=source)
            except ApiError as e:
                if progress:
                    progress(f"  ! 关键词[{kw}] 第{p}页失败：{e}")
                break
            total_hint = res["total"]
            rows = res["pageList"]
            if not rows:
                break
            n = store.upsert_goods(run_id, rows, source=source, biz_date=date, tag=kw)
            total_rows += n
            if progress:
                progress(f"  · [{kw or '全部'}] 第{p}页 {len(rows)} 条（接口 total={res['total']}）")
            if len(rows) < size:
                break
    store.finish_run(run_id, total_rows, f"source={source} date={date}")
    return {"run_id": run_id, "rows": total_rows, "api_total": total_hint, "date": date,
            "source": source, "keywords": kws}


# ------------------------------------------------------------------ 关键词扩展
_RANK_SUFFIXES = ("实时", "年度", "热销", "飙升", "精选", "新品", "热卖", "好评",
                  "畅销", "降价", "品牌", "榜")


def keyword_candidates(name: str) -> list[str]:
    """把「榜单名」扩展成可用的商品搜索词。

    商品池是按商品标题匹配的，榜单名往往太具体（如「加厚垃圾桶」只命中 1 条）。
    中文商品名词多在后缀，所以依次退化为：原名 → 去榜单词 → 尾部 4/3/2 字。
    """
    name = (name or "").strip()
    out: list[str] = []

    def push(k: str) -> None:
        k = k.strip()
        if k and k not in out:
            out.append(k)

    push(name)
    base = name
    for s in _RANK_SUFFIXES:
        base = base.replace(s, "")
    push(base)
    for n in (4, 3, 2):
        if len(base) > n:
            push(base[-n:])
    return out


def sync_goods_by_rank(api: GouVip, store: Store, rank_name: str, min_pool: int = 20,
                       pages: int = 1, size: int = 100, source: str = "today",
                       sort: int = 1, cat: int | str = 0, progress=None) -> dict:
    """按榜单名自动退化关键词，直到商品池数量够用。"""
    tried = []
    for kw in keyword_candidates(rank_name):
        res = sync_goods(api, store, [kw], source=source, pages=pages, size=size,
                         sort=sort, cat=cat, progress=None)
        tried.append({"kw": kw, "rows": res["rows"], "api_total": res["api_total"]})
        if progress:
            progress(f"  · 关键词[{kw}] → {res['rows']} 条（total={res['api_total']}）")
        if (res["api_total"] or 0) >= min_pool:
            res["tried"] = tried
            return res
    res["tried"] = tried
    return res


# ------------------------------------------------------------------ 选品
def pick(store: Store, run_id: int | None = None, config: dict | None = None,
         progress=None) -> dict:
    cfg = merge_config(DEFAULT_CONFIG, config)
    run_id = run_id or store.latest_run_id("goods")
    if not run_id:
        raise RuntimeError("还没有采集过商品池，请先执行 sync")
    cands = store.candidates(run_id)
    passed, dropped = rank(cands, cfg)
    fresh = store.freshness()
    if progress and fresh["warning"]:
        progress(f"  ⚠ 数据陈旧：最新商品 update_time={fresh['data_update_time']}"
                 f"（距今 {fresh['stale_days']} 天），结果仅供参考")
    return {"run_id": run_id, "candidates": len(cands), "passed": passed,
            "dropped": dropped, "config": cfg, "freshness": fresh}


# ------------------------------------------------------------------ 导出
GOODS_URL = "https://mobile.yangkeduo.com/goods.html?goods_id={}"

HEADERS = ["排名", "得分", "商品名", "商品ID", "价格", "价格上限", "销量(区间上限)",
           "增长率", "上架时间", "命中榜单数", "信号", "分项", "商品链接"]


def export_excel(path: str, result: dict, rankings: list[dict] | None = None,
                 meta: dict | None = None) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    wb = Workbook()

    ws = wb.active
    ws.title = "选品清单"
    head_fill = PatternFill("solid", fgColor="283579")
    head_font = Font(color="FFFFFF", bold=True)
    ws.append(HEADERS)
    for c in ws[1]:
        c.fill, c.font = head_fill, head_font
        c.alignment = Alignment(horizontal="center", vertical="center")

    for i, r in enumerate(result["passed"], 1):
        gr = r.get("growth")
        ws.append([
            i, r.get("score"), (r.get("goods_name") or "")[:120], r.get("goods_id"),
            r.get("price"), r.get("price_max"), r.get("sales"),
            round(gr * 100, 1) if gr is not None else None,
            r.get("add_time"), r.get("hits"), " / ".join(r.get("signals") or []),
            " ".join(f"{k}:{v}" for k, v in (r.get("components") or {}).items()),
            GOODS_URL.format(r.get("goods_id")),
        ])
    widths = [6, 8, 60, 16, 8, 10, 14, 10, 12, 12, 32, 46, 52]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    # 榜单目录
    ws2 = wb.create_sheet("行业榜单目录")
    ws2.append(["榜单ID", "榜单名", "上级类目ID", "list_sn", "拼多多榜单链接", "首次出现", "采集次数"])
    for c in ws2[1]:
        c.fill, c.font = head_fill, head_font
    for r in (rankings or []):
        ws2.append([
            r.get("tab_id"), r.get("tab_name"), r.get("superior_tab_id"), r.get("list_sn"),
            ("https://mobile.yangkeduo.com/sjs_cat_rank_list.html?list_id="
             f"{r.get('tab_id')}&list_sn={r.get('list_sn')}"),
            r.get("first_seen"), r.get("seen_count"),
        ])
    for i, w in enumerate([16, 24, 14, 28, 96, 20, 10], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "A2"

    # 被过滤掉的
    ws3 = wb.create_sheet("被过滤")
    ws3.append(["商品名", "商品ID", "价格", "销量", "过滤原因"])
    for r in result["dropped"][:2000]:
        ws3.append([(r.get("goods_name") or "")[:120], r.get("goods_id"), r.get("price"),
                    r.get("sales"), r.get("drop_reason")])

    # 说明
    ws4 = wb.create_sheet("说明")
    cfg = result.get("config") or {}
    info = [
        ["生成时间", now()],
        ["采集批次 run_id", result.get("run_id")],
        ["候选商品数", result.get("candidates")],
        ["入选条数", len(result["passed"])],
        ["数据时间(商品 update_time)", (result.get("freshness") or {}).get("data_update_time")],
        ["数据陈旧天数", (result.get("freshness") or {}).get("stale_days")],
        ["权重", " / ".join(f"{k}={v}" for k, v in (cfg.get("weights") or {}).items())],
        ["价格带", f"{cfg.get('price', {}).get('min')}~{cfg.get('price', {}).get('max')}"
                   f"（甜点 {cfg.get('price', {}).get('sweet_min')}~{cfg.get('price', {}).get('sweet_max')}）"],
        ["最低销量", cfg.get("filters", {}).get("min_sales")],
        ["黑名单词", ",".join(cfg.get("filters", {}).get("exclude_keywords") or [])],
        ["数据来源", "web2.qianxingwl.com / QingYuTool tool_api (type=142/78 热销池, 87/88/89/44 行业榜单)"],
        ["备注", (meta or {}).get("note", "")],
    ]
    for k, v in info:
        ws4.append([k, v])
    ws4.column_dimensions["A"].width = 26
    ws4.column_dimensions["B"].width = 100

    wb.save(path)
    return path
