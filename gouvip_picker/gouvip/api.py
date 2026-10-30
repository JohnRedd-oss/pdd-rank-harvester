"""狗宝助手 / gouvip 网页版接口客户端。

只依赖 requests。核心事实（均由实测确认）：
  - 后端:      http://web2.qianxingwl.com
  - 入口:      /QingYuTool/tool_api.php?type=<N>
  - 登录:      POST type=2  {userName, password, type:2}  -> token(JWT)
  - 鉴权:      Header Authorization: <token>  (不要加 Bearer) + query 参数 uid/level
  - 签名:      sign_true = md5(str(int(tab_id) * 1217 + time_ms)), time 为毫秒时间戳
"""
from __future__ import annotations

import hashlib
import json
import os
import time as _time
from typing import Any, Iterable

import requests

BASE = "http://web2.qianxingwl.com"
API = "/QingYuTool/tool_api.php"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "http://web.gouvip.com/",
    "Origin": "http://web.gouvip.com",
    "Accept": "application/json, text/plain, */*",
}

# 接口编号（来自前端 chunk 反解）
T_LOGIN = 2
T_GOODS_CATS = 31      # 热销商品页的商品分类下拉
T_INDUSTRY_CATS = 87   # 行业榜单：一级类目
T_INDUSTRY_SUBS = 88   # 行业榜单：子榜单目录（左侧）
T_INDUSTRY_LIST = 89   # 行业榜单：榜单条目 + 榜单类型
T_INDUSTRY_FILTER = 90 # 行业榜单：按榜单类型筛选
T_RANK_SEARCH = 44     # 行业榜单：关键词搜索
T_HOT_TODAY = 142      # 今日热销商品
T_HOT_WEEK = 78        # 近 7 日热销商品


class ApiError(RuntimeError):
    pass


def sign(tab_id: int | str, time_ms: int | None = None) -> tuple[str, int]:
    """返回 (sign_true, time_ms)。"""
    if time_ms is None:
        time_ms = int(_time.time() * 1000)
    raw = str(int(float(tab_id)) * 1217 + time_ms)
    return hashlib.md5(raw.encode()).hexdigest(), time_ms


