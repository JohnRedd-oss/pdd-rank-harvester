"""本地 Web 配置界面（stdlib http.server，无额外依赖）。

启动：python cli.py ui  →  http://127.0.0.1:8765
功能：权重/阈值可视化调参 → 采集商品池 → 打分选品 → 导出 Excel；榜单目录浏览。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import api as A
from . import pipeline as P
from .scoring import DEFAULT_CONFIG, merge_config
from .store import Store

CFG_KEYS = ("account", "paths", "weights", "price", "filters", "rank", "collect")

PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>行业榜单选品控制台</title>
<style>
:root{--bg:#f5f6fa;--card:#fff;--ink:#1f2540;--sub:#6b7280;--brand:#283579;--ok:#0f9d58;--warn:#e08b00}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,"Microsoft YaHei",sans-serif}
header{background:var(--brand);color:#fff;padding:14px 22px;display:flex;align-items:center;gap:14px}
header h1{font-size:16px;margin:0;font-weight:600}
header .meta{font-size:12px;opacity:.85;margin-left:auto}
.wrap{display:grid;grid-template-columns:330px 1fr;gap:16px;padding:16px;align-items:start}
.card{background:var(--card);border-radius:10px;padding:16px;box-shadow:0 1px 3px rgba(16,24,40,.08)}
.card h2{font-size:13px;margin:0 0 12px;color:var(--brand);letter-spacing:.5px}
label{display:flex;justify-content:space-between;font-size:12px;color:var(--sub);margin:10px 0 2px}
label b{color:var(--ink)}
input[type=range]{width:100%}
input[type=number],input[type=text],select{width:100%;padding:6px 8px;border:1px solid #dfe3ee;border-radius:6px;font-size:13px;background:#fff}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
button{border:0;border-radius:7px;padding:9px 14px;font-size:13px;cursor:pointer;background:var(--brand);color:#fff;font-weight:600}
button.ghost{background:#eef0f7;color:var(--brand)}
button:disabled{opacity:.5;cursor:not-allowed}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:7px 8px;text-align:left;border-bottom:1px solid #eef0f5;vertical-align:top}
th{color:var(--sub);font-weight:600;position:sticky;top:0;background:#fafbff;z-index:1}
tr:hover td{background:#f7f9ff}
.pill{display:inline-block;padding:1px 7px;border-radius:99px;background:#eef0f7;color:#4b5563;font-size:11px;margin-right:4px}
.pill.new{background:#e6f6ec;color:#0f9d58}.pill.up{background:#e8f1ff;color:#1565c0}
.pill.warn{background:#fff4e0;color:#b26a00}
.num{text-align:right;font-variant-numeric:tabular-nums}
#log{font:12px/1.5 Consolas,monospace;color:var(--sub);white-space:pre-wrap;max-height:110px;overflow:auto;margin-top:10px}
.bar{height:6px;border-radius:3px;background:#eef0f7;overflow:hidden}.bar>i{display:block;height:100%;background:linear-gradient(90deg,#283579,#5b7bd5)}
.banner{padding:8px 12px;border-radius:8px;font-size:12.5px;margin-bottom:12px}
.banner.warn{background:#fff4e0;color:#8a5300;border:1px solid #ffe0a8}
.banner.ok{background:#e9f8ef;color:#0b6b3a;border:1px solid #bfe9d0}
.tabs{display:flex;gap:6px;margin-bottom:10px}
.tabs button{background:#eef0f7;color:#4b5563;padding:6px 12px;font-weight:500}
.tabs button.on{background:var(--brand);color:#fff}
.scroll{max-height:calc(100vh - 320px);overflow:auto}
small.hint{color:#98a2b3;font-size:11.5px}
</style></head><body>
<header><h1>行业榜单选品控制台</h1><span class="meta" id="hdr"></span></header>
<div class="wrap">
  <div>
    <div class="card">
      <h2>选品规则（实时生效）</h2>
      <div id="weights"></div>
      <label>价格带下沿 <b id="pv-min"></b></label><input type="number" id="pmin" step="0.1">
      <label>价格带上沿 <b id="pv-max"></b></label><input type="number" id="pmax" step="0.1">
      <div class="row2" style="margin-top:10px">
        <div><label>甜点下沿</label><input type="number" id="psmin" step="0.1"></div>
        <div><label>甜点上沿</label><input type="number" id="psmax" step="0.1"></div>
      </div>
      <div class="row2" style="margin-top:10px">
        <div><label>最低销量</label><input type="number" id="minsales"></div>
        <div><label>最多选几款</label><input type="number" id="topn"></div>
      </div>
      <label style="margin-top:12px">排除词（逗号分隔）</label><input type="text" id="exkw">
      <div class="btns"><button class="ghost" onclick="saveCfg()">保存配置</button>
      <button class="ghost" onclick="resetCfg()">恢复默认</button></div>
    </div>
    <div class="card" style="margin-top:14px">
      <h2>采集商品池</h2>
      <label>关键词（逗号分隔，留空=全部热销）</label><input type="text" id="kw" placeholder="垃圾桶,厨房垃圾桶">
      <div class="row2" style="margin-top:10px">
        <div><label>页数</label><input type="number" id="pages" value="2"></div>
        <div><label>每页条数</label><input type="number" id="size" value="100"></div>
      </div>
      <div class="row2" style="margin-top:10px">
        <div><label>数据源</label><select id="source"><option value="today">今日热销</option><option value="week">近7日热销</option></select></div>
        <div><label>排序</label><select id="sort"><option value="1">按销量</option><option value="0">默认</option></select></div>
      </div>
      <div class="btns"><button onclick="sync()">开始采集</button></div>
      <div id="log"></div>
    </div>
  </div>
  <div>
    <div class="card">
      <div class="tabs">
        <button id="tab-pick" class="on" onclick="tab('pick')">选品清单</button>
        <button id="tab-rank" onclick="tab('rank')">行业榜单</button>
      </div>
      <div id="banner"></div>
      <div class="btns" style="margin:0 0 10px">
        <button onclick="pick()">按当前规则打分</button>
        <button class="ghost" onclick="exportXlsx()">导出 Excel</button>
        <span id="stat" class="pill"></span>
      </div>
      <div id="view-pick" class="scroll"><table id="tbl"><thead></thead><tbody></tbody></table></div>
      <div id="view-rank" class="scroll" style="display:none">
        <div class="row2" style="margin-bottom:8px">
          <input type="text" id="rkw" placeholder="按榜单名过滤，如 垃圾桶" oninput="loadRank()">
          <select id="rcat" onchange="loadRank()"></select>
        </div>
        <table id="tbl2"><thead><tr><th>榜单名</th><th>榜单ID</th><th>上级类目</th><th>首次出现</th><th>拼多多榜单</th></tr></thead><tbody></tbody></table>
      </div>
    </div>
  </div>
</div>
<script>
const W_KEYS=[["sales","销量规模"],["growth","销量增长"],["price","价格带"],["fresh","上架新鲜度"],["new","新进商品"]];
let CFG=null, LAST=null, CATS=[];
const $=id=>document.getElementById(id);
const fmt=n=>n==null?"":Number(n).toLocaleString();

async function api(path,opt){const r=await fetch(path,opt);if(!r.ok) throw new Error(await r.text());return r.json();}

function renderCfg(){
  $("weights").innerHTML=W_KEYS.map(([k,t])=>`<label>${t} <b id="wv-${k}">${CFG.weights[k]}</b></label>
    <input type="range" min="0" max="1" step="0.05" id="w-${k}" value="${CFG.weights[k]}" oninput="onW('${k}')">`).join("");
  $("pmin").value=CFG.price.min; $("pmax").value=CFG.price.max;
  $("psmin").value=CFG.price.sweet_min; $("psmax").value=CFG.price.sweet_max;
  $("minsales").value=CFG.filters.min_sales; $("topn").value=CFG.rank.top_n;
  $("exkw").value=(CFG.filters.exclude_keywords||[]).join(",");
  $("pv-min").textContent=CFG.price.min; $("pv-max").textContent=CFG.price.max;
}
function onW(k){$("wv-"+k).textContent=$("w-"+k).value;}
function collectCfg(){
  const w={}; W_KEYS.forEach(([k])=>w[k]=parseFloat($("w-"+k).value));
  return {weights:w,
    price:{min:+$("pmin").value,max:+$("pmax").value,sweet_min:+$("psmin").value,sweet_max:+$("psmax").value},
    filters:{min_sales:+$("minsales").value,exclude_keywords:$("exkw").value.split(/[,，]/).map(s=>s.trim()).filter(Boolean)},
    rank:{top_n:+$("topn").value}};
}
async function saveCfg(){
  const r=await api("api/config",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(collectCfg())});
  CFG=r.config; log("配置已保存");
}
async function resetCfg(){CFG=(await api("api/config/reset",{method:"POST"})).config;renderCfg();log("已恢复默认配置");}

function log(t){$("log").textContent=("> "+t+"\n"+$("log").textContent).slice(0,4000);}
function banner(html,cls){$("banner").innerHTML=html?`<div class="banner ${cls}">${html}</div>`:"";}

async function boot(){
  const s=await api("api/state");
  CFG=s.config; renderCfg(); CATS=s.cats||[];
  $("hdr").textContent=`账号 ${s.account} · level ${s.level} · 套餐到期 ${s.expire||"-"} · 库内榜单 ${s.stats.ranking} 条 / 商品 ${s.stats.goods} 款`;
  $("rcat").innerHTML=`<option value="">全部类目</option>`+CATS.map(c=>`<option value="${c.tab_id}">${c.tab_name}</option>`).join("");
  if(s.freshness.warning) banner(`⚠ 数据陈旧：最新商品 update_time=${s.freshness.data_update_time}，距今 ${s.freshness.stale_days} 天。账号套餐可能未生效，接口返回的是历史快照。`,"warn");
  else if(s.freshness.last_fetch) banner(`数据正常：商品更新时间 ${s.freshness.data_update_time}`,"ok");
  await pick();
}
async function sync(){
  const body={keywords:$("kw").value.split(/[,，]/).map(s=>s.trim()).filter(Boolean),
    pages:+$("pages").value,size:+$("size").value,source:$("source").value,sort:+$("sort").value,cat:0};
  log("采集中…");
  try{const r=await api("api/sync",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    log(`采集完成 run_id=${r.run_id} 入库 ${r.rows} 条（接口 total=${r.api_total}）`);await pick();}
  catch(e){log("采集失败："+e.message);}
}
async function pick(){
  try{
    const r=await api("api/pick",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({top:+$("topn").value})});
    LAST=r;
    $("stat").textContent=`候选 ${r.candidates} → 入选 ${r.rows.length}（过滤 ${r.dropped}）`;
    const th=`<tr><th>#</th><th>得分</th><th></th><th>商品</th><th class="num">价格</th><th class="num">销量</th><th class="num">增长</th><th>信号</th></tr>`;
    $("tbl").querySelector("thead").innerHTML=th;
    $("tbl").querySelector("tbody").innerHTML=r.rows.map((x,i)=>`<tr>
      <td>${i+1}</td>
      <td><div class="bar"><i style="width:${x.score}%"></i></div><small class="hint">${x.score}</small></td>
      <td><img src="${x.image_url||''}" style="width:38px;height:38px;object-fit:cover;border-radius:5px;background:#eee"></td>
      <td><a href="https://mobile.yangkeduo.com/goods.html?goods_id=${x.goods_id}" target="_blank">${x.goods_name}</a>
          <div><small class="hint">ID ${x.goods_id} · 上架 ${x.add_time||'-'} · 命中 ${x.hits}</small></div></td>
      <td class="num">${x.price??''}</td>
      <td class="num">${fmt(x.sales)}</td>
      <td class="num">${x.growth==null?'-':(x.growth*100).toFixed(0)+'%'}</td>
      <td>${(x.signals||[]).map(s=>`<span class="pill ${s.includes('新进')?'new':(s.includes('+')?'up':'')}">${s}</span>`).join('')}</td>
    </tr>`).join("");
  }catch(e){log("打分失败："+e.message);}
}
async function exportXlsx(){
  const r=await api("api/export",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({top:+$("topn").value})});
  log("已导出 "+r.path); window.open("download?f="+encodeURIComponent(r.file),"blank");
}
function tab(t){
  $("tab-pick").classList.toggle("on",t=="pick"); $("tab-rank").classList.toggle("on",t=="rank");
  $("view-pick").style.display=t=="pick"?"":"none"; $("view-rank").style.display=t=="rank"?"":"none";
  if(t=="rank") loadRank();
}
async function loadRank(){
  const q=new URLSearchParams({kw:$("rkw").value,cat:$("rcat").value,limit:"500"});
  const r=await api("api/rankings?"+q);
  $("tbl2").querySelector("tbody").innerHTML=r.rows.map(x=>`<tr>
    <td>${x.tab_name}</td><td>${x.tab_id}</td><td>${x.superior_tab_id}</td>
    <td><small class="hint">${x.first_seen||''}</small></td>
    <td><a href="https://mobile.yangkeduo.com/sjs_cat_rank_list.html?list_id=${x.tab_id}&list_sn=${x.list_sn}" target="_blank">打开榜单</a></td>
  </tr>`).join("");
}
boot();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    root = "."
    cfg_path = "config.json"
    server_version = "GouvipPicker/0.1"

    # ---------- helpers ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        pass

    # ---------- 配置 ----------
    def _cfg(self) -> dict:
        try:
            with open(os.path.join(self.root, self.cfg_path), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cfg(self, cfg: dict) -> None:
        p = os.path.join(self.root, self.cfg_path)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def _api_client(self, cfg: dict) -> A.GouVip:
        acct = cfg.get("account") or {}
        return A.GouVip(acct.get("username"), acct.get("password"),
                        state_file=os.path.join(self.root, "data", "token.json"))

    def _store(self, cfg: dict) -> Store:
        return Store(os.path.join(self.root, cfg.get("paths", {}).get("db", "data/picker.db")))

    def _rank_cfg(self, cfg: dict) -> dict:
        return merge_config(DEFAULT_CONFIG, {k: cfg[k] for k in ("weights", "price", "filters", "rank") if k in cfg})

    # ---------- routes ----------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if u.path == "/download":
                name = (q.get("f") or [""])[0]
                p = os.path.join(self.root, "out", os.path.basename(name))
                if not os.path.exists(p):
                    self._json({"error": "not found"}, 404)
                    return
                data = open(p, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type",
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(p)}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            cfg = self._cfg()
            if u.path == "/api/state":
                st = self._store(cfg)
                try:
                    cats = [dict(r) for r in st.db.execute(
                        "SELECT tab_id, tab_name FROM cat_top ORDER BY tab_id").fetchall()]
                    acct = cfg.get("account") or {}
                    tok = os.path.join(self.root, "data", "token.json")
                    lvl, exp = 0, "-"
                    if os.path.exists(tok):
                        d = json.load(open(tok, encoding="utf-8"))
                        lvl, exp = d.get("level", 0), d.get("expire_time") or "-"
                    self._json({
                        "config": {k: cfg.get(k) for k in CFG_KEYS if k in cfg},
                        "stats": st.stats(),
                        "freshness": st.freshness(),
                        "cats": cats,
                        "account": acct.get("username", ""),
                        "level": lvl,
                        "expire": exp,
                    })
                finally:
                    st.close()
                return

            if u.path == "/api/rankings":
                st = self._store(cfg)
                try:
                    kw = (q.get("kw") or [""])[0]
                    cat = (q.get("cat") or [""])[0]
                    limit = int((q.get("limit") or ["500"])[0])
                    self._json({"rows": st.rankings(int(cat) if cat else None, kw, limit)})
                finally:
                    st.close()
                return

            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            cfg = self._cfg()
            body = self._body()

            if u.path == "/api/config":
                for k in ("weights", "price", "filters", "rank"):
                    if k in body:
                        if isinstance(body[k], dict) and isinstance(cfg.get(k), dict):
                            cfg[k].update(body[k])
                        else:
                            cfg[k] = body[k]
                self._save_cfg(cfg)
                self._json({"config": cfg})
                return

            if u.path == "/api/config/reset":
                cfg.update(json.loads(json.dumps(DEFAULT_CONFIG)))
                self._save_cfg(cfg)
                self._json({"config": cfg})
                return

            if u.path == "/api/sync":
                gp = self._api_client(cfg)
                st = self._store(cfg)
                try:
                    gp.ensure_login()
                    col = cfg.get("collect") or {}
                    res = P.sync_goods(
                        gp, st,
                        body.get("keywords") or [""],
                        source=body.get("source") or col.get("source", "today"),
                        pages=int(body.get("pages") or col.get("pages", 2)),
                        size=int(body.get("size") or col.get("size", 100)),
                        sort=int(body.get("sort") or 0),
                        cat=body.get("cat") or 0,
                    )
                    self._json(res)
                finally:
                    st.close()
                return

            if u.path == "/api/pick":
                st = self._store(cfg)
                try:
                    rc = self._rank_cfg(cfg)
                    if body.get("top"):
                        rc["rank"]["top_n"] = int(body["top"])
                    res = P.pick(st, config=rc)
                    rows = res["passed"]
                    self._json({
                        "run_id": res["run_id"],
                        "candidates": res["candidates"],
                        "dropped": len(res["dropped"]),
                        "freshness": res["freshness"],
                        "rows": [{k: r.get(k) for k in
                                  ("goods_id", "goods_name", "image_url", "price", "sales",
                                   "growth", "add_time", "hits", "signals", "score")} for r in rows],
                    })
                finally:
                    st.close()
                return

            if u.path == "/api/export":
                st = self._store(cfg)
                try:
                    rc = self._rank_cfg(cfg)
                    if body.get("top"):
                        rc["rank"]["top_n"] = int(body["top"])
                    res = P.pick(st, config=rc)
                    out_dir = os.path.join(self.root, cfg.get("paths", {}).get("out", "out"))
                    name = f"选品_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
                    path = os.path.join(out_dir, name)
                    P.export_excel(path, res, rankings=st.rankings(keyword=(body.get("kw") or "")))
                    self._json({"path": path, "file": name})
                finally:
                    st.close()
                return

            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def serve(root: str, cfg_path: str = "config.json", port: int = 8765,
          open_browser: bool = True) -> int:
    Handler.root = root
    Handler.cfg_path = cfg_path
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"选品控制台: {url}  （Ctrl+C 退出）", flush=True)
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(serve(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           port=int(sys.argv[1]) if len(sys.argv) > 1 else 8765))
