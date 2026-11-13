# Tibo 雷达（plugins/tibo_radar）

采集 codexradar.com 与 codex-reset.com 的公开数据，跟踪 Codex 额度重置事件和 Tibo 在 X 上的相关动态，提供查询卡片和群订阅推送。

## 指令

根指令 `/tibo`，别名 `/雷达`。子命令另有英文别名（括号内）。

| 指令 | 说明 | 渲染 | 场景 / 权限 |
|---|---|---|---|
| `/tibo`（`总览` `概览` `overview`） | 综合看板：当前状态、最近已确认重置、PT 时区、历史统计、最新 3 条动态、来源健康 | `draw_scope.render_card` | 群聊、私聊 |
| `/tibo 动态 [数量]`（`最新`） | 近期相关 X 动态（原文、翻译、解读），默认 6，1–20，超过 6 条按 6 条分页 | `draw_xfeed.render_xfeed` | 群聊、私聊 |
| `/tibo 状态`（`reset` `重置`） | 当前重置预告、窗口期或疑似信号 | `draw_scope.render_card` | 群聊、私聊 |
| `/tibo 最近`（`last`） | 最近一次严格核验完成的重置 | `draw_scope.render_card` | 群聊、私聊 |
| `/tibo 历史 [数量]`（`history`） | 重置事件列表，默认 6，1–20，每页 3 条 | `draw_scope.render_card` | 群聊、私聊 |
| `/tibo 订阅`（`subscribe` `sub`） | 开启本群新帖推送 | — | 仅群聊；群主、管理员或 SUPERUSER |
| `/tibo 取消订阅`（`unsubscribe` `unsub` `退订`） | 关闭本群新帖推送 | — | 仅群聊；群主、管理员或 SUPERUSER |
| `/tibo 订阅状态`（`订阅 状态` `subscription` `substatus` `subscriptions`） | 本群订阅、最近推送时间、投递方式、连续失败次数 | — | 仅群聊 |
| `/tibo 帮助`（`help` `?`） | 文字帮助 | — | 群聊、私聊 |

图片卡片后附「来源链接」文字。查询类指令只读本地缓存，不触发采集。

## 配置

`entari.yml` 的 `plugins: plugins.tibo_radar:` 段（`plugins/tibo_radar/config.py`），全部可留空，无密钥。

| 键 | 默认 | 说明 |
|---|---|---|
| `refresh_minutes` | 10 | 采集周期分钟数（2–1440） |
| `timeout_seconds` | 15 | 单次请求超时秒数（3–60） |
| `codexradar_enabled` | true | 是否采集 codexradar.com |
| `subscription_batch` | 20 | 群订阅每次最多推送条数（1–100） |
| `subscription_max_pages` | 1 | 群订阅每次最多推送页数（1–10） |
| `codexradar_url` | `https://codexradar.com/` | codexradar 页面地址 |
| `feed_url` | `https://codex-reset.com/api/feed?locale=zh` | 动态 feed 接口 |
| `timeline_url` | `https://codex-reset.com/api/timeline?locale=zh` | 重置时间线接口 |

数值超出范围时按区间截断。

## 数据

`data/tibo_radar/tibo_radar.db`（SQLite）：动态、重置事件、来源状态、群订阅（推送游标、绑定账号、失败次数与退避）。

## 推送与定时任务

- 定时任务 `tibo_radar_refresh` 每 `refresh_minutes` 分钟采集一次；首个账号就绪时先采集一次。采集失败不影响已入库动态的重试推送。
- 每次采集后按群检查订阅，把游标之后的新帖按每页 3 条渲染为 X 动态卡（`render_xfeed`）推送；单次最多 `min(subscription_batch, 3 × subscription_max_pages)` 条。
- 新开启的订阅先在下一次成功采集时建立基线，不补发历史动态。
- 发送经 `plugins/tibo_radar/delivery.py` 的 `deliver_page` 调用 `otae_bot.delivery.deliver`，显式指定账号：
  - 订阅时记录收到 `/tibo 订阅` 的账号，之后固定由它推送；该账号不在线时本群推送暂缓，不换账号。
  - 未绑定账号的订阅用 `otae_bot.delivery.resolve_account` 选择：优先本轮的账号（需在该群群列表中），否则选另一个在线且在该群的账号；都没有则跳过本轮。成功推送后绑定该账号。
- 图片渲染或发送失败时降级为纯文本摘要；文本也失败或没有消息回执时不推进游标，记录失败并退避重试。

## 详细文档

- [tibo_radar_x_usage.md](tibo_radar_x_usage.md)：draw_xfeed / draw_scope 渲染器与预览脚本
- [tibo_radar_draw_v2_usage.md](tibo_radar_draw_v2_usage.md)：备用浅色渲染器 draw_v2（未接入）

## 已知问题

- `draw_scope.render_scope` 与 `draw_v2.py` 未被插件调用，只供预览脚本使用。
- 订阅推送在实际群内的表现未实测。
