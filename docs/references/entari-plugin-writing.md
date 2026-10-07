---
name: entari-plugin-writing
description: Use when writing, modifying, or debugging a plugin for the Entari Python bot framework (ArcletProject). Use whenever uncertain about the arclet.entari plugin API — plugin declaration (metadata), config models (BasicConfModel), commands (command.on/command.mount), event listeners, filters, lifecycle hooks, services, scheduler, keeping/local_data, or entari.yml plugin config keys.
---

# Entari 插件编写

## 概述

Entari（`arclet-entari`，导入名 **`arclet.entari`**，不是 `entari`）是一个基于 Satori / Alconna / letoderea / launart 的 Python 机器人框架。插件就是**一个普通的 Python 模块/包**：模块加载时在模块顶层调用 `metadata(...)` 声明插件，命令、事件监听器、定时任务在模块级通过装饰器自动注册到当前插件作用域。**不继承 `Plugin` 类**。

核心事实（Baseline 测试已证实：不读参考就猜 API 必错）：

| 易错点 | ❌ 错误写法 | ✅ 正确写法 |
|---|---|---|
| 包名 | `from entari import ...` / `import entari` | `from arclet.entari import ...` |
| 插件声明 | `__plugin__ = Plugin(...)` | 模块级调用 `metadata(...)`；`Plugin.current()` 获取当前插件对象 |
| 命令 | `from entari.commands import command` | `from arclet.entari import command`；`@command.on(...)` / `@command.command(...)` / `command.mount(Alconna)` |
| 事件 | `@on_message()` / `event.send(...)` | `@listen(MessageCreatedEvent)` 或 `plugin.dispatch(...)`，处理器参数注入 `Session`，用 `session.send(...)` |
| 配置 | pydantic `BaseModel` + `get_plugin_config` | `BasicConfModel` + `plugin_config(Config)`（pydantic/msgspec 也可用） |

## 快速参考

```python
from arclet.entari import (
    Session, MessageChain, MessageCreatedEvent, MessageEvent,
    command, metadata, plugin_config, filter_, listen, keeping,
    plugin, PluginRole, add_service, declare_static, inject, Entari,
    Image, At, BasicConfModel, ConfigReload, Startup, Ready, Cleanup,
)

class Config(BasicConfModel):
    """插件配置，字段 docstring 会进入生成的 JSON Schema 描述"""
    command: str = "抽卡"
    limit: int = 5

metadata(                                    # 模块级，必须
    "插件显示名",
    PluginRole.NORMAL,                       # NORMAL / UTILITY / LIBRARY / COMPLEX
    [{"name": "作者", "email": "a@b.c"}],
    "0.1.0",
    description="一句话描述",
    config=Config,                           # 可选，传 Config 模型
    readme="README.md",                      # 或直接传 markdown 字符串
)

conf = plugin_config(Config)                 # 校验后的配置实例
conf2 = plugin_config(Config, bind=True)     # 写透代理：改属性自动写回 entari.yml

@command.on("add {a} {b}")                   # 字符串模板指令，签名里的类型即参数类型
async def add(a: int, b: int):
    return f"{a + b =}"                      # 返回值自动发送

@listen(MessageCreatedEvent)
@filter_.public                              # 事件 + 过滤器
async def on_msg(session: Session):
    if session.content == "hello":
        await session.send("hi", at_sender=True)

@plugin.listen(Startup)
async def on_start(): ...

@every(5, "minute", label="定时任务")
async def task(app: Entari): ...

kept = keeping("key", obj_factory=dict, dispose=save)  # 热重载存活的对象
```

## 项目结构（发行插件）

```
entari-plugin-<name>/                  # 发行名：entari-plugin-<name>
├── pyproject.toml                     # pdm-backend；包名 entari_plugin_<name>
├── entari.yml                         # 运行时配置（调试用，通常 gitignore）
├── entari.schema.json                 # 启动时自动生成（basic.schema: true）
├── main.py                            # Entari.load("").run()
└── src/entari_plugin_<name>/
    ├── __init__.py                    # metadata() + import 各子模块注册
    ├── config.py                      # Config(BasicConfModel)
    └── ...                            # 命令/事件/服务模块
```

