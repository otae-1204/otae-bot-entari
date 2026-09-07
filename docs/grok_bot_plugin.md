# Grok Bot 应用接入

`grok_bot` 插件连接 Cursor Grok Bot 云端电脑的 HTTP 网关。Windows 机器人
通过 Tailscale 私网或 SSH 隧道访问该网关。使用的是登录账号里的 Grok Bot，
无需配置 xAI 模型 API Key。网关为未公开的内部接口，应用升级后可能需要适配。

## Windows 配置

在机器人项目根目录的 `.env` 中填写：

```dotenv
GROKBOT_GATEWAY_URL=http://100.x.x.x:1340
GROKBOT_GATEWAY_TOKEN=填写网关Token原文
GROKBOT_AGENT_ID=填写目标Bot的UUID
GROKBOT_TIMEOUT=300
GROKBOT_MAX_PENDING=8
```

网关 Token 来自 Grok Bot 云端电脑的 `/home/box/agent-data/gateway.json`
或 `/home/box/sand-data/gateway.json`。通过自己的安全文件传输方式保存到本地
配置，不要把 Token 发到 QQ、日志或 Git。它与 Cursor API Key、Tailscale 登录凭据不同。
基址只包含协议、主机和端口；`0.0.0.0` 是监听地址，应替换成云端电脑的
Tailscale IP 或 MagicDNS 名称。无需添加 `/v1` 或 `/api`。

网关请求固定直连，忽略 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 和 HYW 代理。
请求不会跟随重定向，也不会把网关错误正文发到 QQ。

两端加入同一个 Tailscale 私网后，在 Windows PowerShell 检查连通性：

```powershell
curl.exe --noproxy "*" --connect-timeout 10 "http://100.x.x.x:1340/health"
```

该检查无需 Token。填写 `.env` 后，在项目根目录运行完整只读检查：

```powershell
.\.venv\Scripts\python.exe -m plugins.grok_bot.check
```

完整检查验证健康状态、认证、目标 Bot 和任务状态接口；不会发送模型任务或输出密钥。
检查通过后重启机器人，发送 `/grok 你好` 进行真实问答测试。

## 使用与排队

- `/grok 问题` 或 `/grokbot 问题`：文字问答。
- 引用一条消息后发送 `/grok 问题`：包含引用正文；引用自动附带的 @ 由公共命令层处理。
- `/grok 帮助` 或 `/help grok`：查看说明。
- `/功能 关闭 grok`、`/功能 开启 grok`：SuperUser、本群管理员或群主管理当前群开关。

当前使用配置的同一个 Bot，沿用它的角色、会话和设置，**所有启用群和私聊共享
该 Bot 的上下文**。引用内容作为本次问题的一部分发送，不是按 QQ 用户隔离的历史。
同一用户可以连续提交问题；为避免打断同一 Bot，最多 8 条请求（含正在处理的）
依次运行。排队和进入处理后的等待分别受 `GROKBOT_TIMEOUT` 限制。
当前只支持文字；包含图片的提问会提示暂未接入。

网关的 `sendPrompt` 只返回接收确认。插件使用唯一 `clientNonce` 查询接收记录，
同时检查 Bot、后台任务和子任务状态。任务完成后，从本次请求标记之后读取回复，
忽略工具记录和流式预览；不会用名单中的 `lastMessagePreview` 作为答案。
如果桌面应用插入另一条输入，导致回答无法可靠对应，插件提示在应用中查看结果。

不要在同一 Bot 中同时安排其他自动化或手动对话，以免改变这段共享会话。
需要人工确认时，请在 Grok Bot 应用中处理，插件不会自动批准操作。
超时或网络中断不代表云端任务停止；插件不会重发问题、清空会话或终止现有任务。
下一个请求会等待远端已有工作结束。重启机器人会清除本地等待队列，云端工作可能继续。

## 云端连接恢复

云端使用 userspace networking 时，可在已登录的 Tailscale 上配置：

```bash
tailscale serve --bg --tcp=1340 tcp://127.0.0.1:1340
```

该配置只向私网转发，不启用公网 Funnel。`--bg` 保留 Serve 配置，但依赖
`tailscaled` 守护进程运行；仅通过 `nohup` 启动的进程不会自动跨机器重启恢复。
云端重启后应运行自己配置的启动脚本，并重新检查 Windows 到 `/health` 的连接。
云端更新可能替换自行安装的软件，建议保留安装及恢复脚本。

## 协议来源与验证范围

接口及字段参考 [grokbot-sdk](https://github.com/Adam91holt/grokbot-sdk/tree/c14347fa82d167b9a5984ec1baff56b2f074485a)
的网关类型定义和测试，Python 客户端直接使用现有 HTTPX，无需安装 Node.js。
涉及 `listAgents`、`getAsyncTasks`、`getSubagents`、`sendPrompt`、
`promptAcceptanceStatus`、`getAgentTranscriptTail`；这些接口均使用 POST，
`/health` 使用 GET。

自动测试模拟协议、排队、超时、旧回复过滤、认证错误和 Entari 命令/群开关。
真实 Windows 到云端的连通性及问答仍须按上述步骤在部署环境验证。

参考：[远程网关说明](https://github.com/Adam91holt/grokbot-sdk/blob/c14347fa82d167b9a5984ec1baff56b2f074485a/docs/remote.md)、
[Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)。
