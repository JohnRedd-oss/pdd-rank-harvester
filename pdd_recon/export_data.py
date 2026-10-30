"""把已采集的数据导出成一个 Excel 工作簿，供直接查看。

工作表：
  1. 榜单清单        采到哪些榜单、什么时候抓的、多少商品
  2. 商品明细        每个商品的价格/24h单量/累计销量/热卖指数/好评率/品牌/标签
  3. 类目汇总        按榜单名聚合：价格分位、目标价带单量占比、日单量与GMV、品牌占比
  4. 材料位vs工具位   按标题关键词分组，对比价格与单量结构
  5. 说明            口径与采集参数

用法：python export_data.py [输出文件名]
"""
from __future__ import annotations

import os
import re
import sqlite3
import statistics as st
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "pdd-raw", "pdd.db")
OUT = os.path.join(HERE, "out")

TOOL_KW = ("工具", "模具", "配件", "镊子", "收纳", "模板", "图纸", "专用", "辅助", "耗材",
           "钳", "剪", "砂纸", "打磨", "胶水", "线组", "绑钩", "卡套", "卡盒")
MAT_KW = ("材料", "套装", "颗粒", "豆", "胶", "粉", "纸", "线", "布", "板", "包", "盒")


def sales_num(text) -> float:
    if not text:
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*(亿|万)?", str(text))
    if not m:
        return 0.0
    n = float(m.group(1))
    return n * {"亿": 1e8, "万": 1e4}.get(m.group(2) or "", 1.0)


def pct(text):
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", str(text))
    return float(m.group(1)) / 100 if m else None


