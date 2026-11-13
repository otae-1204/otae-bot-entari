# 运行状态与指令统计

两张卡片都用 help 同款外框和插画图库，渲染走 `otae_bot/infrastructure/rendering/art_card.py`。

| 命令 | 插件 | 说明 |
| --- | --- | --- |
| `/status`、`/状态` | `plugins/status` | Bot 运行状态。群聊出公开版；**只有超管私聊**才出详细版 |
| `/指令统计`、`/统计` | `plugins/usage_stats` | 指令调用统计，默认本群近 7 天；私聊默认全局 |

## `/统计` 的参数

参数顺序随意，可以组合：

| 参数 | 含义 |
| --- | --- |
| `全局`（`全部`、`global`、`all`） | 所有群和私聊合计，任何人都能查 |
| `30天`、`30d`、`30` | 统计天数，1–90，默认 7 |
| 插件名或别名（`终末地`、`q`、`radar`…） | 只看这个插件 |

卡片内容：总调用、活跃用户、日均、成功率；指令排行前 10（次数、占比、平均耗时、较上一期）；
按插件占比；每日调用；星期 × 小时热力图；用户排行。本群模式下用户排行是「最常用的群友」，
全局模式下是「使用量最高的用户」，显示 QQ 昵称（取最近一次记到的）；没记到昵称时只显示 QQ 号。

## `/status` 的公开版与详细版

| 内容 | 公开版 | 详细版 |
| --- | --- | --- |
| 今日指令、平均出图、成功率、插件数 | ✓ | ✓ |
| QQ 连接、图片渲染排队、响应延迟 | ✓（只说几个账号在线） | ✓（账号、群数、服务地址、渲染并发） |
| CPU、内存、磁盘 | ✓ | ✓，另有 bot 进程内存、缓存目录大小 |
| 今日按小时指令与最常用指令 | ✓ | ✓ |
| 插件列表 | 只列登记过中文名的功能插件 | 全部，管理插件标黄 |
| 后台任务（定时任务的周期、上次结果、错误） | ✗ | ✓ |
| 最近告警（最近 5 条 WARNING 及以上，只取首行，手机号打码） | ✗ | ✓ |
| 运行环境（版本、分支与提交、Python、框架版本、启动时间） | ✗ | ✓ |

管理插件指 `group_features.PROTECTED_PLUGINS` 加上 `peek`，见 `plugins/status/collect.py` 的 `ADMIN_PLUGINS`。
超管在群里发 `/status` 也只出公开版。

## 指令是怎么记的

`otae_bot/command_usage/recorder.py` 给每个插件订阅者挂一个 `CommandUsageRecorder`（安装方式同
`feature_gate.GroupFeatureGate`：`framework_hooks.attach_to_every_subscriber` 在加载插件前装一次，
之后注册的订阅者、热重载的插件自动带上）：

- 它排在 Entari 的 Alconna 解析（优先级 70）之后，只有上下文里有 `alc_result`、也就是指令真正匹配上时才记。
  B 站链接预览、私聊对话这类消息监听没有 `alc_result`，不计；本群关掉的插件在开关检查那一步就被拦下，也不计。
- 处理函数大多以 `session.stop()` 结束，它会抛 `STOP`，letoderea 因此跳过「执行后」的钩子。所以记录是在执行前
  往订阅者的退出栈上登记回调：正常返回和 `STOP` 记成功，其他异常记失败，取消不记。
- **只存指令头和登记过的子命令词，不存参数。** 插件用 `describe_command(names, display=..., subcommands=..., default_sub=...)`
  登记显示名和子命令表；第一个参数不在表里时记成 `default_sub`（终末地是「资料查询」），绝不原样落库。
  目前登记的有终末地（`/ef`）和明日方舟（`/ak`），其他指令按 Alconna 主名显示。
- 记录先放在内存里，每 5 秒（或攒满 200 条）在线程里批量写进 `data/command_usage/usage.sqlite3`，
  写失败只打日志，不影响指令。原始记录保留 90 天，每天清理一次。

运行状态用到的其他数据也都来自本进程：事件循环延迟（`loop_watchdog.loop_lag_snapshot`）、浏览器出图耗时
（`browser.render_timing_snapshot`）、定时任务结果（`timer.snapshot()`）、最近告警（`recent_alerts`），
CPU / 内存 / 磁盘用 `psutil` 和 `shutil.disk_usage`。不发网络请求。

## 预览

```
python scripts/preview/preview_usage_stats.py    # 合成数据：本群 7 天、全局 30 天、单插件、空数据
python scripts/preview/preview_status.py         # 本机真实采集 + 合成的今日指令、任务与告警：公开版、详细版
```

输出在 `output/usage-stats/` 和 `output/status/`，不连 QQ，也不碰 `data/` 下的真实统计库。