class GouVip:
    """一次性长驻客户端；token 失效会自动重登。"""

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        state_file: str | None = None,
        timeout: int = 30,
        delay: float = 0.35,
    ):
        self.username = username or os.environ.get("GOUVIP_USER", "")
        self.password = password or os.environ.get("GOUVIP_PASS", "")
        self.timeout = timeout
        self.delay = delay
        self.state_file = state_file
        self.token: str | None = None
        self.uid: int = 0
        self.level: int = 0
        self.expire_time: str | None = None
        self.last_calls: list[str] = []
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self._load_state()

    # ---------------- token 持久化 ----------------
    def _load_state(self) -> None:
        if not self.state_file or not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.token = d.get("token") or None
            self.uid = int(d.get("uid") or 0)
            self.level = int(d.get("level") or 0)
            self.expire_time = d.get("expire_time")
        except Exception:
            pass

    def _save_state(self) -> None:
        if not self.state_file:
            return
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "token": self.token,
                    "uid": self.uid,
                    "level": self.level,
                    "expire_time": self.expire_time,
                    "saved_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    # ---------------- 请求核心 ----------------
    def _params(self, extra: dict[str, Any] | None) -> dict[str, Any]:
        p: dict[str, Any] = {"uid": self.uid, "level": self.level}
        if extra:
            for k, v in extra.items():
                if v is not None:
                    p[k] = v
        return p

    def _raw(self, type_id: int, params: dict[str, Any] | None = None,
             body: dict[str, Any] | None = None) -> requests.Response:
        url = f"{BASE}{API}"
        h = {}
        if self.token:
            h["Authorization"] = self.token  # 实测：不带 Bearer 前缀
        q = {"type": type_id}
        q.update(self._params(params))
        if self.delay:
            _time.sleep(self.delay)
        self.last_calls.append(f"type={type_id} {params or body or ''}")
        if body is not None:
            return self.s.post(url, params=q, json=body, headers=h, timeout=self.timeout)
        return self.s.get(url, params=q, headers=h, timeout=self.timeout)

    def call(self, type_id: int, params: dict[str, Any] | None = None,
             body: dict[str, Any] | None = None, retries: int = 3) -> Any:
        last = ""
        for attempt in range(retries):
            try:
                r = self._raw(type_id, params, body)
                text = r.text.strip()
                if not text:
                    last = f"empty response (http {r.status_code})"
                elif text in ("error", "error!"):
                    last = f"server returned '{text}' (type={type_id})"
                else:
                    data = json.loads(text)
                    code = data.get("code")
                    if code in (1000, 200) or "pageList" in data:
                        return data
                    if code == -100:  # 身份认证失败 -> 重登一次
                        if attempt == 0:
                            self.login()
                            continue
                        raise ApiError(f"身份认证失败：账号可能已过期或 token 失效（{data.get('message')}）")
                    last = f"code={code} message={data.get('message')}"
            except (requests.RequestException, json.JSONDecodeError) as e:
                last = f"{type(e).__name__}: {e}"
            _time.sleep(1.2 * (attempt + 1))
        raise ApiError(f"type={type_id} 调用失败：{last}")

    # ---------------- 登录 ----------------
    def login(self) -> dict:
        if not self.username or not self.password:
            raise ApiError("缺少账号密码：请在 config.json 或环境变量 GOUVIP_USER/GOUVIP_PASS 中配置")
        r = self.s.post(
            f"{BASE}{API}",
            params={"type": T_LOGIN},
            json={"userName": self.username, "password": self.password, "type": 2},
            timeout=self.timeout,
        )
        try:
            d = r.json()
        except Exception as e:
            raise ApiError(f"登录返回非 JSON（http {r.status_code}）：{r.text[:200]}") from e
        if d.get("code") != 1000:
            raise ApiError(f"登录失败：{d.get('message')}")
        self.token = d.get("token")
        self.uid = int(d.get("user_id") or 0)
        self.level = int(d.get("level") or 0)
        self.expire_time = d.get("expire_time")
        self._save_state()
        return d

    def ensure_login(self) -> None:
        if not self.token:
            self.login()

    # ---------------- 业务接口 ----------------
    def goods_cats(self) -> list[dict]:
        """热销商品页的商品分类。"""
        return self.call(T_GOODS_CATS).get("data") or []

    def industry_cats(self) -> list[dict]:
        """行业榜单一级类目：[{tab_id, tab_name}]（含少量空名/重复项，调用方自行去重）。"""
        rows = self.call(T_INDUSTRY_CATS).get("data") or []
        seen, out = set(), []
        for r in rows:
            tid, name = r.get("tab_id"), (r.get("tab_name") or "").strip()
            if not name or tid in seen:
                continue
            seen.add(tid)
            out.append({"tab_id": tid, "tab_name": name})
        return out

    def industry_subs(self, tab_id1: int | str) -> list[dict]:
        """某个一级类目下的子榜单目录。"""
        sg, t = sign(tab_id1)
        d = self.call(T_INDUSTRY_SUBS, {"tab_id1": tab_id1, "sign_true": sg, "time": t})
        return d.get("data") or []

    def industry_rankings(self, tab_id2: int | str) -> dict:
        """榜单条目 + 榜单类型。

        返回 {"items": [{tab_id, tab_name, superior_tab_id, list_sn}], "types": [str, ...]}
        每个 item 对应拼多多官方榜单页：
          https://mobile.yangkeduo.com/sjs_cat_rank_list.html?list_id=<tab_id>&list_sn=<list_sn>
        """
        sg, t = sign(tab_id2)
        d = self.call(T_INDUSTRY_LIST, {"tab_id2": tab_id2, "sign_true": sg, "time": t})
        items = d.get("data") or []
        types = [x.get("list_name") for x in (d.get("list_type") or []) if x.get("list_name")]
        return {"items": items, "types": types}

    def industry_filter(self, tab_id2: int | str, list_name: str) -> list[dict]:
        """按榜单类型（好评/畅销/降价）筛选。接口不稳定，失败时由调用方回退。"""
        sg, t = sign(tab_id2)
        try:
            d = self.call(T_INDUSTRY_FILTER,
                          {"tab_id2": tab_id2, "list_name": list_name, "sign_true": sg, "time": t},
                          retries=1)
            return d.get("data") or []
        except ApiError:
            return []

    def search_rankings(self, keyword: str) -> list[dict]:
        """跨类目按关键词搜榜单。"""
        d = self.call(T_RANK_SEARCH, {"search": keyword})
        return d.get("data") or []

    def hot_goods(
        self,
        date: str,
        page: int = 1,
        size: int = 100,
        search: str = "",
        sort: int = 0,
        date_span: int = 0,
        cat: int | str = 0,
        source: str = "today",
    ) -> dict:
        """热销商品池。source: today(当日) / week(近7日)。

        返回 {"total": int, "pageList": [...]}
        """
        body = {
            "uid": self.uid,
            "time": date,
            "page": page,
            "size": size,
            "search": search,
            "sortSelect": sort,
            "dateSelect": date_span,
            "cat": cat,
        }
        tid = T_HOT_TODAY if source == "today" else T_HOT_WEEK
        d = self.call(tid, body=body)
        return {"total": int(d.get("total") or 0), "pageList": d.get("pageList") or []}

    def pdd_rank_url(self, item: dict) -> str:
        return (
            "https://mobile.yangkeduo.com/sjs_cat_rank_list.html"
            f"?list_id={item.get('tab_id')}&list_sn={item.get('list_sn')}&scene_id=goods_detail"
        )


def chunks(seq: Iterable, n: int):
    buf = []
    for x in seq:
        buf.append(x)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf
