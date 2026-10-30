"""拼多多榜单页采集器（Phase 2）。

设计目标：**慢、稳、像人**。不是把 PDD 打穿，是把你要的那几十个榜单拿下来。

风控策略（CFG 里改）：
  - 单线程串行，绝不并发
  - 每次导航间隔 6~14 秒随机抖动
  - 每小时 ≤ 45 页，每天 ≤ 300 页，超限直接停
  - 每 35 页强制长休息 3~7 分钟
  - 每次导航后随机滚动 2~4 次、停留 3~8 秒
  - 命中风控信号（验证码/滑块/登录失效/访问频繁）→ 立即停跑 + 冷却 45~90 分钟
  - 走真实 Chrome profile + 真实页面请求，不自造 anti_content、不重放接口
  - 不改 UA / 时区 / 语言，保留 --disable-blink-features=AutomationControlled

命令：
    python pdd_crawler.py login            一次性扫码（也可双击 pdd_login.bat）
    python pdd_crawler.py check            看 Chrome / 登录态
    python pdd_crawler.py probe <榜单URL>   探结构，落 JSON/DOM/截图
    python pdd_crawler.py replay <文件>     离线解析已抓到的 JSON（不联网，调解析用）
    python pdd_crawler.py run --limit 20   从 gouvip 榜单库排队抓（自动跳过已抓）
    python pdd_crawler.py status           进度 / 冷却 / 最近页
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time

import requests
import websocket

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "pdd-profile")
RAW = os.path.join(HERE, "pdd-raw")
SHOTS = os.path.join(HERE, "shots")
DB = os.path.join(HERE, "pdd-raw", "pdd.db")
PICKER_DB = os.environ.get("GOUVIP_DB") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gouvip_picker", "data", "picker.db")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9444
LOGIN_URL = "https://mobile.yangkeduo.com/login.html"
UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 6) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36")

CFG = {
    "min_gap": 15.0,          # B 档：15~35 秒随机
    "max_gap": 35.0,
    "per_hour": 15,
    "per_day": 80,
    "long_rest_every": 12,
    "long_rest_min": 480,
    "long_rest_max": 900,
    "dwell_min": 5.0,
    "dwell_max": 11.0,
    "scroll_min": 2,
    "scroll_max": 4,
    "nav_timeout": 25.0,
    "cooldown_min": 90 * 60,
    "cooldown_max": 180 * 60,
    "tabs_per_list": 1,
    "revisit_days": 1,
    "dead_burst": 3,
    "active_hours": [8, 23],   # 只在白天跑，凌晨不动
}

CFG_FILE = os.path.join(HERE, "pdd_crawler.json")
if os.path.exists(CFG_FILE):
    try:
        with open(CFG_FILE, encoding="utf-8") as _f:
            CFG.update(json.load(_f))
    except Exception as _e:
        print(f"⚠ 读取 {CFG_FILE} 失败：{_e}", flush=True)

# 金丝雀榜单：用来区分「真下架」与「软封返回下架」
CANARY_URL = ("https://mobile.yangkeduo.com/sjs_cat_rank_list.html"
              "?list_id=195101010201&list_sn=YWFtgHg0WOPNeGgyNFPIa7Uk&scene_id=goods_detail")

RISK_WORDS = ("验证码", "滑块", "请完成验证", "访问频繁", "操作过于频繁", "安全验证",
              "unusual traffic", "系统繁忙")
# 说明：「系统繁忙」这类弱信号只在页面几乎没内容时才算风控，避免正常页面的 toast 误伤
WEAK_RISK = ("系统繁忙",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS page (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT UNIQUE,
    list_id     TEXT,
    list_name   TEXT,
    tab_kind    TEXT,          -- 畅销榜 / 好评榜 / ...
    rule        TEXT,
    list_type   INTEGER,
    rule_sn     TEXT,
    status      TEXT,          -- ok / login / risk / empty / error / blocked
    items       INTEGER DEFAULT 0,
    fetched_at  TEXT,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS item (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id     INTEGER,
    list_id     TEXT,
    list_name   TEXT,
    tab_kind    TEXT,
    rank        INTEGER,
    goods_id    TEXT,
    title       TEXT,
    image       TEXT,
    price       REAL,          -- 元
    sales_text  TEXT,          -- 19.9万+
    sales_tip   TEXT,          -- 总售19.9万+件
    hot_score   INTEGER,       -- 畅销榜=热卖指数；好评榜=4(占位)；降价榜=0
    score_text  TEXT,          -- 该 tab 真正的分数文本：6530 / 95.58% / 降价幅度
    tag_text    TEXT,          -- 蝉联榜首90天 / 霸榜前三6天
    brand       TEXT,
    group_text  TEXT,          -- 24小时内999+人拼单
    day_orders  REAL,          -- 日均单量下限（由 group_text 解析，7天窗口已折算）
    order_window TEXT,         -- 原始窗口：24小时 / 7天 / 30天
    tags        TEXT,          -- 正品险/假一赔十/...
    fetched_at  TEXT
);
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS dead_list (
    list_id   TEXT PRIMARY KEY,
    list_name TEXT,
    reason    TEXT,
    at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_item_goods ON item(goods_id);
CREATE INDEX IF NOT EXISTS idx_item_list  ON item(list_id);
CREATE INDEX IF NOT EXISTS idx_page_time  ON page(fetched_at);
"""


# --------------------------------------------------------------- 存储
def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    _migrate(c)
    return c


def _migrate(c: sqlite3.Connection) -> None:
    """老库补列（SQLite 的 CREATE TABLE IF NOT EXISTS 不会加新列）。"""
    want = {
        "item": [("day_orders", "REAL"), ("order_window", "TEXT")],
        "page": [],
    }
    for table, cols in want.items():
        have = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, typ in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    c.commit()


