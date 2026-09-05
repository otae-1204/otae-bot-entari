# 代码结构

本轮以 `5704fd6` 为基线调整职责边界。聊天命令、权限判断、API 请求、
缓存参数、数据库结构和配置项保持原有语义。

```text
bot.py                          原有启动命令
otae_bot/
  application.py               创建 Entari、连接配置、启动协调
  lifecycle.py                 单实例锁、共享资源释放
  plugin_registry.py           按原顺序发现顶层插件
  paths.py                     随源码定位的资源根目录
  config/                      环境变量解析、原有路径配置
  adapters/                    Entari 会话/命令/定时任务、OneBot、消息构造
  infrastructure/
    cache.py                   有界异步缓存
    http/                      HTTP 连接池、资源缓存、User-Agent
    storage/                   JSON、SQLite、旧数据迁移
    rendering/                 浏览器截图、Pillow、OpenCV、渲染线程池
plugins/
  <plugin>/__init__.py          Entari 注册入口
  <plugin>/handlers.py          命令路由、交互流程和后台任务接线
  endfield/
    paths.py                   插件资源定位
    providers/                 Warfarin、AkeData、数据源规则、素材 URL
    catalog/                   查询命令、别名、公开资料模型和获取协调
      views/                   干员、武器、装备、配装、奖章的纯数据转换
    account/                   账号 API、加密、持久化、账号卡片
      detail/                  档案详情
      base/                    帝江号、基建
      investment/              当前档案可见养成投入
      currency/                资源流水
      challenge/               挑战记录模型、解析、绘图、翻译
    gacha/                     抽卡同步、分析、素材、小黑盒导入
    stages/                    公开关卡模型、来源和卡片
    calendar/                  AkeData 与官方版本日历
    medals/                    奖章快照存储
    ownership/                 持有率聚合与渲染
    rendering/                 共享卡片、HTML、素材处理
configs/                       原有 JSON 配置；Python 文件为兼容入口
utils/                         旧公共工具导入的兼容入口
assets/                        原有共享资源
data/                          运行数据（不入库）
scripts/                       原有运维、导出、预览工具
tests/                         功能回归、结构约束、真实插件加载检查
```

## 依赖边界

`application` 负责组装，`adapters` 封装框架和消息协议，`infrastructure`
提供网络、缓存、存储与渲染能力。公共包不依赖具体功能插件。

插件入口仅加载自己的 `handlers`。只有注册入口/处理器负责注册命令和定时任务；
业务模型和纯转换函数不注册事件，也不实例化机器人。Entari 会将相对导入的
处理器作为子模块跟踪，调度适配器使用注册时的插件上下文确定任务归属。

终末地的公开关卡资料位于 `stages`；玩家挑战记录位于 `account/challenge`。
公开资料无需账号绑定。`catalog/service.py` 负责请求、回退与缓存，
`catalog/views` 负责从已获取的数据构造视图，现有计算函数仍从 service 显式导出。
挑战记录的绘图模块也保留原合并模块的数据类型和解析函数导出，方便迁移调用方。

## 兼容和数据路径

- `python bot.py`、Windows 启动/部署脚本和 Satori 多端点配置继续可用。
- `.env`、`data/`、JSON 配置和数据库仍按原先的工作目录规则读取；请在项目根目录启动。
- 源码附带资源统一通过 `otae_bot.paths` 和 `plugins.endfield.paths` 定位，
  不随功能模块的目录深度变化。没有搬迁图片、字体、别名 JSON 或运行数据。
- `utils.*`、`configs.config`、`configs.path_config` 仍指向同一个新模块对象，
  不复制 HTTP 客户端、缓存、环境配置或调度器状态。
- 新代码使用 `otae_bot.*` 和按业务域组织的终末地模块路径；终末地旧平铺模块路径
  已更新到所有仓库内调用方、脚本和测试。

## 扩展方式

增加普通插件时，在 `plugins/<name>/` 提供入口、处理器及需要的业务模块，
顶层自动发现规则无需修改。纯资源目录没有 `__init__.py` 时不会被注册为插件。

增加终末地功能时，将模型、解析/服务及绘图放入对应业务域，在 `handlers.py`
接入命令与交互。涉及静态资料转换时优先使用 `catalog/views`，
无需为了转换一份数据引入账号客户端或事件注册。

运行 `python -m pytest tests/test_architecture.py` 检查依赖边界、
单实例锁、关闭顺序及真实 Entari 插件加载。其余回归测试的运行方式和基线限制
见 [验证记录](refactor_validation.md)。
