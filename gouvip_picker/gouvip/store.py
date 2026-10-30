"""SQLite 存储层：类目 / 行业榜单 / 商品快照 / 榜单差异事件。"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS cat_top (
    tab_id      INTEGER PRIMARY KEY,
    tab_name    TEXT,
    first_seen  TEXT,
    last_seen   TEXT
);

CREATE TABLE IF NOT EXISTS cat_sub (
    tab_id        INTEGER PRIMARY KEY,
    tab_name      TEXT,
    parent_tab_id INTEGER,
    first_seen    TEXT,
    last_seen     TEXT
);

CREATE TABLE IF NOT EXISTS ranking (
    tab_id          INTEGER PRIMARY KEY,
    tab_name        TEXT,
    superior_tab_id INTEGER,
    list_sn         TEXT,
    first_seen      TEXT,
    last_seen       TEXT,
    seen_count      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ranking_event (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    INTEGER,
    tab_id    INTEGER,
    tab_name  TEXT,
    event     TEXT,          -- new(首次出现) / back(消失后又回来)
    at        TEXT
);

CREATE TABLE IF NOT EXISTS goods (
    goods_id   INTEGER PRIMARY KEY,
    goods_name TEXT,
    image_url  TEXT,
    cat_id1    INTEGER,
    cat_id2    INTEGER,
    cat_id3    INTEGER,
    add_time   TEXT,
    first_seen TEXT,
    last_seen  TEXT
);

CREATE TABLE IF NOT EXISTS goods_snap (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER,
    goods_id    INTEGER,
    source      TEXT,        -- today / week
    tag         TEXT,        -- 采集时的关键词/榜单标签，用于统计"多榜单命中"
    biz_date    TEXT,
    price_min   REAL,
    price_max   REAL,
    sales_min   INTEGER,
    sales_max   INTEGER,
    avg_sales   REAL,
    update_time TEXT,
    fetched_at  TEXT
);

CREATE TABLE IF NOT EXISTS run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT,
    params      TEXT,
    started_at  TEXT,
    finished_at TEXT,
    rows        INTEGER DEFAULT 0,
    note        TEXT
);

CREATE INDEX IF NOT EXISTS idx_snap_goods ON goods_snap(goods_id, id);
CREATE INDEX IF NOT EXISTS idx_snap_run   ON goods_snap(run_id);
CREATE INDEX IF NOT EXISTS idx_rank_sup   ON ranking(superior_tab_id);
"""


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # ---------------- run ----------------
    def start_run(self, kind: str, params: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO run(kind, params, started_at) VALUES (?,?,?)",
            (kind, json.dumps(params, ensure_ascii=False), now()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, rows: int, note: str = "") -> None:
        self.db.execute(
            "UPDATE run SET finished_at=?, rows=?, note=? WHERE id=?",
            (now(), rows, note, run_id),
        )
        self.db.commit()

    # ---------------- meta ----------------
    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self.db.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    # ---------------- 类目 / 榜单 ----------------
    def upsert_cat_top(self, cats: Iterable[dict]) -> None:
        ts = now()
        for c in cats:
            self.db.execute(
                """INSERT INTO cat_top(tab_id,tab_name,first_seen,last_seen) VALUES(?,?,?,?)
                   ON CONFLICT(tab_id) DO UPDATE SET tab_name=excluded.tab_name, last_seen=excluded.last_seen""",
                (c["tab_id"], c["tab_name"], ts, ts),
            )
        self.db.commit()

    def upsert_cat_sub(self, parent_tab_id: int, subs: Iterable[dict]) -> None:
        ts = now()
        for s in subs:
            self.db.execute(
                """INSERT INTO cat_sub(tab_id,tab_name,parent_tab_id,first_seen,last_seen) VALUES(?,?,?,?,?)
                   ON CONFLICT(tab_id) DO UPDATE SET tab_name=excluded.tab_name,
                       parent_tab_id=excluded.parent_tab_id, last_seen=excluded.last_seen""",
                (s["tab_id"], s.get("tab_name"), parent_tab_id, ts, ts),
            )
        self.db.commit()

    def upsert_rankings(self, run_id: int, items: Iterable[dict]) -> dict:
        """写入榜单条目，返回 {'new': [...], 'back': [...], 'total': n}。"""
        ts = now()
        new, back = [], []
        for it in items:
            tid = it.get("tab_id")
            if tid is None:
                continue
            row = self.db.execute("SELECT last_seen FROM ranking WHERE tab_id=?", (tid,)).fetchone()
            self.db.execute(
                """INSERT INTO ranking(tab_id,tab_name,superior_tab_id,list_sn,first_seen,last_seen,seen_count)
                   VALUES(?,?,?,?,?,?,1)
                   ON CONFLICT(tab_id) DO UPDATE SET
                       tab_name=excluded.tab_name,
                       superior_tab_id=excluded.superior_tab_id,
                       list_sn=excluded.list_sn,
                       last_seen=excluded.last_seen,
                       seen_count=ranking.seen_count+1""",
                (tid, it.get("tab_name"), it.get("superior_tab_id"), it.get("list_sn"), ts, ts),
            )
            if row is None:
                new.append(it)
                self.db.execute(
                    "INSERT INTO ranking_event(run_id,tab_id,tab_name,event,at) VALUES(?,?,?,?,?)",
                    (run_id, tid, it.get("tab_name"), "new", ts),
                )
            elif row["last_seen"] == ts:
                # 同一天重复采集不算
                pass
        self.db.commit()
        total = self.db.execute("SELECT COUNT(*) c FROM ranking").fetchone()["c"]
        return {"new": new, "back": back, "total": total}

    def rankings(self, superior_tab_id: int | None = None, keyword: str = "",
                 limit: int = 0) -> list[dict]:
        sql = "SELECT * FROM ranking WHERE 1=1"
        args: list[Any] = []
        if superior_tab_id:
            sql += " AND superior_tab_id=?"
            args.append(superior_tab_id)
        if keyword:
            sql += " AND tab_name LIKE ?"
            args.append(f"%{keyword}%")
        sql += " ORDER BY tab_id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def get_ranking(self, tab_id: int) -> dict | None:
        r = self.db.execute("SELECT * FROM ranking WHERE tab_id=?", (tab_id,)).fetchone()
        return dict(r) if r else None

    def ranking_events(self, limit: int = 200) -> list[dict]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM ranking_event ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        ]

    # ---------------- 商品 ----------------
    def prev_sales(self, goods_id: int) -> tuple[float, str] | None:
        """返回该商品上一次快照的 (max_sales, fetched_at)，无历史则 None。"""
        r = self.db.execute(
            "SELECT sales_max, fetched_at FROM goods_snap WHERE goods_id=? ORDER BY id DESC LIMIT 1",
            (goods_id,),
        ).fetchone()
        if not r:
            return None
        return float(r["sales_max"] or 0), r["fetched_at"]

    def upsert_goods(self, run_id: int, rows: Iterable[dict], source: str,
                     biz_date: str, tag: str = "") -> int:
        ts = now()
        n = 0
        for g in rows:
            gid = g.get("goods_id")
            if not gid:
                continue
            avg = g.get("average_sales2")
            try:
                avg = float(avg) if avg is not None and str(avg) != "" else None
            except (TypeError, ValueError):
                avg = None
            self.db.execute(
                """INSERT INTO goods(goods_id,goods_name,image_url,cat_id1,cat_id2,cat_id3,add_time,first_seen,last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(goods_id) DO UPDATE SET
                       goods_name=excluded.goods_name,
                       image_url=excluded.image_url,
                       cat_id1=excluded.cat_id1, cat_id2=excluded.cat_id2, cat_id3=excluded.cat_id3,
                       add_time=COALESCE(excluded.add_time, goods.add_time),
                       last_seen=excluded.last_seen""",
                (
                    gid, g.get("goods_name"), g.get("image_url"),
                    g.get("cat_id1"), g.get("cat_id2"), g.get("cat_id3"),
                    g.get("add_time"), ts, ts,
                ),
            )
            self.db.execute(
                """INSERT INTO goods_snap(run_id,goods_id,source,tag,biz_date,price_min,price_max,
                       sales_min,sales_max,avg_sales,update_time,fetched_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, gid, source, tag, biz_date,
                    g.get("mini_price"), g.get("max_price"),
                    g.get("mini_sales"), g.get("max_sales"), avg,
                    g.get("update_time"), ts,
                ),
            )
            n += 1
        self.db.commit()
        return n

    def latest_run_id(self, kind: str | None = None) -> int | None:
        sql = "SELECT id FROM run WHERE finished_at IS NOT NULL"
        args: list[Any] = []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY id DESC LIMIT 1"
        r = self.db.execute(sql, args).fetchone()
        return int(r["id"]) if r else None

    def candidates(self, run_id: int) -> list[dict]:
        """把某轮采集的商品去重成候选池，并算好成长率 / 是否新进 / 榜单命中数。"""
        sql = """
        SELECT s.goods_id, g.goods_name, g.image_url, g.add_time, g.first_seen,
               s.source, s.tag, s.biz_date, s.price_min, s.price_max,
               s.sales_min, s.sales_max, s.avg_sales, s.update_time,
               (SELECT p.sales_max FROM goods_snap p
                 WHERE p.goods_id = s.goods_id AND p.id < s.id
                 ORDER BY p.id DESC LIMIT 1) AS prev_sales
        FROM goods_snap s JOIN goods g ON g.goods_id = s.goods_id
        WHERE s.run_id = ?
        """
        merged: dict[int, dict] = {}
        tags: dict[int, set] = {}
        for r in self.db.execute(sql, (run_id,)).fetchall():
            gid = r["goods_id"]
            d = merged.get(gid)
            sales = float(r["sales_max"] or 0)
            if d is None:
                merged[gid] = {
                    "goods_id": gid,
                    "goods_name": r["goods_name"],
                    "image_url": r["image_url"],
                    "add_time": r["add_time"],
                    "first_seen": r["first_seen"],
                    "price": r["price_min"],
                    "price_max": r["price_max"],
                    "sales": sales,
                    "sales_min": r["sales_min"],
                    "avg_sales": r["avg_sales"],
                    "update_time": r["update_time"],
                    "prev_sales": r["prev_sales"],
                    "tags": [],
                }
                tags[gid] = set()
            else:
                d["sales"] = max(d["sales"], sales)
            if r["tag"]:
                tags[gid].add(r["tag"])

        out = []
        for gid, d in merged.items():
            prev = d.pop("prev_sales")
            prev = float(prev) if prev is not None else None
            cur = d["sales"]
            if prev is None or prev <= 0:
                d["growth"] = None
                d["is_new"] = True
            else:
                d["growth"] = (cur - prev) / prev
                d["is_new"] = False
            d["hits"] = len(tags.get(gid) or [])
            d["tags"] = sorted(tags.get(gid) or [])
            out.append(d)
        return out

    def freshness(self) -> dict:
        """数据新鲜度自检：榜单/商品数据的最大 update_time 距今多少天。"""
        r = self.db.execute(
            "SELECT MAX(update_time) m, MAX(fetched_at) f, COUNT(*) c FROM goods_snap"
        ).fetchone()
        stale_days = None
        if r and r["m"]:
            try:
                d = time.strptime(str(r["m"])[:10], "%Y-%m-%d")
                stale_days = (time.time() - time.mktime(d)) / 86400
            except Exception:
                stale_days = None
        return {
            "data_update_time": (r["m"] if r else None),
            "last_fetch": (r["f"] if r else None),
            "snapshots": (r["c"] if r else 0),
            "stale_days": round(stale_days, 1) if stale_days is not None else None,
            "warning": bool(stale_days is not None and stale_days > 7),
        }

    def stats(self) -> dict:
        q = lambda s: self.db.execute(s).fetchone()[0]
        return {
            "cat_top": q("SELECT COUNT(*) FROM cat_top"),
            "cat_sub": q("SELECT COUNT(*) FROM cat_sub"),
            "ranking": q("SELECT COUNT(*) FROM ranking"),
            "ranking_new": q("SELECT COUNT(*) FROM ranking_event WHERE event='new'"),
            "goods": q("SELECT COUNT(*) FROM goods"),
            "snapshots": q("SELECT COUNT(*) FROM goods_snap"),
            "runs": q("SELECT COUNT(*) FROM run"),
            "last_update_time": (self.db.execute(
                "SELECT MAX(update_time) FROM goods_snap").fetchone()[0] or ""),
            "last_fetch": (self.db.execute(
                "SELECT MAX(fetched_at) FROM goods_snap").fetchone()[0] or ""),
        }
