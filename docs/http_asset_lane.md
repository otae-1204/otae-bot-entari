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
| 先直连、失败再走代理 | 配置了代理时默认 `PROXY_MODE=fallback`：代理主机先直连，只有连接失败 / 连接超时才对这次请求改走代理，并记住该主机「直连不通」300s，期间直接走代理，过后只放一个请求探测直连。读超时、HTTP 状态说明已连上，不改走代理。熔断只看最终结果：直连失败但代理成功不计数。`always` 保持原来的「代理主机一律走代理」。 |
| 主机改写（默认关闭） | 可配置 `源主机=目标主机` 映射，只改主机名、保留路径。**不内置任何映射**：实测 `bbs.hycdn.cn/asset/endfield_attendance/...png` 在 bbs 上 200，同一路径在 `web.hycdn.cn` 与 `ak.hycdn.cn` 都是 404。 |
| 磁盘缓存不挡取图 | 新图交给渲染后再排队落盘（每个库一个后台写线程，批量事务）；排队中的行立即可读。服务器回 304 只更新校验时间与 ETag / Last-Modified，不删除、不重写正文。写入失败只记日志。 |
| 旧缓存兜底 | 重新校验失败（连接失败、5xx、404、熔断中）时用已过期的磁盘副本，内存里只留 60s 后再试；整批截止时间到点时，没取到但磁盘上有旧副本的也直接用旧图。日志里记 `stale=N`。 |
| 固定地址长缓存 | `*.hycdn.cn` 的图片路径带哈希、内容不变，磁盘副本 7 天内不再校验（`HOST_MAX_AGE`），旧版按 600s 写入的行也立即按 7 天算；其余主机仍是 600s。目前进磁盘缓存的 hycdn 图只有 `bbs.hycdn.cn/image/...`（白名单未改）。 |

失败原因会进 `fetch incomplete` 汇总日志（每批一行）：`connecttimeout`、`circuit_open`、
`deadline`、`http 404` 等；到点截止的批次额外标 `deadline=hit`，用了旧副本的标 `stale=N`。

## 磁盘缓存：写入不再挡取图（2026-10-03）

**现象**：em 上 `/ef` 账号卡下半部分全是占位图。10-03 每次账号渲染都在约 27s 被整卡截止时间截断
（requested=232、resolved=123、failed=109，原因全是 `deadline`），而 232 个响应 5s 内就全到了。

**根因**：`disk.py` 的 `put()` 每写一张图都在全局锁内做一次淘汰扫描
`SELECT key, length(content) ... ORDER BY accessed_at`（外加一次 `sum(length(content))`）。
`accessed_at` 存在正文之后，扫描要走完每行的溢出页；em 上 250MB 的库单次约 141ms，232 张累计约 33s。
放大因素：10 分钟后全部重新校验；304 也整张删掉重写；取图要等写完才算拿到；图按行顺序排队，
所以前约 18 个干员正常、后面全是占位。慢写入从 e0492d1 就有（只表现为渲染 47–131s），
be55f44 加了截止时间后变成缺图。

**现在**：

- `public_images_v1` 加 `size` 列与 `(accessed_at, size)` 索引（`PRAGMA user_version=2`）。
  写入前用覆盖索引算 `count(*)` / `sum(size)`，只有超限才按索引从最久未用的行开始淘汰，不读正文。
- 旧库自动迁移：第一次连接只 `ALTER TABLE ADD COLUMN`（只改表定义），回填 `size` 在后台写线程里
  每 64 行一个事务，读请求用单独的 WAL 连接，不等回填；回填期间写入按 `length(content)` 慢路径算总量，
  结果仍正确。缓存不丢。
- 迁移后再回滚到本次修改之前的代码（be55f44 及更早）时，旧代码按 10 列写入会失败
  （仍能读旧缓存，写入只记 `disk_errors`）；需要旧代码继续写缓存就删掉
  `data/cache/public-images-v1.sqlite3`（可随时重建）。
- 进程退出：`close_http_client()` 和 `atexit` 都会先把排队的写入落盘（最多等 10s / 5s）。

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
| `OTAE_HTTP_ASSET_PROXY_MODE` | fallback | `fallback`：上面的主机先直连，连接失败 / 连接超时才对该请求改走代理；`always`：一律走代理。未配置代理时不起作用 |
| `OTAE_HTTP_ASSET_DIRECT_COOLDOWN` | 300 | fallback 模式下某主机直连失败后，多少秒内直接走代理（1–86400），过后放一个请求探测直连 |
| `OTAE_HTTP_ASSET_TRUST_ENV` | false | 素材客户端是否读取 `HTTP(S)_PROXY` / `NO_PROXY` / `SSL_CERT_*` |
| `OTAE_HTTP_ASSET_HOST_REWRITE` | 空 | 主机改写：`源=目标,源2=目标2` 或 JSON 对象 |
| `OTAE_HTTP_ASSET_HOST_MAX_AGE` | `hycdn.cn=604800` | 内容地址固定的图床，磁盘副本多少秒内不再校验，按主机后缀匹配（`hycdn.cn` 覆盖 `bbs.hycdn.cn` 等），逗号分隔多项；`off` = 全部按 600s。服务器的 `no-cache` / `no-store` / `private` 仍然生效 |

代理地址可能带口令，日志只记 scheme、主机和端口。

