"""榜单选品：直接拿拼多多榜单数据打分排序（不依赖 gouvip 的冻结商品池）。

数据来源：pdd-raw/pdd.db（pdd_crawler.py 采集）
  - 畅销榜：热卖指数 + 销量
  - 好评榜：好评率 + 好评短语
  - 降价榜：降价幅度

打分维度（权重见 CFG/pdd_pick.json，可调）：
  rank    榜单排名（1 名满分，线性衰减）
  hot     热卖指数（对数归一，封顶 10000）
  sales   销量（解析 "19.9万+" 这类文本）
  review  好评率（好评榜 score_text 95.58% → 0.9558）
  stable  榜单稳定性（蝉联榜首 / 霸榜前三 / 新上榜）
  price   价格带命中
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "pdd-raw", "pdd.db")
OUT = os.path.join(HERE, "out")
CFG_PATH = os.path.join(HERE, "pdd_pick.json")

DEFAULT = {
    "weights": {"rank": 0.25, "hot": 0.20, "sales": 0.25,
                "review": 0.15, "stable": 0.10, "price": 0.05},
    "price": {"min": 5.0, "max": 99.0, "sweet_min": 9.9, "sweet_max": 49.9},
    "filters": {"min_sales": 1000, "exclude_keywords": ["赠品", "补差价", "运费", "专拍", "定金"]},
    "top_n": 50,
    "sales_cap": 2_000_000,
}
GOODS_URL = "https://mobile.yangkeduo.com/goods.html?goods_id={}"


def load_cfg() -> dict:
    cfg = json.loads(json.dumps(DEFAULT))
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def parse_sales(text: str | None) -> float:
    """'19.9万+件' / '115.9万' / '1.6万+' / '7874件' / '167' -> 数值"""
    if not text:
        return 0.0
    t = str(text)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(亿|万)?", t)
    if not m:
        return 0.0
    n = float(m.group(1))
    unit = m.group(2)
    if unit == "亿":
        n *= 100_000_000
    elif unit == "万":
        n *= 10_000
    return n


def parse_pct(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", str(text))
    return float(m.group(1)) / 100 if m else None


def clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def load_items() -> list[dict]:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute("""
        SELECT i.*, p.rule, p.fetched_at AS page_time
        FROM item i JOIN page p ON p.id = i.page_id
        ORDER BY i.list_id, i.tab_kind, i.rank""")]
    c.close()
    return rows


def aggregate(rows: list[dict]) -> list[dict]:
    """同商品可能在畅销/好评/降价三个榜都出现，合并成一条。"""
    agg: dict[str, dict] = {}
    for r in rows:
        gid = r["goods_id"]
        sales = parse_sales(r.get("sales_text") or r.get("sales_tip"))
        cur = agg.get(gid)
        if cur is None:
            agg[gid] = {
                "goods_id": gid,
                "title": r["title"],
                "image": r.get("image"),
                "price": r.get("price"),
                "sales": sales,
                "sales_tip": r.get("sales_tip"),
                "lists": {r["list_name"]},
                "tabs": set(),
                "best_rank": r["rank"] or 99,
                "hot": 0.0,
                "review": None,
                "tags": [],
                "phrase": "",
            }
            cur = agg[gid]
        else:
            cur["lists"].add(r["list_name"])
            cur["sales"] = max(cur["sales"], sales)
            cur["best_rank"] = min(cur["best_rank"], r["rank"] or 99)
            if r.get("price"):
                cur["price"] = cur["price"] or r["price"]
        cur["tabs"].add(r["tab_kind"] or "")
        if r["tab_kind"] == "畅销榜":
            cur["hot"] = max(cur["hot"], float(r.get("hot_score") or 0))
        if r["tab_kind"] == "好评榜":
            p = parse_pct(r.get("score_text"))
            if p is not None:
                cur["review"] = max(cur["review"] or 0, p)
            if r.get("tags"):
                cur["tags"].append(r["tags"])
        if r.get("tag_text"):
            cur["tags"].append(r["tag_text"])
    out = []
    for v in agg.values():
        v["lists"] = sorted(v["lists"])
        v["tabs"] = sorted(t for t in v["tabs"] if t)
        v["tags"] = sorted({t for t in v["tags"] if t})
        out.append(v)
    return out


def score_one(it: dict, cfg: dict) -> dict:
    w = cfg["weights"]
    rank = it["best_rank"] or 99
    rank_s = clip(1 - (rank - 1) / 20)
    hot_s = clip((it["hot"] / 10000) ** 0.5) if it["hot"] else 0.0
    sales_s = clip((it["sales"] / cfg["sales_cap"]) ** 0.35) if it["sales"] else 0.0
    review_s = it["review"] if it["review"] is not None else 0.5
    tags = " ".join(it["tags"])
    if "蝉联榜首" in tags:
        stable_s = 1.0
    elif "霸榜前三" in tags:
        stable_s = 0.8
    elif tags:
        stable_s = 0.5
    else:
        stable_s = 0.3
    p = it.get("price")
    pc = cfg["price"]
    if not p:
        price_s = 0.4
    elif pc["sweet_min"] <= p <= pc["sweet_max"]:
        price_s = 1.0
    elif pc["min"] <= p <= pc["max"]:
        price_s = 0.7
    else:
        price_s = 0.3
    comp = {"rank": rank_s, "hot": hot_s, "sales": sales_s,
            "review": review_s, "stable": stable_s, "price": price_s}
    tw = sum(w.values()) or 1
    score = sum(comp[k] * w.get(k, 0) for k in comp) / tw
    return {**it, "score": round(score * 100, 2),
            "components": {k: round(v * 100, 1) for k, v in comp.items()}}


def pick(cfg: dict, tab: str | None, list_kw: str | None, top: int) -> tuple[list[dict], list[dict]]:
    rows = load_items()
    if tab:
        rows = [r for r in rows if r["tab_kind"] == tab]
    if list_kw:
        rows = [r for r in rows if list_kw in (r["list_name"] or "")]
    cands = aggregate(rows)
    passed, dropped = [], []
    for it in cands:
        name = it["title"] or ""
        reason = None
        for kw in cfg["filters"]["exclude_keywords"]:
            if kw in name:
                reason = f"命中黑名单:{kw}"
                break
        if not reason and it["sales"] < cfg["filters"]["min_sales"]:
            reason = f"销量<{cfg['filters']['min_sales']}"
        if reason:
            dropped.append({**it, "drop_reason": reason})
            continue
        passed.append(score_one(it, cfg))
    passed.sort(key=lambda x: (x["score"], x["sales"]), reverse=True)
    return passed[:top], dropped


def export(path: str, passed: list[dict], dropped: list[dict], cfg: dict) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "榜单选品"
    head = ["#", "得分", "商品", "榜单", "最好排名", "价格", "销量", "热卖指数",
            "好评率", "榜单标签", "分项", "商品链接"]
    ws.append(head)
    fill, font = PatternFill("solid", fgColor="283579"), Font(color="FFFFFF", bold=True)
    for c in ws[1]:
        c.fill, c.font = fill, font
        c.alignment = Alignment(horizontal="center", vertical="center")
    for i, r in enumerate(passed, 1):
        ws.append([i, r["score"], (r["title"] or "")[:110], " / ".join(r["lists"]),
                   r["best_rank"], r["price"], r["sales_tip"],
                   r["hot"] or None,
                   f"{r['review']*100:.2f}%" if r["review"] else None,
                   " ".join(r["tags"]),
                   " ".join(f"{k}:{v}" for k, v in r["components"].items()),
                   GOODS_URL.format(r["goods_id"])])
    for i, w in enumerate([5, 8, 58, 26, 10, 8, 16, 10, 9, 26, 40, 50], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    ws2 = wb.create_sheet("被过滤")
    ws2.append(["商品", "销量", "最好排名", "原因"])
    for r in dropped[:2000]:
        ws2.append([(r["title"] or "")[:110], r["sales"], r["best_rank"], r["drop_reason"]])

    ws3 = wb.create_sheet("说明")
    for k, v in [
        ("生成时间", time.strftime("%Y-%m-%d %H:%M:%S")),
        ("数据源", "拼多多官方榜单页（list_id/list_sn）→ 页面自带 JSON / window.rawData"),
        ("采集方式", "真实 Chrome 串行访问，6~14s 间隔，日上限 300 页"),
        ("权重", " / ".join(f"{k}={v}" for k, v in cfg["weights"].items())),
        ("价格带", f"{cfg['price']['min']}~{cfg['price']['max']}（甜点 {cfg['price']['sweet_min']}~{cfg['price']['sweet_max']}）"),
        ("最低销量", cfg["filters"]["min_sales"]),
        ("候选数", len(passed) + len(dropped)),
        ("入选数", len(passed)),
    ]:
        ws3.append([k, v])
    ws3.column_dimensions["A"].width = 16
    ws3.column_dimensions["B"].width = 96

    wb.save(path)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="榜单选品")
    ap.add_argument("--tab", choices=["畅销榜", "好评榜", "降价榜"], default=None)
    ap.add_argument("--list", dest="list_kw", default=None, help="按榜单名过滤，如 垃圾桶")
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--excel", nargs="?", const="", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    cfg = load_cfg()
    top = a.top or int(cfg.get("top_n") or 50)
    passed, dropped = pick(cfg, a.tab, a.list_kw, top)

    if a.json:
        print(json.dumps(passed, ensure_ascii=False, indent=2)[:20000])
        return 0

    print(f"候选 {len(passed)+len(dropped)} → 入选 {len(passed)}（过滤 {len(dropped)}）\n")
    print(f"{'#':>3} {'得分':>6} {'排名':>4} {'价格':>7} {'销量':>12} {'热卖':>6} {'好评':>7}  商品")
    print("-" * 108)
    for i, r in enumerate(passed, 1):
        review_txt = f"{r['review']*100:.1f}%" if r.get("review") else "-"
        print(f"{i:>3} {r['score']:>6} {r['best_rank']:>4} "
              f"{(r['price'] or 0):>7} {(r['sales_tip'] or ''):>12} "
              f"{(r['hot'] or 0):>6.0f} {review_txt:>7}  {(r['title'] or '')[:44]}")

    if a.excel is not None:
        name = a.excel or f"榜单选品_{time.strftime('%Y%m%d_%H%M')}.xlsx"
        path = export(os.path.join(OUT, name), passed, dropped, cfg)
        print(f"\n✓ 已导出 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
