# 测试

规则见 [架构规范](architecture_conventions.md) §9。本文是命令、工具和已知失败用例的唯一清单。

## 运行

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider                      # 全量，约 2–3 分钟
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_forkout.py  # 单个文件
.venv/bin/python -m pytest -q -p no:cacheprovider "tests/test_endfield_daily.py::DailyParallelFetchTests"  # 单个类或用例
```

每次提交前跑全量；提交说明写通过数和仍失败的已知用例。

## 真实事件测试：tests/support/dispatch.py

`run_scenarios(body)` 在临时目录里起一个子进程，用 `create_app()` 装配全部插件（含群开关和指令统计），
用真实 Satori 事件驱动；只伪造协议的 HTTP 调用（`Protocol.call_api`），并记录 bot 发出的内容。
脚本必须以打印 `ALL-OK` 结束。可用的名字：`app`、`plugin(name)`、`make_account(...)`、`default_account`、
`await say(content, user=..., group=..., private=..., account=...)`（返回本次发出的 Satori XML 列表）、
`account.protocol.calls` / `account.protocol.answers[action]`、`run(coro)`。

摘自 `tests/test_forkout.py`：

```python
from tests.support.dispatch import run_scenarios

SCENARIOS = r'''
account = default_account
account.protocol.answers['guild.member.get'] = lambda params: {
    'user': {'id': params['user_id'], 'name': 'QQ名' + params['user_id']}, 'nick': '群名片' + params['user_id'],
}

async def main():
    assert await say('/forkrank') == ['本群暂无被叉记录。']
    from otae_bot.groups.features import feature_store, group_scope
    feature_store.set_enabled(group_scope(account, '100'), 'forkout', False)
    assert await say('/forkrank') == []          # 群功能关闭后不响应
    print('ALL-OK')

run(main())
'''


class ForkoutDispatchTests(unittest.TestCase):
    def test_commands_through_the_real_event_path(self):
        result = run_scenarios(SCENARIOS)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-6000:])
        self.assertIn("ALL-OK", result.stdout)
```

新测试不要用 `SimpleNamespace` 冒充事件和会话。

## 配置

测试里改配置用 `settings.override({...})`（形状同 `entari.yml`：`basic` / `otae` / `plugins`），不改 `os.environ`：

```python
from otae_bot.config import settings

with settings.override({"plugins": {"plugins.bilibilibot": {"lottery_filter_uids": "*"}}}):
    ...
with settings.override({"otae": {"browser": {"concurrency": "3"}}}):
    ...
```

要走真实加载流程（读 `entari.yml` 和 `.env`）时用 `tests/support/config.py` 的 `loaded_config_file(yml, env)`。

## 守卫

```bash
.venv/bin/python -m tests.support.boundary_guard --report   # 按规则列出现有违规及行号
.venv/bin/python -m tests.support.boundary_guard --write    # 按当前代码重写基线
```

`tests/test_boundary_guard.py` 对比 `tests/fixtures/boundary_guard_baseline.json`：新增违规失败，修掉的必须从基线删除。
基线只许缩小，不能用 `--write` 放行新违规。扫描范围是 `otae_bot/` 和 `plugins/`。

| 规则 | 名称 | 查什么 |
|---|---|---|
| R1 | onebot-action | OneBot 动作名（`send_group_forward_msg`、`get_msg`、`onebot11/…` 等） |
| R2 | internal-call | `internal` / `request_internal` 调用，`internal/` 路径字符串 |
| R3 | onebot-http | OneBot HTTP 地址、令牌及旧 OneBot 模块 |
| R4 | framework-private | Entari / Letoderea 的下划线模块和名字、`global_propagators`、`_scope` / `_origin` 等私有属性 |
| R5 | numeric-id | 对 user/group/guild/channel/self id 做 `int()` |
| R6 | platform-name | 按 `platform` / `adapter` 字符串比较分支 |
| R7 | event-class-name | 用 `type(x).__name__` 等类名字符串判断事件 |
| R8 | direct-env-read | `otae_bot/config/` 之外的 `os.getenv` / `os.environ` |
| R9 | local-media-path | 媒体元素带本地路径；管线和边界之外调 `upload.create` |
| L1 | plugin-cross-import | 插件 import 其他插件 |
| L2 | boundary-import | 边界和 `application.py` 之外 import `otae_bot.boundary`、`satori.adapters`、`entari_plugin_server` |
| L3 | infrastructure-upward-import | `infrastructure` / `config` import Entari、satori、`plugins` 或其他 `otae_bot` 包（`config` 可用 `arclet.entari.config`） |
| L4 | core-imports-plugins | `otae_bot` import `plugins` |
| C1 | compat-import | import 已删除的 `otae_bot.compat` |

R1–R7 不适用于 `otae_bot/boundary/`；R9 不适用于 `otae_bot/pipeline/` 和 `otae_bot/boundary/`。

## 已知失败与不稳定用例

已知失败（全量时必然失败，与改动无关）：

| 用例 | 情况 |
|---|---|
| `tests/test_hyw_retry.py` | 与执行顺序有关：全量时 4 个用例 + 2 个子测试失败，单独跑通过 |
| `tests/test_changelog.py::DataIntegrityTests` 的 3 个 | 分支上有未被 `plugins/changelog/changelog.json` 收录的提交时失败：`test_git_history_is_fully_covered_by_the_data`、`test_draft_is_empty_when_nothing_is_uncovered`，提交日期落在最新版本区间之外时还有 `test_version_windows_cover_every_commit_date`。用 `python scripts/generate_changelog.py --check` 核对 |

不稳定（计时相关，负载下偶发）：

| 用例 |
|---|
| `tests/test_bilibili_poller.py::test_video_and_dynamic_targets_are_staggered_and_backed_off` |
| `tests/test_browser_render_queue.py::RenderQueueTests::test_two_slots_render_in_parallel_and_reuse_contexts_by_scale_factor` |
| `tests/test_http_asset_broker.py::SoftDeadlineRefillTests::test_soft_deadline_keeps_downloads_running_and_the_next_query_is_hot` |
| `tests/test_endfield_daily.py::DailyParallelFetchTests::test_accounts_load_in_parallel_in_binding_order_and_fail_independently` |
| `tests/test_endfield_card_detail_cache.py::CommunityExchangeMetricsTests::test_interval_and_lock_waits_are_counted_separately` |

失败时单独重跑确认，并在提交说明里写明是哪个用例、单独重跑是否通过。不要默默重跑到通过为止；
新发现的不稳定用例登记到这里。

## 文档链接

`tests/test_docs_links.py` 检查 `README.md`、`CLAUDE.md`、`docs/index.md` 以及 `docs/operations/`、
`docs/development/`、`docs/features/` 里的相对链接都指向存在的文件。

## 启动冒烟

```bash
cp entari.example.yml entari.yml
# 把 basic.network 唯一一条的 port 改成 1
timeout -s INT 25 .venv/bin/python bot.py
rm entari.yml
```

应加载 20 个插件，除连接失败外没有 ERROR / Traceback。`entari.yml` 不进 Git，跑完删掉。

## 实测

改动收发消息路径必须实测，单元测试通过不等于链路通。步骤和安全要求见 [实测](../operations/live_testing.md)。