def st_get(c, k, default=None):
    r = c.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def st_set(c, k, v):
    c.execute("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
              (k, json.dumps(v, ensure_ascii=False)))
    c.commit()


class Limiter:
    def __init__(self, c, cfg=CFG):
        self.c, self.cfg = c, cfg
        self.count = 0
        self.last = float(st_get(c, "last_nav", 0) or 0)

    def _count_since(self, sec: int) -> int:
        """按「榜单数」计数，而不是 tab 行数。

        一个榜单会写多条 page 行（畅销/好评/降价各一条），按行计数会把 1 次访问算成 3 次，
        导致配额永远提前打满（实测：一批 5 个榜单 = 15 行，正好吃掉整个小时额度）。
        """
        since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - sec))
        row = self.c.execute(
            "SELECT COUNT(DISTINCT substr(url, 1, instr(url || '|', '|') - 1)) n "
            "FROM page WHERE fetched_at > ?", (since,)).fetchone()
        return int(row["n"] or 0)

    def blocked_reason(self):
        until = st_get(self.c, "cooldown_until", 0) or 0
        if until and time.time() < until:
            return f"风控冷却中，剩余 {int((until - time.time()) / 60)} 分钟"
        ah = self.cfg.get("active_hours")
        if ah and len(ah) == 2:
            h = time.localtime().tm_hour
            if not (ah[0] <= h < ah[1]):
                return f"不在运行时段（{ah[0]}:00–{ah[1]}:00）"
        if self._count_since(3600) >= self.cfg["per_hour"]:
            return f"已达每小时上限 {self.cfg['per_hour']} 页"
        if self._count_since(86400) >= self.cfg["per_day"]:
            return f"已达每日上限 {self.cfg['per_day']} 页"
        return None

    def wait(self):
        gap = random.uniform(self.cfg["min_gap"], self.cfg["max_gap"])
        elapsed = time.time() - self.last
        if elapsed < gap:
            time.sleep(gap - elapsed)
        self.count += 1
        every = self.cfg["long_rest_every"]
        if every and self.count % every == 0:
            rest = random.uniform(self.cfg["long_rest_min"], self.cfg["long_rest_max"])
            print(f"  … 长休息 {rest/60:.1f} 分钟（连续 {self.count} 页）", flush=True)
            time.sleep(rest)
        self.last = time.time()
        st_set(self.c, "last_nav", self.last)

    def trip(self, why: str):
        cd = random.uniform(self.cfg["cooldown_min"], self.cfg["cooldown_max"])
        st_set(self.c, "cooldown_until", time.time() + cd)
        print(f"  ⛔ 命中风控[{why}]，冷却 {cd/60:.0f} 分钟", flush=True)


# --------------------------------------------------------------- CDP
def _alive() -> bool:
    try:
        return bool(requests.get(f"http://127.0.0.1:{PORT}/json/version", timeout=1).json())
    except Exception:
        return False


def _browser_close() -> None:
    """只关我们自己那个 Chrome（9444 端口），不动用户自己的浏览器。"""
    try:
        v = requests.get(f"http://127.0.0.1:{PORT}/json/version", timeout=2).json()
        ws = websocket.create_connection(v["webSocketDebuggerUrl"], timeout=5)
        try:
            ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            time.sleep(1.5)
        finally:
            ws.close()
    except Exception:
        pass


def cookies() -> list[dict]:
    try:
        ws = websocket.create_connection(
            requests.get(f"http://127.0.0.1:{PORT}/json/version", timeout=3).json()["webSocketDebuggerUrl"],
            timeout=10)
        try:
            ws.send(json.dumps({"id": 1, "method": "Storage.getCookies", "params": {}}))
            for _ in range(20):
                m = json.loads(ws.recv())
                if m.get("id") == 1:
                    return (m.get("result") or {}).get("cookies", [])
        finally:
            ws.close()
    except Exception:
        pass
    return []


def logged_in() -> bool:
    return any(c.get("name") == "PDDAccessToken" and c.get("value") for c in cookies())


def launch(headless: bool = True, url: str = "about:blank") -> bool:
    os.makedirs(PROFILE, exist_ok=True)
    args = [CHROME, f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            f"--user-agent={UA}", "--window-size=430,1200"]
    if headless:
        args += ["--headless=new", "--disable-gpu"]
    args.append(url)
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        if _alive():
            return True
        time.sleep(0.25)
    return False


class Tab:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=90, max_size=128 * 1024 * 1024)
        self.i = 0

    def cmd(self, method: str, **params):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})

    def eval(self, expr: str):
        return self.cmd("Runtime.evaluate", expression=expr, returnByValue=True,
                        awaitPromise=False).get("result", {}).get("value")

    def shot(self, path: str):
        r = self.cmd("Page.captureScreenshot", format="png", captureBeyondViewport=True)
        open(path, "wb").write(base64.b64decode(r["data"]))

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def new_tab() -> tuple[str, Tab]:
    t = requests.put(f"http://127.0.0.1:{PORT}/json/new?about:blank", timeout=10).json()
    tab = Tab(t["webSocketDebuggerUrl"])
    tab.cmd("Page.enable")
    tab.cmd("Runtime.enable")
    tab.cmd("Network.enable")
    tab.cmd("Emulation.setDeviceMetricsOverride", width=430, height=1200,
            deviceScaleFactor=2, mobile=True)
    return t["id"], tab


