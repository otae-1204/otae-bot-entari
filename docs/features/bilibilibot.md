# B 站订阅（plugins/bilibilibot）

订阅 B 站 UP 主的直播、视频、动态，有更新时推送图片卡片到群或私聊；消息中出现 B 站链接时自动回复预览卡片。

## 指令

根指令 `/bili`（无别名）。群聊、私聊均可，无权限限制。群里订阅归属本群，私聊订阅归属发送者。

| 指令 | 说明 |
|---|---|
| `/bili`、`/bili help` | 文字帮助 |
| `/bili follow <类型> <目标> [更多目标]` | 关注 |
| `/bili unfollow <类型> <目标> [更多目标]` | 取消关注 |
| `/bili list [类型]` | 查看本群 / 本人的订阅；不写类型列出全部 |
| `/bili refresh <类型> <目标> [更多目标]` | 立即刷新目标状态 |

- 类型：`all` `live` `video` `dynamic`（`all` = 三者）。
- 目标：纯数字或 `uid:<数字>` 视为 UID；`room:<直播间号>` 或直播间链接（`live.bilibili.com/...`）视为直播间，只对 `live` / `all` 有效；`space.bilibili.com/<UID>` 链接视为 UID。

## 链接预览

非指令消息中含 `bilibili.com`、`b23.tv` 或 `BV` 时，解析其中的视频（BV 号、`b23.tv` 短链）或直播间链接，回复一张卡片；解析失败静默跳过。

## 配置

`entari.yml` 的 `plugins: plugins.bilibilibot:` 段（`plugins/bilibilibot/config.py`）。

| 键 | 默认 | 说明 |
|---|---|---|
| `sessdata` | 空 | Cookie SESSDATA（密钥，`.env` 的 `BILI_SESSDATA`） |
| `buvid3` | 空 | Cookie buvid3（密钥，`BILI_BUVID3`） |
| `dm_img_list` / `dm_img_str` / `dm_cover_img_str` | 空 | 风控参数（密钥，`BILI_DM_IMG_LIST` / `BILI_DM_IMG_STR` / `BILI_DM_COVER_IMG_STR`） |
| `rsshub_base_urls` | `["https://rsshub.app", "https://rss.materium.io"]` | RSSHub 实例列表（YAML 列表），配置项在前，再接内置备用实例，去重后使用 |
| `lottery_filter_uids` | `"*"` | 动态订阅跳过社交抽奖的账号：`*` / `all` = 全部；逗号分隔的 UID 收窄；留空关闭 |
| `live_batch` | true | 直播状态走批量接口；false 改回逐个房间查询（回滚开关） |

密钥在 `entari.yml` 中写成 `"${{ env.BILI_SESSDATA :- }}"` 形式引用 `.env`。

## 数据

`data/bilibilibot/bilibili.db`（SQLite）：目标、订阅、已见条目、待发送队列（outbox）。旧库 `plugins/bilibilibot/bilibili_2.db` 存在时在首次打开时迁移一次（`meta.legacy_migrated` 标记）。

## 推送与定时任务

| 任务 ID | 周期 | 内容 |
|---|---|---|
| `bili_live_check` | 1 分钟 | 直播状态 |
| `bili_video_check` | 2 分钟 | 新视频 |
| `bili_dynamic_check` | 1 分钟 | 新动态 |
| `bili_wbi_refresh` | 1 小时 | 刷新 WBI 签名密钥 |
| `bili_risk_cookie_refresh` | 6 小时 | 刷新风控 Cookie |

