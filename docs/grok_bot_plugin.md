# Grok Bot 应用接入

`grok_bot` 插件连接 Cursor Grok Bot 云端电脑的 HTTP 网关。Windows 机器人
通过 Tailscale 私网或 SSH 隧道访问该网关。使用登录账号里的 Grok Bot，
无需配置 xAI 模型 API Key。网关为未公开的内部接口，应用升级后可能需要适配。

## 开启与使用

**默认关闭，包括升级后的已有群。只有 SuperUser 可以手动开启。**
先完成下方连接配置，重启机器人，再由 `.env` 的 `SUPERUSERS` 中的用户
在需要使用的群执行：

```text
/功能 开启 grok
/grok 你好，多惠
```

- `/功能 列表`：查看本群插件状态，Grok Bot 会注明仅 SuperUser 可开启。
- `/功能 关闭 grok`：SuperUser、本群管理员或群主可关闭；管理员和群主不能重新开启。
- `/grok 问题` 或 `/grokbot 问题`：文字问答。开启后，本群成员均可使用。
- 引用消息后发送 `/grok 问题`：附上引用正文；引用自带的 @ 由公共命令层处理。
- `/help grok`：未开启时也可查看接入说明；开启后可用 `/grok 帮助`。
- 私聊也默认关闭。SuperUser 可以在自己的私聊执行 `/grok 开启`、`/grok 关闭`。
  普通用户不能自行开启；授权一个群不会开启成员的私聊。

开关仅影响接收命令的机器人在当前会话的使用，保存到
`data/group_manager/switches.json`，重启后保留。开关命令不会创建云端 Bot，
首次实际提问时才创建。关闭后，已排队但未处理的问题不再发送；
已提交的云端任务可能继续运行并完成回复。

## Windows 配置

在机器人项目根目录的 `.env` 中填写：

```dotenv
GROKBOT_GATEWAY_URL=http://100.x.x.x:1340
GROKBOT_GATEWAY_TOKEN=填写网关Token原文
GROKBOT_TIMEOUT=300
GROKBOT_MAX_PENDING=8
GROKBOT_MAX_CONCURRENT=4
GROKBOT_PERSONA_FILE=
# 可选：原 QQBOT UUID，仅供只读诊断，问答不再发送到这个 Bot
GROKBOT_AGENT_ID=
```

网关 Token 来自 Grok Bot 云端电脑的 `/home/box/agent-data/gateway.json`
或 `/home/box/sand-data/gateway.json`。通过自己的安全文件传输方式保存到本地
配置，不要把 Token 发到 QQ、日志或 Git。它与 Cursor API Key、Tailscale 登录凭据不同。
基址只包含协议、主机和端口；`0.0.0.0` 是监听地址，应替换为云端电脑的
Tailscale IP 或 MagicDNS 名称。无需添加 `/v1` 或 `/api`。

网关请求固定直连，忽略 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 和 HYW 代理。
请求不会跟随重定向，也不会把网关错误正文发到 QQ。

两端加入同一个 Tailscale 私网后，在 Windows PowerShell 检查连通性：

```powershell
curl.exe --noproxy "*" --connect-timeout 10 "http://100.x.x.x:1340/health"
```

该检查无需 Token。填写 `.env` 后，在项目根目录运行只读检查：

```powershell
.\.venv\Scripts\python.exe -m plugins.grok_bot.check
```

检查验证网关健康、认证、Bot 列表与本地人设模板；若填写 `GROKBOT_AGENT_ID`，
也检查这个参考 Bot 的任务状态。不会创建 Bot、发送模型任务或输出密钥。
检查通过后，由 SuperUser 开启目标群，再进行实际问答测试。

## 会话与并发

每个会话对应一个新建的云端 Bot：

| QQ 场景 | 会话范围 |
| --- | --- |
| 同一个群的不同成员 | 共用本群对话，发言者 ID 随问题发送 |
| 不同群 | 各自独立 |
| 同一用户在群内与私聊 | 各自独立 |
| 不同用户的私聊 | 各自独立，分别授权 |
| 不同机器人账号或平台 | 各自独立 |
| 有多个频道的平台 | 群下不同频道分别独立，开关仍按群控制 |

新 Bot 名称类似 `多惠·群100·1234abcd`，使用本地人设创建。
**原 QQBOT 和它的历史记录会保留，但不再用于新问答。** 不复制旧 Bot 的
历史、记忆或自动化，也不会在独立 Bot 出错时退回共享 Bot。

会话映射和安装标识保存到 `data/grok_bot/sessions.json`；云端保存对应 Bot 的
对话历史。请同时保留本地数据和云端 Bot。迁移 Windows 部署时需复制完整的
`data/grok_bot` 与 `data/group_manager`，不要把同一套数据同时运行在多个机器人进程。
不要删除映射文件来清理队列，否则会丢失原会话的对应关系。