`pyproject.toml` 关键点：

```toml
[project]
name = "entari-plugin-<name>"
dependencies = ["arclet-entari>=0.18.0"]
requires-python = ">=3.10"

[build-system]
requires = ["pdm-backend"]
build-backend = "pdm.backend"

[tool.pdm]
distribution = true
```

`main.py`（调试入口，不是插件一部分）：

```python
from arclet.entari import Entari

Entari.load("").run()        # 或 Entari.load("entari.yml").run()
```

## 插件声明：metadata

`metadata(...)` 必须是模块级调用（加载器在模块执行时读取它）。等价写法：

```python
metadata("插件名", PluginRole.NORMAL, [{"name": "作者", "email": "a@b.c"}], "0.1.0",
         description="...", config=Config, readme="README.md")
# 或极简：metadata(__file__)  —— 从文件路径推导名字
# 或模块级变量：__plugin_metadata__ = PluginMetadata(name=__name__, config=Config)
```

获取当前插件对象：`plug = Plugin.current()` 或 `plug = plugin.get_plugin()`。
注意 `PluginRole` 从 `arclet.entari.plugin` 导入。

## 配置：BasicConfModel

```python
from arclet.entari import BasicConfModel, plugin_config
from arclet.entari.config import model_field

class Sub(BasicConfModel):
    x: int = 1

class Config(BasicConfModel, extra="allow"):
    command: str = "抽卡"
    """指令名"""                      # docstring → schema 描述
    items: list[Sub] = model_field(default_factory=list)
    opts: dict = model_field(default_factory=dict)
```

- 字段 docstring 自动成为生成的 `entari.schema.json` 中的 `description`。
- 嵌套模型用 `model_field(default_factory=...)`。
- 支持 `pydantic.BaseModel` 与 `msgspec.Struct`，框架按模块前缀自动识别。
- `plugin_config(Config, bind=True)` 返回写透代理，属性赋值会保存回配置。

## 指令：三种写法

**A. 字符串模板（最简单，推荐）** — `@command.on("cmd {参数}")`：

```python
@command.on("add {a} {b}")
async def add(a: int, b: int):
    return f"{a + b =}"

@command.on("del <...content>")        # <...> 为 MultiVar 拼接参数
async def del_(content: MessageChain):
    return content
```

参数类型取函数签名注解；docstring 成为命令描述；返回值自动发送。子命令/别名/选项用 builder：`@command.command("echo <...content>", "显示").option("escape", "-e|--escape # ...", default=False, action=store_true)`。

**B. Alconna + mount（复杂命令）**：

```python
from arclet.alconna import Alconna, Args, Option, Subcommand, CommandMeta, Field
from arclet.entari import Session, command
from arclet.entari.command import Match, Query

cmd = Alconna(
    "微博",
    Args["user", str],
    Option("动态", dest="post", help_text="获取动态"),
    Subcommand("关注", Args["name", str], help_text="关注"),
    meta=CommandMeta("描述", example="$微博 育碧"),
)
cmd.shortcut("别名", {"command": "微博", "prefix": True})   # 或正则形式

disp = command.mount(cmd).as_execute()

@disp.assign("$main")                    # 无子命令时的兜底
async def _(session: Session, user: Match[str]): ...

@disp.assign("post")                     # 子命令路由，点分路径
async def _(session: Session, index: Query[int] = Query("post.index", 1)): ...
```

- `Match[T]`：可匹配参数（`.result` / `.available`）。
- `Query(path, default)`：读取 Option/Subcommand 深层值，路径用 dest。
- 参数名加 `?`（`Args["user?", str]`）= 可选。
- `.as_execute()` 同时注册 `CommandExecute` 事件，可被 `command.execute(...)` 编程调用。
- 平铺命令用 `@disp.handle()`。

**C. `@command.on(Alconna对象)`**：

```python
inspect_cmd = Alconna("inspect", Args["target?", [At, Sharp]], meta=CommandMeta(...))

@command.on(inspect_cmd)
async def inspect(session: Session[MessageEvent], target: Match["At | Sharp"]): ...
```

## 事件监听