def collect_json(tab: Tab, timeout: float) -> list[dict]:
    """读页面自己发出去的 JSON 响应，不自己打接口。

    两阶段：先纯读事件（把 responseReceived / loadingFinished 全部记下来），
    等窗口结束后再逐个取 body。混在一起读写会漏事件——这是踩过的坑。
    """
    seen: dict[str, dict] = {}
    done: set[str] = set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            m = json.loads(tab.ws.recv())
        except Exception:
            break
        meth = m.get("method")
        if meth == "Network.responseReceived":
            p = m["params"]
            r = p["response"]
            url, mt = r.get("url", ""), (r.get("mimeType") or "").lower()
            if "json" in mt and "yangkeduo" in url and not any(
                    k in url for k in ("/api/ding/", "tracker", "log.", "/xg/")):
                seen[p["requestId"]] = {"url": url, "len_hint": r.get("encodedDataLength") or 0}
        elif meth == "Network.loadingFinished":
            done.add(m["params"]["requestId"])

    out = []
    for rid, info in seen.items():
        if rid not in done:
            continue
        try:
            body = tab.cmd("Network.getResponseBody", requestId=rid)
            text = body.get("body", "")
            if body.get("base64Encoded"):
                text = base64.b64decode(text).decode("utf-8", "replace")
            if text.strip()[:1] in "{[":
                out.append({"data": json.loads(text), "len": len(text), "url": info["url"]})
        except Exception:
            pass
    return out


# --------------------------------------------------------------- 解析
def yuan(v):
    """价格单位是分。"""
    try:
        return round(float(v) / 100.0, 2)
    except (TypeError, ValueError):
        return None


TAB_NAMES = {1: "畅销榜", 2: "好评榜", 4: "降价榜"}


DAY_ORDER_RE = re.compile(
    r"(24小时|24h|一天|一周|7天|近7天|30天|一月)[内以]*\s*(\d+(?:\.\d+)?)\s*(万|亿)?\s*\+?\s*人?拼单",
    re.I)


def parse_day_orders(text: str) -> tuple[float | None, str]:
    """把「24小时内999+人拼单」「一周内100+人拼单」解析成日均单量下限。

    返回 (day_orders, window)。数值是**下限**（999+ 表示 ≥999），所以口径要说明。
    """
    if not text:
        return None, ""
    m = DAY_ORDER_RE.search(text)
    if not m:
        return None, ""
    window = m.group(1)
    n = float(m.group(2))
    unit = m.group(3)
    if unit == "亿":
        n *= 1e8
    elif unit == "万":
        n *= 1e4
    if window in ("一周", "7天", "近7天"):
        return round(n / 7, 1), "7天"
    if window in ("30天", "一月"):
        return round(n / 30, 1), "30天"
    return round(n, 1), "24小时"


def _goods_rows(goods_list) -> list[dict]:
    rows = []
    for idx, g in enumerate(goods_list or [], 1):
        if not isinstance(g, dict) or not g.get("goods_id"):
            continue
        ext = g.get("ext") or {}
        goi = ext.get("group_order_info") or {}
        gtext = (goi.get("text") or "")[:60]
        day_orders, order_window = parse_day_orders(gtext)
        rows.append({
            "rank": idx,
            "goods_id": str(g.get("goods_id")),
            "title": g.get("goods_name") or g.get("short_name") or "",
            "image": g.get("hd_thumb_url") or g.get("thumb_url") or g.get("image_url") or "",
            "price": yuan(g.get("min_on_sale_group_price") or g.get("price")),
            "sales_text": (g.get("sales_text") or "")[:40],
            "sales_tip": (g.get("sales_tip") or "")[:60],
            "hot_score": ext.get("hot_score"),
            "score_text": (ext.get("hot_score_text") or "")[:40],
            "tag_text": (ext.get("tag_text") or "")[:40],
            "brand": (ext.get("brand_name") or "")[:60],
            "group_text": gtext,
            "day_orders": day_orders,
            "order_window": order_window,
            "tags": ",".join(x.get("text", "") for x in (g.get("tag_list") or [])
                             if isinstance(x, dict))[:120],
            "phrase": (ext.get("positive_review_phrases") or "")[:80],
        })
    return rows


def _tab_rec(t: dict, body: dict) -> dict:
    name = (t.get("tab_name") or TAB_NAMES.get(t.get("list_type")) or "").strip()
    return {
        "tab_name": name,
        "rule": t.get("rule") or "",
        "list_id": str(t.get("ranking_list_id") or body.get("id") or ""),
        "rule_sn": t.get("ranking_list_sn") or "",
        "list_type": t.get("list_type"),
        "list_name": (t.get("ranking_list_name") or body.get("tag_name")
                      or (t.get("goods_list") or [{}])[0].get("ext", {}).get("list_name") or ""),
        "goods": _goods_rows(t.get("goods_list")),
    }


