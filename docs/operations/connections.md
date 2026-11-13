# 连接

bot 内部只认 Satori。账号有两种接法，可以混用，但同一个 QQ 号只能用一种，否则每条事件会收到两份。

| | 原生 Satori | OneBot 11 边界 |
|---|---|---|
| 配置 | `basic.network` | `plugins.server.adapters` |
| 谁连谁 | bot 作为 WebSocket 客户端连对端的 Satori 服务 | bot 作为 WebSocket 客户端连 LLBot 的 OneBot 11 正向 WebSocket 服务 |
| 适用 | LLBot 原生 Satori、Ir 面板等 Satori 服务端 | LLBot（QQ） |
| 平台名 | 对端上报的，LLBot 原生 Satori 为 `llonebot` | `qq-client`（satori-python 的 OneBot 11 适配器） |
| 令牌 | `.env` `SATORI_TOKEN_n` | `.env` `ONEBOT_TOKEN_n` |

平台名不同，按平台名存的数据（群功能开关、指令统计、订阅等）在切换接法后不再命中，需要迁移，见 [切换到 v2](cutover.md) 第 8 步。

## 原生 Satori：`basic.network`

每条是一个 WebSocket 连接，一个连接可以带多个账号（对端在同一端点暴露几个登录，就只需一条）。

```yaml
basic:
  network:
    - type: ws
      host: "127.0.0.1"
      port: 5500
      path: ""
      token: "${{ env.SATORI_TOKEN_1 :- }}"
    - type: ws                 # 远程主机走 wss
      host: "bot.example.com"
      port: 443
      path: "/satori"
      secure: true
      token: "${{ env.SATORI_TOKEN_2 :- }}"
```

- `secure: true` 时用 `wss://` / `https://`，默认 `false`（satori-python `WebsocketsInfo`）。Ir 面板必须开（现场记录）。
- 同一台机器上的多个 LLBot 实例各用自己的 Satori 端口。
- 只用 OneBot 接入时把 `network` 改成 `[]`。

## OneBot 11 边界：`plugins.server`

```yaml
plugins:
  server:
    adapters:
      - endpoint: "ws://127.0.0.1:3001"
        access_token: "${{ env.ONEBOT_TOKEN_1 :- }}"
      - endpoint: "ws://127.0.0.1:3002"
        access_token: "${{ env.ONEBOT_TOKEN_2 :- }}"
```

- 每个 LLBot 实例一条。`$path` 可省略，默认 `otae_bot.boundary.onebot11:OneBotBoundaryAdapter`。
- 由 bot 进程内的 entari-plugin-server 以 direct-adapter 模式加载（`otae_bot/application.py`、`otae_bot/boundary/__init__.py`）。bot 主动连 `endpoint`，即 LLBot 一侧是服务端。
- 声明了 `plugins.server` 但插件加载失败时，bot 直接报错退出（`RuntimeError`），不会无声地少一个账号。

LLBot 每个实例要开的（LLBot 侧的配置项名称来自现场，代码里核对不到）：

- OneBot 11 的正向 WebSocket 服务（LLBot 的 `ob11` 连接里 `type: ws` 的一项），启用；
- 监听 `127.0.0.1`，每个实例一个不同端口，与 `endpoint` 一致；
- 设令牌，写进 `.env` 的 `ONEBOT_TOKEN_n`。

### 进程内服务的监听地址

entari-plugin-server 一定会起一个 uvicorn 服务，bot 自己不连它。边界层强制：

- `host: 127.0.0.1`，令牌每次启动随机生成，access log 关闭；
- Linux 上改为 unix socket `.runtime/satori-server/satori.sock`（目录权限 0700），不占 TCP 端口；
- Windows 上监听 TCP `127.0.0.1:5140`（entari-plugin-server 的默认端口；现场实测同此）。端口被占用时可在 `plugins.server` 下写 `port:` 改（未实测）。该服务的 HTTP 接口不校验令牌，只靠回环地址隔离。

### 相关设置

- `otae.media.shared_filesystem`：LLBot 与 bot 在同一台机器、同一用户运行时设 `true`，图片以 `file://` 路径交给 LLBot，省去 base64。默认 `false`。

## 账号与发送

- 每个在线账号按（平台，账号 ID）登记（`otae_bot/accounts/runtime.py`），上线后取一次 `guild.list` 记下它在哪些群，15 分钟内复用，发送失败后再刷新。
- 指令回复走收到消息的那个账号。
- 主动消息（推送、定时）走 `otae_bot.delivery.deliver`：群消息选一个 `guild.list` 里有该群的在线账号（订阅记录的账号在群里时优先）；报「不在群」类错误时把该群从这个账号的列表里去掉并刷新。私聊优先记录的账号，其次同平台最近上线的账号，平台未知时依次尝试所有在线账号。
- 新接法下的私聊（指令与推送）：未实测。
