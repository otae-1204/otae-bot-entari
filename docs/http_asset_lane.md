# 素材通道：独立并发、短超时、按主机熔断与整卡截止时间

## 背景

生产机 em（Windows，Python 3.13）直连 `bbs.hycdn.cn`（阿里 CDN）的 443 与 80 全部超时，
其余图床（`web.hycdn.cn`、`ak.hycdn.cn`、AKEData、fz.wiki、森空岛）都能连通。旧逻辑下：

- 素材每次请求 12–20s 超时、最多 3 次尝试，一张 `/ef` 账号卡 231–243 张图全部
  `connecttimeout`，单卡渲染 1764–1865s；`/ef 回响` 29 张图 7 分钟。
- 素材与 API 共用全局 `Semaphore(8)`，渲染占满后签到、绑定、ownership 刷新、
  catalog 检查全部排队（绑定等锁 64.8s，catalog 检查 388s）。

## 现在的行为

| 机制 | 说明 |
| --- | --- |
| 独立通道 | `fetch_many_resilient` 与 `fetch_bytes(..., asset=True)` 走单独的 `httpx.AsyncClient` 和信号量；API 请求仍是原来的客户端与 `Semaphore(8)`，并发与行为不变。内存 / 磁盘缓存两条通道共用。 |
| 短超时、少重试 | 素材连接超时默认 4s、读超时 10s；默认重试 1 次（最多 2 次尝试），退避 0.25s。 |
| 按主机熔断 | 同一主机连续 3 次连接超时 / 连接失败 / 代理失败后打开，冷却 300s 内该主机的素材请求直接判缺失、不发网络请求；冷却期过后只放一个探测请求（半开），成功即关闭，失败则重新计时。404 等 HTTP 状态、读超时说明主机可达，不计数且清零连续失败。日志只在打开（warning）和恢复（info）时各记一次。 |
| 整卡截止时间 | 每批素材有总预算（默认 25s）；`/ef` 账号卡、`/ef 回响`（影拓 / 回响）在整张卡范围共用一个预算，`_resolve_asset_groups`、缺章卡、抽卡图片缓存的首选 + 备用两批也共用一个预算。到点不再等待，已拿到的照常出图，未拿到的走各卡原有的占位 / 留空逻辑，结果不进渲染缓存（下次重新尝试）。每轮至少等 0.5s，即使截止时间已过，内存 / 磁盘缓存命中仍拿得到。 |
| 代理（默认关闭） | 可为素材指定代理，并限定只有哪些主机走代理；或允许素材客户端 `trust_env` 读取 `HTTP(S)_PROXY` / `NO_PROXY`。默认不走代理、`trust_env=False`。 |
| 主机改写（默认关闭） | 可配置 `源主机=目标主机` 映射，只改主机名、保留路径。**不内置任何映射**：实测 `bbs.hycdn.cn/asset/endfield_attendance/...png` 在 bbs 上 200，同一路径在 `web.hycdn.cn` 与 `ak.hycdn.cn` 都是 404。 |

失败原因会进 `fetch incomplete` 汇总日志（每批一行）：`connecttimeout`、`circuit_open`、
`deadline`、`http 404` 等；到点截止的批次额外标 `deadline=hit`。

## 配置

都在 `.env`（或进程环境变量）里，首次取图时读取；留空即默认值。

| 配置 | 默认 | 作用 |
| --- | ---: | --- |
| `OTAE_HTTP_ASSET_CONNECT_TIMEOUT` | 4 | 素材连接超时（秒，0.5–60） |
| `OTAE_HTTP_ASSET_READ_TIMEOUT` | 10 | 素材读超时（秒，1–120；调用方显式传入的超时优先） |
| `OTAE_HTTP_ASSET_RETRIES` | 1 | 失败后重试次数（0–5；0 = 只试一次） |
| `OTAE_HTTP_ASSET_CONCURRENCY` | 8 | 素材通道并发上限（独立于 API 的 8） |
| `OTAE_HTTP_ASSET_BREAKER_THRESHOLD` | 3 | 连续几次连接失败后熔断（0 = 关闭熔断） |
| `OTAE_HTTP_ASSET_BREAKER_COOLDOWN` | 300 | 熔断冷却秒数，过后放一个探测请求 |
| `OTAE_HTTP_ASSET_RENDER_BUDGET` | 25 | 每张卡 / 每批素材准备的总预算（秒；0 = 不限时） |
| `OTAE_HTTP_ASSET_PROXY` | 空 | 素材代理，如 `http://127.0.0.1:7897`（支持 http/https/socks5；省略 scheme 按 http；socks 需额外装 `socksio`） |
| `OTAE_HTTP_ASSET_PROXY_HOSTS` | 空 | 只让这些主机走素材代理，逗号分隔，支持 `*.example.com`；留空且设置了代理时全部素材走代理 |
| `OTAE_HTTP_ASSET_TRUST_ENV` | false | 素材客户端是否读取 `HTTP(S)_PROXY` / `NO_PROXY` / `SSL_CERT_*` |
| `OTAE_HTTP_ASSET_HOST_REWRITE` | 空 | 主机改写：`源=目标,源2=目标2` 或 JSON 对象 |

代理地址可能带口令，日志只记 scheme、主机和端口。

## 实测（otae-1，bbs.hycdn.cn 指向黑洞地址 10.255.255.1）

在 httpcore 网络层把 `bbs.hycdn.cn` 的 TCP 连接改到 TEST-NET 黑洞地址，连接一直挂到超时；
另起本机 HTTP 服务充当可达的图床与 API。「API 排队」是素材准备开始 1s 后发出的一个
API 请求的完成耗时。

| 场景 | 代码 | 素材准备耗时 | API 排队 | 说明 |
| --- | --- | ---: | ---: | --- |
| 16 张不可达，20s × 3 次 | 改前 | 120.9s | 39.1s | 实测 |
| /ef 回响 29 张不可达，20s × 3 次 | 改前 | 241.1s | 59.1s | 实测 |
| /ef 账号卡 231 张不可达，20s × 3 次 | 改前 | 约 1753s | 约 580s | 推算：⌈231/8⌉=29 轮 × 3 次 × 20.15s；生产观测 1764–1865s |
| /ef 账号卡 231 张不可达 + 12 张可达 | 改后 | 4.4s | 0.005s | 实测；只发了 8 次连接，12 张可达图全部取到 |
| /ef 回响 29 张不可达（熔断冷启动） | 改后 | 4.3s | 0.004s | 实测 |
| /ef 回响 29 张不可达（熔断已打开） | 改后 | 0.005s | — | 实测；不发网络请求 |
| 231 张不可达 + 12 张可达，关闭熔断 | 改后 | 25.0s | 0.004s | 实测；只靠整卡截止时间兜底 |

关闭熔断时，可达的 12 张排在不可达的 231 张之后，素材通道 8 个槽位一直被黑洞连接占着，
到截止时间也没轮到；所以熔断应保持开启，整卡截止时间只是最后一道兜底。

## em 建议配置

```env
OTAE_HTTP_ASSET_PROXY=http://127.0.0.1:7897
OTAE_HTTP_ASSET_PROXY_HOSTS=bbs.hycdn.cn
```

其余保持默认。即使 mihomo 未启动或代理也不通，熔断会在约一个连接超时后跳过
`bbs.hycdn.cn`，整张卡最多等 25s 就出图。
