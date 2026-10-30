# 狗宝助手 / gouvip 拆解记录（2026-10-28）

## 1. 架构
- 前端：http://web.gouvip.com ，Vue3 + Arco Design SPA（vite 打包，路由懒加载 chunk）
- 后端：`http://web2.qianxingwl.com`
- 接口：`/QingYuTool/tool_api.php?type=<N>`（GET/POST）+ `/api/user/menu`
- 桌面端：`<工具安装目录>\狗宝助手.exe`（.NET Framework 4.8 + SunnyUI + CefSharp + ClearScript），
  `toolA.exe`(156MB) 非 .NET（壳/内嵌浏览器），本地库 `DataBase\Report.db`(SQLite，店铺千川经营指标，与榜单无关)

## 2. 鉴权
- 登录：`POST /QingYuTool/tool_api.php?type=2`（密码前端 AES-ECB 加密，key=`prettygirl_user_name`）
- 返回 JWT，存 localStorage；请求头 `Authorization: <token>`（无 Bearer 前缀）
- axios baseURL 写死 `http://web2.qianxingwl.com`；额外 `getLevel()` 参数注入
- 未带 token 实测：返回 `error`
- 缓存中账号：138****6684 / 同号，套餐到期 2026-03-30，余额 0（`caches\ScanCode\Local Storage`）

## 3. 签名算法（已还原）
```js
sign_true = md5( String( parseInt(tab_id) * 1217 + time_ms ) )
time      = Date.now()   // 毫秒
```

## 4. 行业榜单数据链（重点）
模块 `/hot-selling/industryList`，chunk: `assets/index.8bfcfac3.js` + `assets/hot-selling.5cc17820.js`

| 步骤 | 接口 | 参数 | 返回 |
|---|---|---|---|
| 类目下拉 | GET type=87 | – | `data[] {tab_id, tab_name}` |
| 子榜单目录（左侧） | GET type=88 | tab_id1, time, sign_true | `data[] {tab_id, tab_name}` |
| 榜单内容/榜单类型 | GET type=89 | tab_id2, time, sign_true | `{data[], list_type[{list_name}]}` |
| 按榜单类型筛选 | GET type=90 | tab_id2, list_name, time, sign_true | `{data[]}` |
| 关键词搜索 | GET type=44 | search | `{data[]}` |

榜单条目点击后打开拼多多官方页：
`https://mobile.yangkeduo.com/sjs_cat_rank_list.html?list_id={tab_id}&list_sn={list_sn}&scene_id=goods_detail`

## 5. 商品级数据（选品可用）
- 今日热销 `/hot-selling/todayHot`：GET type=31（类目列表）；POST type=142
  payload `{uid, time, page, size, search, sortSelect, dateSelect, cat}` → `{code:1000, pageList[], total}`
- 近7日热销 `/hot-selling/aWeekHot`：POST type=78（同结构）
- 商品字段（前端消费的最小集）：`goods_id, goods_name/name, thumb_url/src, sales, price?, product`

## 6. 其余账号类接口
type=2 登录 / 8 改资料 / 10 邀请 / 11 奖励 / 12 提现 / 15 用户信息 / 45-48 店铺 / 61,63,64 分组 / 79 模板 / 91 备注 / 98-104 子账号

## 7. 风险
- 接口有鉴权 + 会员到期控制，绕过/共享 token 违反服务条款；建议用自有账号、低频调用
- 榜单目录本身不含商品明细，商品级数据需 PDD 榜单页（有风控/字体混淆）或本工具热销接口（字段有限）

## 8. 实盘验证结果（2026-10-28，用缓存账号 138****6684 实测）
- 登录 `POST type=2 {userName,password,type:2}` → code=1000，返回 token（JWT）
  - 账号状态：`level=0`、`expire_time=2026-03-30 15:49:52`、`balance=0.00`、`account_status=-1` → **套餐已过期**
- 鉴权必须 **同时**带 `Authorization: <token>`（无 Bearer）和 query `uid/level`；只带 Authorization 会返回 `{"code":-100,"message":"身份认证失败"}`
- 行业榜单接口全部可用：
  - `type=87` → 20 个一级类目（水果生鲜13、食品保健1、医药健康3279、家居百货15、饰品配件24149…）
  - `type=88&tab_id1=13` → 13 个子榜（精选/柑桔橙柚/热带水果/…）
  - `type=89&tab_id2=13` → 547 条榜单 + `list_type=[全部类型,好评,畅销,降价]`
  - `type=44&search=垃圾桶` → 72 条榜单（跨类目）
  - 水果生鲜全量 1444 条、家居百貨全量 15953 条（去重后库内 13k+）
  - 榜单条目字段：`tab_id, tab_name, superior_tab_id, list_sn`
