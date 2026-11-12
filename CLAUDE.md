# CLAUDE.md

基于 Entari / Satori 的 QQ 机器人：`otae_bot/` 是应用骨架与共享代码，`plugins/` 下 20 个功能插件。
外部经 Satori（`entari.yml` 的 `basic.network`）或由边界层内嵌的 OneBot 11 服务端（`plugins.server`）接入。完整约定见 `docs/architecture_conventions.md`。

## 不可违反的规则

- 内部只有 Satori：事件、消息元素、接口都用 Satori 的。
- 平台协议只能出现在 `otae_bot/boundary/`（OneBot 动作名、`internal/` 调用、平台 HTTP 地址与令牌）。
- 只用 Entari / Letoderea 的公开接口，不 monkeypatch。唯一例外文件是 `otae_bot/pipeline/framework_hooks.py`（Letoderea 全局传播器），其他地方只能经它间接使用。
- ID 是不透明字符串：不 `int()`，不从中解析含义；guild 与 channel 不混用。
- 不按平台名或适配器名分支，查 `features` 或直接尝试并降级。
- 主动消息（定时、后台任务）一律走 `otae_bot.delivery.deliver`；指令内回复用会话。
- 凡 `except Exception` 之前必须有 `except ExitState: raise`（`asyncio.CancelledError` 同理）。
- 插件目录名就是数据键（群功能开关、指令统计、`/功能` 参数），不得重命名已有插件目录。
- 插件直接用 Entari 原生写法（`command.on`、`listen`、`Session`）；已删除的 NoneBot 风格兼容层 `otae_bot/compat` 不得重建（守卫 C1）。

## 分层（依赖只能向下）

| 层 | 目录 | 不可以依赖 |
|---|---|---|
| 插件 | `plugins/<名字>/` | 其他插件、`otae_bot.boundary`、平台协议 |
| 核心 | `otae_bot/{groups,hypergryph,command_usage,delivery,pipeline,accounts,messaging,scheduling}` | `plugins`、`boundary` |
| 基础设施 | `otae_bot/{infrastructure,config}` | Entari（`config` 例外：只可用 `arclet.entari.config`）、satori、`plugins`、其他 `otae_bot` 包 |
| 边界 | `otae_bot/boundary/` | `plugins`、核心包 |

`otae_bot/application.py` 负责装配，是唯一可以 import `otae_bot.boundary` 的非边界模块。

## 测试与守卫

- 全量：`.venv/bin/python -m pytest -q -p no:cacheprovider`（约 2–3 分钟）。
- 守卫：`.venv/bin/python -m tests.support.boundary_guard --report` 查看清单；`--write` 重写基线 `tests/fixtures/boundary_guard_baseline.json`。基线只许缩小，不能靠 `--write` 放行新违规。规则 R1–R7 管协议边界，R8 禁止在 `otae_bot/config/` 之外直接读环境变量，L1–L4 管分层，C1 禁止 import 已删除的兼容层 `otae_bot.compat`。
- 已知失败：`tests/test_hyw_retry.py`（与执行顺序有关，全量时 4 个用例 + 2 个子测试失败，单独跑通过）；`tests/test_changelog.py::DataIntegrityTests` 的 3 个（分支上有未被 `changelog.json` 收录的提交时失败）。
- 不稳定：`tests/test_bilibili_poller.py::test_video_and_dynamic_targets_are_staggered_and_backed_off`；负载下也见过 `test_browser_render_queue`、`test_http_asset_broker` 与 `test_endfield_daily`（并行加载）的计时用例、`tests/test_endfield_card_detail_cache.py::CommunityExchangeMetricsTests::test_interval_and_lock_waits_are_counted_separately` 偶发失败。失败时单独重跑确认，并在提交说明里写明。
- 启动冒烟：`cp entari.example.yml entari.yml`，把 `basic.network` 唯一一条的端口改成 `1`，然后 `timeout -s INT 25 .venv/bin/python bot.py`，应加载 20 个插件，除连接失败外无 ERROR/Traceback；跑完删掉 `entari.yml`（它不进 Git）。

## 配置

- `entari.yml`（不进 Git，模板 `entari.example.yml`）放全部设置；`.env` 只放密钥，经 `"${{ env.名字 :- }}"` 引用。旧部署用 `scripts/convert_config.py` 转换。
- 插件设置是 `otae_bot.config.model.Settings` 子类，`plugin_settings("<目录名>", 模型)` 读取；核心设置在 `otae:` 段（`core_settings()`）。测试里用 `settings.override({...})`，不要改 `os.environ`。
- `otae_bot/config/` 之外不读环境变量（守卫 R8）；新增配置项要写进 `entari.example.yml`（测试核对）。

## 提交与分支

- `main` 必须始终可部署。架构改动在独立分支上做；未经仓库所有者确认，不推送、不合并到 `main`。
- 一个提交只做一件事；移动文件与修改逻辑分开提交，移动提交不夹带行为变化。
- 提交说明：`类型(范围): 摘要`，中文；正文写改了什么、为什么、怎么验证的（通过数、仍失败的已知用例）。没实测的写「未实测」。
- 不加 `Co-Authored-By` 或任何 AI / 工具署名。
- 合并前每个提交都要被 `plugins/changelog/changelog.json` 收录；用 `python scripts/generate_changelog.py --check` / `--draft` 核对。

## 实测与线上安全

- 改动收发消息路径要实测：只用测试账号、只在指定的测试群里，并开着双向限制（只接收测试群消息，只向测试群发送）。
- 线上环境默认只读，除非明确要求才执行变更，且逐步确认、不批量执行。
- 不复制凭据存储到开发机或任何别处；需要了解结构时只远程查询条数和键的形状。
