# apple-cn-monitor

Apple **中国大陆**官网的「官方翻新降价 / 上架」与「直营店到店取货有货」监控，只往一个 ntfy 主题发提醒。零第三方 Python 依赖，GitHub Actions 定时跑，不下单、不加购、不抢购。

订阅地址：`http://112.74.95.74:8858/x-apple-refurb`（主题 `x-apple-refurb`）

与 `x2ding`（X 监控）、`xq-dadao`（雪球监控）完全独立：独立仓库、独立 state、独立主题，互不影响。

---

## 监控什么

| 链路 | 数据源 | 触发提醒的事件 |
|---|---|---|
| 官翻价格 | `www.apple.com.cn/shop/refurbished/{mac,ipad,watch,accessories,homepod}` 页面内嵌的 `REFURB_GRID_BOOTSTRAP` | **降价**（价格低于上次记录）、**新上架**、**达到目标价**（`target_rules` 配了才有） |
| 直营店自提 | 优先 `GET /shop/sba/availability-message`（2026 新版，一次拿全量有货门店）；境外 IP 自动切 `GET /shop/retail/pickup-message?store=<门店>`（显式指定门店，不依赖 IP 定位） | **无货 / 未知 → 有货** 的跃迁；持续有货默认不重复提醒 |

两条链路的消息都发到同一个主题 `x-apple-refurb`，用标题前缀「官翻降价 / 官翻新上架 / 官翻到目标价 / 到店取货有货」和 ntfy tags 区分。要拆成两个主题，复制一份 config 改 `ntfy_topic` 即可。

**范围**：只做中国大陆。CN 官翻店**没有 iPhone 和 Apple TV**（请求 404），AirPods 分类存在但当前为空（`tiles: null`），所以默认只抓实测有商品的 5 类：Mac / iPad / Watch / 配件 / HomePod。自提侧监控当前在售 iPhone 机型在中国大陆全部直营店（实测 49 家）的取货库存。第三方电商（京东、拼多多等）**不在范围内**。

---

## 快速开始

```bash
cd apple-cn-monitor
cp local.env.example local.env     # 填入 ntfy 发布令牌，文件名已被 .gitignore 忽略
./run.sh --init                    # 建基线：抓全量、写 state.json 与 data/catalog.json，不推送
./run.sh --dry-run                 # 跑一轮，只打印将要推送的消息，不落盘
./run.sh                           # 正式跑一轮
```

`local.env` 内容：`APPLE_NTFY_TOKEN=<你的 ntfy 发布令牌>`。令牌只授权 `x-*` 前缀主题，配置里若改成别的主题前缀会 403，脚本启动时会拦截。

### 命令

| 命令 | 作用 |
|---|---|
| `--init` | 建基线并写入 `state.json` / `data/catalog.json`。**首轮不推送**，防止把现有几百件官翻商品当成「新上架」刷屏 |
| `--dry-run` | 跑完整流程，只打印 ntfy payload，不推送、不写 state、不归档 |
| `--test` | 往主题发一条链路自检消息 |
| `--list-stores` | 联网打印中国大陆直营店代码与名称 |
| `--list-products` | 打印当前监控的 SKU 与名称 |
| `--refresh-catalog` | 重建门店 / SKU 快照（新品发布后跑一次） |
| `--max N` | 覆盖单轮推送上限（默认 3） |
| `python3 selftest.py` | 离线自检：不联网、不推送、不改 state，验证降价 / 上架 / 目标价 / 有货跃迁 / 失败保护 / 重试语义共 18 项 |

---

## 配置

`config.json`：

