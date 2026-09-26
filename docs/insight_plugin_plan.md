# `/统计` 命令使用统计插件方案（plugins/insight）

状态：**方案定稿，未实现**。本文档只描述设计，仓库中尚不存在 `plugins/insight/`。

统计目标：命令使用频率、用户数量、日活量。

---

## 0. 已定口径（本文档的基准）

| 决策项 | 结论 |
| --- | --- |
| 统计口径 | **命令口径**（消息口径不采用） |
| 日活定义 | 当天 `outcome='executed'` 的**去重用户数** |
| 群开关 | insight **加入 `PROTECTED_PLUGINS`**，任何群都无法关闭它 |
| 权限 | v1 仅 SuperUser 可查询 |
| 交付 | 先出方案，不实现 |

---

## 1. 实测事实（设计依据，均可复现）

以下结论来自本机对 `C:\Code\qqbot\bot-entari` 的实测探针，不是推测。

### 1.1 命令清单

- 已注册 dispatcher：**49 个**，覆盖 **15 个**插件。
- 前缀配置：`EntariConfig.instance.basic.prefix == ['/']`，`nickname` 为空。
- 命令名 `统计` / `insight` / `usage` 在 Alconna trie 中**均不存在**，可安全用作新命令名与别名。

### 1.2 事件语义（决定采集点）

| 事件 | 实测行为 | 是否可用 |
| --- | --- | --- |
| `CommandReceive`（before_parse） | 每个 trie 前缀命中的候选各触发一次；是「**尝试解析**」信号 | 用于 `unparsed` 补录 |
| `CommandParse`（after_parse） | 解析完成后触发，带 `result: Arparma` | **主采集点** |
| `CommandExecute` | 由 `command.execute()` 程序化发布；**入站消息从不触发** | **不可用** |
| `CommandOutput` | 仅「未匹配但有帮助文本」分支发布，是 `CommandParse(matched=False)` 的子集 | 冗余，不用 |

`CommandParse` 触发时的实测分布：

| 输入 | `CommandParse` | `matched` | handler |
| --- | --- | --- | --- |
| `/ef` | 1 次（`终末地`） | `True` | 执行 |
| `/efs` / `/bili` / `/q` / `/更新日志` / `/radar` | 1 次 | `True` | 执行 |
| `/mod --help` | 1 次 | `False` | 不执行（帮助路径） |
| `/e` / `/b` / `/steam` / `/资料` | **0 次** | — | 不执行（head 不匹配） |
| `/mod`（缺必填参数） | **0 次**，只有 `CommandReceive` | — | 不执行（静默失败） |

**关键推论**：仓库内 `grep "Assign("` 无任何命中，即不存在 propagator 否决路径；因此 `matched=True` ⇒ handler 必然执行，`CommandParse` 是「已执行」的可靠信号。

### 1.3 命令 → 插件归属

遍历 `_commands.subscribers`，对每个 subscriber 取 `get_propagator(AlconnaSuppiler).cmd.name` 作为命令名，用 `plugin_key(sub.callable_target.__module__)` 取插件名。实测 **49/49 全部命中**，例如：

```
('endfield', '终末地')   ('steamInfo', 'steamgame')   ('hyw', 'q')
('bilibilibot', 'bili')  ('minecraft_plugin', 'ping') ('peek', 'peek')
```

`_extra["commands"]` 为空，不可用；**必须**走 `get_propagator` 路线。

### 1.4 群开关盲区（本方案的核心约束）

`GroupFeatureGate`（`otae_bot/adapters/feature_gate.py`）以 **priority `-100`** 挂在各插件 scope 的订阅者上，**跑在 Alconna 解析之前**：插件被禁用即 `STOP`。

实测：

```
ENABLED   → [('PARSE', 'q')]
DISABLED  → []                 # /q 被拦，零事件
DIS_HELP  → [('PARSE', 'help')]  # /help 属 help_plugin，未被拦
```

**结论：被群开关关闭的插件，其命令不产生任何 `CommandParse`/`CommandReceive`，命令口径无法统计。** 这不是 insight 注册位置能解决的问题——事件在 `AlconnaSuppiler.supply()` 内部才 `post`，被 gate STOP 后根本不会发布。

### 1.5 事件字段

`session.event.message.id` / `.user.id` / `.guild.id` / `.channel.id` / `.channel.type`、`session.account.platform` / `.self_id` 均可取。

`CommandReceive` 的监听签名实测可用（参数名 `message`，内容已去前缀）：

```python
async def on_receive(session: Session, command: Alconna, message: MessageChain) -> None
```

