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
HYW_SEARCH_PROXY=
```

接口需兼容 OpenAI Chat Completions，`HYW_BASE_URL` 填 API 根地址，
程序追加 `/chat/completions`。模型需能遵循 XML 指令；图片解释还要求视觉输入能力。
这里的默认地址与模型沿用上游，可替换为你实际使用的供应商和模型。
`HYW_CONFIG_SOURCE=llm` 整组复用 `LLM_API_KEY/LLM_BASE_URL/LLM_MODEL`，
`steam` 整组复用 `STEAM_LLM_*`，不会混用不同供应商的密钥和接口。
未配置密钥时仍能正常加载插件，调用时提示管理员配置。

`HYW_PROXY` 用于模型和用户图片下载；留空时继承 `HTTPS_PROXY/HTTP_PROXY`。
填 `HYW_PROXY=direct` 可让它们直连，其他插件继续使用原有全局代理。
`HYW_SEARCH_PROXY` 单独控制 DuckDuckGo 搜索及 `web_fetch` 网页读取；
留空沿用 HYW_PROXY 的有效配置，填 `direct` 表示直连。
如果模型接口可以直连，但搜索需要代理，可设置：

```dotenv
HYW_PROXY=direct
HYW_SEARCH_PROXY=http://127.0.0.1:7890
```

其中 `7890` 仅为示例，需要换成机器人运行机器实际可用的代理地址和端口。
HTTP 代理通常填写 `http://` 地址，即使目标网页是 HTTPS。
默认搜索 DuckDuckGo Lite，失败后尝试其 HTML 接口，无需额外搜索密钥。
目前这两个固定入口属于同一个搜索引擎，尚未接入其他搜索 API 或模型供应商自带的联网工具。
搜索引擎可能要求验证，此时明确返回工具错误，不能保证所有网络环境都可搜索。
开发环境最初遇到 HTTP 202 验证页，随后实测 Lite 接口在全时段和近一周筛选下均返回 5 条结果；
仍建议在部署网络验证可用性，遇到验证页时可检查代理。
插件不会绕过验证页，也不会把验证页当成搜索结果。

卡片使用项目共用 Playwright Chromium。若尚未安装：

```bash
.venv/bin/python -m playwright install chromium
```

`HYW_RENDER=false` 可关闭卡片。渲染失败或卡片过长时回退为分段文字。

## 排查模型连接失败

接口地址和模型参数填写正确，也可能因运行机器的 DNS、TLS、代理或连接中断而失败。
旧版统一显示“无法连接模型服务”，无法仅凭这句话判断根因。
现在聊天回复会显示错误分类和 HTTPX 异常类型，并注明当前使用代理还是直连；
日志会记录如 `code=tls_certificate error=ConnectError route=direct`，不会输出原始异常中的密钥或代理密码。

- `dns`：检查运行机器的域名解析；使用代理时也要检查代理域名。
- `tls_certificate`：检查系统时间、Python CA 证书和接口/代理的证书链，保持证书校验开启。
- `proxy` / `connection_refused`：检查代理是否启动、地址端口及认证；可设置 `HYW_PROXY=direct` 对比直连。
- `connection_interrupted`：接口网关或代理连接中断，需结合服务端日志排查。
- `request_protocol`：检查复制密钥时是否带入了换行等异常字符。

仅验证连通性时无需密钥。例如在 Windows 机器人运行机器上执行：

```powershell
curl.exe --noproxy "*" --connect-timeout 10 --max-time 20 -i https://llm.hyw.mom/v1/models
```

未带密钥返回 HTTP 401 表示此次直连已到达接口；它不验证密钥、模型是否可用，
也不能代替机器人使用相同 Python 环境和代理路径时的连接测试。
修改 `.env` 后需重启，且进程已有的环境变量优先于 `.env` 文件。

## 排查“外部网络检索服务不可用”

这句话不是预设的最终回答。上游提示词强制事实性问题优先检索，
模型收到工具错误后可能在最终回答中转述失败情况；模型接口正常不代表搜索入口也可达。
搜索请求由机器人运行机器发出，并不会自动使用模型供应商的联网能力。

日志中的 `[hyw] request routes` 显示模型、搜索各自使用代理还是直连；
`[hyw] search endpoint=... results=N` 表示实际检索成功（0 为没有匹配结果）。
失败日志和工具返回值会保留 `dns`、`tls_certificate`、`connection`、`http_403`、
`http_202`、`challenge`（验证页）、`unexpected_page`（页面格式异常）等原因。
这样可以区分网络故障、搜索引擎验证和没有搜索结果；仅模型自行声称不能联网而没有搜索日志，
不能作为工具不可用的证据。

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

允许同一用户同时发起多个问题，全局最多处理 4 个请求（按请求数计，不按用户数计）。
每个问题独立管理上下文和引用，回复会引用对应的原始问题，便于区分并发结果。
`/q 清空` 需等自己在当前聊天中的所有请求结束后使用。
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
