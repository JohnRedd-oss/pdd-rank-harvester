"""类目候选菜单：从 gouvip 榜单目录里筛出「可能做到 100 元客单」的细分类目，供分批验证。

这一步不联网、不消耗风控配额 —— 纯本地筛选，把 13k 条榜单收敛成一份可执行的待验证清单。
价格倾向是**先验假设**（按品类常识），真实价格带要用 pdd_crawler 采样后用 pdd_category 验证。
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

# 高客单倾向：家具/大件/电器/宠物耐用品/运动器材
HIGH = ["柜", "桌", "椅", "沙发", "床", "床垫", "枕", "被", "四件套", "地毯", "窗帘",
        "置物架", "货架", "衣架", "鞋架", "书架", "收纳箱", "收纳柜", "整理箱",
        "锅", "炒锅", "蒸锅", "奶锅", "空气炸锅", "电饭煲", "破壁", "养生壶", "电煮",
        "保温杯", "水杯", "茶具", "刀具", "砧板", "餐具套装",
        "猫爬架", "猫砂盆", "宠物窝", "狗窝", "喂食器", "宠物笼",
        "加湿器", "净化器", "风扇", "取暖器", "挂烫机", "吹风机", "吸尘器", "除螨",
        "瑜伽", "健身", "哑铃", "跑步机", "露营", "帐篷", "折叠床", "野餐",
        "台灯", "落地灯", "镜子", "化妆镜", "香薰", "加湿",
        "积木", "玩具", "儿童", "婴儿", "餐椅", "推车", "书包"]
# 低客单：小配件/耗材/一次性
LOW = ["贴纸", "挂钩", "夹子", "卡套", "钥匙扣", "手机壳", "数据线", "袜子", "发圈",
       "口罩", "湿巾", "垃圾袋", "保鲜膜", "棉签", "牙签", "一次性", "试用", "小样",
       "散装", "批发", "代购", "赠品", "补差", "运费", "福袋", "盲盒"]
BAD_NAME = ["实时", "年度", "总榜", "热卖榜", "今日", "大促", "礼盒", "组合", "套装"]


def family(name: str) -> str:
    """把榜单名归到词族，用于聚合。"""
    for k in sorted(HIGH + LOW, key=len, reverse=True):
        if k in name:
            return k
    return "其他"


def main() -> int:
    ap = argparse.ArgumentParser(description="类目候选菜单")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--excel", nargs="?", const="", default=None)
    a = ap.parse_args()

    if not os.path.exists(PICKER_DB):
        print("找不到 gouvip 榜单库，先跑 gouvip-picker 的 industry")
        return 1

    c = sqlite3.connect(PICKER_DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT tab_id, tab_name, superior_tab_id, list_sn FROM ranking").fetchall()]
    c.close()

    # 已经验证过的（本地有商品数据的）
    verified = {}
    if os.path.exists(PDD_DB):
        p = sqlite3.connect(PDD_DB)
        for r in p.execute("""SELECT list_name, COUNT(*) n FROM item
                              GROUP BY list_name"""):
            verified[r[0]] = r[1]
        p.close()

    cand = []
    for r in rows:
        name = (r["tab_name"] or "").strip()
        if not name or any(b in name for b in BAD_NAME):
            continue
        if any(l in name for l in LOW):
            continue
        score = sum(1 for k in HIGH if k in name)
        if not score:
            continue
        cand.append({
            "list_id": str(r["tab_id"]),
            "list_name": name,
            "family": family(name),
            "cat": str(r["superior_tab_id"] or ""),
            "prior": score,
            "verified_items": verified.get(name, 0),
            "list_sn": r["list_sn"],
        })

    # 同名词族去重，优先取未验证的
    by_name = {}
    for x in cand:
        cur = by_name.get(x["list_name"])
        if cur is None or x["prior"] > cur["prior"]:
            by_name[x["list_name"]] = x
    cand = list(by_name.values())
    cand.sort(key=lambda x: (-x["prior"], x["list_name"]))

    # 词族汇总
    fam: dict[str, list] = {}
    for x in cand:
        fam.setdefault(x["family"], []).append(x)

    print(f"候选榜单 {len(cand)} 条，覆盖 {len(fam)} 个词族（低客单词已排除）\n")
    print(f"{'词族':<8} {'条数':>4}  示例")
    print("-" * 92)
    for f, items in sorted(fam.items(), key=lambda kv: -len(kv[1]))[: a.top]:
        names = "、".join(x["list_name"] for x in items[:6])
        print(f"{f:<8} {len(items):>4}  {names}")

    if a.excel is not None:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
        wb = Workbook()
        ws = wb.active
        ws.title = "待验证类目"
        ws.append(["词族", "榜单名", "榜单ID", "上级类目", "高客单先验分", "已验证商品数", "list_sn"])
        fill, font = PatternFill("solid", fgColor="283579"), Font(color="FFFFFF", bold=True)
        for cell in ws[1]:
            cell.fill, cell.font = fill, font
        for x in cand:
            ws.append([x["family"], x["list_name"], x["list_id"], x["cat"], x["prior"],
                       x["verified_items"], x["list_sn"]])
        for i, w in enumerate([10, 26, 18, 10, 12, 12, 30], 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"

        ws2 = wb.create_sheet("词族汇总")
        ws2.append(["词族", "候选条数", "示例榜单"])
        for f, items in sorted(fam.items(), key=lambda kv: -len(kv[1])):
            ws2.append([f, len(items), "、".join(x["list_name"] for x in items[:10])])
        for i, w in enumerate([10, 10, 96], 1):
            ws2.column_dimensions[get_column_letter(i)].width = w

        ws3 = wb.create_sheet("说明")
        for k, v in [
            ("生成时间", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("来源", "gouvip 榜单目录（本地）"),
            ("筛选逻辑", "按品类常识给高客单关键词加分，排除低客单/耗材/促销词"),
            ("重要", "「高客单先验分」只是假设，不是实测价格。必须抽样抓榜单再用 pdd_category.py 验证"),
            ("已验证", "「已验证商品数」>0 表示本地已有该榜单的真实商品数据"),
        ]:
            ws3.append([k, v])
        ws3.column_dimensions["A"].width = 14
        ws3.column_dimensions["B"].width = 92

        path = os.path.join(OUT, a.excel or f"待验证类目_{time.strftime('%Y%m%d_%H%M')}.xlsx")
        os.makedirs(OUT, exist_ok=True)
        wb.save(path)
        print(f"\n✓ 已导出 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
