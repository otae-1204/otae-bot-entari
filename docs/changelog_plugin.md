# 更新日志插件（plugins/changelog）

面向 Bot 用户的「我们这个 Bot 都更新了什么」查询面：`/更新日志` 出图片卡片，
渲染失败时自动降级为分块纯文本。

## 命令

| 输入 | 行为 |
| --- | --- |
| `/更新日志` | 最新一个版本的详情卡 |
| `/更新日志 列表 [页码]` | 全部版本目录，每页 12 个 |
| `/更新日志 <版本号>` | 指定版本详情，支持前缀（`v1.14`、`1.14`） |
| `/更新日志 <序号>` | 按目录序号取版本，`1` 即最新 |
| `/更新日志 <关键词>` | 在版本号、日期、标题、概括、标签与每条更新正文里检索 |
| `/更新日志 统计` | 版本数、更新条数、覆盖提交数与按类型的条数分布 |
| `/更新日志 帮助` | 命令说明卡 |

别名：`/更新`、`/changelog`、`/版本`。`/更新日志` 作为命令头要求词边界，
所以 `/更新群信息`、`/更新服务器昵称` 仍由各自插件处理，不会被截走。

## 版本号怎么来的

仓库**没有打 tag**，所以版本号是按提交时间划分时间段、由人工划定的：
`v1.0.0`（2026-06-17 迁移到 Entari）到 `v1.14.0`（2026-09-16），共 15 个版本。

版本号的含义：`vX.Y.0` 的 `X` 是「阶段」而非语义化大版本；`Y` 按时间递增。
划分原则是让每个版本对应一件用户能感知到的事（一个插件上线、一次卡片改版、
一批修复），而不是按提交数或等长时间切分。

> `scripts/_regroup_changelog.py` 里的 `VERSIONS` 表是最初一次性重组的产物
> （把按日期聚合的 schema 1 数据改成按版本聚合的 schema 2）。**不要再跑它**：
> 它只接受 schema 1，对现在的数据会直接拒绝执行，因为重跑会按版本窗口重新
> 贴标签、把每条更新的 `date` 改成窗口结束日。新增版本直接改 JSON。

## 新增一次更新

日常只有这一步：**改 `plugins/changelog/changelog.json`，然后让 `--check` 通过。**

### 1. 看哪些提交还没被覆盖

```
python scripts/generate_changelog.py --check     # 默认动作；不一致时退出码 1
python scripts/generate_changelog.py --report    # 覆盖统计
python scripts/generate_changelog.py --draft     # 打印未覆盖提交的骨架
```

`--check` 失败时会逐条指出问题，例如漏了提交：

```
[FAIL] plugins/changelog/changelog.json 与 git 历史不一致：
  - releases[1]（v1.14.0）的 highlights 必须是非空数组。
  - 有 1 个提交没有被任何版本覆盖：0f9bd8f
```

### 2. 把提交写进某个版本

`--draft` 输出的是**可以直接粘贴的 JSON**，按提交日期分好组：

```json
{
    "kind": "feat",
    "date": "2026-09-16",
    "text": "待补写：说明这次改动对用户意味着什么。",
    "commits": [
        "0f9bd8f"
    ]
}
```

把它粘进对应版本的 `highlights` 数组里，把 `text` 改成用户看得懂的话，
必要时调整 `kind`。一次改动涉及多个提交就都塞进同一个 `commits` 里——
**一条 highlight 讲一件事**，而不是一个提交一条。

如果这批提交属于一个还没开的新版本，就在 `releases` 数组**最前面**（最新的在最前）
新开一项：

```json
{
    "version": "v1.15.0",
    "date": "2026-09-17",
    "from": "2026-09-17",
    "to": "2026-09-17",
    "title": "一句话版本标题",
    "summary": "一句话概括这个版本做了什么。",
    "tags": ["雷达"],
    "highlights": [ ... ]
}
```

字段约束（`models.py` 与 `--check` 双重把关）：

| 字段 | 约束 |
| --- | --- |
| `version` | 唯一，不能重复 |
| `from` / `to` | 提交日期必须落在这个闭区间内；`from > to` 报错 |
| 相邻版本窗口 | 不能重叠 |
| `releases` 顺序 | 必须按时间倒序（最新在前） |
| `kind` | 只能是 `feat`/`fix`/`perf`/`refactor`/`docs`/`chore`/`style`/`test` |
| `text` | 非空 |
| `commits` | 非空；每个短哈希必须真实存在、**全局恰好出现一次** |
| `date`（highlight） | 必须等于所包含提交的提交日期 |