- 检测到的更新写入 outbox，由 `Notifier` 逐条渲染并发送；启动时先发完上次遗留的队列。单条失败按指数退避重试，累计 4 次失败后丢弃；无在线账号（`SenderUnavailable`）不计次数。
- 视频获取失败时依次退到 RSSHub、动态流中的视频；动态获取失败时退到 RSSHub（终末地官号改用 RSS 发现后回查详情）。RSSHub 实例并发竞速，取最先成功的。
- 普通 UP 的动态 feed 偶尔只返回空壳条目（无正文、无封面）。feed 接口本身保持原样返回，空壳补全由轮询器在即将检测的选定窗口内单独回查 detail：参与抽奖过滤的账号看整页（通常最多 10 条），其余普通账号看前 5 条；只处理发布时间晚于订阅基线、且确实可补全的条目（跳过直播卡片、直播推荐、common/music/PGC 分享卡片、ADDITIONAL_TYPE_LIVE，以及原始动态缺失/为空/不可渲染的转发），回查条数上限始终覆盖选定窗口。10 秒预算只是详情请求的总时间上限：详情确认不可见（已删除/被隐藏）时保留占位并缓存该结果 30 分钟；超时、风控等临时失败或预算用完时整个动态轮次软延后——不推进游标、不写已读、不推送，下一轮重试同一窗口。软延后带上本轮已补全（含确认不可见与本轮放行的条目）的条数与 id：只要有进展，下一轮就按原周期立即重试，成功回查的详情缓存 5 分钟、下一轮不再花时间，慢窗口也会逐步收敛；若照旧套用失败退避，120/240/480 秒的间隔会超过 5 分钟缓存，已缓存的成功过期后窗口永远补不完。只有连续 3 轮完全没有进展的软延后才改走失败退避并只记一次警告，避免窗口永远放不下时每轮 debug 级重试。同一条目连续 3 次回查失败后会放行其占位卡（记作本轮进展），已放行的条目在同一窗口内不再回查；这样单个永远补不出的条目不会把该 UP 后续所有动态挡在游标之后，代价是该条动态只推送一条无正文的占位卡。补全后的条目沿用 feed 的发布时间：详情缺失或回退了 `pub_ts` 时回填 feed 时间，避免检测静默跳过。订阅基线（`latest_dynamic`）与视频回退（`dynamic_latest_video`）都只读原始 feed，从不回查动态详情（视频回退仍可能补全视频卡片本身）。官号动态走独立详情缓存与采集路径，不做这项回查。
- 动态订阅首次建立基线时取当前页发布时间最新的一条，订阅前的历史动态不会补发。
- 同一 UP 同时订阅了视频或直播时，对应的视频动态、直播推荐动态只标记已读、不重复推送（开播卡已经推开播，动态副本不再重复）。直播这条只认顶层「直播推荐动态」卡片（`DYNAMIC_TYPE_LIVE_RCMD` / `MAJOR_TYPE_LIVE_RCMD`）：转发别人的直播推荐仍按普通动态推送，正文里贴直播间链接的普通动态、带 `live_rcmd` 载荷的普通直播卡也都不参与去重。订阅直播间号与推荐卡直播间号都已知且不同时，视为推荐的是别的直播间、照常推送；同房间或任一侧未知（含直播目标行缺失、room_id 未采到）则按重复跳过。只订阅动态、没订阅直播的 UP 不参与这条去重，推荐卡仍按普通动态推送。跳过按 UP 全局生效、与视频动态一致，不按订阅者分别判断。
- 开播卡片之后另发一条直播间链接文字，与图片同一账号。
- 发送走 `otae_bot.delivery.deliver`，账号选择：
  - 群：候选为群列表中含该群的在线账号（`get_bot()` 返回的最近登录账号排在前面），其次是群列表未知的账号；某账号被判定「不在该群」时，同一次尝试内换下一个账号，不消耗重试次数。没有任何在线账号在该群时记错误并按失败重试。
  - 私聊：不指定账号，由 `deliver` 依次尝试在线账号。
- 群推送受群功能开关约束（`deliver` 内的 `allow_group_delivery`）。

## 详细文档

- [bilibili_cards.md](bilibili_cards.md)：视频、直播卡片样式与预览脚本
- [bilibili_endfield_lottery_filter.md](bilibili_endfield_lottery_filter.md)：抽奖动态过滤规则
- [../operations/rsshub_selfhost.md](../operations/rsshub_selfhost.md)：自建 RSSHub

## 已知问题

- `plugins/bilibilibot/api/rsshub.py` 的注释仍写「请用 BILI_RSSHUB_BASE_URLS」，现行配置为 `plugins.bilibilibot.rsshub_base_urls`。
- 多账号下群推送与私聊推送的账号选择未实测。