`PROXY_MODE` 默认 `fallback` 的理由：配代理是为了「直连不通时还能取到图」，不是必须绕开直连。
BOT云运维 10-03 实测直连 `bbs.hycdn.cn` 中位 0.05s、走代理 2.2s；`always` 下 232 张图在 8 个并发槽里
要约 232/8 × 2.2s ≈ 64s，冷缓存时必然被 25s 截止时间截断。`fallback` 在直连正常时和不配代理一样快；
直连坏了也只多付一次连接超时（默认 4s），之后 300s 内直接走代理。必须绕开直连（例如出口审计）时设 `always`。

磁盘缓存的位置与预算沿用原有变量：`OTAE_PUBLIC_IMAGE_CACHE_PATH`（默认
`data/cache/public-images-v1.sqlite3`）、`OTAE_PUBLIC_IMAGE_CACHE_MIB`（默认 256），表格缓存同理
（`OTAE_PUBLIC_TABLE_CACHE_*`）。

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

## 实测：磁盘缓存（2026-10-03，otae-1）

脚本不入库。库用 be55f44 原版 `disk.py` 的建库语句（auto_vacuum=FULL、WAL）与 10 列行格式造：
3931 行、每行 45–80KB 随机内容，正文合计 250.0MB、文件 254.7MB；预算 256MiB（默认）。
Linux、Python 3.12.3、SQLite 3.45.1，页缓存是热的。这台机器原版单次写入约 18ms，
em（Windows、Python 3.13）上实测约 141ms，约慢 7.7 倍；改后取图不再等磁盘，这个差距只影响后台落盘。

| 场景 | 改前（be55f44） | 改后 |
| --- | ---: | ---: |
| 一次淘汰扫描（`sum(length(content))` + `ORDER BY accessed_at`） | 18.9ms，每次写入都做 | 不再执行；超限判断走覆盖索引 |
| 232 张已有图重写，同步 `put()` | 共 5.00s；中位 18.3ms，p95 24.0ms | 共 0.77s；中位 2.3ms，p95 8.1ms |
| 232 张后台写入 `put_later()` | — | 入队共 1ms；全部落盘 0.23s |
| 232 张 304（改前也是整张重写） | 同上，约 5.0s | 只刷新时间：入队 1ms，落盘 0.11s |
| 库已满，232 张新图都要淘汰，同步 `put()` | 共 4.94s；中位 18.2ms | 共 0.48s；中位 0.53ms |
| 库已满，232 张后台写入 | — | 全部落盘 0.32s；之后正文合计 250.03MB ≤ 预算，`integrity_check` ok |
| 旧库迁移 | — | 首次连接加列 4.6ms；后台回填 3931 行 2.8s；回填期间读 545 次，中位 0.11ms、最大 0.31ms |

端到端：本机 HTTP 服务当图床（每个请求固定 50ms，带 ETag / 304），URL 用
`https://bbs.hycdn.cn/image/...` 或 `https://data.akedata.wiki/public/images/...`（由 transport
转发到本机），`fetch_many_resilient` 一次拉 232 张，素材并发 8，库约 250MB。

| 场景 | 改前 | 改后 |
| --- | ---: | ---: |
| S1 232 张 hycdn 已缓存、过了 10 分钟，部署后第一次（库还要迁移） | 6.19s，232 次 304 | 0.12s，0 次网络（7 天长缓存），迁移在后台 2.5s 跑完 |
| S1 同上，迁移完成后 | 同上 | 0.12s，0 次网络 |
| S2 232 张 AKE 图都要 304 重新校验 | 6.11s | 1.86s（网络下限约 232/8 × 50ms = 1.45s） |
| S3 232 张新图（200），库已满（预算 238MiB）每张都要淘汰 | 6.48s | 2.04s；之后落盘再用 0.01s |
| S4 图床挂住不回、旧副本已过期，整卡预算 3s | 3.0s 出图，232 张全空（`deadline`） | 3.05s 出图，232 张全部用旧副本 |

按 em 的 141ms / 次推算，改前 232 张光写盘就要约 33s，与生产上 27s 被截断一致；改后取图路径不碰写盘。

## em 建议配置

```env
OTAE_HTTP_ASSET_PROXY=http://127.0.0.1:7897
OTAE_HTTP_ASSET_PROXY_HOSTS=bbs.hycdn.cn
# OTAE_HTTP_ASSET_PROXY_MODE 留空即 fallback：先直连（实测 0.05s），连不上才走 mihomo（2.2s）
# OTAE_HTTP_ASSET_RENDER_BUDGET 留空（25s），去掉临时设的 45
```

其余保持默认。理由：

- **去掉 `OTAE_HTTP_ASSET_RENDER_BUDGET=45`**：被截断前那 27s 基本都在排队等同步写盘（每张约 141ms），
  网络 5s 内就全到了。现在取图不等磁盘，304 不重写，hycdn 图 7 天内直接读盘，
  到点没取到的还会先用旧副本；25s 足够，留 45 只会在图床真不可达时让用户多等 20s。
- **代理用 fallback**：直连正常时和不配代理一样快；直连坏了第一次多等一个连接超时（4s），
  之后 300s 内直接走代理，熔断不受直连失败影响。mihomo 没开、代理也不通时，熔断仍会在约一个
  连接超时后跳过 `bbs.hycdn.cn`，整张卡最多等 25s，有旧副本的照常出图。
- 部署后第一次启动会在后台把约 250MB 的旧库迁移到新表结构（这里 2.8s，按 7.7 倍推算 em 约 20s），
  期间读缓存不受影响，新图先排队、迁移完再落盘；日志会记一行
  `[http-disk] public-images-v1.sqlite3 upgraded to schema 2`。
