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

本机若把 DNS 改写成 `198.18.0.0/15`，图片下载允许连接这一段，仍拒绝局域网地址。
