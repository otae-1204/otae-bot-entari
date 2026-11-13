# 代码结构

本文只描述目录和各模块的职责。分层规则、依赖方向和守卫见 [架构规范](architecture_conventions.md)；
新增插件的写法见 [新增插件](adding_a_plugin.md)，测试见 [测试](testing.md)。

## 启动链

| 文件 | 作用 |
|---|---|
| `bot.py` | 启动入口，调用 `otae_bot.launcher.run()` |
| `otae_bot/__main__.py` | 同上，支持 `python -m otae_bot` |
| `otae_bot/launcher.py` | 先 `load_config()` 读 `entari.yml`，再 import `application`（插件在 import 时就读配置）；缺 `entari.yml` 时提示并返回 2 |
| `otae_bot/application.py` | 装配：创建 `EventLoginEntari`；有 `plugins: server:` 时加载内嵌 OneBot 服务端；安装全局钩子；按目录加载全部插件 |
| `otae_bot/lifecycle.py` | 单实例锁 `.runtime/bot.lock`；退出时释放共享资源 |
| `otae_bot/plugin_registry.py` | `discover_plugins()`：`plugins/` 下含 `__init__.py` 的目录，按名字排序 |

`application.py` 是唯一可以 import `otae_bot.boundary` 的非边界模块。

## otae_bot/

| 目录 | 层 | 内容 |
|---|---|---|
| `boundary/` | 边界 | 进程内 OneBot 11 接入：`__init__.py` 生成 `entari-plugin-server` 的配置（POSIX 下只建 unix socket、令牌每次随机）；`onebot11.py` 继承上游 OneBot 11 适配器，只替换对 LLBot 实测有问题的部分 |
| `pipeline/` | 核心 | 所有事件和订阅器都经过的全局钩子，见下节 |
| `delivery/` | 核心 | `deliver(Target, message)`：主动消息唯一出口，选发送账号、应用群开关、不抛异常而返回 `DeliveryResult`；`target.py` 的 `Target` 只装不透明 ID |
| `accounts/` | 核心 | `runtime.py` 在线账号登记（按 platform + self_id）与各账号所在群；`ready.py` 的 `on_ready` 和后台选号；`event_login.py` 见下 |
| `messaging/` | 核心 | `media.py`（`png` / `image` / `file` / `image_file`，只从字节构造媒体）；`inbound.py` 事件取值；`prompts.py` 多步对话；`forward.py` 合并转发 |
| `scheduling/` | 核心 | `jobs.py` 的 `timer`：定时任务的 ID、重叠跳过、运行统计、关停 |
| `groups/` | 核心 | `features.py` 按账号和群保存插件开关，`PLUGIN_NAMES` 是插件中文名与别名；`permissions.py` 群管理员核验；`superusers.py` |
| `hypergryph/` | 核心 | `/ak` 与 `/ef` 共用：跨游戏签到（`attendance_registry.py`、`attendance_delivery.py`）、通行证极验（`captcha.py`）、终末地官方内容分类与推送回执（`endfield_notifications/`） |
| `command_usage/` | 核心 | 指令调用统计：`recorder.py` 挂到每个订阅器上记录，`store.py` 存 SQLite；`/指令统计` 和 `/status` 读取 |
| `config/` | 基础设施 | `settings.py`（`load_config`、`plugin_settings`、`core_settings`、测试用 `override`）；`model.py` 的 `Settings` 基类；`core.py` 的 `otae:` 段；`root.py` 资源根目录；`paths.py` 旧资源路径常量 |
| `infrastructure/` | 基础设施 | `storage/`（`JsonStore`、`SQLiteHelper`、`Plugin_Data`）；`http/`（共享 HTTP 客户端 `fetch_json` / `fetch_bytes`、素材通道、磁盘缓存、TLS）；`rendering/`（共享浏览器与渲染队列、Pillow、OpenCV、渲染线程池、帮助卡片与插画外框）；`cache.py`、`changelog.py`、`errors.py`、`loop_watchdog.py`、`recent_alerts.py`、`redaction.py` |

