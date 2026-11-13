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
