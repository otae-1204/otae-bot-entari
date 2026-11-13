# Steam（plugins/steamInfo）

群成员绑定 Steam 账号后，可查个人主页、本群好友状态和某款游戏的群内时长；后台轮询好友在玩的游戏并在群里播报。用户说明见 `/help steam`（见 [帮助](help_plugin.md)）。

## 命令

除特别说明外只能在群聊或频道中用，无权限要求。

| 命令 | 别名 | 说明 |
| --- | --- | --- |
| `/steambind <Steam ID 或好友代码>` | `/绑定steam` | 绑定或更新自己的账号（只接受数字） |
| `/steamunbind` | `/解绑steam` | 解绑 |
| `/steaminfo [@成员 / Steam ID / 好友代码]` | `/steam信息` | 个人主页图；不带参数查自己 |
| `/steamcheck` | `/查看steam`、`/查steam` | 本群已绑定成员的在线状态图；带参数时不回复 |
| `/steamgame <游戏名或 AppID>` | `/steam游戏`、`/steam时长` | 本群成员在该游戏的时长、成就；需要 Steam API Key |
| `/steamnickname <昵称>` | `/steam昵称` | 设置自己在播报里的昵称 |
| `/steamenable` | `/启用steam` | 开启本群播报 |
| `/steamdisable` | `/禁用steam` | 停用本群播报 |
| `/steamupdate <群名> + 图片` | `/更新群信息`、`/steam_update_parent` | 设置播报图里的群名和头像，两者都要有 |
| `/steamcache ...` | `/steam缓存` | 仅超级用户：`refresh`；`alias list/set/del`；`lookup list/set/del`；`ambiguous list/del` |
| `/steamhelp` | `/steam帮助` | 见已知问题 |

## 配置

`plugins.steamInfo`：

| 键 | 说明 |
| --- | --- |
| `api_keys` | 多个 Steam Web API Key，逗号分隔轮换（密钥，`.env` 的 `STEAM_API_KEYS`） |
| `api_key` | 单个 Key，`api_keys` 为空时用（密钥，`STEAM_API_KEY`） |
| `request_interval` | 播报轮询间隔秒数，默认 120 |
| `disable_broadcast_on_startup` | 为 true 时只跳过启动后的首次刷新；定时轮询照常注册 |
| `llm_api_key` / `llm_base_url` / `llm_model` | `/steamgame` 解析游戏名时用的 LLM（`llm_api_key` 是密钥，`STEAM_LLM_API_KEY`） |

代理用进程的 `HTTP(S)_PROXY`。

## 数据

`data/steam_info_entari/`：`bind_data.json`（绑定）、`steam_info.json`（上次状态）、`parent_data.json`（群名与头像）、`disable_parent_data.json`（停用播报的群）、`cache/`（头像、应用列表等缓存）。启动时会从旧目录 `data/steam_info` 补回缺失记录。

## 定时播报

按 `request_interval` 轮询各群绑定账号，有人开始、停止或切换游戏时经 `otae_bot.delivery.deliver` 发到该群（文字加开始游戏卡片）。没有在群账号时下次补发；发送失败且判定为不可发送时自动把该群加入停用列表。

## 已知问题

- `/steamhelp`、`/steam帮助` 没有回复：处理函数用了未定义的 `__plugin_meta__`（`handlers.py:816`），抛 NameError。
- `/steaminfo @成员` 没有回复：代码读 `At.target`（`handlers.py:985`），satori 的 `At` 只有 `id`，抛 AttributeError。
- `/steamgame`、`/steamnickname`、`/steamcache` 的格式提示和 `/steaminfo` 的未绑定提示里 `<…>` 被当成消息元素解析，尖括号内容丢失（原因见 [帮助](help_plugin.md) 的已知问题）。
