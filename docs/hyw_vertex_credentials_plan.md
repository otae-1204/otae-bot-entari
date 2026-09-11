# HYW 插件兼容 Google 服务账号凭据 —— 修改方案（v2，已实施）

> **实施状态（2026-09-11）**：本方案已按用户批准落地。新增 `plugins/hyw/google_auth.py`；
> 改动 `config.py`、`agent.py`、`handlers.py`、`network_errors.py`、`.env.example`、
> `docs/hyw_plugin.md`、`tests/test_hyw.py`。回归 `python -m unittest tests.test_hyw`
> = **71 个测试全通过（`OK`，退出码 0，4.3s）**；改造前那个唯一的既有失败
> `test_same_user_parallel_answers_keep_their_own_replies_and_history` 已随 §6.3 的
> SSL 上下文复用修复转为通过（套件耗时也从 ~9s 降到 ~4s）。
> 真实凭据端到端已跑通：配置推导、纯文本 `/q`、XML 工具循环、内联 JPEG 视觉、
> 坏模型名 404 → `sa_model` 话术且不泄漏项目号（见 §10）。
>
> **实施中发现的新约束（方案未预见，务必注意）**：本机到 `googleapis.com` 的直连不稳定，
> 实测多次为 `ConnectTimeout`，经 `http://127.0.0.1:7890` 可达；而 OpenAI 中转站
> `llm.hyw.mom` 恰好相反——只有直连可达，经该代理会 `ConnectTimeout`。
> 因此**服务账号模式需要把 `HYW_PROXY` 指向能访问 Google 的代理**，
> 沿用现有 `HYW_PROXY=direct` 会在换 token 时超时（§3.1 的「只加一行即可」在直连可用时成立，
> 本机当前不成立）。另外仓库 `.env` 里的 `HTTPS_PROXY=https://127.0.0.1:7890`
> 用了 `https://` 前缀，httpx 会对代理本身发起 TLS 握手，表现为 `tls_handshake` 失败，
> 应写成 `http://`。搜索同理：`HYW_SEARCH_PROXY` 也要指向该代理（直连 DDG 超时）。
>
> **实施中发现的安全回归（已修复，见 §11）**：新增的 access token 与 RSA 私钥一度
> 经 entari 的 loguru `diagnose=True` 泄漏进日志，已用 `Secret` 包装 + 「危险操作放进
> 吞异常的独立函数」重构修掉，8 条失败路径复验均无泄漏。

目标：让 `plugins/hyw` 除了现有的「静态 API Key + OpenAI 兼容中转」之外，也能直接使用
用户提供的 Google Cloud 服务账号密钥 JSON，走 Vertex AI 的 OpenAI 兼容端点。

本文描述方案与验证依据。文中「已实测」均指本机只读探针
（`%TEMP%\hyw_sa_probe*.py`，仅用仓库已有的 pycryptodome，未新增依赖）。

---

## 1. 结论先行

用户提供的 JSON 是 **Google Cloud 服务账号密钥**（`type=service_account`、
`project_id=<项目号>`、`client_email=<服务账号>@<项目号>.iam.gserviceaccount.com`、
`token_uri=https://oauth2.googleapis.com/token`）。

> 本文不记录该凭据的真实项目号、`client_email` 与 `private_key_id`——按 §9 的口径，
> 这些属于凭据身份信息，只应存在于部署机器的仓库之外。文中示例与测试夹具统一使用
> 合成值（`example-project-123456`）。

它**不能**当作 `Authorization: Bearer <key>` 直接使用，必须：

1. 用 `private_key` 自签一个 RS256 的 JWT assertion；
2. 以 `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer` 表单 POST 到 `token_uri`，换回 access token；
3. 用 `Authorization: Bearer <access_token>` 调用 Vertex 的 OpenAI 兼容端点。

已实测通过的完整链路（HTTP 200，可正常出中文回答与视觉回答）：

```
POST https://oauth2.googleapis.com/token            -> 200, expires_in=3599, token_type=Bearer
POST https://aiplatform.googleapis.com/v1/projects/{project_id}/locations/global/endpoints/openapi/chat/completions
     body {"model":"google/gemini-3.8-flash","messages":[...],"temperature":0.5,"max_tokens":4096,"stream":false}
     -> 200 {"choices":[{"finish_reason":"stop","message":{"content":"<final_response>1+1等于2。</final_response>"}}]}
```

要点（全部实测）：