```jsonc
{
  "ntfy_url": "http://112.74.95.74:8858",
  "ntfy_topic": "x-apple-refurb",     // 必须以 x- 开头
  "max_per_run": 3,                   // 单轮最多 3 条，超出的事件留到下一轮
  "refurb": {
    "categories": ["mac","ipad","watch","accessories","homepod"],
    "notify_new_listing": true,
    "target_rules": [                 // 示例：命中关键词且价格低于上限时提醒
      {"match": "MacBook Neo", "max_price": 4500},
      {"match": "Mac mini",   "max_price": 3000}
    ]
  },
  "pickup": {
    "families": ["iphone-duo", "iphone-air"],  // 购买页 slug，--list-products 可查
    "max_parts": 20,                          // 参与查询的 SKU 上限
    "store_whitelist": [],                    // 新版接口用：空 = 全部 49 家；填 ["R389","R320"] 只看浦东/三里屯
    "fallback_stores": ["R320","R389","R577","R761"],  // 境外兜底用：北京三里屯/上海浦东/广州天环/深圳万象城
    "fallback_max_parts": 10,                // 兜底模式按产品族均衡取 10 个 SKU
    "reminder_hours": 6,
    "remind_while_in_stock": false            // 持续有货是否低频重复提醒
  }
}
```

- **加机型**：把购买页 slug 加进 `pickup.families`，然后 `./run.sh --refresh-catalog`。每多 10 个 SKU 每轮多 1 个请求，别一次加太多。
- **收窄门店**：`store_whitelist` 填 `--list-stores` 查到的代码（新版接口用，不填就是全大陆任一家有货都提醒）；`fallback_stores` 是境外环境实际查的门店，减到 1–2 家可进一步省请求。
- **双接口自动切换**：新版接口按出口 IP 定位门店，中国大陆出口一次请求就能拿到全量有货门店；GitHub Actions 的境外 runner 拿不到大陆门店上下文（`storeId` / `eligibleStores` / 配送地区全空），脚本自动改用显式指定门店的旧接口。两种数据源覆盖范围不同，切换那一轮只重建基线、不发提醒，避免跨环境误报。
- **设目标价**：`target_rules` 用标题子串匹配，价格是人民币元（整数或小数）。默认空数组，即只推降价 / 上架 / 有货跃迁。
- **下架不提醒**：官翻商品下架只写 `state.json` 的 `gone` 标记，不发消息；重新上架时能识别为新上架。

---

## GitHub Actions

仓库 `.github/workflows/apple.yml`：**每小时第 30 分钟**一轮，也支持手动触发（可勾选 dry-run / 重建基线）。官翻价格与门店库存变化都不快，一小时一轮已经够用，也把对 Apple 的请求压到最低。

首次部署需要在仓库 Settings → Secrets 里加一个 `APPLE_NTFY_TOKEN`，值是 ntfy 发布令牌。CI 里用 `secrets.APPLE_NTFY_TOKEN` 注入环境变量，令牌不入库。

`permissions: contents: write` 用于回写 `state.json`、`data/catalog.json` 和 `archive/`。脚本只在**有事件或冷却状态变化**时写 state，无事件时磁盘不变，CI 的 `git diff --cached --quiet` 会跳过 commit，不会每半小时制造一条空提交。

### 每轮请求预算

| 请求 | 次数 |
|---|---|
| 请求 | 中国大陆出口 | GitHub Actions（境外） |
|---|---|---|
| 官翻 5 个分类列表页 | 5 | 5 |
| 购买页会话预热 | 1 | 1 |
| 新版 `availability-message`（20 个 SKU 分 2 批） | 2 | **1**（第一批即判定无门店上下文，立刻切换） |
| 显式门店 `retail/pickup-message`（4 家 × 10 SKU） | 0 | 4 |
| 门店 GraphQL + 购买页 SKU 目录 | **0**（读仓库里的 `data/catalog.json` 快照，只在 `--refresh-catalog` / `--init` 时联网） | 0 |
| **合计** | **8 / 轮** | **11 / 轮** |

一小时一轮，即每小时 8–11 个请求。官翻列表页是 CDN 静态资源（`max-age=120`），自提接口是唯一会触发 Apple 限频的地方。想再省请求就把 `fallback_stores` 减到 1–2 家。

### 克制机制（重要）

