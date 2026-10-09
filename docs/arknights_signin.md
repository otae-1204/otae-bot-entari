# 明日方舟森空岛签到

独立插件 `plugins/arknights/`，提供鹰角账号绑定与明日方舟（森空岛）每日签到。
它不依赖 `plugins.endfield`：导入终末地包会执行其入口并注册命令，因此本插件只复用
`otae_bot.*` 共享设施，并自带客户端、加密与存储。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/ak 帮助` | 子命令与选择器说明 |
| `/ak 绑定` | **仅私聊**。Token（`data.content`）或手机号验证码两种方式 |
| `/ak 账号` | 查看已绑定角色、渠道与编号（群聊隐藏完整 UID） |
| `/ak 主账号 <选择器>` | 设置默认角色 |
| `/ak 解绑 <选择器>` | **仅私聊**。删除单个角色，并清理无引用凭据 |
| `/ak 签到 [全部\|编号\|昵称\|UID后四位]` | 默认签到全部角色 |

别名根命令：`/明日方舟`、`/arknights`。

选择器只做精确匹配，匹配顺序为：主账号关键字 → 完整 UID → UID 后缀（≥4 位）→
编号（`/ak 账号` 中从 1 开始的序号，少于 4 位）→ 完整昵称。**不存在模糊匹配**：命中 0 个时提示
未找到，命中多个时列出不带编号的候选，并提示先用 `/ak 账号` 查看完整列表编号，绝不会任选一个账号执行签到或解绑。
两个补充规则防止误选：

- 少于 4 位的前缀**不**参与 UID 后缀匹配（`234` 不会匹配 `10001234`）；
- ≥4 位且未命中任何 UID 后缀的纯数字**不会**退化成编号（与终末地 `resolve_role`
  对 4 位数字的处理一致），因此 `0001`、`0002` 不是第 1、2 个账号，`5678` 在只有 2 个角色时
  也只是“未找到”。`/ak 签到`、`/ak 主账号` 与 `/ak 解绑` 共用这条规则。

`/ak 解绑` 不接受空选择器，也不接受“全部”，因此不会误删主账号。

签到默认“全部”。单角色失败（凭据失效、网络错误、并发重复请求、数据库异常）只影响它自己，
其余角色继续签到。命令不会注册任何自动签到定时任务。
统一入口 `/签到` 会依次签到终末地与明日方舟的全部角色，将两款游戏的结果卡上下
合成一张图片发送；它与 `/ak 签到` 共用同一把角色防重锁，因此同一角色只提交一次请求。
详见 [统一签到入口](unified_signin.md)。

`/ak 绑定` 的整个对话都在 `sensitive_input` 里进行（与终末地绑定一致）：用户回复的 Token、
手机号与短信验证码在 Entari 的 `[message]` 日志中被遮盖，对话结束或出错后自动恢复。

## 配置

```dotenv
# 必填；Base64 编码的 32 字节密钥
ARKNIGHTS_CREDENTIAL_KEY=
```

- 生成：`python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"`
- 只读取 `ARKNIGHTS_CREDENTIAL_KEY`，**不会**回退或复用 `ENDFIELD_CREDENTIAL_KEY`，
  两款游戏的密钥可以各自轮换、吊销。留空时 `/ak 绑定` 与 `/ak 签到` 会直接返回
  “未配置环境变量 ARKNIGHTS_CREDENTIAL_KEY”的提示；插件不会自动生成或改写 `.env`。
- 密钥只用于 AES-256-GCM 加密账号 Token（`associated_data = b"arknights-account-token-v1"`），
  不会写入日志或聊天消息。

## 存储

独立 SQLite 文件 `data/arknights/arknights.db`，与 `data/endfield/` 完全隔离。

- `credentials`：按 `(qq_user_id, token_fingerprint)` 唯一，密文列存 nonce/ciphertext/tag。
- `roles`：按 `(qq_user_id, uid, game_id)` 唯一；每个 QQ 用户最多一个主账号（部分唯一索引）。
- 一个 QQ 可绑定多个鹰角账号（多行 `credentials`），一个账号可含官服与 B服 多个角色。
- 重复绑定同一角色会原地更新（去重），不会新增行。
- 解绑后若某条凭据已无角色引用，会同步删除，避免残留密文。
- 凭据读取按 `(credential_id, qq_user_id)` 双重限定：即使传入别的用户的角色对象，
  也读不到对方的密文，只会得到“凭据不存在或不属于当前用户”。

