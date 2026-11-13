# 窥视（plugins/peek）

向群里登记的远程 HTTP 服务请求 `<地址>/screenshot`，把返回的桌面截图发到群里。远程服务需自行部署，不在本仓库。

## 命令

| 命令 | 别名 | 说明 | 权限 |
| --- | --- | --- | --- |
| `/peek [昵称]` | `/窥视` | 取截图；不带昵称用本群默认目标 | 本群须在白名单，否则不回复 |
| `/add_whitelist <群号>` | `/添加白名单`、`/add_whitelist_group` | 把群加入白名单 | 超级用户 |
| `/add_peek <昵称> <http地址>` | `/添加窥视` | 登记目标（先试连 `/screenshot`）；本群第一个目标自动设为默认 | 超级用户，群内 |
| `/del_peek <昵称>` | `/删除窥视` | 删除目标 | 超级用户，群内 |
| `/set_default_peek <昵称>` | `/设置默认窥视` | 设默认目标 | 超级用户，群内 |

地址必须以 `http://` 或 `https://` 开头。截图请求超时 5 秒。

## 配置

无。超级用户取 `basic.superusers`。

## 数据

`data/peek/state.json`：`white_list`（群号列表）与各群的 `peek_list`、`def_peek_path`。旧的 `configs/peek/config.json` 由 `scripts/convert_config.py` 迁移。

## 已知问题

- `/add_peek`、`/set_default_peek` 的格式提示里 `<…>` 被当成消息元素解析，尖括号内容丢失（见 [帮助](help_plugin.md) 的已知问题）。