- 商品池接口可用但**数据冻结**：
  - `POST type=142`（今日热销）/ `type=78`（近7日），body `{uid,time,page,size,search,sortSelect,dateSelect,cat}`
  - 字段：`goods_id, goods_name, image_url, mini_price/max_price, mini_sales/max_sales, average_sales2, cat_id1/2/3, add_time, update_time`
  - `total` 恒为 167056；换 `time`（10-27/10-01/06-15/04-25）结果完全不变；所有行 `update_time=2026-04-29`
  - 结论：**过期账号拿到的是 2026-04-29 冻结快照**，非实时
- 关键词搜索供给量差异大：`垃圾桶`→74 条、`收纳盒`→916 条、`厨房垃圾桶`→9 条、`加厚垃圾桶`→1 条
- `type=15`（用户信息）即便带正确 token 仍返回 `error!`，说明该接口另有参数要求，未深挖

## 9. 交付
见 `gouvip_picker/`（CLI + 本地 Web 控制台 + 定时任务 + Excel 导出）

## 10. 浏览器实测（2026-10-28，无头 Chrome + CDP，见 cdp_probe.py）
- 网站能走：注入 token 后 `web.gouvip.com/hot-selling/industryList` 正常渲染
  - 顶部「类目选择：水果生鲜」、榜单类型「全部类型/好评榜/畅销榜/降价榜」、左栏 13 个子榜、
    右栏数百条榜单（桔子榜/辣椒榜/番薯榜…）—— 与 `type=89` 返回完全一致
  - 弹出「【插件未安装,请先安装插件】」提示（站点依赖 Chrome 扩展做商家后台请求）
  - 截图：`shots/gouvip_industry.png`
- 站内「今日热销」页：商品列表销量全部显示 `1-500`，底部「共 167056 条」，
  与 `type=142` 接口返回完全相同 → **网页与接口同源同数据**，换浏览器不会变出新数据
  - 截图：`shots/gouvip_todayhot.png`
- 拼多多官方榜单页：**未登录拿不到**
  - 请求 `mobile.yangkeduo.com/sjs_cat_rank_list.html?list_id=...&list_sn=...`
  - 实际被 302 到 `mobile.yangkeduo.com/login.html?from=...&refer_page_name=bangdan_list`
  - `document.title = 登录`，页面只有「手机登录/扫码登录/发送验证码」
  - 截图：`shots/pdd_rank.png`
  - 结论：榜单内的真实商品排名需要**拼多多登录态**，任何抓取方案都绕不开

## 11. 二期入口（已交付脚手架）
- `pdd_login.bat` → 有头 Chrome + 固定 profile（`pdd-profile\`），扫码登录一次
- `pdd_session.py check|login|fetch <url>` → 复用登录态无头抓榜单页，落 HTML/截图
- 登录判定依据：cookie `PDDAccessToken`（`.yangkeduo.com`）
- 注意：本机 pwsh 处于无桌面会话，有头 Chrome 无法由代理拉起（报「无法在此会话中打开」），
  扫码必须由用户在自己桌面双击 `pdd_login.bat` 完成

## 12. 二期已跑通（2026-10-28 17:39 扫码后）
- 登录：无头 Chrome + `pdd_qr.py` 出码，用户扫码 → `PDDAccessToken` 落盘（profile 158MB）
- 榜单页数据结构（两路，缺一不可）：
  1. `POST/GET /proxy/api/api/imalgo-ranking-list/landing_page?p_list_id=<hash>&anti_content=...`
     返回 `{data:{id,tag_name,data_list:[{tab_name,rule,ranking_list_id,ranking_list_sn,list_type,goods_list[]}]}}`
     —— 每次只带当前 tab 的商品
  2. `window.rawData.store.tabDataMap`（服务端直出，key=list_type）
     —— **默认 tab（畅销榜）只在这里**，不抓它就会漏掉整榜
- tab 语义：1=畅销榜(hot_score=热卖指数 6530)、2=好评榜(hot_score_text=好评率 95.58%)、
  4=降价榜(hot_score_text=降价幅度 0.3516)
- 商品字段：goods_id/goods_name/hd_thumb_url/min_on_sale_group_price(分)/sales_text/sales_tip/
  ext.hot_score/ext.hot_score_text/ext.tag_text(蝉联榜首90天)/ext.brand_name/
  ext.group_order_info.text(24小时内999+人拼单)/tag_list(正品险,假一赔十)
- **榜单页不含店铺名与卖家数**，竞争度需进商品详情页
- 实测：11 页 / 186 商品 / 0 次风控命中，配额 11/45(时) 11/300(日)
- 交付：`pdd_crawler.py`（采集+风控+解析+断点续跑）、`pdd_pick.py`（榜单选品+Excel）
