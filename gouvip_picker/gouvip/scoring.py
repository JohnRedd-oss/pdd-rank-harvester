"""选品打分引擎。

设计原则：
  - 全部权重/阈值来自 config.json，Web UI 可可视化调整
  - 有硬过滤（最低销量、价格带、关键词黑名单），过滤后按加权分排序
  - 每个商品输出「得分 + 分项 + 命中信号」，方便人工复核而不是黑盒
"""
from __future__ import annotations

import datetime as _dt
import math
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "weights": {
        "sales": 0.40,    # 销量规模（对数归一）
        "growth": 0.30,   # 相对上次快照的销量增长率
        "price": 0.15,    # 价格带命中
        "fresh": 0.10,    # 上架新鲜度
        "new": 0.05,      # 本轮新出现
    },
    "price": {
        "min": 5.0,
        "max": 99.0,
        "sweet_min": 9.9,
        "sweet_max": 49.9,
    },
    "filters": {
        "min_sales": 100,
        "max_sales": 0,           # 0 = 不限
        "min_price": 0.0,
        "max_price": 0.0,         # 0 = 不限
        "exclude_keywords": ["赠品", "补差价", "运费", "测试"],
        "include_keywords": [],
    },
    "rank": {
        "top_n": 50,
        "sales_norm_cap": 100000,
    },
}


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def merge_config(base: dict, override: dict | None) -> dict:
    out = {k: (v.copy() if isinstance(v, dict) else v) for k, v in base.items()}
    if not override:
        return out
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def sales_score(sales: float, cap: float) -> float:
    if sales <= 0:
        return 0.0
    return _clip(math.log10(1 + sales) / math.log10(1 + cap))


def growth_score(growth: float | None) -> float:
    """growth 为相对上次快照的变化率；None 表示无历史（给中性 0.5）。"""
    if growth is None:
        return 0.5
    return _clip(0.5 + growth * 0.5)


def price_score(price: float | None, cfg: dict) -> float:
    if price is None or price <= 0:
        return 0.4  # 价格未知，给偏低中性分
    p = cfg["price"]
    if p["sweet_min"] <= price <= p["sweet_max"]:
        return 1.0
    if p["min"] <= price <= p["max"]:
        return 0.75 if price < p["sweet_min"] else 0.6
    # 区间外按偏离比例衰减
    ref = max(p["sweet_min"], 1.0) if price < p["min"] else max(p["max"], 1.0)
    dev = abs(price - ref) / ref
    return _clip(0.5 * (1 - dev), 0.0, 0.5)


def fresh_score(add_time: str | None, today: _dt.date) -> float:
    if not add_time:
        return 0.3
    try:
        d = _dt.date.fromisoformat(str(add_time)[:10])
    except ValueError:
        return 0.3
    days = (today - d).days
    if days <= 30:
        return 1.0
    if days <= 90:
        return 0.8
    if days <= 180:
        return 0.6
    if days <= 365:
        return 0.35
    return 0.1


def hard_filter(g: dict, cfg: dict) -> str | None:
    """返回过滤原因；None 表示通过。"""
    f = cfg["filters"]
    name = (g.get("goods_name") or "").lower()
    for kw in f.get("exclude_keywords") or []:
        if kw and kw.lower() in name:
            return f"命中黑名单词:{kw}"
    inc = f.get("include_keywords") or []
    if inc and not any(kw.lower() in name for kw in inc if kw):
        return "未命中白名单词"
    sales = g.get("sales") or 0
    if f.get("min_sales") and sales < f["min_sales"]:
        return f"销量<{f['min_sales']}"
    if f.get("max_sales") and sales > f["max_sales"]:
        return f"销量>{f['max_sales']}"
    price = g.get("price")
    if price is not None:
        if f.get("min_price") and price < f["min_price"]:
            return f"价格<{f['min_price']}"
        if f.get("max_price") and price > f["max_price"]:
            return f"价格>{f['max_price']}"
    return None


def score_row(g: dict, cfg: dict, today: _dt.date) -> dict:
    """g 需含: goods_id, goods_name, price(可空), sales, add_time, growth, is_new, hits(命中榜单数)"""
    w = cfg["weights"]
    comp = {
        "sales": sales_score(float(g.get("sales") or 0), cfg["rank"].get("sales_norm_cap", 100000)),
        "growth": growth_score(g.get("growth")),
        "price": price_score(g.get("price"), cfg),
        "fresh": fresh_score(g.get("add_time"), today),
        "new": 1.0 if g.get("is_new") else 0.0,
    }
    total_w = sum(float(v) for v in w.values()) or 1.0
    score = sum(comp[k] * float(w.get(k, 0)) for k in comp) / total_w

    signals = []
    if g.get("is_new"):
        signals.append("新进商品池")
    gr = g.get("growth")
    if gr is not None and gr >= 0.3:
        signals.append(f"销量+{gr*100:.0f}%")
    elif gr is not None and gr <= -0.3:
        signals.append(f"销量{gr*100:.0f}%")
    p = g.get("price")
    if p and cfg["price"]["sweet_min"] <= p <= cfg["price"]["sweet_max"]:
        signals.append("价格带内")
    if (g.get("hits") or 0) >= 2:
        signals.append(f"多榜单命中x{g['hits']}")

    return {
        "score": round(score * 100, 2),
        "components": {k: round(v * 100, 1) for k, v in comp.items()},
        "signals": signals,
    }


def rank(candidates: list[dict], cfg: dict, today: _dt.date | None = None) -> tuple[list[dict], list[dict]]:
    """返回 (通过并排序的清单, 被过滤掉的清单)。"""
    today = today or _dt.date.today()
    passed, dropped = [], []
    for g in candidates:
        reason = hard_filter(g, cfg)
        if reason:
            row = dict(g)
            row["drop_reason"] = reason
            dropped.append(row)
            continue
        row = dict(g)
        row.update(score_row(g, cfg, today))
        passed.append(row)
    # 同分时用「销量 → 榜单命中数 → 近上架」做次级排序，避免并列堆叠
    passed.sort(
        key=lambda r: (
            r["score"],
            float(r.get("sales") or 0),
            int(r.get("hits") or 0),
            str(r.get("add_time") or ""),
        ),
        reverse=True,
    )
    top_n = int(cfg["rank"].get("top_n") or 50)
    return passed[:top_n], dropped