def parse_rank_payload(blobs: list[dict]) -> tuple[list[dict], list[dict]]:
    """从抓到的 JSON 里取榜单 tab 与商品；返回 (tabs, 兜底商品)。

    两种已知形态（实测 2026-10-28）：
      A. 接口 landing_page: {data:{id, tag_name, data_list:[{tab_name, rule,
            ranking_list_id, ranking_list_sn, list_type, goods_list:[...]}]}}
         只有「当前 tab」的 goods_list 有货。
      B. 服务端直出: window.rawData.store.tabDataMap = {"1":{tab_name, rule,
            list_type, ranking_list_id, ranking_list_sn, goods_list:[...]}, "2":..}
         key 是 list_type（1 畅销 / 2 好评 / 4 降价）。
    """
    tabs: dict[str, dict] = {}

    def absorb(rec: dict) -> None:
        old = tabs.get(rec["tab_name"])
        if old is None or len(rec["goods"]) > len(old["goods"]):
            tabs[rec["tab_name"]] = rec

    for b in blobs:
        d = b.get("data")
        if not isinstance(d, dict):
            continue
        for body in (d, d.get("data") if isinstance(d.get("data"), dict) else None,
                     (d.get("store") or {}) if isinstance(d.get("store"), dict) else None):
            if not isinstance(body, dict):
                continue
            dl = body.get("data_list")
            if isinstance(dl, list):
                for t in dl:
                    if isinstance(t, dict) and "goods_list" in t:
                        absorb(_tab_rec(t, body))
            tdm = body.get("tabDataMap")
            if isinstance(tdm, dict):
                for v in tdm.values():
                    if isinstance(v, dict) and "goods_list" in v:
                        absorb(_tab_rec(v, body))
            if isinstance(dl, list) or isinstance(tdm, dict):
                break

    if tabs:
        return list(tabs.values()), []

    # 兜底：通用抓取（schema 变了也不至于一条都拿不到）
    rows, seen = [], set()
    def walk(o, depth=0):
        if depth > 12:
            return
        if isinstance(o, dict):
            gid = o.get("goods_id") or o.get("goodsId")
            nm = o.get("goods_name") or o.get("goodsName") or o.get("title")
            if gid and nm and str(gid) not in seen:
                seen.add(str(gid))
                ext = o.get("ext") or {}
                rows.append({
                    "rank": len(rows) + 1, "goods_id": str(gid), "title": str(nm)[:200],
                    "image": o.get("thumb_url") or o.get("image_url") or "",
                    "price": yuan(o.get("min_on_sale_group_price") or o.get("price")),
                    "sales_text": str(o.get("sales_text") or o.get("sales_tip") or "")[:40],
                    "sales_tip": str(o.get("sales_tip") or "")[:60],
                    "hot_score": ext.get("hot_score"), "tag_text": "",
                    "brand": "", "group_text": "", "tags": "",
                })
            for v in o.values():
                walk(v, depth + 1)
        elif isinstance(o, list):
            for v in o[:300]:
                walk(v, depth + 1)
    for b in blobs:
        walk(b.get("data"))
    return [], rows


def grab_state(tab: Tab) -> dict | None:
    """服务端直出的榜单数据在 window.rawData 里，默认 tab 只从这里拿得到。"""
    try:
        s = tab.eval("JSON.stringify(window.rawData || null)")
        if not s or s in ("null", "{}"):
            return None
        return {"data": json.loads(s), "len": len(s), "url": "window.rawData"}
    except Exception:
        return None


DEAD_WORDS = ("已下架", "榜单不存在", "榜单已失效", "该榜单已删除")


def page_dead(tab: Tab, blobs: list[dict]) -> tuple[bool, str]:
    """拼多多对已下架榜单返回 error_code=54001 / 正文「该榜单已下架」。

    gouvip 的榜单目录里大量条目已被 PDD 下架，属于目录陈旧，不是抓取问题。
    """
    for b in blobs:
        d = b.get("data")
        if isinstance(d, dict) and d.get("error_code") == 54001:
            return True, "error_code=54001"
    txt = tab.eval("document.body ? document.body.innerText : ''") or ""
    for w in DEAD_WORDS:
        if w in txt:
            return True, w
    return False, ""


def page_risk(tab: Tab):
    txt = (tab.eval("document.body ? document.body.innerText : ''") or "")
    if "/login" in (tab.eval("location.href") or ""):
        return "login"
    for w in RISK_WORDS:
        if w not in txt:
            continue
        if w in WEAK_RISK and len(txt.strip()) > 200:
            continue          # 正常页面里的提示文案，不算风控
        return w
    return None


# --------------------------------------------------------------- 抓取
def build_url(list_id: str, list_sn: str, active_tab: int = 0) -> str:
    u = ("https://mobile.yangkeduo.com/sjs_cat_rank_list.html"
         f"?list_id={list_id}&list_sn={list_sn}&scene_id=goods_detail")
    return u + (f"&activeTab={active_tab}" if active_tab else "")


def store_rows(c, pid: int, url: str, meta: dict, tabs: list[dict], fallback: list[dict]) -> int:
    """按 tab 落地；同一个榜单会写多条 page（每 tab 一条）。"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    def put_page(key: str, tab: dict | None, status: str, n: int, note: str) -> int:
        t = tab or {}
        c.execute("""INSERT INTO page(url,list_id,list_name,tab_kind,rule,list_type,rule_sn,
                                     status,items,fetched_at,note)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(url) DO UPDATE SET list_name=excluded.list_name,
                         tab_kind=excluded.tab_kind, rule=excluded.rule,
                         list_type=excluded.list_type, rule_sn=excluded.rule_sn,
                         status=excluded.status, items=excluded.items,
                         fetched_at=excluded.fetched_at, note=excluded.note""",
                  (key, t.get("list_id") or meta.get("list_id"),
                   t.get("list_name") or meta.get("list_name"), t.get("tab_name") or "",
                   t.get("rule") or "", t.get("list_type"), t.get("rule_sn") or "",
                   status, n, ts, note))
        return int(c.execute("SELECT id FROM page WHERE url=?", (key,)).fetchone()["id"])

    def put_items(page_id: int, tab: dict, rows: list[dict]) -> None:
        c.execute("DELETE FROM item WHERE page_id=?", (page_id,))
        for g in rows:
            c.execute("""INSERT INTO item(page_id,list_id,list_name,tab_kind,rank,goods_id,
                            title,image,price,sales_text,sales_tip,hot_score,score_text,
                            tag_text,brand,group_text,day_orders,order_window,tags,fetched_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (page_id, tab.get("list_id") or meta.get("list_id"),
                       tab.get("list_name") or meta.get("list_name"), tab.get("tab_name") or "",
                       g.get("rank"), g["goods_id"], g.get("title"),
                       g.get("image"), g.get("price"), g.get("sales_text"),
                       g.get("sales_tip"), g.get("hot_score"), g.get("score_text"),
                       g.get("tag_text"), g.get("brand"), g.get("group_text"),
                       g.get("day_orders"), g.get("order_window"),
                       g.get("tags"), ts))

    n = 0
    for t in tabs:
        key = f"{url}|{t['tab_name'] or '?'}"
        status = "ok" if t["goods"] else "empty"
        pid2 = put_page(key, t, status, len(t["goods"]), "" if t["goods"] else "该 tab 无商品")
        put_items(pid2, t, t["goods"])
        n += len(t["goods"])
    if fallback:
        key = f"{url}|fallback"
        pid2 = put_page(key, None, "ok", len(fallback), "fallback-parser")
        put_items(pid2, {}, fallback)
        n += len(fallback)
    if not tabs and not fallback:
        pid2 = put_page(url, None, "empty", 0, "未解析到榜单数据")
    c.commit()
    return n


