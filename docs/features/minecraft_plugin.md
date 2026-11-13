# Minecraft 服务器（plugins/minecraft_plugin）

按群保存 Minecraft Java 服务器，查询状态与在线玩家，定时播报开关服和玩家进出，统计玩家在线时长。用户说明见 `/help mc`（见 [帮助](help_plugin.md)）。

## 命令

群聊和私聊都能用（私聊时以用户 ID 作为分组键）；无权限要求。

| 命令 | 别名 | 说明 |
| --- | --- | --- |
| `/ping [名称/昵称/地址]` | `/Ping`、`/PING`、`/p`、`/P` | 不带参数查本群全部已存服务器；参数不是已存服务器时按地址直接查 |
| `/pinglist` | `/Pinglist`、`/ping_list`、`/pl`、`/PL`、`/服务器列表` | 已存服务器列表（文字） |
| `/addserver <名称> <地址>` | `/Addserver`、`/add_server`、`/as`、`/添加服务器` | 保存服务器 |
| `/removeserver <名称>` | `/Removeserver`、`/remove_server`、`/rs`、`/delserver`、`/del_server`、`/ds`、`/删除服务器` | 删除 |
| `/addservernickname <名称> <昵称>` | `/asnn`、`/asn`、`/添加服务器昵称` | 追加昵称（可多个） |
| `/removeservernickname <名称>` | `/rsnn`、`/rsn`、`/删除服务器昵称` | 清空昵称 |
| `/updateservername <名称> <新名称>` | `/usn`、`/un`、`/editname`、`/修改服务器名称` 等 | 改名 |
| `/updateserveraddress <名称> <新地址>` | `/usa`、`/ua`、`/editaddress`、`/修改服务器地址` 等 | 改地址 |
| `/addbroadcast <名称> [地址]` | `/Addbroadcast`、`/add_broadcast`、`/ab`、`/添加播报服务器` | 加入播报；未保存的服务器要带地址 |
| `/removebroadcast <名称>` | `/Removebroadcast`、`/remove_broadcast`、`/rb`、`/删除播报服务器` | 移出播报 |
| `/updatebroadcastname <名称> <新名称>` | `/ubn`、`/ebn`、`/修改播报服务器名称` 等 | 改播报名 |
| `/updatebroadcastaddress <名称> <新地址>` | `/uba`、`/ub`、`/eba`、`/修改播报服务器地址` 等 | 改播报地址 |
| `/broadcastlist` | `/Broadcastlist`、`/broadcast_list`、`/bl`、`/BL`、`/播报服务器列表` | 播报列表和当前间隔 |
| `/broadcastinterval [时间 / reset]` | `/Broadcastinterval`、`/broadcast_interval`、`/bi`、`/BI`、`/播报间隔` | 查看或设置本群播报间隔，60 秒到 24 小时；`reset`/`default`/`默认`/`重置` 恢复默认 |
| `/online [服务器] [-a]` | `/Online`、`/ol`、`/在线`、`/排行榜`、`/rank` | 在线时长排行图，默认前 10，`-a` 全部 |

完整别名见 `plugins/minecraft_plugin/handlers.py:113-127`。

## 配置

`plugins.minecraft_plugin.broadcast_interval`：全局默认播报间隔秒数，默认 300；各群可用 `/broadcastinterval` 覆盖。

## 数据

`data/minecraft_plugin/data.json`（经 `Plugin_Data`）：按群、按地址存服务器名、昵称列表、是否播报、玩家累计在线时长。

## 定时播报

每 30 秒检查一次，只处理到达各自播报间隔的群。开服、关服、玩家加入/离开时经 `deliver` 发到群；发送失败的群退避重试，未发出的变化下次补发。异常摘要私聊发给 `basic.superusers` 的第一个；没配置时代码里写死了一个 QQ 号作为回退（`broadcast.py:34`）。

## 已知问题

- `/broadcastinterval 10`（纯数字）被拒，回复「时间格式错误」，虽然提示说纯数字按分钟：正则不带单位时 `group(2)` 是 `None`，而代码只认 `""`（`handlers.py:93-107`）。`10m`、`600s` 可用。
- `/pinglist` 把昵称按 Python 列表原样显示，如 `生存服 (['主服'])`：昵称存的是列表，代码直接拼进字符串（`handlers.py:330-332`）。
- 各命令格式提示里的 `<…>` 被当成消息元素解析，尖括号内容丢失（原因见 [帮助](help_plugin.md) 的已知问题）。
