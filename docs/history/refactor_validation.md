# 结构重构验证记录

验证日期：2026-09-05。先将 `main` 从 `d3af664` 快进到远端最新提交
`5704fd6`，再以该提交为重构和对照基线。开始时工作区没有未提交改动。

## 测试结果

在相同的 Python 3.12.3、Entari 0.17.4、Playwright 1.62.0 / Chromium 151、
Pillow 11.3.0 环境分别运行原始版本与重构版本。原始版本通过 `git archive`
提取到独立临时目录，避免混入重构后的代码。

| 检查 | 原始版本 | 重构版本 |
| --- | --- | --- |
| 完整 pytest | 800 通过、29 失败、3 跳过 | 812 通过、29 失败、3 跳过 |
| 新增失败 | — | 0；失败用例集合完全相同 |
| 新增结构/生命周期测试 | — | 12 项全部通过 |
| Entari 实际加载 | 13 个原有插件 | 13 个插件全部加载成功 |
| 命令、事件及任务注册表达式 | 对照基线 | 13 个插件全部一致 |
| 8 张离线卡片渲染 | 对照基线 | 尺寸及 RGBA 像素 SHA-256 全部一致 |
| Python 编译、补丁空白检查 | — | 通过 |
| Wheel 构建 | — | 通过 |
| 抽卡导出/统计脚本 `--help` | — | 两个脚本均可加载 |

首次测试环境尚未安装浏览器及部分系统库，原始结果为 794 通过、35 失败、3 跳过。
补齐依赖后，两套代码均在同一环境重新验证；表格使用这次可比结果。

`tests/test_architecture.py` 覆盖公共包依赖边界、纯模型/转换模块的职责、
插件入口、兼容导入的模块身份、资源定位、插件发现、跨进程单实例锁、
启动异常时释放锁、多 Satori 端点、调度注册归属、真实插件加载和资源关闭顺序。
插件加载测试在临时工作目录中运行，检查关键后台任务已经注册，不连接 QQ。

## 行为一致性检查

比较原版与重构版的 AST，排除文档字符串及导入位置后，2,401 个顶层类和函数
定义保持一致。以下两处变化经过单独核对：

- `_plugin_module`：使用 Entari 当前注册上下文定位任务归属。
  原入口拆到 `handlers.py` 后，该文件成为 Entari 子模块；从 service 导入的
  定时任务必须归属注册它的子模块，否则 Entari 拒绝加载插件。
- 挑战记录 `_multiline`：将卡片模块的 `esc(text[:420])` 改为
  标准库 `escape(text[:420], quote=False)`，输入已经是字符串，转义行为相同。
  这使解析模块无需依赖浏览器绘图模块。

启动代码单独提取为应用工厂、连接构造、锁和生命周期模块，保持原有连接默认值、
插件加载顺序和资源关闭顺序。新增的 `python -m otae_bot` 与原 `bot.py`
共用启动函数。业务数据、图片、字体、别名 JSON、数据库结构和生产依赖版本约束
均未修改。

离线图像对照使用现有视觉测试中的相同数据，在两棵源码树中分别渲染。
远程素材请求统一替换为相同的空响应，保留内嵌和本地素材，消除外部网络波动。
检查覆盖密集/稀疏干员卡、密集/稀疏武器卡、密集/稀疏关卡详情、关卡总览及目录。
比较解码后的 RGBA 像素，不依赖 PNG 容器压缩是否一致。

## 原有失败和限制

完整测试尚未全绿，以下失败在未改动的 `5704fd6` 上同样存在：

| 测试文件 | 原有失败数 |
| --- | ---: |
| `test_endfield.py` | 17 |
| `test_endfield_performance.py` | 4 |
| `test_endfield_account.py` | 3 |
| `test_endfield_account_ui.py` | 2 |
| `test_endfield_challenge.py` | 1 |
| `test_endfield_visual.py` | 1 |
| `test_http_client.py` | 1 |

原因包括素材下载函数由 `fetch_many` 改为 `fetch_many_resilient` 后旧测试替身
尚未同步、旧素材 URL/图片尺寸预期、测试客户端缺少最新 AkeData 接口、
本地时区预期、旧视觉快照及共享 HTTP 客户端跨测试事件循环的关闭问题。
本次没有通过删除测试、放宽断言或标记 xfail 隐藏这些失败。
既有测试仅更新模块/源码路径；帮助图路径的源码检查改为跟随新的路径定义模块。

新提取及新增模块的语法及未定义名称检查通过（Ruff 规则 `E9,F63,F7,F82`）。
这不表示全部代码风格规则通过；默认检查仍会提示导入排序等规范问题。
全仓库仍保留与基线相同的 6 条未定义名称诊断：
`Match`、两处注解中的 `BuildImage`、两处 `_is_gacha_height_limit_error` 和
Steam 帮助中的 `__plugin_meta__`。这些既有问题未混入结构重构修改。

未使用真实 QQ、森空岛、小黑盒、Bilibili、Steam 或 MCSManager 账号进行线上
端到端操作，因此以上结果证明的是现有自动化测试、插件加载和离线对照范围内
没有新增回归，不等于已完成线上所有业务路径验证。

## 复核命令

在项目根目录安装 `requirements-dev.txt` 和浏览器依赖后：

```bash
python -m pytest -q tests/test_architecture.py
python -m pytest -q tests
python -m compileall -q bot.py otae_bot plugins configs utils scripts tests
python scripts/export_endfield_gacha_history.py --help
python scripts/aggregate_endfield_gacha_stats.py --help
git diff --check
```

## 发布分支

发布前再次运行完整 pytest：812 通过、29 失败、3 跳过，用时 46.00 秒。
将 JUnit 结果中的失败用例集合与原始版本日志比较，新增失败为 0，
12 项结构/生命周期测试全部通过。编译检查、两个抽卡脚本的 `--help`
以及暂存区补丁空白检查通过。

架构重构使用 `refactor/project-architecture` 分支，基于
`design/endfield-ui-code-preview` 的独立预览提交新增一个架构提交。
预览提交只修正作者信息，源码树未改变；架构提交包含模块迁移、调用方和测试的
导入路径更新、开发依赖及本验证记录，不修改机器人配置数据或接入预览 UI。

两个提交均使用仓库此前的身份 `HanazonoTae` 及
`77999307+otae-1204@users.noreply.github.com`。`main` 保持在 `5704fd6`，
本次不合并到主分支。