def canary_ok(attempts: int = 2, settle: float = 12.0) -> tuple[bool, str]:
    """用一条已知有效的榜单做金丝雀：若它也返回「已下架」或验证码，说明是软封而不是真下架。

    必须给足加载时间并重试一次——慢网会把「还在加载」误判成「已下架」，
    进而把整个采集器错误地拉去冷却（2026-10-29 09:11 踩过）。
    """
    if not _alive():
        return False, "chrome-down"
    last = "unknown"
    for _ in range(attempts):
        tid, tab = new_tab()
        try:
            tab.cmd("Page.navigate", url=CANARY_URL)
            time.sleep(settle)
            txt = (tab.eval("document.body.innerText") or "")
            if "热卖指数" in txt or "畅销榜" in txt:
                return True, "ok"
            r = page_risk(tab)
            if r:
                last = f"risk:{r}"
            elif any(w in txt for w in DEAD_WORDS):
                last = "canary-dead"
            else:
                last = "canary-empty"
        except Exception as e:
            last = f"error:{type(e).__name__}"
        finally:
            tab.close()
            try:
                requests.get(f"http://127.0.0.1:{PORT}/json/close/{tid}", timeout=5)
            except Exception:
                pass
        time.sleep(5)
    return False, last


def crawl_one(url: str, meta: dict | None = None) -> dict:
    meta = meta or {}
    c = db()
    lim = Limiter(c)
    reason = lim.blocked_reason()
    if reason:
        c.close()
        return {"status": "blocked", "note": reason}
    if not _alive():
        launch(headless=True)
    if not logged_in():
        c.close()
        return {"status": "login", "note": "没有登录态，先跑 pdd_login.bat"}

    lim.wait()
    tid, tab = new_tab()
    try:
        tab.cmd("Page.navigate", url=url)
        time.sleep(random.uniform(CFG["dwell_min"], CFG["dwell_max"]))
        blobs = collect_json(tab, CFG["nav_timeout"])
        for _ in range(random.randint(CFG["scroll_min"], CFG["scroll_max"])):
            tab.eval("window.scrollBy(0, window.innerHeight * 0.9); 1")
            time.sleep(random.uniform(0.8, 2.2))
        blobs += collect_json(tab, 6)
        st = grab_state(tab)
        if st:
            blobs.append(st)

        risk = page_risk(tab)
        if risk:
            lim.trip(risk)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            c.execute("""INSERT INTO page(url,list_id,list_name,status,items,fetched_at,note)
                         VALUES(?,?,?,?,?,?,?)
                         ON CONFLICT(url) DO UPDATE SET status=excluded.status,
                             fetched_at=excluded.fetched_at, note=excluded.note""",
                      (url, meta.get("list_id"), meta.get("list_name"), "risk", 0, ts, risk))
            c.commit()
            os.makedirs(SHOTS, exist_ok=True)
            name = f"pdd_risk_{int(time.time())}"
            tab.shot(os.path.join(SHOTS, name + ".png"))
            return {"status": "risk", "note": risk}

        dead, why = page_dead(tab, blobs)
        if dead:
            # 先别急着判死刑：金丝雀也挂 = 软封
            okc, cinfo = canary_ok()
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            if not okc:
                lim.trip(f"疑似软封({why} / canary={cinfo})")
                c.execute("""INSERT INTO page(url,list_id,list_name,status,items,fetched_at,note)
                             VALUES(?,?,?,?,?,?,?)
                             ON CONFLICT(url) DO UPDATE SET status=excluded.status,
                                 fetched_at=excluded.fetched_at, note=excluded.note""",
                          (url, meta.get("list_id"), meta.get("list_name"), "throttled", 0, ts,
                           f"{why}; canary={cinfo}"))
                c.commit()
                return {"status": "throttled", "note": f"软封信号：{why} / canary={cinfo}"}

            c.execute("""INSERT INTO page(url,list_id,list_name,status,items,fetched_at,note)
                         VALUES(?,?,?,?,?,?,?)
                         ON CONFLICT(url) DO UPDATE SET status=excluded.status,
                             fetched_at=excluded.fetched_at, note=excluded.note""",
                      (url, meta.get("list_id"), meta.get("list_name"), "dead", 0, ts, why))
            if meta.get("list_id"):
                c.execute("""INSERT INTO dead_list(list_id,list_name,reason,at) VALUES(?,?,?,?)
                             ON CONFLICT(list_id) DO UPDATE SET reason=excluded.reason, at=excluded.at""",
                          (str(meta["list_id"]), meta.get("list_name"), why, ts))
            c.commit()
            return {"status": "dead", "note": f"{why}（金丝雀正常，判真下架）"}

        tabs, fallback = parse_rank_payload(blobs)
        n = store_rows(c, 0, url, meta, tabs, fallback)

        os.makedirs(RAW, exist_ok=True)
        keep = [{"len": b["len"]} for b in blobs]
        with open(os.path.join(RAW, f"page_{int(time.time())}.json"), "w", encoding="utf-8") as f:
            json.dump({"url": url, "meta": meta, "blobs": keep,
                       "tabs": [{k: t[k] for k in ("tab_name", "rule", "list_id",
                                                   "rule_sn", "list_type", "list_name")}
                                | {"goods": len(t["goods"])} for t in tabs]},
                      f, ensure_ascii=False, indent=1)

        title = tabs[0]["list_name"] if tabs else meta.get("list_name", "")
        kinds = "/".join(t["tab_name"] for t in tabs) or "-"
        return {"status": "ok" if n else "empty", "items": n, "list": title, "tabs": kinds,
                "note": "" if n else "没有解析到商品"}
    finally:
        tab.close()
        try:
            requests.get(f"http://127.0.0.1:{PORT}/json/close/{tid}", timeout=5)
        except Exception:
            pass
        c.close()