`date` 和窗口对不上是最常见的报错，报错信息会同时给出两个值：

```
  - releases[1]（v1.15.0）highlights[1] 的提交 0f9bd8f 提交日期是 2026-09-16，却标注为 2026-09-17。
```

照着报错把 `date` 改成提交日期（或把窗口开宽）即可。

### 3. 确认三层都没破

```
python scripts/generate_changelog.py --check        # 数据与 git 对齐
python -m unittest tests.test_changelog            # 数据 / 文本 / 卡片
```

### 4. 目检卡片（可选）

改完文案想看看卡片长什么样，用预览脚本——它走的是**线上同一条渲染路径**，
但不发消息、不连网络：

```
python scripts/preview_changelog.py                      # 全部类型
python scripts/preview_changelog.py --only stats         # 只渲染一类
python scripts/preview_changelog.py --only index --page 2
python scripts/preview_changelog.py --only version --version v1.13
python scripts/preview_changelog.py --only search --keyword 雷达
```

输出在 `output/`，文件名 `cl-<类型>.png`（多页时带 `-01`、`-02` 后缀），
每行会打印字节数与标题，方便确认渲染成功。类型取值为
`latest` / `index` / `stats` / `help` / `search` / `version`。

### 关于 `head` 与 `generated_from`

`changelog.json` 顶层的 `head` 记录生成时的仓库 HEAD，`generated_from` 记录
首次生成时的统计。**两者都不参与 `--check` 校验**，只是给人看的快照，
所以提交新版本后它们会显得陈旧——这是正常的，不影响数据正确性。
真正决定「有没有漏」的只有 `releases` 里的 `commits`。

## 数据链路

```
git log ──► scripts/generate_changelog.py ──► plugins/changelog/changelog.json
                     (校验)                            (只读数据)
                                                          │
                              ┌───────────────────────────┼───────────────────────────┐
                        models.py                   formatters.py              presentation.py
                    (dataclass + 校验)              (纯文本降级)                (已转义 HTML 片段)
                                                                                      │
                                                                              rendering.py + assets/card.css
                                                                                (离线 HTML/CSS → PNG)
                                                                                      │
                                                                                 handlers.py
```

- `scripts/generate_changelog.py --check` 是数据的守卫：校验 `changelog.json` 里
  每个**非 merge 提交恰好出现一次**——不重不漏，也不含未知提交。
  `tests/test_changelog.py` 会调用它，所以数据一旦和 git 历史漂移，测试就红。
- `plugins/changelog/changelog.json`（schema 2）是唯一数据源，**只读**，
  插件运行时不碰 git、不发网络请求。
- `formatters.py` 出纯文本，`presentation.py` 出卡片视图，两者共享同一份数据，
  所以「图上的内容」和「降级文本的内容」始终一致。

## 分层职责

| 文件 | 职责 |
| --- | --- |
| `models.py` | 只读数据模型与校验（`Changelog` / `Release` / `Highlight`） |
| `formatters.py` | 纯文本渲染，图片投递失败时的降级内容 |
| `presentation.py` | 已转义的结构化 HTML 片段（`ChangelogPage` / `ChangelogReply`） |
| `rendering.py` + `assets/card.css` | 离线 HTML/CSS → PNG，复用共享浏览器渲染器 |
| `handlers.py` | 命令入口：解析参数、取数据、出图、降级 |

`handlers.py` 不在 import 期读 `.env`、不发网络请求、不注册定时任务，
这样 `tests/test_architecture.py` 能在没有 `.env` 的临时沙箱里导入它。

## 投递约定

`_reply` 先把**所有**页面渲染完，再开始发第一张图：宁可整体失败降级成文本，
也不让用户收到半套卡片。降级只发生在真正的异常上，`asyncio.CancelledError` 直接抛出，
不吞取消。

## 卡片风格

与雷达卡片共用同一套纸色/墨绿视觉语言（`--paper` / `--ink` / `--forest`），
但类名统一加 `cl-` 前缀，避免两套卡片样式互相污染。字体用仓库内的
`assets/font/steamInfo/MiSans-Regular.ttf`，base64 内嵌，渲染全程零联网，
CSP 为 `default-src 'none'`。

## 维护方式

新增一次更新后：

1. 跑 `python scripts/generate_changelog.py --check`，它会指出哪个提交没被覆盖。
2. 按需要把该提交归入已有版本窗口，或在 `changelog.json` 里新开一个版本。
3. 跑 `python -m unittest tests.test_changelog` 确认数据、文本、卡片三层都没破。