```python
from arclet.entari import MessageCreatedEvent, Session, listen, plugin
from arclet.entari.event.lifespan import Startup, Ready, Cleanup
from arclet.entari.event.config import ConfigReload
from arclet.entari.event.api import SendRequest, SendResponse

@listen(MessageCreatedEvent)                 # 顶层 listen
async def h(session: Session): ...

@plugin.listen(MessageCreatedEvent, priority=100)   # plugin 命名空间
async def h2(event: MessageCreatedEvent): ...

disp = plugin.dispatch(MessageCreatedEvent)  # 复用分发器
@disp.on()
async def h3(session: Session): ...

plug = Plugin.current()                      # 或通过插件对象
@plug.dispatch(MessageCreatedEvent)
async def h4(session: Session): ...
```

- 处理器通过参数类型注入：`session: Session`、`event: MessageCreatedEvent`、`app: Entari`、`ctx: Contexts`、任意 `launart` Service 类型。
- `MessageEvent` 有 `content: MessageChain`、`quote`；`Session.content` 为字符串。
- 返回 `BLOCK`（`from arclet.letoderea import BLOCK`）停止传播。
- 自定义事件：`@make_event` / `define()` 创建，其他插件用 `plugin.dispatch(CustomEvent)` 订阅。

## 过滤器

```python
from arclet.entari import filter_, F, Session

@listen(MessageCreatedEvent).if_(filter_.to_me)          # 链式
@filter_.public & filter_.to_me                          # 组合（场景过滤）
@filter_(lambda sess: sess.content.startswith("$$"))     # 自定义谓词
# 场景：user/guild/channel/platform/self_/direct/private/public/notice_me/reply_me/to_me

from arclet.entari.filter import startswith, endswith, fullmatch, regexmatch, interval, semaphore, admins, superusers

@startswith("!")                          # 消息内容过滤（装饰器）
@interval(2, limit_prompt="太快了")        # 频率限制
@admins() / @superusers()                 # 权限
```

配置级 `$filter` 表达式（在 entari.yml 的插件键下写）：可用名 `type, channel, guild, user, member, platform, self_id, message, reply_me, notice_me, to_me, env`，操作符 `eq ne neq gt ge gte lt le lte nin exists`。

## 生命周期

无 `enable()/disable()` 覆写；用事件钩子：

```python
from arclet.entari import Plugin, inject
from arclet.entari.event.lifespan import Startup, Cleanup

@plugin.listen(Startup)
@inject("database/sqlalchemy")            # 按服务 id 注入；也可 inject({"id": ..., "stage": "blocking"})
async def on_start(): ...

plug = Plugin.current()
@plug.use("::startup")                    # 字符串钩子：::startup ::ready ::cleanup
async def prepare(app: Entari): ...       #        ::before_send ::after_send ::config_reload
```

## 服务（launart Service）

库/服务型插件（`PluginRole.LIBRARY`）通常声明一个 Service 供其他插件注入：

```python
from launart import Launart, Service
from launart.status import Phase
from arclet.entari import plugin

class MyService(Service):
    id = "my.plugin/service"              # 全局唯一 id，注入时用

    @property
    def required(self) -> set[str]:
        return {"database/sqlalchemy"}    # 依赖其他服务

    @property
    def stages(self) -> set[Phase]:
        return {"preparing", "blocking", "cleanup"}

    async def launch(self, manager: Launart):
        async with self.stage("preparing"):
            ...
        async with self.stage("blocking"):
            await manager.status.wait_for_sigexit()
        async with self.stage("cleanup"):
            ...

service = MyService()
plugin.add_service(service)
```

其他插件消费：`@inject("my.plugin/service")`、`@plugin.model.inject("my.plugin/service")`、或直接在参数上写 Service 类型注解。`declare_static()` 标记的插件**不能** dispatch 事件。纯服务型插件不要用 `declare_static()` 之外的方式注册命令。

## 数据存储

```python
from arclet.entari import keeping
from arclet.entari.localdata import local_data

file = local_data.get_data_file("插件名", "data.json")    # .entari/data/<插件名>/...
cache_dir = local_data.get_cache_dir("插件名")
kept = keeping("key", obj_factory=dict, dispose=save)     # 热重载后仍存活
```

关系型数据用 `entari-plugin-database` 插件（不是自建连接）：

