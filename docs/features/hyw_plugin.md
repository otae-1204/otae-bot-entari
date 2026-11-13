# HYW 搜索问答

本地命令在 `plugins/hyw/`。问答、搜索、读网页和 Markdown 出图由 [Hyw-Frontier](https://github.com/kumoSleeping/Hyw-Frontier) `0a1fede` 完成，本仓库不安装它的 Entari 插件包。核心库源码收在 `vendor/hyw-frontier/`，由 `requirements.txt` 以可编辑方式安装，不需要额外 clone。

2026-10-01 之前的 XML / Playwright 实现已从仓库移除，仍留在提交 `d80759d` 里；回滚用 `git checkout d80759d -- plugins/hyw tests/test_hyw.py tests/test_hyw_evidence.py docs/hyw_plugin.md`。

## 配置

在 `entari.yml` 的 `plugins: plugins.hyw:` 段填写，密钥放 `.env` 并用 `${{ env.X :- }}` 引用，重启机器人后生效。`config_source: llm` 或 `steam` 时，`api_key` / `base_url` / `model` 整组改用 `otae.llm` 或 `plugins.steamInfo` 的 `llm_*`。下面是全部键与默认值：

```yaml
plugins:
  plugins.hyw:
    config_source: hyw
    api_key: "${{ env.HYW_API_KEY :- }}"
    base_url: https://openrouter.ai/api/v1
    model: gemini-3.8-flash
    credentials_file: ""
    vertex_location: global
    vertex_base_url: ""
    proxy: direct
    search_proxy: direct
    render: true
    search_provider: ddgs
    search_mode: turbo
    reader_engine: browser
    max_tool_images: 600
    max_reader_images: 30
    max_concurrent: 2
    timeout: 300
    request_timeout: 90
    reasoning_mode: auto
    max_rounds: 10
    render_timeout: 45
    render_max_images: 16
    render_max_edge: 1024
    home: data/hyw-frontier
```

`proxy: direct` 与 `search_proxy: direct` 表示模型、用户图片和 DuckDuckGo 都直连，不继承全局代理（`.env` 的 `HTTP_PROXY` / `HTTPS_PROXY`）。留空才继承。

重试参数由 hyw-frontier 自己从进程环境读取，仍写在 `.env`：`HYW_RETRY_ATTEMPTS`、`HYW_RETRY_BASE_DELAY`、`HYW_RETRY_MAX_DELAY`、`HYW_RETRY_BUDGET`；`jina` / `parallel` 的 `JINA_API_KEY` / `PARALLEL_API_KEY` 同理。

搜索默认 `ddgs`，不需要搜索密钥。`jina` / `parallel` 需要对应的 API Key。读网页走 Jina Reader，网址会发给 Jina。

## 使用 Google 服务账号凭据（Vertex AI）

把服务账号 JSON 指给 `plugins.hyw.credentials_file`，或在 `.env` / 环境里设 `GOOGLE_APPLICATION_CREDENTIALS`。文件不要提交进 Git。

服务账号模式下 `base_url` 与 `vertex_base_url` 会被忽略，避免把凭据发给中转站。端点由 Google 提供商按凭据里的 `project_id` 和 `vertex_location`（默认 `global`）推导。模型名使用 `gemini-3.8-flash` 这种 id，不再把模型名写成 `google/<model>`；提供商字段本身是 `google`。

未配置密钥或凭据文件时插件仍能加载，调用时提示管理员配置。

## 用法

- `/q 问题`，别名 `/hyw`、`/何意味`。可附最多 4 张图，单张原图 20 MB，发送前会压缩。
- 引用别人的消息再提问，会带上那段文字、图片、JSON/XML 分享卡片和合并转发。合并转发只读取当前这条，按原顺序展开，不执行小程序、不下载音视频。引用自己的 HYW 回答则继续该对话，记忆 1 小时，只在内存里。
- 模型调用遇到 HTTP 408/429/500/502/503/504，或超时、连接中断时，会在这一次问答内重试。默认最多 3 次，累计等待不超过 20 秒（`HYW_RETRY_ATTEMPTS`、`HYW_RETRY_BASE_DELAY`、`HYW_RETRY_MAX_DELAY`、`HYW_RETRY_BUDGET`）。确定性的 4xx、DNS 和证书错误不重试。
- `/q 清空` 清除自己在当前会话的续聊和来源记录。
- `/qstop` 取消自己正在进行的问答。
- 回答发出后不再自动附带参考资料。回复一条 HYW 回答再发送 `/link` 或 `/qlink`，才会收到该次的标题和链接。同频道的其他人也可以回复一条公开回答来查。

全局同时最多 2 个问答，整轮 300 秒，单次模型请求 90 秒。工具图最多尝试 600 张。卡片由 md2png 绘制；出图失败时改为文字。

## 引用的内容从哪里来（合并转发、卡片）

`/q` 不再另行查询引用的消息，只读事件里带来的内容。经 `ONEBOT_CLIENTS` 接入时，边界适配器
（`otae_bot/boundary/onebot11.py`）把引用写成 `<quote id>`，里面是 `<author>` 和原消息内容；
被引用的是合并转发时，内容是 `<message id="<resid>" forward="true">`，子元素是各条 `<message>`，
每条以 `<author id name/>` 开头（嵌套转发最多 3 层、总共 200 条）。HYW 按原顺序展开每条的发送者、
文字和图片，嵌套的转发同样展开；展开时仍受深度、2000 条消息、32 000 字节文本和 20 张图片的上限约束。

QQ 卡片是扩展元素 `<onebot:json data>` / `<onebot:xml data>`，LLBot 原生 Satori 下是 `<llonebot:ark data>`，
三者都读；消息里没有这些元素时什么也不做。

适配器取不到引用时，引用只剩 `<quote id/>`；Entari 会再试 `message.get`，失败时由
`otae_bot/pipeline/quote_fallback.py` 让事件照常分发（记 `[entari] quoted message fetch failed …`）。
合并转发没展开时只剩没有子元素的 `<message id forward="true"/>`。这两种情况 `/q` 照常回答问题，
只是不带引用内容，并记一条 warning：

```text
[hyw] quoted message unavailable, answering without it: channel=875241970 quote=2|875241970|3969610 reason=引用的合并转发没有随事件展开
```

| `reason` | 含义 |
| --- | --- |
| `引用的消息没有随事件送达` | 引用是空的 `<quote id/>`，`message.get` 也没取到 |
| `引用的合并转发没有随事件展开` | 被引用的合并转发没有子消息 |
| `读取出错：…` | 展开时出现意外异常 |

只发了 `/q` 没写问题时提示"没能读取引用的消息，请在 /q 后直接写出问题。"。
引用的是自己的 HYW 回答时仍按引用 id 续聊，`/link` 也按引用 id 取来源。

## 失败日志

模型失败（`[hyw] answer failed`）、出图失败（`[hyw] card render failed`）和其它异常（`[hyw] request failed`）
各记一行 warning，例如：

```text
[hyw] answer failed: code=http_401 http_status=401 retryable=False chain=[FrontierError: 模型认证失败，请检查 API Key 或服务账号凭据。 <- AuthenticationError: Error code: 401 - Incorrect API key provided: <redacted>]
```

`code` / `http_status` 取自 Hyw-Frontier 的 `diagnostics`，或异常链上的 `code`、`status_code`、
`response.status_code`。`chain` 沿 `__cause__` / `__context__` 最多记 6 层的类型与消息：
Hyw-Frontier 用 `raise ... from None` 隐藏了 SDK 原始异常，但它仍在 `__context__` 里，真正的原因通常在这一层。
消息先脱敏再写日志：`api_key` 与代理地址原文、`Bearer` / `Basic` 凭据、`Authorization` / `Cookie` /
`api_key` / `token` 等键值、URL 的查询串与用户信息、`sk-…`、`AIza…`、`ya29.…`、JWT 和私钥块都替换成 `<redacted>`。

本机若把 DNS 改写成 `198.18.0.0/15`，图片下载允许连接这一段，仍拒绝局域网地址。