---

## 2. 采集口径

单表 + `outcome` 字段区分三态：

| `outcome` | 触发条件 | 含义 |
| --- | --- | --- |
| `executed` | `CommandParse` 且 `matched=True` | 真正执行 —— **使用频率 / 日活主口径** |
| `help` | `CommandParse` 且 `matched=False` | 看了帮助或用例（`--help`、缺参带提示） |
| `unparsed` | 只有 `CommandReceive`，无对应 `CommandParse` | 尝试了但没解析出来（如 `/资料`、`/mod` 缺参） |

**日活 = 当日 `outcome='executed'` 的去重用户数。** `help` 与 `unparsed` 不计入日活，但计入命令热度明细。

### 2.1 `unparsed` 噪音控制

`CommandReceive` 对每个前缀候选各触发一次，例如 `/peek add` 会同时触发 `ping` 与 `peek` 两个候选的 RECV。为避免把「同一条消息」记成多次未解析：

> **仅当该消息只有 1 个 RECV 候选、且整条消息没有任何 `CommandParse` 时**，才记 `unparsed`；否则丢弃。

整体可由 `INSIGHT_TRACK_UNPARSED`（默认 `true`）关闭。

### 2.2 去重

`UNIQUE(message_id, command_path)`。实测无重复触发，但保留约束以抗重放/重试/热重载。

---

## 3. 盲区处理（明确取舍）

**不硬补，如实暴露。**

- `/统计` 系列输出**固定带一行脚注**：`※ 仅统计生效范围内的调用；群内被关闭的插件不计入。`
- 补偿方案（**v1 不做**，列入 v1.1）：在 `MessageCreatedEvent` 层做「影子匹配」——用 `_commands.trie` 自行匹配文本，再结合 `feature_store.is_enabled(scope, plugin)` 判定「本会执行但被禁」。代价是重复解析、前缀碰撞、与框架语义可能漂移。

理由：命令口径已由用户选定；影子层会引入第二套解析真相，风险高于收益。先把已知缺口写进输出，比给出一个来源可疑的补数更诚实。

---

## 4. 插件分层

```
plugins/insight/
├── __init__.py      # 仅 from .handlers import *
├── config.py        # INSIGHT_* 环境变量（唯一读环境变量的模块）
├── models.py        # 纯 dataclass，不碰 sqlite / 事件
├── store.py         # SQLite WAL，唯一落盘层
├── registry.py      # 命令 → 插件归属索引（运行期解析 + 缓存）
├── collector.py     # 事件订阅 + 内存缓冲 + flush
├── service.py       # 聚合查询（纯 SQL，不出网、不拼文案）
├── formatters.py    # 纯文本渲染
└── handlers.py      # /统计 → service → formatters
```

约束（`tests/test_architecture.py` 强制）：

- `otae_bot` **不得** import `plugins`；
- `plugins/insight/__init__.py` **只能** `from .handlers import *`（模块 docstring 之外的语句会失败）。

参照 `plugins/radar` 的分层与文档风格；SQLite 约定（WAL、`row_factory = sqlite3.Row`、`Path("data") / ...`）对齐 `plugins/bilibilibot/store.py`。

---

## 5. 数据模型

`data/insight/insight.db`

```sql
CREATE TABLE IF NOT EXISTS command_usage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,            -- unix 秒（UTC）
    day          TEXT    NOT NULL,            -- 'YYYY-MM-DD'（Asia/Shanghai）
    platform     TEXT    NOT NULL,
    self_id      TEXT    NOT NULL,
    scope_kind   TEXT    NOT NULL,            -- 'group' | 'private'
    group_id     TEXT    NOT NULL DEFAULT '',
    user_id      TEXT    NOT NULL,
    command_name TEXT    NOT NULL,            -- Alconna.name
    command_path TEXT    NOT NULL,            -- 'Alconna::bili'
    plugin       TEXT    NOT NULL DEFAULT '', -- 未归属时为空串
    alias_used   TEXT    NOT NULL DEFAULT '',
    outcome      TEXT    NOT NULL,            -- 'executed' | 'help' | 'unparsed'
    message_id   TEXT    NOT NULL,
    UNIQUE(message_id, command_path)
);

CREATE INDEX IF NOT EXISTS ix_usage_day_cmd  ON command_usage(day, command_name);
CREATE INDEX IF NOT EXISTS ix_usage_day_user ON command_usage(day, user_id);
CREATE INDEX IF NOT EXISTS ix_usage_day_grp  ON command_usage(day, group_id);
CREATE INDEX IF NOT EXISTS ix_usage_plugin   ON command_usage(plugin, day);
```