```python
from entari_plugin_database import Base, Mapped, mapped_column, get_session, AsyncSession, select

class Record(Base):
    __tablename__ = "my_plugin_record"     # 建议用 <插件名>.<表名> 命名空间
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

async with get_session() as session:
    await session.scalars(select(Record).where(...))
```

## 定时任务

```python
from arclet.entari.scheduler import every, cron, invoke

@every(5, "minute", label="推送")          # second/minute/hour
async def task(app: Entari): ...

@cron("0 0 * * *")                          # 需要 croniter extra
async def daily(): ...

@invoke(5)                                  # 一次性延迟
async def delayed(): ...
```

## entari.yml 插件键约定

```yaml
plugins:
  ::echo: {}                    # ::   内置插件（arclet.entari.builtins.<name>）
  .record_message: {}           # .    内置根插件（.commands .scheduler .localdata .main）
  entari_plugin_myplugin:       # 无前缀 = 已安装的发行插件（entari_plugin_<name>）
    key: value
  ~::echo: {}                   # ~    默认禁用
  ?plugin: {}                   # ?    可选（存在才加载）
  reusable@1: {}                # @uid 可复用插件实例
  $prefix: [{key: "", plugins: ["x"]}]   # $ 控制段
  $prelude: ["::auto_reload"]
  $files: ["dir"]
```

每个插件键可带 `$disable`（bool 或表达式，如 `${{ env.X }}` 或 `env.ENV == "prod"`）、`$priority`（越低越先，默认 16）、`$filter`。配置内可插值 `${{ env.NAME }}` / `${{ env.NAME :- default }}`。加载时 `::name` 替换为 `arclet.entari.builtins.name`，无前缀 `name` 自动前缀为 `entari_plugin_name`。

## 插件间依赖与集成

- **作为库导入**：直接 `from entari_plugin_database import Base, get_session`、`from entari_plugin_browser import playwright_api`。在 pyproject 里把依赖插件写成普通 `dependencies`。
- **声明服务依赖**：`metadata(..., depend_services=["database/sqlalchemy"])`。
- **声明插件依赖**：`requires("entari-plugin-database", ...)`（模块名或发行名）。
- **扩展点**：库插件导出自定义 letoderea 事件类，其他插件 `plugin.dispatch(CustomEvent)` 注册回调；WebUI 类插件暴露注册函数（如 `webui_extend("id")`）。
- 子插件/子包：`package(...)`、`# entari: plugin` 注释标记，或插件包内子模块各自调用 `metadata(...)`。

## 常见错误

| 症状 | 原因 / 修复 |
|---|---|
| `ImportError: No module named 'entari'` | 包名是 `arclet.entari` |
| 处理器不注册 / `RegisterNotInPluginError` | 命令/事件处理器必须与 `metadata()` 在同一模块定义；子模块处理器要在插件包内且被 import |
| 指令参数不解析 | 用 `@command.on("cmd {arg}")` + 函数签名注解；或 `Match`/`Query`；不要编造 Alconna 语法 |
| 配置读不到 | 先定义 `Config` 再 `plugin_config(Config)`，且 `metadata(..., config=Config)`；注意配置键用发行名 `entari_plugin_<name>` |
| 事件里没有 `.send()` | 处理器注入 `session: Session`，用 `session.send(...)` |
| 热重载后状态丢失 | 用 `keeping(...)`；连接类对象（aiohttp/浏览器）放 `keeping` 或 Service 里管理，启动/关闭用 `Startup`/`Cleanup` |
| 需要数据库 | 用 `entari-plugin-database` 的 `Base`/`get_session`，别自己开连接 |
| 插件需要常驻服务 | 定义 `Service` 子类 + `plugin.add_service`，`declare_static()` 防重载 |
| 想给命令加冷却/权限 | `@interval(2)` / `@admins()` / `@superusers()` / 自定义 `Propagator`（`require_permission` 等） |
| 带必填位置参数的命令配子命令时，输入子命令报 `ArgumentMissing` | 位置参数与子命令（`Subcommand` / `assign`）共存时，位置参数必须可选：`Args["city?", str]`，`$main` 处理器里再检查 `.available` |