同一用户可以连续提交多条问题；**同会话依次处理，不同会话默认最多 4 个同时处理**。
`GROKBOT_MAX_PENDING` 限制全部已接收的问题（含运行和排队），默认 8，范围 1～32。
`GROKBOT_MAX_CONCURRENT` 限制同时处理的会话数，范围 1～16。
同群的排队请求不会占满其他会话的执行名额。排队最多等待 `GROKBOT_TIMEOUT` 秒；
进入处理后，创建 Bot、同步人设和等待回答合计再受同样的时限约束。
默认各 300 秒，配置范围 30～1800。

每个独立会话会占用一个云端 Bot 名额，仍受账号的 Bot 数量限制和订阅额度限制。
对话隔离不等于电脑隔离：同一账号的 Bots 仍共享云端电脑的文件系统、浏览器和工具连接。
人设要求不访问其他会话的记录，但它不构成文件系统权限隔离。
见 [Cursor Grok Bot 工作方式](https://cursor.com/docs/grok-bot/work)。

## 花园多惠人设

默认模板：[plugins/grok_bot/persona.md](../plugins/grok_bot/persona.md)。
以花园多惠的音乐热情、天然随性的思考方式和对兔子的喜爱为基底，使用中文。
闲聊简短温和，偶尔自然跳脱；技术问题先讲清事实与步骤，不为角色效果编造答案。
没有固定猫娘口癖，不把每个话题都转到吉他，也不硬编码搜索服务故障。
模板中的对话示例为原创。

音乐和性格参考官方的[角色介绍](https://bang-dream.bushimo.jp/collabo/railgun_t/)，
QQ 回答习惯和会话约束是本项目的角色设计。

可以直接编辑默认模板，也可以用 `GROKBOT_PERSONA_FILE` 指向自定义 UTF-8 文本，
支持 Windows UTF-8 BOM；相对路径以机器人项目根目录为基准，内容须为 1～20000 字。
编辑模板内容后，**每个会话在下次提问前同步人设**，保留对话和 Bot 的名称、头像设置。
修改环境变量中的文件路径或并发配置后建议重启机器人。
不要在云端单独修改受管 Bot 的描述作为长期配置，它会被本地模板同步覆盖。

## 异常与恢复

插件用唯一请求编号定位回答，检查接收记录、Bot 状态、后台任务和子任务，
只读取当前问题之后的完整回复，忽略旧答案、工具记录和流式预览。
不要同时在受管 Bot 中安排自动化或手动对话；若另一条输入插入当前问题后，
插件会停止匹配答案并提示在应用中查看。需要人工确认时，也需在应用中处理。

超时或网络中断不代表云端任务停止。插件不会重发问题或终止现有任务；
下个请求先等待该会话的远端工作结束。重启机器人清除本地等待队列，云端工作可能继续。

创建 Bot 前会保存待确认记录。创建响应丢失或映射写入失败时，下次请求根据
安装标识和会话标记找回同一个 Bot，不依赖显示名称。如果仍无法确认创建结果，
会停止重复创建；Bot 不存在、映射冲突或文件损坏也会停止发送。
遇到这类错误，管理员应先检查云端 Bot 列表和 `sessions.json`，保留备份。
只有确认创建失败且云端不存在对应 Bot 后，才可在停机状态删除该会话的
`agent_id: null` 待确认项并重启重试。不要删除整个文件或随意填入其他会话的 ID。
若云端 Bot 被手动删除，确认接受丢失该会话历史后，停机删除其对应绑定项，
重启后会在下次提问时创建新的独立 Bot。

## 云端连接恢复

云端使用 userspace networking 时，可在已登录的 Tailscale 上配置：

```bash
tailscale serve --bg --tcp=1340 tcp://127.0.0.1:1340
```

该配置只向私网转发，不启用公网 Funnel。`--bg` 保留 Serve 配置，但依赖
`tailscaled` 守护进程运行；仅通过 `nohup` 启动的进程不会自动跨机器重启恢复。
云端重启后运行自己配置的启动脚本，再检查 Windows 到 `/health` 的连接。

## 协议来源与验证范围

接口及字段参考 [grokbot-sdk c14347f](https://github.com/Adam91holt/grokbot-sdk/tree/c14347fa82d167b9a5984ec1baff56b2f074485a)
的网关类型定义和测试，Python 客户端直接使用现有 HTTPX，无需安装 Node.js。
涉及 `listAgents`、`createAgent`、`updateAgent`、`getAsyncTasks`、`getSubagents`、
`sendPrompt`、`promptAcceptanceStatus`、`getAgentTranscriptTail`，均使用 POST；
`/health` 使用 GET。

自动测试覆盖会话映射、重启恢复、人设同步、并发和排队、创建响应丢失、旧回复过滤、
认证错误，以及真实 Entari 命令分发与群开关权限。真实 Windows 到云端的连通性、
创建 Bot 和问答仍需在部署环境按上述步骤验证。

参考：[远程网关说明](https://github.com/Adam91holt/grokbot-sdk/blob/c14347fa82d167b9a5984ec1baff56b2f074485a/docs/remote.md)、
[Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)。
