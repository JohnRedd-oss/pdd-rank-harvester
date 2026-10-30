# PDD Rank Harvester

拼多多「行业榜单」采集与选品分析工具集。两件事：

1. **`gouvip_picker/`** —— 复刻第三方选品工具（狗宝助手网页版）的行业榜单目录接口，拿到类目 → 子榜 → 榜单条目的完整树。
2. **`pdd_recon/`** —— 用真实浏览器打开拼多多官方榜单页，采集榜单内的商品、价格、24 小时单量、热卖指数、好评率，并做类目级与商品级分析。

两段合起来，可以回答「某个细分类目能不能做到 X 元客单」「哪个价位带真的有人买」这类问题。

> 仅用于分析自己有权访问的公开榜单数据。请遵守目标站点的服务条款，低频访问，不要用于批量采集对外提供服务。

---

## 目录结构

```
pdd-rank-harvester/
├─ gouvip_picker/               # 榜单目录（第三方选品平台网页版接口）
│  ├─ cli.py
│  ├─ config.example.json
│  ├─ gouvip/
│  │  ├─ api.py                 # 登录 / 签名 / 11 个接口
│  │  ├─ store.py               # SQLite：类目 / 榜单 / 商品快照 / 差异
│  │  ├─ scoring.py             # 选品打分（权重可配）
│  │  ├─ pipeline.py            # 采集 → 打分 → 导出
│  │  └─ ui.py                  # 本地 Web 控制台
│  ├─ run_daily.ps1
│  └─ install_schedule.ps1
├─ pdd_recon/                   # 拼多多榜单页采集
│  ├─ pdd_crawler.py            # 采集器（风控 + 解析 + 入库 + 断点续跑）
│  ├─ pdd_pick.py               # 榜单选品打分 + Excel
│  ├─ pdd_category.py           # 类目机会分析（价格带 / 24h 单量 / 品牌占比）
│  ├─ export_data.py            # 一键导出全部数据到 Excel
│  ├─ band_targets.py           # 价格段榜单清单
│  ├─ category_menu.py          # 类目候选菜单
│  ├─ pdd_crawler.example.json  # 风控参数（复制为 pdd_crawler.json 生效）
│  ├─ pdd_pick.example.json
│  ├─ run_bands.ps1             # 受控批次脚本（配合计划任务）
│  ├─ pdd_login.bat             # 扫码登录
│  └─ probe_pdd.bat             # 单页结构探针
└─ docs/REVERSE-NOTES.md        # 接口还原记录（签名算法、字段、坑）
```

## 环境

- Windows（脚本用了 Chrome + 计划任务；核心 Python 逻辑跨平台）
- Python 3.10+
- Chrome（已安装即可，脚本会读它的 CDP 接口）
- `pip install requests websocket-client openpyxl`

## 快速开始

### 一、榜单目录（gouvip_picker）

```powershell
cd gouvip_picker
copy config.example.json config.json     # 填自己的账号
python cli.py login
python cli.py cats --save
python cli.py industry --cat 15          # 15=家居百货，拉该类目全部榜单
python cli.py pick --top 30 --excel
python cli.py ui                         # 本地可视化调参
```

### 二、拼多多榜单页（pdd_recon）

```powershell
cd pdd_recon
copy pdd_crawler.example.json pdd_crawler.json

# 1) 扫码登录（一次性，登录态存 pdd-profile/）
pdd_login.bat

# 2) 探一次页面结构
python pdd_crawler.py probe "<榜单URL>"

# 3) 受控采集
python pdd_crawler.py run --sample 4 --keywords "模型,钓鱼,手串,鱼缸,手办" --no-wait

# 4) 分析
python pdd_category.py --top 20 --excel
python pdd_pick.py --top 30 --excel
python export_data.py                    # 全量导出 5 张表的 Excel
```

## 榜单 URL 怎么来

榜单目录里的每一条都可以拼成拼多多官方页：

```
https://mobile.yangkeduo.com/sjs_cat_rank_list.html?list_id=<tab_id>&list_sn=<list_sn>
```

`tab_id` / `list_sn` 来自 `gouvip_picker` 抓到的榜单目录。

## 采集到的字段

| 字段 | 说明 |
|---|---|
| `price` | 到手价（券后/活动价） |
| `sales_text` / `sales_tip` | 累计销量，如 `19.9万+` |
| `day_orders` | **24 小时单量下限**（解析「24小时内999+人拼单」，7 天/30 天窗口已折算） |
| `hot_score` | 热卖指数（畅销榜专有，按近 7 日销量与成交额计算） |
| `score_text` | 好评率（好评榜专有，如 `95.58%`） |
| `tag_text` | `蝉联榜首90天` / `霸榜前三6天` 这类稳定性标签 |
| `brand` | 品牌归属（用于判断白牌能否进入） |
| `rank` | 榜内排名 |

榜单分三种 tab，语义不同：

| tab | 排序依据 | 关键字段 |
|---|---|---|
| 畅销榜 | 近 7 日销量 + 成交额 | `hot_score` 热卖指数 |
| 好评榜 | 近 30 日好评质量 | `hot_score_text` 好评率 |
| 降价榜 | 近 30 日降价幅度 | `hot_score_text` 降幅 |

## 风控设计

目标不是「把站打穿」，是「稳定拿到需要的几十个榜单」。

| 项 | 默认 | 说明 |
|---|---|---|
| 并发 | 1 | 绝不并发 |
| 导航间隔 | 15–35 秒随机 | 固定间隔最容易被识别 |
| 每小时上限 | 8 个榜单 | 按榜单数计，不按页面行数 |
| 每日上限 | 80 个榜单 | |
| 长休息 | 每 8 个榜单歇 8–15 分钟 | |
| 冷却 | 命中风控后 90–180 分钟 | 自动写入，后续批次被拦 |
| 运行时段 | 08:00–23:00 | 凌晨不动 |
| 下架复核 | 金丝雀榜单 | 区分「真下架」与「软封」 |
| 连续下架熔断 | 3 个 | 直接停轮 |

关键取舍：**不自造 `anti_content`，不重放接口**。让页面自己发请求，工具只读响应。伪造签名是最容易被识别的特征。

## 已知限制

1. 拼多多榜单只覆盖主流类目；超小众爱好（拼豆、滴胶、热缩片等）**没有建榜**，抓不到。
2. 榜单页不含店铺名与卖家数，竞争度需要进商品详情页（另算风控预算）。
3. 24 小时单量与 GMV 都是**下限**（`999+` 即 ≥999），适合横向比较，不适合绝对规模承诺。
4. 榜单目录会大面积过期（活动系列 `3283*` 基本全下架），需按 id 前缀过滤。
5. 榜单每天更新，长期采集要按 `revisit_days` 控制重复。

## 免责

代码仅用于个人对小众类目的市场研究。使用者需自行确保符合目标平台的服务条款与当地法律。
