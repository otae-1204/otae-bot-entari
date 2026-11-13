# 群内功能管理

`group_manager` 随机器人自动加载，无需额外配置。更新代码并重启后即可使用。
按整个插件控制。所有插件默认开启。

在需要管理的群内发送：

```text
/功能 列表
/功能 关闭 hyw
/功能 开启 hyw
/功能 关闭 ef
/功能 开启 steam
```

`/功能` 查看用法，也支持 `/插件`、`/plugin`。插件名大小写不敏感，
可使用 `/功能 列表` 中的完整插件名，或者以下别名：

| 插件 | 别名（含显示名，见 `otae_bot/groups/features.py` 的 `PLUGIN_NAMES`） |
| --- | --- |
| `McModQuery` | `MC 百科`、`mod`、`模组`、`mcmod` |
| `McWikiQuery` | `MC Wiki`、`wiki` |
| `arknights` | `明日方舟`、`ak`、`arknights`、`森空岛` |
| `bilibilibot` | `B站`、`bili`、`bilibili` |
| `changelog` | `更新日志`、`changelog`、`版本` |
| `endfield` | `终末地`、`ef`、`zmd` |
| `forkout` | `叉出去`、`fork` |
| `help_plugin` | `帮助`、`help` |
| `hyw` | `HYW 问答`、`q`、`何意味` |
| `mcsm` | `MCSManager` |
| `minecraft_plugin` | `Minecraft`、`mc`、`我的世界` |
| `peek` | `窥视` |
| `radar` | `AI 智商雷达`、`radar`、`智商雷达` |
| `status` | `运行状态`、`status`、`状态` |
| `steamInfo` | `Steam`、`steam` |
| `tibo_radar` | `Tibo 雷达`、`tibo`、`雷达` |
| `usage_stats` | `指令统计`、`统计`、`usage` |

只有 `entari.yml` 的 `basic.superusers` 中列出的用户、本群管理员或群主可以执行管理命令，
包括查看状态。
普通成员无法修改开关，SuperUser 也必须在目标群内执行。
命令不接受群号，不能通过私聊或另一个群修改目标群的配置。
管理员信息优先读取本次事件的 Satori 成员角色（`owner` / `admin`），缺失时通过当前机器人账号的
Satori 接口 `guild.member.get` 查询；查询失败或超时（5 秒）时拒绝操作。

关闭一个插件后，该插件在当前群的命令、消息自动解析等监听不再触发，
订阅推送也不会向该群发送。其他插件、其他群及私聊继续使用各自的设置。
已开始处理的请求可能完成回复；开关不会中止已经运行的请求。
群开关不会删除订阅、解绑账号或清空插件数据。后台轮询仍服务其他群，
重新开启后恢复后续处理，不保证补发关闭期间的推送。

开关按「平台 + 机器人账号 + 群号」独立保存到
`data/group_manager/switches.json`，立即生效，重启后保留。无需修改配置文件。
多机器人接入时，只修改接收命令的机器人在本群的设置。
版本 2 文件同时保存显式开启和关闭项，自动读取旧版关闭列表，关闭状态保留。
如需手动编辑开关文件，请先停止机器人；格式错误会拒绝读取和覆盖，
需修复文件后重启。数据文件应随运行数据一起备份。

`group_manager` 始终可用，以便重新开启已关闭的插件；负责好友及入群审批的
全局 `request_handler` 不提供群开关。插件原有的权限、白名单和订阅配置仍生效，
开启插件不会绕过这些限制。系统启动、清理及全局任务不受群开关影响。