- Apple 按出口 IP 限频，连续密集查询会返回 **HTTP 541**，冷却通常 10–15 分钟。脚本遇到 541/429 立刻写 `state.json` 的 `cooldown_until = now + 900`，**整段跳过自提查询**，且这一轮不写任何自提状态；冷却跨轮生效（因为 state 会回写）。
- **三态**：`available` / `unavailable` / `unknown`。查不到该 SKU、接口报错、非 JSON、境外拿不到门店上下文，一律记为 unknown，**绝不当成无货**，也不会产生「无货→有货」的假提醒。
- **跨环境不误报**：两种自提数据源覆盖的门店范围不同，`state.json` 记 `pickup_mode`（`sba` / `retail`）；切换时那一轮只重建基线、不发提醒。
- **抓取失败不当下架**：官翻页返回 200 但找不到 bootstrap，或整类请求失败时，该分类本轮完全不参与 diff，不会把已知商品误判成下架 / 重新上架。
- **地域护栏**：官翻商品自报货币不是 CNY 时整类丢弃，防止境外出口 IP 被重定向到别国站点后产生大面积误报。
- 批次之间 sleep 1 秒，分类之间 sleep 0.6 秒；不并发、不轮换 UA（稳定的浏览器 UA 比随机 UA 更像正常流量）。
- 单轮最多 3 条推送；推送失败的条目**不更新状态**，下一轮自动重试。

---

## 状态与归档

| 文件 | 内容 |
|---|---|
| `state.json` | 官翻每件商品的当前价格 / 标题 / 链接 / `gone` 标记；每个 SKU 的自提三态与上次提醒时间；541 冷却截止时间戳 |
| `data/catalog.json` | 门店代码→名称、SKU→名称与购买页直达链接的快照，约每周或新品发布时刷新一次 |
| `archive/events-YYYY-MM.jsonl` | 每月一个事件文件，逐行记录降价 / 上架 / 目标价 / 有货事件及 `pushed` 结果 |

CI 每轮把这三个位置一起 `git add`，有变化才提交；push 带 `git pull --rebase` 自愈，避免网页端同时改仓库造成非快进。

---

## 已知边界与风险

1. **境外出口 IP（已实测并解决）**：2026-11-13 在 GitHub Actions 上实测，官翻 5 类全部正常（422 件），但新版自提接口返回 `storeId` / `eligibleStores` / 配送地区全空——Apple 不给境外 IP 中国大陆门店上下文，cookie 预热也无法绕过。已实现双接口自动切换：境外自动改用显式指定门店的 `retail/pickup-message`，按 4 家重点门店查询。代价是境外只覆盖这 4 家（大陆出口则是全 49 家），且只看 10 个 SKU；在 `fallback_stores` 里加门店会线性增加请求数。
2. **CN 官翻无 iPhone**：这是 Apple 中国官网的现状，不是 bug。想盯 iPhone 官翻只能换地区站点。
3. **接口会变**：门店用 GraphQL persisted query（hash 写在 `apple_push.py` 顶部）、SKU 用购买页 `productSelectionData`、自提用 2026 版 `availability-message`（老的 `fulfillment-messages` / `retail/pickup-message` 已失效）。Apple 改版时脚本会记 warning 并保留旧状态，不会乱推；届时按 `--list-stores` / `--list-products` 的输出更新 slug 即可。
4. **GitHub Actions 60 天无活动会停用 schedule**：偶尔手动触发一次即可恢复。
5. **官翻 CDN 缓存 120 秒**：30 分钟一轮远大于这个下限，不会读到过期快照。
6. 只覆盖同步到官网页面的信息，**不含第三方电商价格、不含国补 / 优惠券到手价**。

---

## 只提醒

本项目不会登录 Apple 账号、不会加入购物车、不会提交订单、不会调用任何下单接口。它只是定时读公开页面，把变化推到 ntfy。请保持低频、不要用它做批量抢购——接口一旦对自动化收紧，对所有人都会关掉。

## 接口思路来源

实现时读了以下开源项目，只借鉴接口与工程思路，**代码全部重写、未复制任何源码**：

- `XGFan/apple-store-watcher`（Go，2026 版自提接口 / GraphQL 门店 / 购买页 bootstrap，无 LICENSE）
- `hh-io/refurb-sentry`（Go，MIT，官翻事件模型、抓取失败保护、货币护栏）
- `moonhorsemmy/apple-pickup-bash`（Shell，GPL-3.0，三态库存、541 冷却自律、跃迁才提醒）

本项目为私有仓库，不对外分发。