def dead_ids() -> set[str]:
    if not os.path.exists(DB):
        return set()
    c = db()
    ids = {str(r["list_id"]) for r in c.execute("SELECT list_id FROM dead_list")}
    c.close()
    return ids


def queue_from_picker(limit: int, cat=None, keyword=None) -> list[dict]:
    if not os.path.exists(PICKER_DB):
        return []
    c = sqlite3.connect(PICKER_DB)
    c.row_factory = sqlite3.Row
    sql = "SELECT r.tab_id, r.tab_name, r.superior_tab_id, r.list_sn FROM ranking r"
    args: list = []
    if cat:
        sql += " WHERE r.superior_tab_id=?"
        args.append(int(cat))
    elif keyword:
        sql += " WHERE r.tab_name LIKE ?"
        args.append(f"%{keyword}%")
    sql += " ORDER BY r.tab_id LIMIT ?"
    args.append(limit * 3)
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    c.close()
    dead = dead_ids()
    rows = [r for r in rows if str(r["tab_id"]) not in dead
            and not _is_activity(r["tab_id"])][:limit]
    return [{"url": build_url(str(r["tab_id"]), r["list_sn"]),
             "list_id": str(r["tab_id"]), "list_name": r["tab_name"],
             "cat": str(r["superior_tab_id"] or "")} for r in rows]


# 「实时」「年度」这类是全站综合榜，不是细分类目，分析时要排除
MIXED_PAT = ("实时", "年度", "总榜", "热卖榜", "今日")
# 一人电商做白牌，品牌榜直接跳过（品牌有授权与控价，做了也是给品牌方打工）
SKIP_NAME_PAT = ("品牌", "旗舰", "官方", "专营", "专卖", "百亿补贴")
# 3283547… 这类 19 位 id 是活动榜单系列，PDD 侧基本全部下架（实测），直接跳过省配额
ACTIVITY_PREFIX = ("3283",)


# 一人电商做小件、代发、能拍内容：大件/服装/生鲜/需安装的全部不抓（省配额）
EXEC_BAD = ("大件", "家具", "沙发", "床垫", "衣柜", "鞋柜", "餐边柜", "斗柜", "餐桌", "书桌",
            "办公桌", "书架", "货架", "衣帽架", "床架", "双人床", "上下床",
            "吊灯", "吸顶灯", "壁灯", "落地灯", "筒灯", "灯带", "窗帘", "地毯",
            "外套", "T恤", "卫衣", "裤", "裙", "文胸", "内衣", "衬衫", "马甲", "羽绒",
            "服", "桌布", "盖布", "沙发垫", "墙贴",
            "鞋", "靴", "拖鞋", "袜",
            "水果", "蔬菜", "生鲜", "肉类", "零食", "熟食", "冷冻", "饮料",
            "电动车", "摩托", "轮胎", "电池", "发动机",
            "汽车", "车载", "车用", "座套", "脚垫", "雨刮")


def _skip_name(name: str) -> bool:
    n = name or ""
    return (any(p in n for p in MIXED_PAT)
            or any(p in n for p in SKIP_NAME_PAT)
            or any(p in n for p in EXEC_BAD))


def _is_activity(list_id: str) -> bool:
    return str(list_id or "").startswith(ACTIVITY_PREFIX)


