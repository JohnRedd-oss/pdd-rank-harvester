"""类目机会分析：从榜单数据算「哪些细分类目能做到 100 元客单」。

输入：pdd-raw/pdd.db（pdd_crawler.py 采集的榜单商品）
输出：类目机会榜（Excel）+ 终端摘要

核心判断（三件事）：
  1. 价格承载力 —— 这个细分里已经有人卖到 80~150 元，说明客单 100 能做
  2. 需求规模   —— 榜单内销量总和/中位数，别是死类目
  3. 差异化空间 —— 价格离散度越大、头部集中度越低，越不像红海拼价

注意：榜单每条只有 20 个商品（畅销榜），是「头部 20 名」样本，
不代表全类目价格分布，但足够判断「高价带能不能卖得动」。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics as st
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "pdd-raw", "pdd.db")
OUT = os.path.join(HERE, "out")
CFG_PATH = os.path.join(HERE, "pdd_category.json")

DEFAULT = {
    "weights": {"price_cap": 0.40, "premium": 0.10, "demand": 0.25, "diff": 0.25},
    "target_price": 100.0,        # 目标客单价
    "premium_floor": 80.0,        # 算作「高价位」的门槛
    "min_goods": 5,               # 类目至少几个商品才评估
    "min_sales": 3000,            # 类目销量总和下限
    "min_review": 0.0,            # 好评率下限（0=不限）
}


def parse_sales(text) -> float:
    if not text:
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(亿|万)?", str(text))
    if not m:
        return 0.0
    n = float(m.group(1))
    return n * {"亿": 1e8, "万": 1e4}.get(m.group(2) or "", 1.0)


def parse_pct(text):
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", str(text))
    return float(m.group(1)) / 100 if m else None


def clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def load() -> list[dict]:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    have = {r[1] for r in c.execute("PRAGMA table_info(item)").fetchall()}
    extra = ", i.day_orders" if "day_orders" in have else ", NULL AS day_orders"
    rows = [dict(r) for r in c.execute(f"""
        SELECT i.goods_id, i.list_id, i.list_name, i.tab_kind, i.rank, i.title,
               i.price, i.sales_text, i.sales_tip, i.hot_score, i.score_text,
               i.tag_text, i.brand, i.tags, i.fetched_at{extra}
        FROM item i ORDER BY i.list_name, i.tab_kind, i.rank""")]
    c.close()
    return rows


def build_categories(rows: list[dict]) -> list[dict]:
    """按 list_name 聚合；同一商品多 tab 出现只算一次（取畅销榜价/销量，补齐好评率）。"""
    cats: dict[str, dict] = {}
    for r in rows:
        key = r["list_name"] or ""
        cat = cats.setdefault(key, {
            "list_name": key,
            "list_ids": set(),
            "goods": {},
            "reviews": [],
            "sticky": 0,
        })
        cat["list_ids"].add(str(r["list_id"]))
        gid = r["goods_id"]
        g = cat["goods"].setdefault(gid, {"price": None, "sales": 0.0, "hot": 0.0,
                                          "day": 0.0, "title": r["title"], "rank": 99,
                                          "tag": "", "brand": r["brand"] or ""})
        g["rank"] = min(g["rank"], r["rank"] or 99)
        g["sales"] = max(g["sales"], parse_sales(r["sales_text"] or r["sales_tip"]))
        if r.get("day_orders"):
            g["day"] = max(g["day"], float(r["day_orders"]))
        if r["tab_kind"] == "畅销榜":
            g["hot"] = max(g["hot"], float(r["hot_score"] or 0))
        if r["tab_kind"] == "好评榜":
            p = parse_pct(r["score_text"])
            if p:
                cat["reviews"].append(p)
        if r["price"]:
            g["price"] = g["price"] or r["price"]
        if r["tag_text"]:
            g["tag"] = g["tag"] or r["tag_text"]
            if "蝉联" in r["tag_text"] or "霸榜" in r["tag_text"]:
                cat["sticky"] += 1

    out = []
    for name, cat in cats.items():
        goods = list(cat["goods"].values())
        prices = [g["price"] for g in goods if g["price"]]
        sales = [g["sales"] for g in goods if g["sales"]]
        if not prices:
            continue
        prices_sorted = sorted(prices)
        sales_total = sum(sales) if sales else 0.0
        top3 = sorted(sales, reverse=True)[:3]

        # ---- 24 小时成交量（下限口径）----
        days = [g["day"] for g in goods if g["day"]]
        orders_day = sum(days) if days else 0.0
        gmv_day = sum(g["day"] * (g["price"] or 0) for g in goods if g["day"])
        out.append({
            "list_name": name,
            "list_ids": sorted(cat["list_ids"]),
            "n_goods": len(goods),
            "n_priced": len(prices),
            "p25": round(st.quantiles(prices_sorted, n=4)[0], 2) if len(prices) >= 4 else prices_sorted[0],
            "p50": round(st.median(prices_sorted), 2),
            "p75": round(st.quantiles(prices_sorted, n=4)[2], 2) if len(prices) >= 4 else prices_sorted[-1],
            "p90": round(prices_sorted[max(0, int(len(prices_sorted) * 0.9) - 1)], 2),
            "price_max": max(prices_sorted),
            "premium_n": sum(1 for p in prices if p >= 80),
            "sales_total": round(sales_total, 0),
            "sales_median": round(st.median(sales), 0) if sales else 0,
            "orders_day": round(orders_day, 0),               # 头部榜日均单量下限
            "orders_day_median": round(st.median(days), 0) if days else 0,
            "orders_month": round(orders_day * 30, 0),
            "gmv_day": round(gmv_day, 0),                     # 日均 GMV 下限（元）
            "gmv_month": round(gmv_day * 30, 0),
            "n_orders": len(days),                            # 有单量数据的商品数
            "hot_max": max((g["hot"] for g in goods), default=0),
            "hot_median": round(st.median([g["hot"] for g in goods if g["hot"]] or [0]), 0),
            "review_avg": round(st.mean(cat["reviews"]), 4) if cat["reviews"] else None,
            "sticky": cat["sticky"],
            "brand_n": sum(1 for g in goods if g.get("brand")),
            "brand_ratio": round(sum(1 for g in goods if g.get("brand")) / max(len(goods), 1), 3),
            "top3_share": round(sum(top3) / sales_total, 3) if sales_total else 0,
            "goods": goods,
        })
    return out


def score_cat(c: dict, cfg: dict) -> dict:
    w = cfg["weights"]
    target = cfg["target_price"]
    band_lo, band_hi = target * 0.7, target * 1.3

    # 目标价位带（70~130 元）里，24h 单量占了多少 —— 直接回答「100 元能不能卖得动」
    band = [g for g in c["goods"] if g["price"] and band_lo <= g["price"] <= band_hi]
    band_sales = sum(g["sales"] for g in band)
    band_share = (band_sales / c["sales_total"]) if c["sales_total"] else 0.0
    band_orders = sum(g["day"] for g in band if g["day"])
    orders_all = c["orders_day"] or 0
    band_orders_share = (band_orders / orders_all) if orders_all else 0.0
    c = {**c, "band_n": len(band), "band_sales": round(band_sales, 0),
         "band_share": round(band_share, 3),
         "band_orders": round(band_orders, 0), "band_orders_share": round(band_orders_share, 3)}

    # 价格承载力：P75 离目标客单多近（P75 够到 target 就算满）
    price_cap = clip(c["p75"] / target)
    # 高价带供给：目标价位带内的商品数占比（*2 放大，20 个里 10 个就满）
    premium = clip((c["band_n"] / max(1, c["n_priced"])) * 2.0)
    # 需求规模：优先用 24h 单量（真金白银），没有就退回累计销量
    if c["orders_day"]:
        demand = clip((c["orders_day"] / 3000) ** 0.5)
    else:
        demand = clip((c["sales_total"] / 200_000) ** 0.35) if c["sales_total"] else 0.0
    # 差异化：价格离散度（越高越不是纯拼价）+ 头部集中度反向
    prices = [g["price"] for g in c["goods"] if g["price"]]
    cv = (st.pstdev(prices) / st.mean(prices)) if len(prices) > 1 and st.mean(prices) else 0
    cv_n = clip(cv / 0.8)
    conc = clip(1 - c["top3_share"])
    diff = 0.5 * cv_n + 0.5 * conc

    comp = {"price_cap": price_cap, "premium": premium, "demand": demand, "diff": diff}
    tw = sum(w.values()) or 1
    score = sum(comp[k] * w.get(k, 0) for k in comp) / tw

    verdict = []
    if c["band_n"]:
        verdict.append(f"目标价带{c['band_n']}款/占单量{band_orders_share*100:.0f}%")
    if c["p75"] >= target * 0.8:
        verdict.append("客单100可行")
    if c["top3_share"] <= 0.5:
        verdict.append("头部不垄断")
    if c["review_avg"] and c["review_avg"] >= 0.94:
        verdict.append("好评门槛高")
    if c["hot_max"] >= 7000:
        verdict.append("热度高")
    if c["sticky"] >= 3:
        verdict.append("老款霸榜多(需差异化)")
    return {**c, "score": round(score * 100, 2),
            "components": {k: round(v * 100, 1) for k, v in comp.items()},
            "verdict": " / ".join(verdict)}


MIXED_PAT = ("实时", "年度", "总榜", "热卖榜", "今日")


def analyze(cfg: dict) -> tuple[list[dict], list[dict]]:
    cats = build_categories(load())
    passed, rejected = [], []
    for c in cats:
        reason = None
        if any(p in c["list_name"] for p in MIXED_PAT):
            reason = "综合榜（非细分类目）"
        elif c["p50"] and c["p90"] / c["p50"] > 12:
            reason = f"价格跨度异常（P90/P50={c['p90']/c['p50']:.0f}），疑似混合目录"
        elif c["n_priced"] < cfg["min_goods"]:
            reason = f"商品数<{cfg['min_goods']}"
        elif c["sales_total"] < cfg["min_sales"]:
            reason = f"销量<{cfg['min_sales']}"
        elif cfg["min_review"] and (c["review_avg"] or 0) < cfg["min_review"]:
            reason = f"好评<{cfg['min_review']}"
        if reason:
            rejected.append({**c, "reject": reason})
        else:
            passed.append(score_cat(c, cfg))
    passed.sort(key=lambda x: x["score"], reverse=True)
    return passed, rejected


def export(path: str, passed: list[dict], rejected: list[dict], cfg: dict) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "类目机会榜"
    head = ["#", "机会分", "细分类目", "商品数", "价格P25", "价格中位", "价格P75", "价格P90",
            "最高价", "目标价带商品数", "目标价带单量占比",
            "日单量下限", "日均GMV下限", "月GMV下限(估)",
            "热卖指数最高", "好评率", "蝉联/霸榜数", "头部集中度", "判断", "分项"]
    ws.append(head)
    fill, font = PatternFill("solid", fgColor="283579"), Font(color="FFFFFF", bold=True)
    for c in ws[1]:
        c.fill, c.font = fill, font
        c.alignment = Alignment(horizontal="center", vertical="center")
    for i, c in enumerate(passed, 1):
        ws.append([i, c["score"], c["list_name"], c["n_goods"], c["p25"], c["p50"], c["p75"],
                   c["p90"], c["price_max"], c["band_n"],
                   c["band_orders_share"],
                   c["orders_day"], c["gmv_day"], c["gmv_month"],
                   c["hot_max"],
                   f"{c['review_avg']*100:.2f}%" if c["review_avg"] else None,
                   c["sticky"], c["top3_share"], c["verdict"],
                   " ".join(f"{k}:{v}" for k, v in c["components"].items())])
    for i, w in enumerate([4, 8, 22, 7, 8, 9, 9, 9, 8, 13, 14, 12, 14, 15, 11, 9, 11, 11, 46, 40], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    ws2 = wb.create_sheet("类目商品明细")
    ws2.append(["类目", "排名", "商品", "价格", "累计销量", "24h单量(下限)",
                "热卖指数", "榜单标签", "品牌"])
    for c in passed:
        for g in sorted(c["goods"], key=lambda x: x["rank"]):
            ws2.append([c["list_name"], g["rank"], (g["title"] or "")[:100], g["price"],
                        g["sales"], g["day"] or None,
                        g["hot"] or None, g["tag"], g["brand"]])
    for i, w in enumerate([22, 6, 62, 8, 12, 14, 10, 16, 16], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w

    ws3 = wb.create_sheet("未达标类目")
    ws3.append(["细分类目", "商品数", "价格P75", "销量合计", "剔除原因"])
    for c in rejected:
        ws3.append([c["list_name"], c["n_goods"], c["p75"], c["sales_total"], c["reject"]])

    ws4 = wb.create_sheet("说明")
    for k, v in [
        ("生成时间", time.strftime("%Y-%m-%d %H:%M:%S")),
        ("数据源", "拼多多榜单页（畅销/好评/降价三榜）→ pdd-raw/pdd.db"),
        ("样本性质", "每个榜单取头部 20 名商品，代表类目头部，不代表长尾价格分布"),
        ("目标客单价", cfg["target_price"]),
        ("权重", " / ".join(f"{k}={v}" for k, v in cfg["weights"].items())),
        ("price_cap", "价格承载力：类目价格 P75 接近目标客单 = 高价带已被验证"),
        ("premium", "目标价带商品数占比（越高说明该价位供给充分）"),
        ("demand", "24h 单量下限（没有则退回累计销量）"),
        ("diff", "差异化空间：价格离散度 50% + 头部不垄断 50%"),
        ("24h单量口径", "榜单卡片「24小时内XXX+人拼单」，XXX 是下限；7 天/30 天窗口已折算成日均"),
        ("GMV口径", "Σ(24h单量 × 到手价)，同样是下限；月 GMV = 日均 × 30"),
        ("筛选", f"商品数≥{cfg['min_goods']}，销量≥{cfg['min_sales']}"),
    ]:
        ws4.append([k, v])
    ws4.column_dimensions["A"].width = 18
    ws4.column_dimensions["B"].width = 96

    wb.save(path)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="类目机会分析")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--excel", nargs="?", const="", default=None)
    ap.add_argument("--min-goods", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    cfg = json.loads(json.dumps(DEFAULT))
    if os.path.exists(CFG_PATH):
        cfg.update(json.load(open(CFG_PATH, encoding="utf-8")))
    if a.min_goods:
        cfg["min_goods"] = a.min_goods

    passed, rejected = analyze(cfg)
    if a.json:
        print(json.dumps([{k: v for k, v in c.items() if k != "goods"} for c in passed],
                         ensure_ascii=False, indent=2))
        return 0

    print(f"类目 {len(passed) + len(rejected)} 个 → 达标 {len(passed)}（剔除 {len(rejected)}）\n")
    print(f"{'#':>3} {'分':>6} {'类目':<15} {'商品':>4} {'P50':>7} {'P75':>7} "
          f"{'带内款':>5} {'带内单量':>7} {'日单量':>8} {'日均GMV':>9} {'月GMV(估)':>10} {'集中':>5}  判断")
    print("-" * 150)
    for i, c in enumerate(passed[: a.top], 1):
        print(f"{i:>3} {c['score']:>6} {c['list_name'][:15]:<15} {c['n_goods']:>4} "
              f"{c['p50']:>7} {c['p75']:>7} {c['band_n']:>5} "
              f"{c['band_orders_share']*100:>6.0f}% {c['orders_day']:>8.0f} "
              f"{c['gmv_day']:>9.0f} {c['gmv_month']:>10.0f} {c['top3_share']:>5}  {c['verdict'][:40]}")

    if a.excel is not None:
        name = a.excel or f"类目机会_{time.strftime('%Y%m%d_%H%M')}.xlsx"
        path = export(os.path.join(OUT, name), passed, rejected, cfg)
        print(f"\n✓ 已导出 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
