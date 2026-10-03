# HYW 搜索问答

本地命令在 `plugins/hyw/`。问答、搜索、读网页和 Markdown 出图由 [Hyw-Frontier](https://github.com/kumoSleeping/Hyw-Frontier) `0a1fede` 完成，本仓库不安装它的 Entari 插件包。核心库源码收在 `vendor/hyw-frontier/`，由 `requirements.txt` 以可编辑方式安装，不需要额外 clone。

2026-10-01 之前的 XML / Playwright 实现已从仓库移除，仍留在提交 `d80759d` 里；回滚用 `git checkout d80759d -- plugins/hyw tests/test_hyw.py tests/test_hyw_evidence.py docs/hyw_plugin.md`。

## 配置

在项目根目录 `.env` 填写，重启机器人后生效。`HYW_CONFIG_SOURCE=llm` 或 `steam` 时，整组改用 `LLM_*` 或 `STEAM_LLM_*`。

```dotenv
HYW_CONFIG_SOURCE=hyw
HYW_API_KEY=
HYW_BASE_URL=https://openrouter.ai/api/v1
HYW_MODEL=gemini-3.8-flash
HYW_CREDENTIALS_FILE=
HYW_VERTEX_LOCATION=global
HYW_VERTEX_BASE_URL=
HYW_PROXY=direct
HYW_SEARCH_PROXY=direct
HYW_SEARCH_PROVIDER=ddgs
HYW_RENDER=true
HYW_TIMEOUT=300
HYW_REQUEST_TIMEOUT=90
HYW_MAX_CONCURRENT=2
HYW_MAX_TOOL_IMAGES=600
HYW_MAX_READER_IMAGES=30
HYW_HOME=data/hyw-frontier
```

`HYW_PROXY=direct` 与 `HYW_SEARCH_PROXY=direct` 表示模型、用户图片和 DuckDuckGo 都直连，不继承全局代理。留空才继承。

搜索默认 `ddgs`，不需要搜索密钥。`jina` / `parallel` 需要对应的 API Key。读网页走 Jina Reader，网址会发给 Jina。

## 使用 Google 服务账号凭据（Vertex AI）

把服务账号 JSON 指给 `HYW_CREDENTIALS_FILE`，或使用 `GOOGLE_APPLICATION_CREDENTIALS`。文件不要提交进 Git。

服务账号模式下 `HYW_BASE_URL` 与 `HYW_VERTEX_BASE_URL` 会被忽略，避免把凭据发给中转站。端点由 Google 提供商按凭据里的 `project_id` 和 `HYW_VERTEX_LOCATION`（默认 `global`）推导。模型名使用 `gemini-3.8-flash` 这种 id，不再把模型名写成 `google/<model>`；提供商字段本身是 `google`。

未配置密钥或凭据文件时插件仍能加载，调用时提示管理员配置。

## 用法

- `/q 问题`，别名 `/hyw`、`/何意味`。可附最多 4 张图，单张原图 20 MB，发送前会压缩。
- 引用别人的消息再提问，会带上那段文字、图片、JSON/XML 分享卡片和合并转发。合并转发只读取当前这条，按原顺序展开，不执行小程序、不下载音视频。引用自己的 HYW 回答则继续该对话，记忆 1 小时，只在内存里。
- 模型调用遇到 HTTP 408/429/500/502/503/504，或超时、连接中断时，会在这一次问答内重试。默认最多 3 次，累计等待不超过 20 秒（`HYW_RETRY_ATTEMPTS`、`HYW_RETRY_BASE_DELAY`、`HYW_RETRY_MAX_DELAY`、`HYW_RETRY_BUDGET`）。确定性的 4xx、DNS 和证书错误不重试。
- `/q 清空` 清除自己在当前会话的续聊和来源记录。
- `/qstop` 取消自己正在进行的问答。
- 回答发出后不再自动附带参考资料。回复一条 HYW 回答再发送 `/link` 或 `/qlink`，才会收到该次的标题和链接。同频道的其他人也可以回复一条公开回答来查。

全局同时最多 2 个问答，整轮 300 秒，单次模型请求 90 秒。工具图最多尝试 600 张。卡片由 md2png 绘制；出图失败时改为文字。

## 引用的消息取不到时（合并转发）

Entari 0.17.4 组装消息事件时，引用不带内联内容就调 Satori `message.get` 取原消息
（`arclet/entari/event/base.py:329`）。LLBot 取合并转发会回 `500 ServerException: 消息为空`，
异常在分发前抛出，整条消息被丢掉：`/q` 没反应，日志里连这条消息都没有。

现在分两层兜住：

1. **事件照常分发。** `otae_bot/adapters/quote_fallback.py` 在 `create_app()` 里包装
   `MessageEvent.gather`，并把 letoderea 已经存下旧 gather 的 publisher 一起改指过来。
   `message_get` 抛任何异常时记一条 warning，然后按"没有引用"重跑原 gather：没有 `session.reply`，
   `is_reply_me` 为 False，`event.quote`（含 id）原样留给插件。日志只有频道、引用 id、异常类型和消息，
   连接 token 会被替换成 `<redacted>`：

   ```text
   [entari] quoted message fetch failed, dispatching without reply: channel=123456 quote=7412... error=ServerException: 消息为空
   ```

   Entari 升级后 gather 的签名或其中的 `message_get` / `Reply` 等引用对不上时，不打补丁，
   启动时记 `quote fetch fallback not installed: ...`；装上时记 `quote fetch fallback installed on 5 event publishers`。

2. **HYW 自己读引用。** `session.reply` 为空而 `event.quote` 还在时，`/q` 不再走 `message.get`：
   - 先用 OneBot `get_forward_msg(message_id=<引用 id>)` 取合并转发（LLBot 接受转发消息本身的 id），
     按原顺序展开每条的发送者、文字和图片，与转发卡片同样交给 Hyw-Frontier 的 `message_content`；
   - 不是转发时再用 OneBot `get_msg(message_id=<引用 id>)` 取原消息的文字、图片，以及其中嵌套的转发；
   - 每一步先试 Satori 内部接口 `internal/<action>`，失败再走该账号的 OneBot HTTP（见下）；
   - 都读不到时 `/q` 照常回答问题，只是不带引用内容，并记
     `[hyw] quoted message unavailable, answering without it: channel=... quote=... failures=[...]`；
     只发了 `/q` 没写问题时提示"没能读取引用的消息，请在 /q 后直接写出问题。"。
   - 引用的是自己的 HYW 回答时仍按引用 id 续聊，`/link` 也按引用 id 取来源。

   消息里直接带的 Satori `<message forward>`（子元素是各条 `<message>`、`<author>` 是发送者）在本地展开，
   只有 id 没有子元素时同样用 `get_forward_msg` 取。

### em 部署配置

LLBot 的 Satori 端口（5500、5550）读不了合并转发，需要每个实例的 **OneBot 11 HTTP 服务**：

1. 在两个 LLBot 实例里各自开启 OneBot 11 的 HTTP 服务（端口互不相同，例如 3000、3001），记下各自的 access token。
2. 在 `.env` 的 `SATORI_CLIENTS` 里给每个连接写上自己的 OneBot 地址：

   ```dotenv
   SATORI_CLIENTS=[{"host":"127.0.0.1","port":5500,"token":"SATORI_TOKEN_1","onebot_url":"http://127.0.0.1:3000","onebot_token":"ONEBOT_TOKEN_1"},{"host":"127.0.0.1","port":5550,"token":"SATORI_TOKEN_2","onebot_url":"http://127.0.0.1:3001","onebot_token":"ONEBOT_TOKEN_2"}]
   ```

   写了 `onebot_url` 的账号只用自己的地址，不会把请求发到另一个 QQ 号的 LLBot。没写 `onebot_token` 时
   依次用 `ONEBOT_ACCESS_TOKEN`、该连接的 Satori `token`。只有一个实例时也可以只填 `ONEBOT_HTTP_URL` /
   `ONEBOT_ACCESS_TOKEN`。
3. 重启机器人。

## 失败日志

模型失败（`[hyw] answer failed`）、出图失败（`[hyw] card render failed`）和其它异常（`[hyw] request failed`）
各记一行 warning，例如：

```text
[hyw] answer failed: code=http_401 http_status=401 retryable=False chain=[FrontierError: 模型认证失败，请检查 API Key 或服务账号凭据。 <- AuthenticationError: Error code: 401 - Incorrect API key provided: <redacted>]
```

`code` / `http_status` 取自 Hyw-Frontier 的 `diagnostics`，或异常链上的 `code`、`status_code`、
`response.status_code`。`chain` 沿 `__cause__` / `__context__` 最多记 6 层的类型与消息：
Hyw-Frontier 用 `raise ... from None` 隐藏了 SDK 原始异常，但它仍在 `__context__` 里，真正的原因通常在这一层。
消息先脱敏再写日志：`HYW_API_KEY` 与代理地址原文、`Bearer` / `Basic` 凭据、`Authorization` / `Cookie` /
`api_key` / `token` 等键值、URL 的查询串与用户信息、`sk-…`、`AIza…`、`ya29.…`、JWT 和私钥块都替换成 `<redacted>`。

本机若把 DNS 改写成 `198.18.0.0/15`，图片下载允许连接这一段，仍拒绝局域网地址。