存储与 HTTP 客户端都是**懒初始化**：导入插件不会创建 `data/` 目录或打开连接。
连接与客户端在 Entari `Cleanup` 事件中关闭。HTTP 客户端使用
`verify=shared_ssl_context(trust_env=False)`；首次创建前先用 `ashared_ssl_context` 在线程里
构建共享 TLS 上下文，因此第一次签到不会在事件循环里同步读取 CA 证书。

## 协议

森空岛（`zonai.skland.com`）与鹰角通行证（`as.hypergryph.com`）。参考公开实现：

- [ProbiusOfficial/Skland_API](https://github.com/ProbiusOfficial/Skland_API) — 端点、
  `bindingList` 字段、`{"uid","gameId"}` 请求体与 `awards[].resource.name` 响应。
- [AEtherside/skland-daily-attendance](https://github.com/AEtherside/skland-daily-attendance) —
  现行客户端，用 `GET /api/v1/game/attendance` 的 `records` 判定“今日已签到”。

流程：

1. `POST https://as.hypergryph.com/user/oauth2/v2/grant`
   `{"appCode":"4ca99fa6b56cc2ba","token":<账号Token>,"type":0}` → `data.code`
2. `POST https://zonai.skland.com/api/v1/user/auth/generate_cred_by_code`
   `{"code":<授权码>,"kind":1}` → `data.cred` + `data.token`（签名密钥）。
   授权码一次性使用：`/api/v1/...` 路由缺失时重新取码并改用 `/web/v1/...`；
   两条路由都不可用时沿用第三方客户端做法，把授权码当 `cred`，再用
   `GET /web/v1/auth/refresh` 取签名密钥。
3. `GET /api/v1/game/player/binding`（签名）→ `data.list` 中 **仅取 `appCode == "arknights"`**
   的 `bindingList`，每条即一个角色：`uid`、`gameId`、`channelMasterId`（渠道标识，与游戏 ID 不同）、`channelName`、`nickName`。
4. 签到：
   - 优先使用绑定返回的 `gameId`；旧版绑定缺此字段时，已确定为明日方舟的条目使用官方游戏 ID `1`，B服也不使用渠道 ID `2`。
   - `GET /api/v1/game/attendance?uid=<uid>&gameId=<gameId>`（签名）判定今日是否已签。
   - `POST /api/v1/game/attendance`，请求体 `{"uid":<uid>,"gameId":<gameId>}`（紧凑分隔符）。
     返回 `data.awards[].resource.name` 与 `count`。

### 签名

```text
canonical = path + (GET 用 query，POST 用请求体字符串) + timestamp
          + '{"platform":"1","timestamp":"<ts>","dId":"...","vName":"1.45.1"}'
sign      = md5( hmac_sha256( sign_token, canonical ) )
```

- 四个字段顺序固定（`platform`、`timestamp`、`dId`、`vName`），不可排序。
- `timestamp` = 取 `cred` 响应的时间戳 + 本地已过秒数，避免设备时间偏差。
- **签名体与发出的字节完全一致**：请求体字符串只构造一次，既用于签名也作为
  `content=` 原样发送（不使用 `json=`，避免二次序列化）。

### 已签到判定

只有明确的“今日已签”信号才产生 `already`：

1. 状态接口返回的 `data.records[].ts` 落在上海时区（UTC+8）的今天；或
2. 签到 POST 返回业务码 `10001`（且操作必须是签到本身）。

`401`/`403` 一律视为凭据失效并报错，**绝不会**被误判成“已签到”。其它业务码只回显
数字码与本插件自己的操作名，不回传服务端的自由文本、Token 或 URL。

### 凭据失效与重试

签名请求（查询绑定、签到状态、签到）被拒绝且业务码或 HTTP 状态为 `10000`、`10003`、`401`
时（与终末地客户端的 `_SKLAND_CONTEXT_RETRY_CODES` 相同），先丢弃缓存的 `cred`/签名密钥，
用账号 Token 重新换取一次上下文再完整重试一次；被拒绝的 POST 没有签到成功，重试时仍会先读状态，
因此已经签到的日子只会显示“今日已签到”。重试后仍被拒绝时提示
“森空岛凭据已失效或登录状态异常，请重新私聊使用 /ak 绑定”。并发请求被同一个旧上下文拒绝时
只换取一次新上下文。其它业务码不重试。

业务码回显规则：只有**纯十进制数字**才会被写进提示与日志；`code` 字段若含自由文本、
十六进制串或疑似令牌片段，一律折叠为 `unknown`。非数字码因此永远不可能等于
`10001`/`401`，既不会误报已签到，也不会把服务端任意内容带进群聊。

### 奖励明细

`data.awards[]` 的每一项都保留（不会因为字段缺失而静默丢弃），名称按
`resource.name` → `resource.id` → `签到奖励` 回退。数量策略：

- 可用的数量（含显式 `1`）原样保留；
- 缺失、`null`、布尔、负数或非数字一律取 `0`，**不会**臆造为 `1`，
  也不会把奖励藏起来——卡片宁可显示 × 0，也不夸大或隐瞒服务端返回的内容。

### 当月累签（monthly_count）

与终末地签到卡一致，卡片展示“当月累签 N 天”。口径与官方签到页一致
（`signIn-CDDzUwtl.js` 的 `checkInCount = calendar.filter(e => e.done).length`）：

- **首选**：官方 `data.calendar[]` 中 `type == "daily"` 的条目里 `done is True` 的个数。
  `first` / `activity` 一律不计入，历史 `records` 也不会覆盖已经解析成功的日历。
- **降级**：日历缺失、为空、含非对象条目（无法可靠编号）或完全没有 `daily` 条目时，
  才改用 `data.records[].ts`：按 UTC+8 折算日期，只统计 `data.currentTs`
  （缺失时回退本地 UTC+8）所属月份，并按日期去重。`records` 是历史记录，可能包含
  `first`/`activity` 发放，所以只在没有可用 `daily` 日历时才使用。
- 格式正确但为空的结构是真实的 `0`；缺失或整段无法解析是 `None`
  （卡片隐藏该行，而不是编造 0）。
- 签到流程：先做一次状态读取；若今日已签，这次读取即包含今天，直接复用，不再发 POST。
  POST 成功后**再读一次**状态，用新日历刷新累签与里程碑；这次读取失败只是让
  `monthly_count` 为 `None`、`milestones` 为空，**不会**影响已经成功的签到结果与奖励，
  也**不会**回退使用签到前的旧日历。POST 返回“已签到”（`10001`）时同样补读一次。

### 累计签到第 N 天与合成玉里程碑

[官方签到页](https://game.skland.com/arknights/sign-in)的
[编解码器](https://assets.skland.com/_static_assets/game-tools/dist-BZImVwlH.js)先
`calendar.filter(e => e.type === "daily")`，再 `.map((e, i) => ({...e, day: i + 1}))`：
**累计第 N 天 = 过滤 `daily` 之后的第 N 个条目**（1 起），与自然日期无关，
`first`/`activity` 不参与编号。

`plugins/arknights/client.py`：

- `parse_attendance_milestones(payload)` 只从当次响应 `type == "daily"` 的条目中，
  取 `resourceInfoMap[resourceId].name == "合成玉"` 的节点，产出
  `AttendanceMilestone(day, name, count, done, available)`。
  名称一律来自 `resourceInfoMap`（不猜资源 ID），数量一律来自响应的 `count`；
  **不内置任何 5/10/15/25/30 档位表**。
- 编号先于奖励筛选：先按 `daily` 顺序累计 `day`，再判断是否合成玉，因此被跳过的节点
  不会让后续天数前移。
- 名称缺失、数量不是正整数、`done`/`available` 不是布尔值的节点不展示；
  但它的 `daily` 位置照常占用。
- 日历里出现非对象条目时无法保证编号正确，此时**不生成任何节点**（宁可不显示）。
- `daily_calendar_entries(data)` 是上述“可用 `daily` 日历”的判定入口：缺失/为空/
  含非对象条目/没有 `daily` 时返回 `None`。

实际卡片的奖励数值来自接口响应；预览与测试里的数字均为**合成数据**，
用于验证解析与布局，**不构成任何固定奖励承诺**。

终末地与明日方舟的差异：终末地用 `calendar[].done` 统计当月累签与 `hasToday`；
明日方舟额外有 `records[]`，且累计天数是 `daily` 序号。

边界处理：月累签只接受 0–31 的整数；daily 条目的 done 必须全部为布尔值，否则回退记录或隐藏未知进度。越界/非有限时间戳按无效记录处理，不能将已成功签到降级为失败。

## 卡片与回退

- 视图模型：`plugins/arknights/models.py`（纯数据）。
- 渲染：`plugins/arknights/rendering/cards.py`，复用
  `otae_bot.infrastructure.rendering.browser.screenshot_web_element`。
- 奖励和里程碑使用 `plugins/arknights/assets/rewards/` 中用户已审核的 12 个图标。
  `manifest.json` 记录名称、官方资源 URL 与原图/处理图哈希；仅裁去透明边缘并居中，
  通过固定名称映射以本地 PNG 的 data URL 嵌入，不在签到时请求远程图标。
  未收录的奖励保留文字，不猜测相似图标。所有昵称/渠道/消息经过 HTML 转义，
  其中 URL 形状的片段在渲染前被替换为占位符。
- 沿用上游新版 zmd 签到卡布局：1280px 宽、430px 最小高度（QQ 预览宽高比低于 3:1）、纯浅灰底、圆角白色账号行、浅灰圆角奖励框与当月累签块。明日方舟标题栏采用蓝黑底色（#142536）与亮蓝底线（#54b9ef），以区分终末地。
- 合成玉里程碑采用「已领 / 本月合计 · 签到格轨道 · 下一档」三段布局。`daily_progress` 保留接口 `daily` 条目的布尔 `done` 状态，格数和已签格直接来自该数据，不按自然月份猜测长度，也不把累计天数强行画成连续前缀。已领总量和节点状态以各奖励的 `done` / `available` 为准。
- 缺少完整日历但仍有可靠里程碑时，只展示已有奖励节点，不画完整轨道；无可靠节点或签到失败时隐藏里程碑。日历与里程碑复用原有状态请求，POST 后刷新失败时一起清空。
- 截图失败、内容超高或超过 60 个角色时回退为完整文本结果，不发送截断后的部分结果。
- 图片始终展示脱敏 UID（`****1234`）；群聊文本同样脱敏，私聊文本可展示完整 UID。

## 离线预览

```powershell
.\.venv\Scripts\python.exe scripts\render_arknights_attendance_preview.py
```

使用合成账号（虚构昵称与 UID）在 `output/arknights-attendance/` 生成
HTML、PNG 与 `validation.json`，覆盖单角色、混合结果、长文本、空态、全部已签、全部失败、12 角色、当月累签，以及待领取、可领取、全部领取、缺少节点、缺少日历、已签到和密集节点，共 15 种场景。预览在空临时目录加载共享渲染配置，不读取工作区 `.env`、真实账号或数据库。

附加 `--zmd-reference` 可生成同一组模拟数据的 zmd 对照图，只提取纯绘图函数，不加载终末地插件入口。
工作分支可能缺少上游卡片更新，对照前先 fetch；使用 `--zmd-ref upstream/main` 明确读取已获取的上游版本：

```powershell
git fetch upstream
.\.venv\Scripts\python.exe scripts/render_arknights_attendance_preview.py --zmd-reference --zmd-ref upstream/main
```

`zmd-reference-source.txt` 会记录实际使用的提交 SHA；不指定 `--zmd-ref` 时使用工作区源码。
本次视觉基准为 `9497992` 中的终末地签到渲染，包含 `faa3c07` 的 QQ 预览比例修复和
`27b26ec` 的里程碑轨道。统一签到直接复用终末地现有渲染函数，并完整传递月历与里程碑，
与单游戏签到保持一致。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_arknights_attendance.py tests/test_arknights_final.py tests/test_arknights_calendar.py tests/test_arknights_style.py
.\.venv\Scripts\python.exe -m pytest tests/test_architecture.py tests/test_group_features.py
```

测试覆盖真实签名与请求体一致性、仅解析 arknights 绑定（官服/B服）、登录与异常响应、
加密往返、重复绑定与跨 QQ 隔离、凭据读取的用户归属校验、选择器优先级与歧义/未找到、
解绑清理、默认签到全部、单角色失败继续（含凭据解密失败与存储异常）、重复签到不算失败、
奖励数量回退策略、业务码只回显数字、缺少密钥文案（且不回退终末地密钥）、群聊拒绝绑定、
渲染失败文本回退、`0001`/`0002` 这类非后缀长数字不会选中或解绑角色、
`10000`/`10003`/HTTP `401` 换取一次新上下文后重试（B服角色请求体保持 `gameId` 为 `1`）、
共享 TLS 上下文在线程里构建、Token/手机号/验证码不进入 `[message]` 日志。

`tests/test_arknights_calendar.py` 单独覆盖累计签到：`first`/`activity` 不参与 `daily`
编号、跳过畸形节点不移动天数、数量来自响应而非档位表、非对象日历条目不生成节点、
`daily` 优先于 `records` 统计当月累签、`available`/`done` 判定今日已签（空 `daily`
不会凭空判为已签）、POST 后刷新进度、刷新失败保持签到成功且不下发旧进度、
以及里程碑在视图与文字降级中的状态标注（已领取优先于可领取）。

自动化测试使用临时数据库与模拟接口，包含真实 Entari 的插件加载、命令分发、卸载事件
及重新加载检查。图片预览使用样例账号，通过正式渲染器与拼接函数生成。
人工接口检查已覆盖短信登录、角色发现、签到状态读取和已签到分支；真实首次签到 POST、
B服账号、凭据失效后的重试及机器人平台端到端发送仍需部署环境验证，当前由模拟测试覆盖
（没有真实 B服账号的验证记录）。