def queue_sample(n: int, seed: int | None = None, exclude_mixed: bool = True,
                 keywords: list[str] | None = None) -> list[dict]:
    """从榜单库里抽样。

    keywords 给定时优先抽「类目名命中关键词」的榜单（用于定向铺类目）；
    已下架的榜单永久跳过，不再浪费配额。
    """
    if not os.path.exists(PICKER_DB):
        return []
    c = sqlite3.connect(PICKER_DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT tab_id, tab_name, superior_tab_id, list_sn FROM ranking").fetchall()]
    c.close()
    if exclude_mixed:
        rows = [r for r in rows if not _skip_name(r["tab_name"])]
    dead = dead_ids()
    done = done_ids(CFG.get("revisit_days", 1))
    rows = [r for r in rows
            if str(r["tab_id"]) not in dead
            and str(r["tab_id"]) not in done
            and not _is_activity(r["tab_id"])]
    rnd = random.Random(seed) if seed is not None else random.Random()
    if keywords:
        kw = [k for k in keywords if k]
        hit = [r for r in rows if any(k in (r["tab_name"] or "") for k in kw)]
        rnd.shuffle(hit)
        picked = hit[:n]
    else:
        rnd.shuffle(rows)
        picked = rows[:n]
    return [{"url": build_url(str(r["tab_id"]), r["list_sn"]),
             "list_id": str(r["tab_id"]), "list_name": r["tab_name"],
             "cat": str(r["superior_tab_id"] or "")} for r in picked]


def done_ids(days: int) -> set[str]:
    """最近 N 天内成功抓过的榜单 id；抽样时直接排除，避免整批跳过。"""
    if not os.path.exists(DB):
        return set()
    c = db()
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
    ids = {str(r["list_id"]) for r in c.execute(
        "SELECT DISTINCT list_id FROM page WHERE fetched_at > ? AND status='ok' AND list_id IS NOT NULL",
        (since,))}
    c.close()
    return ids


def already_done(url: str, days: int) -> bool:
    if not os.path.exists(DB):
        return False
    c = db()
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
    r = c.execute("SELECT COUNT(*) n FROM page WHERE url LIKE ? AND fetched_at > ? AND status='ok'",
                  (url + "%", since)).fetchone()
    c.close()
    return bool(r["n"])


def probe(url: str) -> int:
    if not _alive():
        launch(headless=True)
    if not logged_in():
        print("✗ 没有登录态，先扫码")
        return 2
    tid, tab = new_tab()
    try:
        tab.cmd("Page.navigate", url=url)
        time.sleep(8)
        blobs = collect_json(tab, 20)
        for _ in range(3):
            tab.eval("window.scrollBy(0, window.innerHeight); 1")
            time.sleep(1.5)
        blobs += collect_json(tab, 8)
        st = grab_state(tab)
        if st:
            blobs.append(st)

        os.makedirs(RAW, exist_ok=True)
        os.makedirs(SHOTS, exist_ok=True)
        stamp = int(time.time())
        with open(os.path.join(RAW, f"probe_{stamp}.json"), "w", encoding="utf-8") as f:
            json.dump(blobs, f, ensure_ascii=False, indent=1)
        open(os.path.join(RAW, f"probe_{stamp}.html"), "w", encoding="utf-8").write(
            tab.eval("document.documentElement.outerHTML") or "")
        tab.shot(os.path.join(SHOTS, f"probe_{stamp}.png"))

        tabs, fallback = parse_rank_payload(blobs)
        print(json.dumps({
            "url": tab.eval("location.href"),
            "title": tab.eval("document.title"),
            "risk": page_risk(tab),
            "blobs": [b["len"] for b in blobs],
            "tabs": [{"tab": t["tab_name"], "rule": t["rule"], "list": t["list_name"],
                      "sn": t["rule_sn"], "type": t["list_type"], "goods": len(t["goods"])}
                     for t in tabs],
            "fallback_goods": len(fallback),
            "sample": tabs[0]["goods"][:5] if tabs else fallback[:5],
            "text_head": (tab.eval("document.body.innerText") or "")[:600],
        }, ensure_ascii=False, indent=2)[:7000])
        return 0
    finally:
        tab.close()


def replay(path: str) -> int:
    blobs = json.load(open(path, encoding="utf-8"))
    tabs, fallback = parse_rank_payload(blobs)
    print(json.dumps({"tabs": [{"tab": t["tab_name"], "list": t["list_name"],
                                "rule": t["rule"], "goods": len(t["goods"])} for t in tabs],
                      "fallback": len(fallback)}, ensure_ascii=False, indent=2))
    for t in tabs:
        print(f"\n--- {t['list_name']} / {t['tab_name']} / {t['rule']} ---")
        for g in t["goods"][:30]:
            print(f"{g['rank']:>3}. [{g['hot_score']}] ¥{g['price']:<8} {g['sales_tip']:<16} "
                  f"{g['tag_text']:<12} {g['title'][:34]}")
    return 0


def run(limit: int, cat=None, keyword=None, sample: int | None = None,
        wait: bool = True, keywords: str | None = None, bands: bool = False,
        target_lo: int = 100, target_hi: int = 100) -> int:
    kws = [k.strip() for k in (keywords or "").split(",") if k.strip()]
    if bands:
        items = queue_bands(limit, target_lo, target_hi)
    elif sample:
        items = queue_sample(sample, keywords=kws or None)
    else:
        items = queue_from_picker(limit, cat, keyword)
    if not items:
        print("队列为空：先跑 gouvip-picker 的 industry / rsearch 灌榜单")
        return 1
    todo = [x for x in items if not already_done(x["url"], CFG["revisit_days"])]
    print(f"队列 {len(items)} 条，跳过 {len(items)-len(todo)} 条（{CFG['revisit_days']} 天内抓过）",
          flush=True)
    ok = dead = 0
    for n, it in enumerate(todo, 1):
        try:
            r = crawl_one(it["url"], it)
            if r.get("status") == "blocked" and wait:
                print(f"  配额/冷却限制：{r.get('note')} → 等 5 分钟", flush=True)
                time.sleep(300)
                r = crawl_one(it["url"], it)
        except Exception as e:                     # 单页异常不拖垮整批
            r = {"status": "error", "note": f"{type(e).__name__}: {e}"}
            print(f"[{n}/{len(todo)}] {it['list_name']:<16} → 异常 {r['note'][:80]}", flush=True)
            _browser_close()                       # Chrome 可能已经挂死，关掉重来
            continue
        print(f"[{n}/{len(todo)}] {it['list_name']:<16} → {str(r.get('status')):<8}"
              f"{r.get('items', '')}  {r.get('tabs', '')} {r.get('note', '')}", flush=True)
        if r.get("status") == "ok":
            ok += 1
            dead = 0
        elif r.get("status") == "dead":
            dead += 1
            if dead >= CFG["dead_burst"]:
                print(f"连续 {dead} 个已下架 → 按软封处理，停止本轮", flush=True)
                break
        elif r.get("status") in ("risk", "login", "throttled"):
            print(f"遇到 {r.get('status')} → 停止本轮，冷却已写入", flush=True)
            break
    print(f"\n本轮完成：{ok} 页有效，{dead} 页已下架")
    return 0


def status() -> int:
    if not os.path.exists(DB):
        print("还没有采集数据")
        return 0
    c = db()
    print("页面状态：", {r["status"]: r["n"] for r in
                    c.execute("SELECT status, COUNT(*) n FROM page GROUP BY status")})
    print("商品总数：", c.execute("SELECT COUNT(*) n FROM item").fetchone()["n"])
    dl = c.execute("SELECT COUNT(*) n FROM dead_list").fetchone()["n"]
    print(f"已下架榜单（永久跳过）：{dl}")
    print("按榜单类型：", {r["tab_kind"]: r["n"] for r in
                     c.execute("SELECT tab_kind, COUNT(*) n FROM item GROUP BY tab_kind")})
    cd = st_get(c, "cooldown_until", 0) or 0
    print("冷却：" + (f"剩余 {int((cd-time.time())/60)} 分钟" if cd > time.time() else "无"))
    today = time.strftime("%Y-%m-%d 00:00:00")
    n_hour = c.execute("SELECT COUNT(*) n FROM page WHERE fetched_at > ?",
                       (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600)),)
                       ).fetchone()["n"]
    n_day = c.execute("SELECT COUNT(*) n FROM page WHERE fetched_at > ?", (today,)).fetchone()["n"]
    print(f"配额：近1小时 {n_hour}/{CFG['per_hour']} 页，今日 {n_day}/{CFG['per_day']} 页")
    print("最近 5 页：")
    for r in c.execute("SELECT fetched_at,list_name,tab_kind,status,items FROM page "
                       "ORDER BY id DESC LIMIT 5"):
        print(f"  {r['fetched_at']}  {r['list_name']}  {r['tab_kind']}  {r['status']}  {r['items']}")
    print("热卖指数 Top5：")
    for r in c.execute("SELECT list_name,title,hot_score,price,sales_tip FROM item "
                       "ORDER BY hot_score DESC LIMIT 5"):
        print(f"  {r['hot_score']}  ¥{r['price']}  {r['sales_tip']}  {r['title'][:30]}")
    c.close()
    return 0


