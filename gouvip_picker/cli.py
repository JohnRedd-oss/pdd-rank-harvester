#!/usr/bin/env python
"""命令行入口。

示例：
  python cli.py login
  python cli.py cats
  python cli.py industry --cat 13
  python cli.py rsearch --kw 垃圾桶
  python cli.py sync --kw 垃圾桶 --kw 厨房垃圾桶 --pages 3
  python cli.py pick --excel
  python cli.py diff
  python cli.py ui
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from gouvip import api as A            # noqa: E402
from gouvip import pipeline as P       # noqa: E402
from gouvip.scoring import DEFAULT_CONFIG, merge_config  # noqa: E402
from gouvip.store import Store         # noqa: E402


def load_config(path: str) -> dict:
    cfg = {"account": {}, "paths": {"db": "data/picker.db", "out": "out"},
           "collect": {"pages": 3, "size": 100, "source": "today", "sort": 1}}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def make(cfg: dict) -> tuple[A.GouVip, Store]:
    acct = cfg.get("account") or {}
    db_path = os.path.join(ROOT, cfg["paths"]["db"])
    gp = A.GouVip(
        username=acct.get("username"),
        password=acct.get("password"),
        state_file=os.path.join(ROOT, "data", "token.json"),
    )
    return gp, Store(db_path)


def rank_cfg(cfg: dict) -> dict:
    keys = ("weights", "price", "filters", "rank")
    return merge_config(DEFAULT_CONFIG, {k: cfg[k] for k in keys if k in cfg})


def say(*a):
    print(*a, flush=True)


def _force_utf8() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    ap = argparse.ArgumentParser(prog="gouvip-picker", description="行业榜单选品工具")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="登录并缓存 token")
    sub.add_parser("stats", help="查看本地库概况 / 数据新鲜度")

    p_cats = sub.add_parser("cats", help="列出行业榜单一级类目")
    p_cats.add_argument("--save", action="store_true", help="写入本地库")

    p_ind = sub.add_parser("industry", help="拉取某类目下的全部行业榜单（含差异追踪）")
    p_ind.add_argument("--cat", required=True, help="一级类目 tab_id，如 13=水果生鲜")
    p_ind.add_argument("--no-subs", action="store_true", help="只拉本级，不展开子榜")

    p_rs = sub.add_parser("rsearch", help="按关键词跨类目搜榜单")
    p_rs.add_argument("--kw", required=True)

    p_sync = sub.add_parser("sync", help="采集热销商品池（选品原料）")
    p_sync.add_argument("--kw", action="append", default=None, help="关键词，可多次")
    p_sync.add_argument("--cat", default="0", help="商品类目 id，0=全部")
    p_sync.add_argument("--pages", type=int, default=None)
    p_sync.add_argument("--size", type=int, default=None)
    p_sync.add_argument("--source", choices=["today", "week"], default=None)
    p_sync.add_argument("--sort", type=int, default=None, help="0=默认 1=销量")

    p_pick = sub.add_parser("pick", help="按当前配置打分选品")
    p_pick.add_argument("--run", type=int, default=None, help="指定采集批次 run_id")
    p_pick.add_argument("--top", type=int, default=None)
    p_pick.add_argument("--excel", nargs="?", const="", default=None, help="导出 xlsx")
    p_pick.add_argument("--rank-cat", default=None, help="同时导出该类目的榜单目录 sheet")
    p_pick.add_argument("--rank-kw", default=None, help="同时导出命中该关键词的榜单目录 sheet")
    p_pick.add_argument("--from-rank", default=None,
                        help="按榜单ID选品：拿榜单名当关键词采商品池，再打分")

    sub.add_parser("diff", help="查看榜单新增/最近批次差异")

    p_daily = sub.add_parser("daily", help="每日任务：刷榜单 + 采商品池 + 打分 + 导出")
    p_daily.add_argument("--cat", action="append", default=None, help="覆盖 watch.cats")
    p_daily.add_argument("--kw", action="append", default=None, help="覆盖 watch.keywords")
    p_daily.add_argument("--pages", type=int, default=None)
    p_daily.add_argument("--no-excel", action="store_true")

    p_ui = sub.add_parser("ui", help="启动本地 Web 配置界面")
    p_ui.add_argument("--port", type=int, default=8765)
    p_ui.add_argument("--no-browser", action="store_true")

    args = ap.parse_args(argv)
    cfg = load_config(args.config)

    if args.cmd == "ui":
        from gouvip.ui import serve
        return serve(ROOT, args.config, port=args.port, open_browser=not args.no_browser)

    gp, store = make(cfg)
    try:
        if args.cmd == "login":
            d = gp.login()
            say(f"✓ 登录成功 uid={d.get('user_id')} level={d.get('level')} "
                f"套餐到期={d.get('expire_time')} 余额={d.get('balance')}")
            if int(d.get("level") or 0) == 0:
                say("⚠ level=0：套餐未生效或已过期，接口会返回历史快照数据（非实时）")
            return 0

        if args.cmd == "stats":
            s = store.stats()
            f = store.freshness()
            say(json.dumps({"库概况": s, "数据新鲜度": f}, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "cats":
            gp.ensure_login()
            cats = gp.industry_cats()
            for c in cats:
                say(f"{c['tab_id']:>8}  {c['tab_name']}")
            if args.save:
                store.upsert_cat_top(cats)
                say(f"✓ 已写入本地库（{len(cats)} 个类目）")
            return 0

        if args.cmd == "industry":
            gp.ensure_login()
            res = P.sync_industry(gp, store, int(args.cat), with_subs=not args.no_subs,
                                  progress=say)
            say(f"\n✓ 类目[{res['cat']}] 子榜 {res['subs']} 个，榜单 {res['rankings']} 条"
                f"（库内合计 {res['total_in_db']}）")
            if res["new"]:
                say(f"★ 新出现榜单 {len(res['new'])} 条，例如：")
                for it in res["new"][:10]:
                    say(f"   - {it.get('tab_name')}  list_sn={it.get('list_sn')}")
            return 0

        if args.cmd == "rsearch":
            gp.ensure_login()
            res = P.sync_rankings_by_keyword(gp, store, args.kw)
            say(f"✓ 关键词[{res['keyword']}] 命中榜单 {res['rankings']} 条")
            for it in res["items"][:20]:
                say(f"   {it.get('tab_id'):>18}  {it.get('tab_name')}  ← 上级 {it.get('superior_tab_id')}")
            return 0

        if args.cmd == "sync":
            gp.ensure_login()
            col = cfg.get("collect") or {}
            kws = args.kw or [""]
            res = P.sync_goods(
                gp, store, kws,
                source=args.source or col.get("source", "today"),
                pages=args.pages or int(col.get("pages", 3)),
                size=args.size or int(col.get("size", 100)),
                sort=args.sort if args.sort is not None else int(col.get("sort", 1)),
                cat=args.cat,
                progress=say,
            )
            say(f"\n✓ 采集完成 run_id={res['run_id']} 入库 {res['rows']} 条"
                f"（接口 total={res['api_total']} date={res['date']} source={res['source']}）")
            return 0

        if args.cmd == "pick":
            rc = rank_cfg(cfg)
            if args.top:
                rc["rank"]["top_n"] = args.top
            run_id = args.run
            if args.from_rank:
                rk = store.get_ranking(int(args.from_rank))
                if not rk:
                    say(f"✗ 本地库里没有榜单 {args.from_rank}，先跑：cli.py industry --cat <类目> 或 rsearch --kw <词>")
                    return 1
                kw = rk["tab_name"]
                gp.ensure_login()
                say(f"榜单【{kw}】→ 用该榜单名做关键词采集商品池")
                col = cfg.get("collect") or {}
                sres = P.sync_goods_by_rank(
                    gp, store, kw,
                    pages=min(2, int(col.get("pages", 2))),
                    size=int(col.get("size", 100)),
                    source=col.get("source", "today"),
                    sort=int(col.get("sort", 1)),
                    progress=say,
                )
                run_id = sres["run_id"]
            result = P.pick(store, run_id=run_id, config=rc, progress=say)
            passed = result["passed"]
            say(f"候选 {result['candidates']} 条 → 入选 {len(passed)} 条 "
                f"（过滤掉 {len(result['dropped'])} 条）\n")
            head = f"{'#':>3} {'得分':>6} {'价格':>6} {'销量':>8} {'增长':>7}  商品名"
            say(head)
            say("-" * 100)
            for i, r in enumerate(passed[: int(rc["rank"]["top_n"])], 1):
                g = r.get("growth")
                say(f"{i:>3} {r['score']:>6} {(r.get('price') or 0):>6} {r.get('sales'):>8} "
                    f"{(f'{g*100:.0f}%' if g is not None else '-'):>7}  {(r.get('goods_name') or '')[:50]}")
            if args.excel is not None:
                out_dir = os.path.join(ROOT, cfg["paths"]["out"])
                name = args.excel or f"pick_{result['run_id']}_{__import__('time').strftime('%Y%m%d_%H%M')}.xlsx"
                path = os.path.join(out_dir, name)
                rk = []
                if args.rank_cat:
                    rk = store.rankings(int(args.rank_cat))
                elif args.rank_kw:
                    rk = store.rankings(keyword=args.rank_kw)
                P.export_excel(path, result, rankings=rk)
                say(f"\n✓ 已导出 {path}")
            return 0

        if args.cmd == "daily":
            watch = cfg.get("watch") or {}
            cats = args.cat if args.cat else [str(c) for c in (watch.get("cats") or [])]
            kws = args.kw if args.kw else (watch.get("keywords") or [""])
            col = cfg.get("collect") or {}
            pages = args.pages or int(watch.get("pages") or col.get("pages", 2))
            gp.ensure_login()
            say(f"== 1/3 刷新行业榜单：{len(cats)} 个类目 ==")
            for c in cats:
                try:
                    res = P.sync_industry(gp, store, int(c), progress=say)
                    say(f"  ✓ {res['cat']}: 榜单 {res['rankings']} 条，新增 {len(res['new'])}")
                except A.ApiError as e:
                    say(f"  ! 类目 {c} 失败：{e}")
            say(f"\n== 2/3 采集商品池：{kws} ==")
            sres = P.sync_goods(gp, store, kws, source=col.get("source", "today"),
                                pages=pages, size=int(col.get("size", 100)),
                                sort=int(col.get("sort", 1)), progress=say)
            say(f"  ✓ run_id={sres['run_id']} 入库 {sres['rows']} 条")
            say("\n== 3/3 打分选品 ==")
            rc = rank_cfg(cfg)
            result = P.pick(store, run_id=sres["run_id"], config=rc, progress=say)
            passed = result["passed"]
            say(f"  候选 {result['candidates']} → 入选 {len(passed)}")
            for i, r in enumerate(passed[:10], 1):
                say(f"   {i:>2}. [{r['score']}] {(r.get('goods_name') or '')[:44]}"
                    f"  ¥{r.get('price')}  销量{r.get('sales')}")
            if not args.no_excel:
                out_dir = os.path.join(ROOT, cfg["paths"]["out"])
                name = f"选品_{sres['date']}_{__import__('time').strftime('%H%M')}.xlsx"
                path = os.path.join(out_dir, name)
                P.export_excel(path, result, rankings=store.rankings(keyword=kws[0] if kws else ""))
                say(f"\n✓ 已导出 {path}")
            return 0

        if args.cmd == "diff":
            ev = store.ranking_events(limit=50)
            f = store.freshness()
            say(f"数据新鲜度：update_time={f['data_update_time']} 距今 {f['stale_days']} 天"
                f"{'  ⚠ 数据陈旧' if f['warning'] else ''}")
            if not ev:
                say("暂无榜单差异事件（先跑 industry 或 rsearch）")
            else:
                say("\n最近榜单新增事件：")
                for e in ev:
                    say(f"  {e['at']}  {e['event']:<4}  {e['tab_name']}  (tab_id={e['tab_id']})")
            return 0

        ap.print_help()
        return 1
    except A.ApiError as e:
        say(f"✗ 接口错误：{e}")
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