跨保留期保活（明细会清理，总用户数不能随之丢失）：

```sql
CREATE TABLE IF NOT EXISTS user_profile (
    platform    TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    first_day   TEXT NOT NULL,
    last_day    TEXT NOT NULL,
    total_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, user_id)
);
```

### 5.1 `alias_used` 还原

在 `command.prefixes` 中取**消息内容的最长前缀匹配**。实测 `Alconna.command` 为空串、trie key 即别名，故可从文本确定性还原：`/ef` → `ef`，`/终末地` → `终末地`。

该字段**仅用于展示**，不参与任何统计口径。

---

## 6. 采集实现要点

- 监听函数**必须带类型注解**（`CommandParse` 经全局 `post` 发布、无 scope，靠注解解析 provider）：

  ```python
  async def on_parse(session: Session, command: Alconna, result: Arparma) -> None
  async def on_receive(session: Session, command: Alconna, message: MessageChain) -> None
  ```

- **不阻塞主流程**：事件回调只写内存 `deque`；`timer.add_job(flush, "interval", seconds=30, id="insight_flush", replace_existing=True, misfire_grace_time=...)` 批量落库，`@listen(Cleanup)` 时补一次 flush。定时任务在 `handlers.py` 注册（对齐 `plugins/bilibilibot/handlers.py:163` 的写法，规避 `_plugin_module` 的 scope 归属问题）。
- **异常隔离**：采集与 flush 全程 `try/except` + `logger`，**绝不影响命令处理**。
- **归属缓存**：启动时遍历 `_commands.subscribers` 建 `command_path → plugin` 表；未知命令（第三方后续加载）回退 `plugin=''`，在 `/统计 插件` 中显示为「未归属」。

---

## 7. 查询命令

主命令 `/统计`，别名 `/insight`、`/usage`（trie 实测无冲突）。

| 子命令 | 输出 |
| --- | --- |
| `/统计` | 今日概览：执行次数、去重用户、群数、Top 5 命令、活跃插件数 |
| `/统计 命令 [n]` | 命令榜 Top N（默认 15）：次数 / 去重用户 / 占比 |
| `/统计 用户` | 总用户数（`user_profile`）、今日活跃、近 7 日活跃 |
| `/统计 日活 [n天]` | 近 N 天（默认 14）每日活跃用户数 + 命令数 |
| `/统计 插件` | 按插件聚合；含「未归属」 |
| `/统计 群 [n]` | 群活跃榜 Top N |
| `/统计 导出 [天]` | 导出 CSV 至 `data/insight/exports/`，返回路径 |

- 权限：**v1 仅 SuperUser**（`otae_bot.permissions.is_superuser`）。
- 渲染：v1 纯文本表格（`formatters.py`）。图片卡片可后续复用 `otae_bot.infrastructure.rendering`，**不列入 v1**。
- 时区：`Asia/Shanghai`（`zoneinfo`），`day` 取本地日期。
- 所有输出结尾附 §3 的盲区脚注。

---

## 8. 保留与聚合

- 明细 `command_usage` 保留 **180 天**；每日定时任务清理，**只删明细**。
- `user_profile` 永久保留 → 总用户数不受清理影响。
- **不预建 rollup 表**：49 条命令、按 1 万条/日估算，180 天约 180 万行，SQLite + 上述索引足够。数据量超预期时再加 `daily_rollup`。

---

## 9. 共享代码改动（豁免群开关）

豁免 `insight` 是**必需**的，不是可选优化：否则任一群执行 `/功能 关闭 统计` 后，该群采集器被 gate `STOP`，数据**静默丢失**且无任何报错——对统计插件是最坏的失败模式。

`otae_bot/group_features.py`：

```python
PROTECTED_PLUGINS = {"group_manager", "request_handler", "insight"}
```

两处生效点（均已确认）：

- `otae_bot/adapters/feature_gate.py:41` —— `install_group_feature_gates()` 跳过豁免插件，**不挂 gate**；
- `otae_bot/group_features.py:122` —— `set_enabled()` 抛 `ValueError`，`/功能 关闭 统计` 被明确拒绝。

### 9.1 文案需同步改写

`plugins/group_manager/handlers.py` 有两处硬编码文案，加入 insight 后**不再准确**（当前只提「群管理和全局请求处理」）：

- `L57`（`/功能 列表` 页脚）
- `L70`（拒绝关闭时的回复）