`accounts/event_login.py`：satori-python 1.3.10 起按 `Login.identifier` 匹配账号，而符合 Satori
规范的服务端在非登录事件里只给不完整的 `login`，事件会被当成未知账号丢弃；`EventLoginEntari`
在同一连接上找回对应账号后再交给原有 `post`。

### pipeline/

| 文件 | 作用 |
|---|---|
| `framework_hooks.py` | 唯一使用框架私有接口的地方：Letoderea 的 `global_propagators`（另有 `current_subscriber`），注释写明缺的上游接口 |
| `feature_gate.py` | 群功能开关，作用于指令、监听器和 `deliver` |
| `command_input.py` | 在 Entari 判断前缀前整理引用和空白 |
| `quote_fallback.py` | 引用消息取不到时事件照常派发 |
| `outbound_media.py` | 每次发送前按账号 `features` 把媒体上传或内联为 `data:`，不带本地路径 |
| `message_log.py` | `[message]` 日志里的手机号、验证码、令牌脱敏 |

这些钩子和指令统计都在 `application.create_app()` 里、加载插件之前统一安装一次，
之后注册的订阅器（包括热重载的插件）自动覆盖。

## plugins/

20 个目录，每个是一个插件。目录名就是数据键（群功能开关、指令统计、`/功能` 参数），不得重命名。

```text
McModQuery  McWikiQuery  arknights  bilibilibot  changelog  eft_helper  endfield
forkout  group_manager  help_plugin  hyw  mcsm  minecraft_plugin  peek  radar
request_handler  status  steamInfo  tibo_radar  usage_stats
```

- `__init__.py` 只有 `from .handlers import *`（`tests/test_architecture.py` 检查）。
  `eft_helper` 的 `__init__.py` 为空，不注册任何指令，也不在 `PLUGIN_NAMES` 里。
- 拆出 `service.py` 的只有 bilibilibot、endfield（按子功能各一个）、radar、tibo_radar。
- `endfield/` 按功能分子包：`account/`、`announcements/`、`catalog/`、`encyclopedia/`、`gacha/`、
  `medals/`、`ownership/`、`stages/`、`usage/`、`providers/` 等。

## 其他目录

| 目录 | 内容 |
|---|---|
| `scripts/` | 根目录：`convert_config.py`（旧部署转换）、`generate_changelog.py`、`migrate_platform.py`、`help_pages.json`、`tibo_help_pages.json`、Windows 启停与部署脚本 |
| `scripts/ops/` | 运维小工具（`smoke_bilibili_tick.py`、`_probe_dispatch.py`、`_regroup_changelog.py` 等） |
| `scripts/render/` | 出图脚本（`render_help_cards.py`、各类终末地卡片预览） |
| `scripts/preview/` | 卡片预览与回放（雷达、B 站、状态、更新日志等） |
| `scripts/endfield/` | 终末地数据检查、基准、素材提取、抽卡统计导出 |
| `tests/` | `test_*.py`；`support/`（`dispatch.py`、`config.py`、`boundary_guard.py` 等）；`fixtures/`（含守卫基线） |
| `vendor/hyw-frontier/` | 不发布到 PyPI 的 hyw 依赖源码，`requirements.txt` 以可编辑方式安装 |
| `assets/` | 随源码的资源：图片、字体、音频、文本、`json/`、`sql/` |
| `data/` | 运行时数据，`data/<插件目录名>/`，另有 `data/cache/`；不进 Git |
| `.runtime/` | 运行时文件：`entari.yml` 的副本（Entari 退出时改写的是它）、`bot.lock`、`satori-server/` socket 目录 |
| `docs/` | `development/`、`operations/`、`features/`、`reference/`、`history/`、`adr/` |
| `output/` | 预览与检查脚本的输出；不进 Git |
| `archive/` | 已停用代码的归档包，见 `archive/README.md`；不加载、不测试 |