BAND_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*元")


def queue_bands(limit: int, lo: int = 100, hi: int = 100) -> list[dict]:
    """优先抓「榜单名自带价格段且覆盖目标客单」的榜单。

    拼多多的榜单命名里有价格分段（如「50-100元不粘锅炒锅」「100-300元炖锅」），
    这是平台自己给出的「该品类在这个价位有量」的信号，比任何推断都可靠。
    """
    if not os.path.exists(PICKER_DB):
        return []
    c = sqlite3.connect(PICKER_DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute(
        "SELECT tab_id, tab_name, superior_tab_id, list_sn FROM ranking").fetchall()]
    c.close()
    dead = dead_ids()
    picked = []
    for r in rows:
        name = r["tab_name"] or ""
        m = BAND_RE.search(name)
        if not m or str(r["tab_id"]) in dead or _is_activity(r["tab_id"]):
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if not (a <= lo <= b or a <= hi <= b or (a <= lo and b >= hi)):
            continue
        picked.append({**r, "band": (a, b)})
    # 50-100 优先（100 元能落在区间上沿，更容易做），其次 100-300
    picked.sort(key=lambda x: (0 if x["band"] == (50, 100) else 1, x["band"][0], x["tab_name"]))
    return [{"url": build_url(str(r["tab_id"]), r["list_sn"]),
             "list_id": str(r["tab_id"]), "list_name": r["tab_name"],
             "cat": str(r["superior_tab_id"] or "")} for r in picked[:limit]]


def main() -> int:
    a = sys.argv[1:] or ["status"]
    cmd = a[0]
    if cmd == "login":
        if not _alive():
            launch(headless=False, url=LOGIN_URL)
        print("窗口已开，请扫码；检测到 PDDAccessToken 即成功")
        t0 = time.time()
        while time.time() - t0 < 600:
            if logged_in():
                print("✓ 登录态已保存", PROFILE)
                return 0
            time.sleep(3)
        print("✗ 超时")
        return 1
    if cmd == "check":
        print("chrome:", _alive(), "| logged-in:", logged_in() if _alive() else "-",
              "| profile:", PROFILE)
        return 0
    if cmd == "probe" and len(a) >= 2:
        return probe(a[1])
    if cmd == "replay" and len(a) >= 2:
        return replay(a[1])
    if cmd == "run":
        limit, cat, kw, sample, wait, kws = 20, None, None, None, True, None
        bands, tlo, thi = False, 100, 100
        i = 1
        while i < len(a):
            if a[i] == "--limit":
                limit = int(a[i + 1]); i += 2
            elif a[i] == "--cat":
                cat = a[i + 1]; i += 2
            elif a[i] == "--keyword":
                kw = a[i + 1]; i += 2
            elif a[i] == "--keywords":
                kws = a[i + 1]; i += 2
            elif a[i] == "--sample":
                sample = int(a[i + 1]); i += 2
            elif a[i] == "--bands":
                bands = True; i += 1
            elif a[i] == "--target":
                parts = a[i + 1].split("-")
                tlo = int(parts[0]); thi = int(parts[-1]); i += 2
            elif a[i] == "--no-wait":
                wait = False; i += 1
            else:
                i += 1
        return run(limit, cat, kw, sample, wait, kws, bands, tlo, thi)
    if cmd == "status":
        return status()
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