建议统一改为：`部分插件（群管理、全局请求处理、命令统计）不支持群开关。`

已核实：**无任何测试**断言这两条字符串（`grep "不支持群开关|群管理和全局请求" tests/` 无命中），改写安全。

### 9.2 `PLUGIN_NAMES` 的处理（与上版方案不同）

**保持与现有先例一致：不加入 `PLUGIN_NAMES`。**

理由：`PROTECTED_PLUGINS` 的两个现有成员 `group_manager`、`request_handler` **都不在** `PLUGIN_NAMES` 中——受保护插件本就不作为「可切换插件」对外宣传。

后果：`/功能 关闭 insight` 走 `resolve_plugin` 返回 `None`，报「未找到该插件」而非「不支持群开关」。二者都不允许关闭，语义可接受。

### 9.3 豁免的实际效果

- 采集器在**所有群持续运行**，数据连续；
- `/功能 关闭 统计` 明确报错，而非假装成功；
- **代价**：群管理员无法单独关闭该插件。考虑到 insight 只**读**命令、仅在 SuperUser 主动查询时发言，不干扰群内使用，代价可接受。

### 9.4 豁免**不能**修复盲区

两者是不同问题，勿混淆：

| | 豁免群开关 | 命令盲区 |
| --- | --- | --- |
| 保护对象 | **insight 自己**不被关掉 | 统计不到**别的**被关插件的命令 |
| 手段 | 加入 `PROTECTED_PLUGINS` | 命令口径下**无法解决** |
| 效果 | 数据连续 | 数据完整 |

某群关闭 `hyw`，是 **hyw 自己的 gate** 在拦 `/q`，insight 豁免与否都无济于事。

### 9.5 可选文档改动

`plugins/help_plugin/handlers.py` 的 `TEXT_TOPICS` 可加一条纯文本说明（`/help 统计`）。注意 `TOPIC_MAP` 需要配套 PNG，**不建议**改动。

---

## 10. 测试计划（`tests/test_insight.py`）

- **归属映射**：49/49 命令均能解析出 plugin（`AlconnaSuppiler` + `plugin_key` 契约测试）。
- **三态判定**：`/q 你好` → `executed`；`/mod --help` → `help`；`/资料` → `unparsed`；`/e` → 零记录。
- **去重**：同一 `message_id` 重复发布只落一行。
- **别名还原**：`/ef` → `alias_used='ef'`；`/终末地` → `'终末地'`。
- **噪音控制**：`/peek add` 不产生 `unparsed` 行。
- **口径正确性**：日活 / 总用户数在跨日、跨群、私聊下正确；私聊 `scope_kind='private'`。
- **保留清理**：180 天前明细被删、`user_profile` 保留。
- **异常隔离**：store 抛错时命令仍正常返回。
- **豁免生效**：`set_enabled(scope, 'insight', False)` 抛 `ValueError`；`install_group_feature_gates()` 不给 insight 挂 gate。
- **盲区固定**：`feature_store.set_enabled(scope, 'hyw', False)` 后 `/q` 不落库 —— **把已知行为写成断言**，防止将来误以为是 bug。

---

## 11. 风险

| 风险 | 说明 | 处置 |
| --- | --- | --- |
| 群开关盲区 | 被禁插件不计入，数字系统性偏低 | 输出常驻脚注；v1.1 可选影子层 |
| `unparsed` 归属歧义 | 前缀碰撞导致重复记录 | 「单候选」规则 + 开关 |
| 影响主流程 | 写库异常 / 延迟 | 内存缓冲 + 定时 flush + try/except |
| 别名还原偏差 | 重叠前缀 | 取最长匹配；仅展示字段 |
| 上游字段变更 | entari 升级 | 全部 `getattr` 兜底，缺字段降级跳过 |
| 存储增长 | 明细累积 | 180 天清理 + `user_profile` 保活 |

---

## 12. 实施顺序（待批准后执行）

1. `otae_bot/group_features.py` 加 `insight` 到 `PROTECTED_PLUGINS`；改 `group_manager` 两处文案。
2. `plugins/insight/` 骨架：`__init__.py` / `config.py` / `models.py`。
3. `registry.py`（归属索引）+ `store.py`（schema、写入、清理）。
4. `collector.py`（双事件订阅、缓冲、flush、异常隔离）。
5. `service.py` + `formatters.py` + `handlers.py`（`/统计` 各子命令）。
6. `tests/test_insight.py`。
7. 回归：`tests/test_architecture.py`、`tests/test_group_features.py`、`tests/test_mcsm_commands.py`。