| 事项 | 实测结果 |
| --- | --- |
| 是否必须装 `google-auth` | 不必。pycryptodome 手写 `Crypto.Signature.pkcs1_15` + `SHA256` 即可签名 |
| 是否需要代理 | 不需要。剥离全部代理变量后 token 交换与 Vertex 调用均 200（`HYW_PROXY=direct` 安全） |
| 模型名 | 必须带发布者前缀。`google/gemini-3.8-flash`、`google/gemini-2.5-flash`、`google/gemini-2.5-pro` 均 200；`gemini-2.5-flash`（无前缀）→ 400 `Malformed publisher model` |
| 端点版本/区域 | `v1` 与 `v1beta1` 同路径均可用；`locations/global`、`locations/us-central1` 均可用（路径里的 location 不做校验，`mars` 也返回 200） |
| token 请求体编码 | 必须表单编码（`data=dict` 或显式 `application/x-www-form-urlencoded`）。`content=str`/`content=bytes` → 400 `Invalid JSON payload received` |
| 视觉输入 | 内联 data-URI（JPEG）可用并答对颜色；**远程 http(s) 图片 URL 被拒**（400 `Cannot fetch content from the provided URL`） |
| `max_tokens` | 上限为 65537（不含），4096 正常；999999 → 400 |
| 空正文风险 | `max_tokens` 过小时 200 但 `choices[0].message.content` 缺失（推理 token 吃光预算）——解析必须容错 |
| 延迟参考 | `google/gemini-2.5-flash` 短答约 5s，`google/gemini-3.8-flash` 约 8–16s（均含数百~上千 reasoning token） |
| 本项目 XML 协议 | `google/gemini-3.8-flash` 能按 170 行系统提示词输出 `<final_response>`（实测遵循） |

失败形态（用于错误话术设计）：

| 触发 | 返回 |
| --- | --- |
| 私钥签名不匹配 | 400 `{"error":"invalid_grant","error_description":"Invalid JWT Signature."}` |
| `client_email` 不存在 | 400 `invalid_grant: Invalid grant: account not found` |
| `exp` 超出合理区间 | 400 `invalid_grant: Token must be a short-lived token (60 minutes)...` |
| assertion 不是 JWT | 400 `invalid_request: Bad Request` |
| token 伪造/缺失 | 401 `UNAUTHENTICATED`（`ACCESS_TOKEN_TYPE_UNSUPPORTED`） |
| 用了别的 project_id | 403 `PERMISSION_DENIED` / `CONSUMER_INVALID` |
| 模型名不存在 | 404 `NOT_FOUND`（`publishers/google/models/...`） |

---

## 2. 现状与差距

现有 `HywConfig` 只有一条鉴权路径：`api_key`（来自 `HYW_/LLM_/STEAM_LLM_` 前缀的环境变量），
`agent.complete()` 固定发送 `Authorization: Bearer config.api_key`，`base_url` 固定追加 `/chat/completions`。

差距共 4 处：

1. **鉴权**：无法从服务账号 JSON 派生短期 Bearer token（需要签名 + 交换 + 缓存 + 过期刷新）。
2. **端点**：Vertex 的地址形态是 `/v1/projects/{project}/locations/{location}/endpoints/openapi/chat/completions`，
   不能由「根地址 + `/chat/completions`」拼出。
3. **模型名**：Vertex 要求 `publisher/model` 形式，而 `HYW_MODEL=gemini-3.8-flash` 无前缀。
4. **错误语义**：Google 的 400/401/403/404 与现有「鉴权失败 / 请求过于频繁 / 其他失败」三类话术不对应，
   管理员看不出是私钥问题、项目权限问题还是模型名问题。

已确认**不存在**的隐患：`plugins/hyw` 全仓库无任何 `googleapis`/`service_account` 处理代码，
`web.py` 的 SSRF 护栏只作用于 `download()`（搜索与网页/图片读取），模型请求路径不受其影响。

---

## 3. 方案总览

**在现有 `api_key` 模式旁边新增一条「服务账号凭据」模式，二者互斥且自动选择，默认行为不变。**

```
HywConfig.from_env()
  ├─ 有 HYW_CREDENTIALS_FILE / GOOGLE_APPLICATION_CREDENTIALS  ->  mode="service_account"
  │     base_url 缺省推导为 Vertex OpenAI 兼容地址（用 JSON 的 project_id + HYW_VERTEX_LOCATION）
  │     model 缺省自动补 "google/" 前缀
  │     Bearer token 由 plugins/hyw/google_auth.py 现签现换并缓存
  └─ 否则有 HYW_API_KEY                                        ->  mode="api_key"（现状，完全不变）
        base_url / model 沿用现有逻辑
```

- 不引入 `google-auth` / `cryptography` / `PyJWT`（本机 `.venv` 均未安装，`requirements.txt` 也未声明）。
- 不动 XML 工具协议、提示词、渲染、历史、并发等既有逻辑。
- 不改变 `HYW_CONFIG_SOURCE=hyw|llm|steam` 的语义；凭据模式与之正交（可叠加，例如 `llm` 前缀取 `LLM_MODEL`）。

### 3.1 优先级规则（关键：本机 `.env` 已经设了中转地址）

只读检查本机 `C:\Code\qqbot\bot-entari\.env`（只列键名，未读取/输出任何密钥值）：
`HYW_CONFIG_SOURCE=hyw`、`HYW_API_KEY` 已设、`HYW_BASE_URL=https://llm.hyw.mom/v1`、
`HYW_MODEL=gemini-3.8-flash`、`HYW_PROXY=direct`（另有全局 `HTTP_PROXY`/`HTTPS_PROXY`）。

这意味着：**如果按「显式 `HYW_BASE_URL` 优先」的朴素规则实现，用户只加一行
`HYW_CREDENTIALS_FILE=...` 打开凭据模式时，`base_url` 会继续指向 `llm.hyw.mom/v1`**，
于是拿服务账号令牌去请求一个 OpenAI 中转，表现为难排查的 401/403，而不是
「配置生效了但地址不对」这种一眼可见的失败。因此必须把优先级写死：

| 情形 | 判定 |
| --- | --- |
| 设置了 `HYW_CREDENTIALS_FILE` 且文件可解析 | `mode=service_account`，**忽略** `HYW_BASE_URL`（若其非 `aiplatform.googleapis.com` 则记一条 warning 说明已忽略），`base_url` 一律推导为 Vertex 地址 |
| 同上，但显式设置 `HYW_VERTEX_BASE_URL` | 用它（供私有域名/测试替身用），仍追加 `/chat/completions` |
| 未设 `HYW_CREDENTIALS_FILE`，设了 `HYW_API_KEY` | `mode=api_key`，现有逻辑逐字不变 |
| 两者都没设 | `mode=none`，`handlers` 给出配置指引 |

`HYW_MODEL` 沿用（`gemini-3.8-flash` 在 SA 模式下自动补成 `google/gemini-3.8-flash`，
实测可用），因此**用户迁移只需新增一行 `HYW_CREDENTIALS_FILE`，其余键保持原样**；
若要回退，删掉这一行即回到现状中转，`HYW_API_KEY` 无需改动。

推荐的两步上线顺序（实施阶段执行）：
1. 先只加 `HYW_CREDENTIALS_FILE`，在聊天里跑一次 `/q`，看日志
   `[hyw] request routes: model=... auth=service_account` 与卡片是否正常；
2. 确认无误后再决定是否把 `HYW_BASE_URL` 从 `.env` 里清掉（清掉更不易误读，但非必需）。

**实施补充**：本机实测「只加一行」不足够——`googleapis.com` 直连不通、
需经代理，而现有 `.env` 是 `HYW_PROXY=direct`。所以服务账号模式还要同时把
`HYW_PROXY` 指向能访问 Google 的代理（如 `http://127.0.0.1:7890`），
否则第一步就会以 `ConnectTimeout / timeout` 失败。详见文首「实施状态」。

---

## 4. 逐文件改动清单

### 4.1 新增 `plugins/hyw/google_auth.py`（约 120 行）

职责：服务账号 JSON → access token，带缓存与并发去重。

```
@dataclass(frozen=True)
class ServiceAccount:
    project_id: str
    client_email: str
    private_key_id: str
    private_key: str          # 只在内存中，不进 config、不进日志、不进异常文本
    token_uri: str
    source: str               # 文件路径（仅用于日志/诊断，不打印内容）

def load_service_account(path: str) -> ServiceAccount
    # 文件不存在 / 非 JSON / 缺 private_key、client_email、token_uri / private_key 不是 PEM
    #   -> HywError("HYW 服务账号凭据不可用：<固定原因>")，绝不回显密钥内容

def build_assertion(account, *, now=None) -> str
    # header {"alg":"RS256","typ":"JWT","kid":private_key_id}
    # claims {"iss":client_email,"scope":CLOUD_PLATFORM,"aud":token_uri,"iat":now,"exp":now+3600}
    # Crypto.PublicKey.RSA.import_key + Crypto.Signature.pkcs1_15.new(key).sign(SHA256.new(...))

class TokenProvider:
    def __init__(self, account, *, refresh_margin=300, skew=60)
    async def bearer(self, client) -> str
        # 缓存命中且剩余寿命 > refresh_margin 直接返回；
        # 否则持 asyncio.Lock 重新交换（并发请求只换一次）
    async def _exchange(self, client) -> tuple[str, float]
        # POST account.token_uri, data={"grant_type": GRANT, "assertion": assertion}, timeout=20
        # 表单编码（必须），不携带 Authorization，不跟随重定向
```

缓存键：`(凭据文件路径, client_email, private_key_id)`——换密钥自动失效。
时钟：`iat` 回拨 `skew` 秒以容忍小偏差。

### 4.2 `plugins/hyw/config.py`

- 新增字段（全部非敏感，`repr` 可打印）：

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `credentials_file: str` | `HYW_CREDENTIALS_FILE`，回退 `GOOGLE_APPLICATION_CREDENTIALS` | 服务账号 JSON 路径 |
| `vertex_location: str` | `HYW_VERTEX_LOCATION`（默认 `global`） | Vertex 区域段 |
| `auth_mode: str` | 推导：`service_account` / `api_key` / `none` | 供日志与报错分支使用 |

- `private_key` **不进入 dataclass**，只由 `google_auth` 按需读取（保持 `repr`/测试断言简单）。
- `credentials_file` 路径规范化：`expanduser()` + 相对路径按**进程工作目录**解析为绝对路径
  （`otae_bot/config/settings.py` 的 `.env` 加载器只读 cwd 下的 `.env`，即从仓库根启动，
  因此 `.env` 里写 `data/hyw-sa.json` 会落在 `C:\Code\qqbot\bot-entari\data\hyw-sa.json`）；
  解析后立即 `Path(...).is_file()` 检查，不存在则 `auth_mode=none` 并在日志给出**绝对路径**，
  避免「文件明明在，却说不存在」的 cwd 误判。`data/` 已被 `.gitignore` 忽略（第 24 行），
  文档推荐放这里，避免密钥进 Git。
- `base_url`：`service_account` 模式按 §3.1 的优先级——**忽略** `HYW_BASE_URL`，
  优先 `HYW_VERTEX_BASE_URL`（测试/私有端点用），否则推导为
  `https://aiplatform.googleapis.com/v1/projects/{project_id}/locations/{location}/endpoints/openapi`
  （`complete()` 仍追加 `/chat/completions`，无需改拼接方式）。`HYW_BASE_URL` 被忽略时记一条
  warning（只记键名与被忽略的事实，不记值）。
- `model` 规范化：无 `/` 时自动补 `google/`（`gemini-3.8-flash` → `google/gemini-3.8-flash`），
  显式带前缀时原样保留。
- `from_env()` 现在会在凭据文件存在但不可解析时**不抛异常**，只把 `auth_mode` 置为 `none` 并记录原因，
  由调用点给出聊天提示（保持「未配置密钥也能加载插件」的既有行为）。

### 4.3 `plugins/hyw/agent.py`

- `complete()` 增加一步取 token：

```python
token = await bearer_for(client, config)      # api_key 模式直接返回 config.api_key
headers = {"Authorization": f"Bearer {token}"}
```

- 保留现有 `client.stream(...)` + 2MB 上限 + `stream=False` 的读取方式（已实测该形态对 Vertex 正常）。
- 非 200 分支扩展：解析 Google 的 JSON 错误体，**只取 `error.status`/`code` 与固定话术**，
  不回显 `error.message`（可能含项目号等，但主要是保持不泄露上游正文的既有原则）。
- 响应解析容错：`choices[0].message.content` 缺失或非字符串时，给出
  「模型返回了空内容，请重试或更换模型」而不是 `KeyError`/`IndexError`（实测小 `max_tokens` 会命中）。

### 4.4 `plugins/hyw/handlers.py`

- 「未配置密钥」分支改为按 `auth_mode` 分流：
  - `none`：提示填写 `HYW_API_KEY`，**或**把服务账号 JSON 放到 `HYW_CREDENTIALS_FILE` 指向的路径。
  - `service_account`：`model_content()` 里的远程图片下载保持不变（Vertex 拒绝远程 URL，必须内联）。
- `run_request()` 无需结构性改动；`ask()` 签名不变。
- 日志：`[hyw] request routes: model=... auth=service_account|api_key`（不打印 project_id 之外的信息，`client_email` 也不打）。

### 4.5 `plugins/hyw/network_errors.py`

- 新增 `classify_auth_error(status, payload) -> NetworkFailure`（或在 `report_error` 内分支）：

| 上游 | code | 面向管理员的话术 |
| --- | --- | --- |
| token 交换 400 `invalid_grant: Invalid JWT Signature` | `sa_signature` | 服务账号私钥与凭据不匹配，请重新下载 JSON |
| token 交换 400 `account not found` | `sa_account` | 服务账号不存在或已被删除 |
| token 交换 400 `short-lived token` | `sa_clock` | 运行机器系统时间偏差过大 |
| token 交换 400 `invalid_request` | `sa_assertion` | 凭据文件内容不完整 |
| 401 `UNAUTHENTICATED` | `sa_token` | 访问令牌被拒绝（已自动刷新，请重试） |
| 403 `PERMISSION_DENIED` / `CONSUMER_INVALID` | `sa_project` | 该服务账号无权访问此项目，或项目未启用 Vertex AI |
| 404 `NOT_FOUND` | `sa_model` | 模型名或区域不可用（需 `google/<model>` 且区域支持） |
| 400 `Malformed publisher model` | `sa_model_prefix` | 模型名缺少发布者前缀 |
| 400 `Provided image is not valid` | `sa_image` | 图片无法被模型接受，请改用 JPEG/PNG 重新发送 |
| 429 | `quota` | 配额/频率限制，稍后重试 |

- 沿用现有约定：只记录 `code` + 异常类名 + 路由，不落上游正文与任何密钥。

### 4.6 `.env.example` 与 `docs/hyw_plugin.md`

`.env.example` 追加（放在 HYW 段）：

```dotenv
# 方式 A：静态密钥 + OpenAI 兼容中转（现状，保持不变，默认值见当前 .env.example）
HYW_API_KEY=
HYW_BASE_URL=https://openrouter.ai/api/v1
HYW_MODEL=gpt-4o

# 方式 B：Google 服务账号（Vertex AI OpenAI 兼容端点）
# 只需加下面这一行即可切换；路径支持 ~，相对路径按启动目录（仓库根）解析。
# 凭据 JSON 放在仓库外或 data/ 下（data/ 已被 .gitignore 忽略），不要提交进 Git
HYW_CREDENTIALS_FILE=
# 也可用标准变量 GOOGLE_APPLICATION_CREDENTIALS
HYW_VERTEX_LOCATION=global
# 可选：覆盖 Vertex 基址（留空自动推导，见上）
HYW_VERTEX_BASE_URL=
# 说明：service_account 模式下 HYW_BASE_URL 会被忽略（避免把 SA 令牌发给中转）；
# 切换后 HYW_MODEL 可留原值，不带 "/" 时自动补 "google/" 前缀
HYW_MODEL=gemini-3.8-flash
```

`docs/hyw_plugin.md` 增加「使用 Google 服务账号凭据」一节：原理（JWT→token→Bearer）、
配置项、区域与模型名规则、常见 400/401/403/404 的排查表、密钥轮换（换 JSON 自动失效缓存）、
以及「凭据文件权限与不要提交」的提醒。

### 4.7 `requirements.txt` / `pyproject.toml`

**不改**。签名依赖 `pycryptodome>=3.20.0`（已在依赖里，实测可导入）。
不新增 `google-auth`，理由是保持零新依赖、离线可跑，且交换逻辑不到 100 行。

---

## 5. 测试计划（`tests/test_hyw.py` 新增，沿用 unittest + MockTransport 风格）

1. **JWT 正确性**：`build_assertion()` 产物用 `RSA.import_key(private_key).publickey()` 验签，
   断言 header 的 `alg/kid`、claims 的 `iss/aud/scope/iat/exp`。
2. **token 交换请求形态**：MockTransport 断言 `Content-Type: application/x-www-form-urlencoded`、
   `grant_type` 正确、**不带 Authorization 头**、body 里含 assertion。
3. **缓存与刷新**：同一凭据连续两次 `bearer()` 只发一次 token 请求；
   把 `now` 推到剩余寿命 < `refresh_margin` 时再次刷新；`expires_in` 缺失时用保守默认值。
4. **并发去重**：`asyncio.gather` 4 个并发请求只发 1 次 token 交换（对应现有全局并发 4）。
5. **错误映射**：对 4 种 400 `invalid_grant`、401、403、404、`Malformed publisher model`
   逐一断言 `code` 与话术，且 `str(error)`、`repr(config)`、日志参数中**不含**私钥片段与 `client_email`。
6. **凭据文件异常**：路径不存在 / 非法 JSON / 缺字段 / `private_key` 非 PEM → `HywError`，
   文案固定、不回显内容；`~` 展开与相对路径按 cwd 解析（`monkeypatch.chdir` 断言绝对路径）。
7. **配置推导与优先级**（对应 §3.1，回归重点）：
   - `HYW_CREDENTIALS_FILE` 存在且 `HYW_BASE_URL=https://llm.hyw.mom/v1` 同时设置时，
     `base_url` **必须**是 Vertex 地址（不是 `llm.hyw.mom`），`auth_mode=service_account`；
   - 显式 `HYW_VERTEX_BASE_URL` 时以它为准；
   - 无 `HYW_CREDENTIALS_FILE`、有 `HYW_API_KEY` 时 `base_url` 逐字沿用 `HYW_BASE_URL`（现状回归）；
   - `HYW_MODEL=gemini-3.8-flash` → `google/gemini-3.8-flash`；`google/xxx` 原样保留；
     两者都不设时 `auth_mode=none`。
8. **响应容错**：`choices[0].message` 无 `content` → `HywError`（而非崩溃）。
9. **回归**：现有测试保持通过。改造前基线（只读实测）：`python -m unittest tests.test_hyw` 共 47 个测试，
   其中 46 个通过、1 个稳定失败（`test_same_user_parallel_answers_keep_their_own_replies_and_history`，
   2s `wait_for` 超时，根因见 §6.3，属传输层建链耗时而非本次改造引入）。
   改造后：**71 个测试全通过（`OK`，退出码 0，约 4.3s）**——新增的服务账号/错误映射/令牌缓存/
   模型调用四组测试全部通过，§6.3 的 `SSLContext` 复用修复同时把那个既有失败转为通过。
10. **凭据不出现在 repr**：`Secret("sa-token")` 的 `repr` 为 `<hidden>`，
    `bearer_headers()`/`form_data()` 的 `repr` 不含明文，`repr(ServiceAccount)` 不含
    `PRIVATE KEY` 与 `private_key_id`。
11. **签名失败不泄漏私钥**：patch `pkcs1_15.new` 抛错后抛出的 `HywError` 文案固定、
    不含 `PRIVATE KEY`，且 `__cause__ is None`（异常链不把私钥对象带进 traceback）。
12. **api_key 模式对拍**：把 git HEAD 的 `plugins/hyw` 物化到临时目录，用同一个 mock transport
    分别驱动新旧代码，逐字段比较线上请求（method/URL/headers/JSON body）与返回结果——
    success / 401 / 403 / 429 / 500 / empty-choices / 非 JSON 响应 **全部一致**，
    `Authorization` 仍逐字为 `Bearer <api_key>`；唯一差异是刻意改动的空内容文案
    （`模型服务返回了无法识别的响应。` → `模型返回了空内容，请重试或更换模型。`）。

端到端验证（人工，不写进单测）：真实凭据跑一次 `/q 问题` 与一次带图 `/q`，
确认卡片渲染、来源链接、引用追问续聊均正常。已执行结果见 §10。

---

## 6. 顺带发现的三个相邻问题（与本次改造相关但独立）

1. **DNS 曾把外部域名解析到 `198.18.0.0/15`（代理 fake-ip）**，
   导致 `web.py` 的 SSRF 护栏（要求 `is_global`）拒绝**一切**外部主机：
   当时实测 `lite.duckduckgo.com`、`html.duckduckgo.com`、`example.com`、`multimedia.nt.qq.com.cn`
   全部返回 `address_policy`。后果：本机跑 `/q` 时 `web_search`/`web_fetch` 必然失败，
   带图提问的**图片下载**也会失败（`handlers.model_content` 走同一个 `download()`）。
   **后续复测该现象已消失**（DNS 恢复为真实公网 IP：`lite.duckduckgo.com → 108.160.167.158`、
   `example.com → 104.20.23.154`、`googleapis.com → 142.251.34.196` 等），
   但同一台机器上搜索仍因**直连超时**失败、经 `http://127.0.0.1:7890` 才成功——
   即当前症状的成因已从「护栏拒绝」变成「出口网络不可达」。方案未改护栏。
2. **模型请求路径不受护栏影响**：token 交换与 Vertex 调用不走 `validate_url`。
   注意此处原结论「直连 200」在本机当前网络已不成立（直连 `ConnectTimeout`，
   需经代理），详见文首「实施中发现的新约束」。
3. **每次请求重建 httpx 传输层，单次约 1 秒（已实测，与本次改造同路径）**：
   `handlers.run_request` 每个请求都新建 `httpx.AsyncHTTPTransport`，而 httpx 在 `verify=True`
   时用 certifi 证书包构建 TLS 上下文（`certifi/cacert.pem`，236095 字节），实测单次
   `load_verify_locations(certifi)` 约 0.96s，而裸 `ssl.create_default_context()`（本机
   `ssl` 默认 verify paths 的 `cafile=None`）只要 0.03s。复用一个 `SSLContext` 后，
   4 个 transport 从 3.797s 降到 0.102s。
   可观测后果：`tests/test_hyw.py::...::test_same_user_parallel_answers_keep_their_own_replies_and_history`
   的 2s `wait_for` 稳定超时（改造前 47 个测试中唯一失败项）。
   **已在本轮实施中修复**（§10）：`handlers.py` 新增模块级 `_ssl_contexts` 缓存 +
   `shared_ssl_context()` + `make_transport(proxy)`，两处 transport 创建点都改用它；
   实测 `httpx.AsyncHTTPTransport` 从 **1211 ms/call 降到 0.0 ms/call**，
   套件从 ~9s 降到 ~4.3s，上述测试由失败转为通过。
   实现上不需要打 httpx 私有内部：`httpx.create_ssl_context` 是公开顶层导出
   （在 `httpx.__all__` 中），直接把缓存好的上下文传给 `verify=` 即可——
   `create_ssl_context(verify=<SSLContext>)` 会原样返回该对象，从而跳过 certifi 读取。
   缓存的键含 `SSL_CERT_FILE` / `SSL_CERT_DIR`，与 httpx 的 `trust_env` 语义保持一致。

---

## 7. 不做的事

- 不引入 `google-auth`/`cryptography`/`PyJWT`，不改 `requirements.txt`。
- 不改 XML 工具协议、`assets/system_prompt.txt`、卡片渲染、历史与并发策略。
- 不删除现有中转（`HYW_BASE_URL=https://llm.hyw.mom/v1`）路径，保留可回退。
- 不支持「远程图片 URL 交给 Vertex 自己抓」（实测 400），维持本地下载 + 内联 base64。
- 不做多凭据轮换、配额统计、按用户计费等上游 `costs.py` 类能力。

---

## 8. 验收标准

- `HYW_CREDENTIALS_FILE` 指向服务账号 JSON 后，`/q 问题` 能正常返回回答与来源卡片，日志出现
  `auth=service_account`，且**不出现**任何私钥/令牌内容。
- 令牌只在其寿命剩余 < 5 分钟时重新交换；4 路并发只交换一次。
- 私钥错误、项目无权限、模型名缺前缀、区域不支持四类故障，聊天里给的是可操作的中文提示，
  且不含上游正文与凭据。
- `HYW_API_KEY` 模式（现有部署）行为与文案零变化，现有测试全绿。
- 仅新增 `HYW_CREDENTIALS_FILE` 时（`.env` 里 `HYW_BASE_URL` 仍指向 `llm.hyw.mom/v1`），
  实际请求**必须**打到 `aiplatform.googleapis.com`（§3.1 的优先级规则），不得把 SA 令牌发给中转；
  显式 `HYW_VERTEX_BASE_URL` 时以其为准。
- `docs/hyw_plugin.md`、`.env.example` 覆盖新配置，读者可照文档完成接入。

---

## 9. 工作量与风险

| 项 | 规模 | 风险 |
| --- | --- | --- |
| `google_auth.py`（签名/交换/缓存/并发） | M | 低（链路已实测打通） |
| `config.py` 模式选择与端点推导 | S | 低（需保证 api_key 模式回归不变） |
| `agent.py` token 注入 + 响应容错 | S | 低 |
| 错误语义映射 | S | 低（Google 错误码形态已采集） |
| 测试与文档 | M | 低 |

主要不确定项（**未验证**，实施时需注意）：
- 该服务账号项目的配额与计费状态未知；高并发下是否 429 未实测。
- `locations/global` 对部分模型可能不可用（实测 `google/gemini-2.5-flash`、
  `google/gemini-2.5-pro`、`google/gemini-3.8-flash` 在 global 均可用，其余模型未逐一验证）。
- 1×1 像素等极小图会被 Vertex 拒绝（400 `Provided image is not valid`）；
  插件现有管线会统一转 JPEG q85，正常情况下不会命中。
- `client_email`/`private_key_id` 是否属于「可记录」信息按团队口径定；本方案默认不记录。
- **网络可达性随部署网络变化**：实测本机 `googleapis.com` 直连不通、需代理，
  而 `llm.hyw.mom` 只有直连可达。切到服务账号模式时 `HYW_PROXY` 需一并调整；
  该结论只对本机当前网络成立，部署环境需各自验证。
- 服务账号模式的搜索/图片链路未变，仍受 §6.1 的 fake-ip DNS 问题影响。
- **该服务账号项目存在真实配额限制**：2026-09-11 实测一次全链路请求拿到 `HTTP 429`，
  插件按 `quota` 话术提示「模型请求过于频繁或额度不足，请稍后重试。」。
  这是上游配额状态而非代码缺陷，本次**未**实现重试/退避。

---

## 10. 端到端验证结果（2026-09-11，真实凭据）

用用户提供的服务账号 JSON（始终留在仓库外的附件路径，从未复制入库）驱动**真实命令处理器**
`handlers.handle_hyw()`，以最小 `Session` 替身收集回复。刻意同时设置
`HYW_BASE_URL=https://llm.hyw.mom/v1` 与伪造的 `HYW_API_KEY=should-not-be-used`，
以证明两者都被忽略。14 项检查全部通过：

| 检查 | 结果 |
| --- | --- |
| `auth_mode` = `service_account` | OK |
| 模型自动补前缀 → `google/gemini-3.8-flash` | OK |
| `base_url` = `https://aiplatform.googleapis.com/v1/projects/<project>/locations/global/endpoints/openapi` | OK |
| 中转 `HYW_BASE_URL` 被忽略（URL 里无 `llm.hyw.mom`） | OK |
| 纯文本 `/q` 返回回答 | OK（「你好呀，我是たえ……」） |
| 带图 `/q` 返回回答且认对颜色 | OK（回复「红色」） |
| 坏模型名 → 404 `sa_model` 话术 | OK |
| 错误文案不含项目号 | OK |
| 日志不含私钥 / access token / `client_email` | OK |

日志佐证：`[hyw] request routes: model=proxy search=proxy auth=service_account`，
`POST https://oauth2.googleapis.com/token "HTTP/1.1 200 OK"`，
`POST https://aiplatform.googleapis.com/.../chat/completions "HTTP/1.1 200 OK"`，
以及被忽略的警告
`[hyw] service account credentials in use: HYW_BASE_URL is ignored, endpoint derived from the credentials file`
（不含被忽略的值）。

---

## 11. 实施中发现并修复的密钥泄漏（重要）

**问题**：entari 把 loguru 的 handler 显式配置为 `diagnose=True`
（`arclet/entari/logger.py` 默认 stdout handler，`apply_log_save` 的文件 handler 亦然），
并把 `sys.excepthook` 接到 loguru。`diagnose=True` 会**渲染 traceback 每一帧抛错行上被引用变量的值**——
实测：普通局部变量、属性、关键字参数、f-string、字典字面量、以及「先赋值再调用」全部会泄漏。

这导致本次新增的两个秘密进入日志：
1. `agent.py` 里 `return await _request(client, config, messages, token)` 这一行的 `token` 局部
   （活的 Vertex access token，实测哨兵出现 2 次，真实运行中确实写入了 `ya29.…` 明文）；
2. `build_assertion` 里 `key` 局部——pycryptodome 的 `RsaKey.__repr__` 会打印
   `n/e/d/p/q/u`，**含私钥指数**。

**注意这是本次改造引入的回归**：`git show HEAD:plugins/hyw/agent.py` 的原始
`complete()` 直接内联 `f"Bearer {config.api_key}"`，不存在 `token` 这个局部。

**修复（两道，缺一不可）**：
1. `Secret` 持有类 + 打码 `__repr__` 的 dict 子类（`_MaskedHeaders` / `_MaskedForm`）：
   `repr(Secret("…")) == "<hidden>"`，`bearer_headers()` / `form_data()` 的 `repr` 也不含明文。
   `TokenProvider` 缓存 `Secret`，`bearer_for()` 在 api_key 模式也包成 `Secret`。
2. **把危险操作放进会吞掉异常的独立函数**：`_sign(account, signing_input) -> bytes | None`
   捕获全部异常（含 `RSA.import_key`）并返回 `None`，它自己的帧永不进入 traceback；
   `build_assertion` 随后在**只引用 `account`（repr 已打码）与 `signing_input` 的干净行**上抛错。
   实测证明只写 `raise … from None` **不够**：调用方的帧仍被记录、其行上的 `key` 照样被渲染
   （探针 `v1 RsaKey_repr=2` vs 修复后 `v2 = 0`）。

另外 `ServiceAccount` 的 `client_email` / `private_key_id` / `private_key` / `source`
全部 `field(repr=False)`，只留 `project_id` 与 `token_uri`。

**复验**：探针覆盖 8 条失败路径——model-429、model-timeout、exchange-400、exchange-timeout、
sign-failure、bad-json、bad-pem、api_key-429——哨兵（token / assertion / api_key /
`RsaKey(n=` / PEM 正文）**全部 `leaks=none`**，api_key 这条既有路径同样干净。
对应的回归测试是 `test_secrets_never_appear_in_repr` 与
`test_signing_failure_is_chat_safe_and_keeps_key_out_of_the_frame`。

**一般化结论（后续写任何涉及秘密的代码都适用）**：秘密值不能以「可能抛错的那一行上被引用的名字」
形式存在；要么包装成 repr 安全的对象，要么让抛错发生在只引用安全名字的行上。
