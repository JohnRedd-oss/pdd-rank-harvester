"""导出「价格段榜单」目标清单：拼多多自己按价位分好的类目榜。

榜单名形如「50-100元不粘锅炒锅」「100-300元炖锅」——平台已经替我们标注了
「这个细分类目在该价位有量」。这是判断「能否做到 100 元客单」最硬的信号。

用法：
    python band_targets.py                 # 只看覆盖 100 元的
    python band_targets.py --target 100-300
    python band_targets.py --excel
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PICKER_DB = os.environ.get("GOUVIP_DB") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gouvip_picker", "data", "picker.db")
PDD_DB = os.path.join(HERE, "pdd-raw", "pdd.db")
OUT = os.path.join(HERE, "out")
BAND_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*元")


def verified_map() -> dict[str, int]:
    if not os.path.exists(PDD_DB):
        return {}
    c = sqlite3.connect(PDD_DB)
    m = {r[0]: r[1] for r in c.execute("SELECT list_name, COUNT(*) FROM item GROUP BY list_name")}
    c.close()
    return m


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="100", help="目标客单，如 100 或 100-300")
    ap.add_argument("--excel", nargs="?", const="", default=None)
    a = ap.parse_args()

    lo, _, hi = a.target.partition("-")
    lo = int(lo)
    hi = int(hi) if hi else lo

    c = sqlite3.connect(PICKER_DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT tab_id, tab_name, superior_tab_id, list_sn FROM ranking").fetchall()]
    c.close()
    ver = verified_map()

    picked = []
    for r in rows:
        m = BAND_RE.search(r["tab_name"] or "")
        if not m:
            continue
        a1, b1 = int(m.group(1)), int(m.group(2))
        if not (a1 <= lo <= b1 or a1 <= hi <= b1):
            continue
        picked.append({
            "list_name": r["tab_name"], "band": f"{a1}-{b1}",
            "band_lo": a1, "band_hi": b1,
            "list_id": str(r["tab_id"]), "cat": str(r["superior_tab_id"] or ""),
            "list_sn": r["list_sn"], "verified": ver.get(r["tab_name"], 0),
        })
    picked.sort(key=lambda x: (-x["verified"], x["band_lo"], x["list_name"]))

    print(f"目标客单 {lo}~{hi} 元：命中 {len(picked)} 个价格段榜单"
          f"（已采集 {sum(1 for x in picked if x['verified'])} 个）\n")
    print(f"{'价格段':<10} {'已验证':>6}  {'类目':<26} {'榜单ID':<20}")
    print("-" * 78)
    for x in picked[:60]:
        print(f"{x['band']:<10} {x['verified']:>6}  {x['list_name'][:24]:<26} {x['list_id']:<20}")

    if a.excel is not None:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
        wb = Workbook()
        ws = wb.active
        ws.title = "价格段榜单"
        ws.append(["价格段", "类目", "榜单ID", "上级类目", "list_sn", "已采商品数",
                   "榜单链接"])
        fill, font = PatternFill("solid", fgColor="283579"), Font(color="FFFFFF", bold=True)
        for cell in ws[1]:
            cell.fill, cell.font = fill, font
        for x in picked:
            ws.append([x["band"], x["list_name"], x["list_id"], x["cat"], x["list_sn"],
                       x["verified"],
                       f"https://mobile.yangkeduo.com/sjs_cat_rank_list.html?list_id={x['list_id']}&list_sn={x['list_sn']}"])
        for i, w in enumerate([10, 28, 20, 10, 30, 12, 96], 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"

        ws2 = wb.create_sheet("说明")
        for k, v in [
            ("生成时间", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("来源", "拼多多榜单命名里的价格分段（平台自带标签）"),
            ("为什么可信", "平台把榜单按价位切分，说明该细分在该价位有成交、值得单独排行"),
            ("怎么用", "pdd_crawler.py run --bands --limit 30 → pdd_category.py 出报告"),
            ("目标客单", f"{lo}~{hi}"),
        ]:
            ws2.append([k, v])
        ws2.column_dimensions["A"].width = 16
        ws2.column_dimensions["B"].width = 92

        os.makedirs(OUT, exist_ok=True)
        path = os.path.join(OUT, a.excel or f"价格段榜单_{lo}元_{time.strftime('%Y%m%d_%H%M')}.xlsx")
        wb.save(path)
        print(f"\n✓ 已导出 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
