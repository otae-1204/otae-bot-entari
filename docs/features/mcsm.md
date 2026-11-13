# MCSM 面板（plugins/mcsm）

把 MCSManager 面板绑定到群，在群里查看和操作服务器实例，也可以从闪传链接部署新实例。用户说明见 `/help mcsm`（见 [帮助](help_plugin.md)）。

## 命令

全部是 `/mcsm <子命令>`。回复多为图片，渲染失败时退回文字。

| 子命令 | 说明 | 权限 |
| --- | --- | --- |
| （无）、`list`/`列表` `[-a/--all/all/全部]` | 实例列表 | 所有人 |
| `status`/`info <别名>` | 实例状态；隐藏实例只有管理者能看 | 所有人 |
| `<别名或名称>` | 等同 `status` | 所有人 |
| `help`/`帮助` | 提示看 `/help mcsm` | 所有人 |
| `bind <面板地址>` | 记录面板地址，然后私聊索要 API Key（回复「取消」放弃） | 管理者 |
| `unbindpanel`/`解绑面板` | 解绑面板 | 管理者 |
| `bind <节点ID>` | 私聊发候选实例清单，回复序号或 `序号=别名` 批量绑定（15 分钟有效） | 管理者 |
| `bind <实例UUID> <别名> [节点ID]` | 直接绑定实例；不给节点时自动查找 | 管理者 |
| `unbind <别名>` | 从本群移除实例 | 管理者 |
| `delete`/`del`/`remove`/`删除 <别名> [--files]` | 删除面板上的实例 | 管理者 |
| `start`/`启动`/`open`、`stop`/`停止`、`restart`/`重启`、`kill`/`强制结束`/`强杀` `<别名>` | 实例操作 | 管理者 |
| `cmd`/`命令`/`exec <别名> <命令>` | 发送控制台命令 | 管理者 |
| `log`/`日志 <别名> [-a / -n 数量]` | 控制台日志，默认 10 条，上限 200 | 管理者 |
| `hide`/`隐藏`、`unhide`/`取消隐藏`/`显示` `<别名>` | 隐藏/显示实例 | 管理者 |
| `admin list` | 本群 MCSM 管理员 | 所有人 |
| `admin add`/`del <QQ号>` | 增删本群 MCSM 管理员（`del` 别名 `delete`、`remove`） | 管理者 |
| `deploy`/`部署 <别名> <闪传URL> [--port 端口] [--node 节点] [--image 镜像] [--cmd 启动命令] [--mem MB] [--dry-run]` | 部署新实例 | 管理者 |

管理者 = 超级用户、本群 MCSM 管理员、群主或群管理员（按成员角色判断）。`deploy`、`delete` 不在帮助图里。

## 配置

`plugins.mcsm.deploy_large_package_mb`：超过多少 MB 的压缩包改走远程直链安装，默认 200。面板地址和 API Key 不是配置，是各群用命令绑定的数据。

## 数据

`data/mcsm/bindings.json`：按群存面板地址、API Key（明文）、管理员、实例别名与隐藏状态。等待私聊 API Key 和批量绑定向导的状态只在内存里，重启即丢。

## 私聊

API Key 和批量绑定清单经 `deliver` 私聊发给执行命令的人，需要先加机器人好友。

## 已知问题

- `/mcsm admin add @成员`、`admin del @成员` 认不出 @：`_extract_at_users` 读 `target`/`user_id`（`handlers.py:163`），satori 的 `At` 只有 `id`；而 @ 在参数里变成 `<at id="…"/>` 文本，不是纯数字，结果回复「未指定目标成员」。用 QQ 号可以。