def main() -> int:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    name = sys.argv[1] if len(sys.argv) > 1 else f"采集数据_{time.strftime('%Y%m%d_%H%M')}.xlsx"
    path = os.path.join(OUT, name)
    os.makedirs(OUT, exist_ok=True)

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    items = [dict(r) for r in c.execute("""
        SELECT i.*, p.fetched_at AS page_time, p.status, p.rule
        FROM item i JOIN page p ON p.id = i.page_id
        WHERE p.status='ok'
        ORDER BY i.list_name, i.tab_kind, i.rank""")]
    pages = [dict(r) for r in c.execute(
        "SELECT * FROM page WHERE status='ok' ORDER BY id DESC")]
    c.close()

    wb = Workbook()
    head_fill = PatternFill("solid", fgColor="283579")
    head_font = Font(color="FFFFFF", bold=True)

    def style(ws, widths):
        for cell in ws[1]:
            cell.fill, cell.font = head_fill, head_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"

    # ---------- 1 榜单清单 ----------
    ws = wb.active
    ws.title = "榜单清单"
    ws.append(["榜单", "榜单类型", "商品数", "榜单ID", "采集时间", "排序规则"])
    for p in pages:
        ws.append([p["list_name"], p["tab_kind"], p["items"], p["list_id"],
                   p["fetched_at"], (p["rule"] or "")[:40]])
    style(ws, [24, 10, 8, 20, 20, 42])

    # ---------- 2 商品明细 ----------
    ws2 = wb.create_sheet("商品明细")
    ws2.append(["榜单", "榜单类型", "排名", "商品", "价格", "24h单量(下限)", "累计销量",
                "热卖指数", "好评率", "品牌", "榜单标签", "商品ID"])
    for r in items:
        ws2.append([r["list_name"], r["tab_kind"], r["rank"], (r["title"] or "")[:100],
                    r["price"], r["day_orders"], r["sales_tip"],
                    r["hot_score"] if (r["tab_kind"] == "畅销榜") else None,
                    f"{pct(r['score_text'])*100:.2f}%" if pct(r["score_text"]) else None,
                    r["brand"] or None, r["tag_text"] or None, r["goods_id"]])
    style(ws2, [22, 10, 6, 62, 8, 13, 16, 10, 9, 14, 16, 18])
    ws2.auto_filter.ref = ws2.dimensions

    # ---------- 3 类目汇总 ----------
    cats: dict[str, dict] = {}
    for r in items:
        key = r["list_name"] or ""
        cat = cats.setdefault(key, {"prices": [], "days": [], "gmv": 0.0,
                                    "brand": 0, "n": 0, "samples": set()})
        cat["n"] += 1
        cat["samples"].add(r["goods_id"])
        if r["price"]:
            cat["prices"].append(r["price"])
        if r["day_orders"]:
            cat["days"].append(r["day_orders"])
            cat["gmv"] += r["day_orders"] * (r["price"] or 0)
        if r["brand"]:
            cat["brand"] += 1

    ws3 = wb.create_sheet("类目汇总")
    ws3.append(["榜单", "商品数(去重)", "价格P50", "价格P75", "价格最高",
                "70-130元商品数", "日单量下限", "日均GMV下限", "品牌占比"])
    rows3 = []
    for k, v in cats.items():
        ps = sorted(v["prices"])
        band = sum(1 for p in ps if 70 <= p <= 130)
        rows3.append([k, len(v["samples"]),
                      round(st.median(ps), 2) if ps else None,
                      round(ps[int(len(ps) * 0.75)], 2) if ps else None,
                      max(ps) if ps else None, band,
                      round(sum(v["days"]), 0), round(v["gmv"], 0),
                      round(v["brand"] / max(v["n"], 1), 2)])
    rows3.sort(key=lambda x: -(x[6] or 0))
    for r in rows3:
        ws3.append(r)
    style(ws3, [24, 12, 10, 10, 10, 13, 13, 14, 10])

    # ---------- 4 材料位 vs 工具位 ----------
    ws4 = wb.create_sheet("材料位vs工具位")
    ws4.append(["分组", "商品数", "价格P50", "价格P75", "70-130元占比", "日单量合计",
                "品牌占比", "示例商品"])
    groups = {"工具/配件位": [], "材料/耗材位": [], "其它": []}
    for r in items:
        t = r["title"] or ""
        if any(k in t for k in TOOL_KW):
            groups["工具/配件位"].append(r)
        elif any(k in t for k in MAT_KW):
            groups["材料/耗材位"].append(r)
        else:
            groups["其它"].append(r)
    for g, rows in groups.items():
        if not rows:
            continue
        ps = sorted([x["price"] for x in rows if x["price"]])
        band = sum(1 for p in ps if 70 <= p <= 130)
        ws4.append([g, len(rows),
                    round(st.median(ps), 2) if ps else None,
                    round(ps[int(len(ps) * 0.75)], 2) if ps else None,
                    round(band / max(len(ps), 1), 3),
                    round(sum(x["day_orders"] or 0 for x in rows), 0),
                    round(sum(1 for x in rows if x["brand"]) / len(rows), 2),
                    (rows[0]["title"] or "")[:60]])
    style(ws4, [14, 8, 10, 10, 13, 13, 10, 62])

    # ---------- 5 说明 ----------
    ws5 = wb.create_sheet("说明")
    for k, v in [
        ("导出时间", time.strftime("%Y-%m-%d %H:%M:%S")),
        ("榜单数(去重)", len({p["list_id"] for p in pages})),
        ("榜单页(含tab)", len(pages)),
        ("商品数", len(items)),
        ("数据源", "拼多多榜单页（畅销/好评/降价/新品），真实 Chrome 串行访问"),
        ("24h单量口径", "榜单卡片「24小时内XXX+人拼单」，XXX 为下限；7天/30天窗口已折算日均"),
        ("GMV口径", "Σ(24h单量 × 到手价)，同为下限"),
        ("热卖指数", "畅销榜字段，按近7日销量与成交额计算，仅在畅销榜有值"),
        ("好评率", "好评榜字段，仅好评榜有值"),
        ("价格", "券后/活动价，同一商品在不同榜可能不同"),
        ("筛选", "已排除品牌榜、综合榜、活动系列、大件家具、服装鞋、生鲜、汽车车载"),
    ]:
        ws5.append([k, v])
    ws5.column_dimensions["A"].width = 18
    ws5.column_dimensions["B"].width = 100

    wb.save(path)
    print(f"✓ 导出 {path}")
    print(f"  榜单 {len({p['list_id'] for p in pages})} 个 / 榜单页 {len(pages)} / 商品 {len(items)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
