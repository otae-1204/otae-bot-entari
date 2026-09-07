# HYW 搜索问答

本地插件位于 `plugins/hyw/`，启动时自动发现，无需修改 `entari.yml` 或安装上游包。
基于 [HYW 固定版本](https://github.com/kumoSleeping/entari-plugin-hyw/tree/0ca5b645ba63de5f637be4df2358d55aeeaaa17d)
适配，沿用系统提示词、XML 工具协议和 Vue/Markdown/KaTeX 回复卡片。
原始文件及许可说明见 [NOTICE](../plugins/hyw/NOTICE.md)。

## 配置

在项目根目录 `.env` 填写，重启机器人后生效：

```dotenv
HYW_CONFIG_SOURCE=hyw
HYW_API_KEY=你的密钥
HYW_BASE_URL=https://openrouter.ai/api/v1
HYW_MODEL=gpt-4o
HYW_RENDER=true
HYW_PROXY=
```

接口需兼容 OpenAI Chat Completions，`HYW_BASE_URL` 填 API 根地址，
程序追加 `/chat/completions`。模型需能遵循 XML 指令；图片解释还要求视觉输入能力。
这里的默认地址与模型沿用上游，可替换为你实际使用的供应商和模型。
`HYW_CONFIG_SOURCE=llm` 整组复用 `LLM_API_KEY/LLM_BASE_URL/LLM_MODEL`，
`steam` 整组复用 `STEAM_LLM_*`，不会混用不同供应商的密钥和接口。
未配置密钥时仍能正常加载插件，调用时提示管理员配置。

`HYW_PROXY` 用于模型、搜索和图片下载；留空时继承 `HTTPS_PROXY/HTTP_PROXY`。
默认搜索 DuckDuckGo Lite，失败后尝试其 HTML 接口，无需额外搜索密钥。
搜索引擎可能要求验证，此时明确返回工具错误，不能保证所有网络环境都可搜索。
开发环境最初遇到 HTTP 202 验证页，随后实测 Lite 接口在全时段和近一周筛选下均返回 5 条结果；
仍建议在部署网络验证可用性，遇到验证页时可检查代理。
插件不会绕过验证页，也不会把验证页当成搜索结果。

卡片使用项目共用 Playwright Chromium。若尚未安装：

```bash
.venv/bin/python -m playwright install chromium
```

`HYW_RENDER=false` 可关闭卡片。渲染失败或卡片过长时回退为分段文字。

## 用法

- `/q 问题`：每次开始一个新问题。别名 `/hyw`、`/何意味`。
- `/q 帮助` 或 `/q`：帮助。
- 在问题后附带图片：最多 3 张、每张 5 MB、2000 万像素，缩放后作为模型视觉输入。
- 引用其他消息后 `/q 这是什么意思`：把被引用的文字和图片一起分析。
- 引用自己的 HYW 回答后 `/q 继续解释第二点`：恢复上下文继续追问。
- `/q 清空`：删除自己在当前聊天中的历史。

回复中的 `[1]` 等引用对应工具返回的真实网址；卡片之外也会发送可点击的来源链接。
支持 `web_search(query, time_range, kl)` 和 `web_fetch(url)`，后者只提取公开网页正文。
PDF、登录页面、需要执行脚本才能显示正文的网站暂不支持。

## 运行边界

同一账号、群/频道、发送者只能同时处理一个问题，全局最多 4 个；
单次最多 120 秒、10 次模型调用、8 次工具调用，每轮最多 4 次。
本插件不记录密钥、模型错误响应正文、完整对话或原始图片；
HTTP 组件仍可能按应用日志配置记录请求 URL（包括搜索词）。
历史只存内存，1 小时过期，最多 128 条/4 MB，重启即清空；
每次最多保留最近 6 组文字问答，图片不存入续聊历史，需要再次分析时重新发送。
历史按平台、机器人账号、群/频道和发送者隔离，不会从其他用户的回复恢复私有上下文。

问题、引用内容和图片会发送至所配置模型服务；检索关键词会发送至 DuckDuckGo。
回复卡片不加载远程图片或脚本。网页及图片下载限制大小并检查每次重定向的公开地址。

## 验证记录

已验证 Entari 实际加载和三个命令别名的分发、模型 XML 工具循环、
多人并发隔离、引用追问及来源编号、图片输入、超限与错误回退。
实际浏览器截图确认中文、Markdown 表格、代码、KaTeX 公式和引用正常显示；
实际网络请求验证 DuckDuckGo 搜索及公开网页正文读取。
工作区没有模型密钥，因此模型请求使用模拟 HTTP 响应验证，尚未进行真实模型或 QQ 消息联调。
