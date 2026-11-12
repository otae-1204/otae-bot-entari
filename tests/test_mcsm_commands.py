"""Regression guards for the MCSM subcommand router.

``/mcsm 重启``, ``强制结束``, ``强杀``, ``命令`` and the ``全部`` flag were silently
unreachable: the Chinese aliases had been corrupted from UTF-8 into GBK mojibake
(``'閲嶅惎'`` instead of ``'重启'``), so every Chinese form fell through to the
``Unknown command`` tail.  The English forms kept working, which is why the
regression stayed invisible for Chinese-speaking users.

These tests assert the router's literals are readable Chinese and that the
mojibake forms are gone, so an encoding-corrupting edit cannot come back silently.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MCSM_HANDLERS = ROOT / "plugins/mcsm/handlers.py"
HANDLER_SOURCES = tuple(
    sorted(
        path
        for base in ("plugins", "otae_bot", "scripts")
        for path in (ROOT / base).rglob("*.py")
        if "__pycache__" not in path.parts
    )
)


def _mojibake_runs(text: str) -> list[tuple[str, str]]:
    """Return (corrupted, recovered) pairs for UTF-8-read-as-GBK text."""
    found: list[tuple[str, str]] = []
    for match in re.finditer(r"[\u4e00-\u9fff]{2,}", text):
        value = match.group()
        try:
            recovered = value.encode("gbk").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if recovered != value and any("\u4e00" <= char <= "\u9fff" for char in recovered):
            found.append((value, recovered))
    return found


def _subcommand_tuples() -> dict[int, tuple[str, ...]]:
    """Collect the literal tuples compared against ``subcmd`` in handle_mcsm."""
    tree = ast.parse(MCSM_HANDLERS.read_text(encoding="utf-8"))
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_mcsm"
    )
    literals: dict[int, tuple[str, ...]] = {}
    for node in ast.walk(func):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "subcmd"):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Tuple):
                values = tuple(
                    element.value
                    for element in comparator.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                )
                if values:
                    literals[comparator.lineno] = values
    return literals


def _show_all_flags() -> tuple[str, ...]:
    """Collect the flag literals accepted by the `list` subcommand's show_all check.

    The source reads ``show_all = any(p in ("-a", "--all", "all", "全部") for p in parts[1:])``,
    so the comparison is against the loop variable ``p``; anchor on the assignment instead.
    """
    tree = ast.parse(MCSM_HANDLERS.read_text(encoding="utf-8"))
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_mcsm"
    )
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if "show_all" not in targets:
            continue
        for inner in ast.walk(node.value):
            if not isinstance(inner, ast.Compare):
                continue
            for comparator in inner.comparators:
                if isinstance(comparator, ast.Tuple):
                    values = tuple(
                        element.value
                        for element in comparator.elts
                        if isinstance(element, ast.Constant) and isinstance(element.value, str)
                    )
                    if values:
                        return values
    return ()


class McsmMojibakeTests(unittest.TestCase):
    def test_no_mojibake_anywhere_in_shipped_python(self):
        offenders: list[str] = []
        for path in HANDLER_SOURCES:
            for corrupted, recovered in _mojibake_runs(path.read_text(encoding="utf-8", errors="replace")):
                offenders.append(f"{path.relative_to(ROOT)}: {corrupted!r} should be {recovered!r}")
        self.assertEqual(offenders, [], "mojibake reintroduced:\n" + "\n".join(offenders))

    def test_chinese_aliases_are_readable_and_reachable(self):
        literals = _subcommand_tuples()
        flat = {value for values in literals.values() for value in values}

        # The previously broken aliases must now match real user input.
        for alias in ("重启", "强制结束", "强杀", "命令"):
            self.assertIn(alias, flat, f"{alias!r} is missing from the subcommand router")

        # And the corrupted forms must be gone entirely.
        for corrupted in ("閲嶅惎", "寮哄埗缁撴潫", "寮烘潃", "鍛戒护", "鍏ㄩ儴"):
            self.assertNotIn(corrupted, flat, f"mojibake {corrupted!r} still routed on")

    def test_list_all_flag_is_readable(self):
        """`/mcsm list -a` accepts the Chinese `全部` flag alongside -a/--all/all."""
        flags = _show_all_flags()
        self.assertIn("全部", flags, "'全部' show-all flag is missing")
        for corrupted in ("鍏ㄩ儴",):
            self.assertNotIn(corrupted, flags, f"mojibake {corrupted!r} still accepted")
        for flag in ("-a", "--all", "all"):
            self.assertIn(flag, flags, f"English flag {flag!r} was dropped")

    def test_english_aliases_still_present(self):
        flat = {value for values in _subcommand_tuples().values() for value in values}
        for alias in ("list", "start", "stop", "restart", "kill", "cmd", "log", "deploy"):
            self.assertIn(alias, flat, f"English alias {alias!r} was dropped")

    def test_help_subcommand_is_reachable(self):
        """``help`` must be tested by a real branch, not sit after a return."""
        source = MCSM_HANDLERS.read_text(encoding="utf-8")
        tree = ast.parse(source)
        func = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_mcsm"
        )
        tests = [ast.unparse(node.test) for node in ast.walk(func) if isinstance(node, ast.If)]
        self.assertTrue(
            any("help" in test for test in tests),
            "handle_mcsm has no `help` branch; the help tip is unreachable",
        )

    def test_no_unreachable_statements_in_handler(self):
        """The orphaned help tip used to sit after a return inside `if not subcmd`."""
        tree = ast.parse(MCSM_HANDLERS.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list):
                continue
            for index, statement in enumerate(body[:-1]):
                if isinstance(statement, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                    following = body[index + 1]
                    self.fail(
                        "unreachable statement at "
                        f"{MCSM_HANDLERS.name}:{following.lineno}: {ast.unparse(following)[:80]}"
                    )


class EftHelperRegistrationTests(unittest.TestCase):
    """The abandoned eft_helper package must not be advertised as switchable."""

    def test_empty_plugin_is_not_registered_as_group_feature(self):
        from otae_bot.groups.features import PLUGIN_NAMES

        init = ROOT / "plugins/eft_helper/__init__.py"
        self.assertEqual(init.read_bytes().strip(), b"", "eft_helper gained an entrypoint; re-check this test")
        self.assertNotIn(
            "eft_helper",
            PLUGIN_NAMES,
            "eft_helper registers no commands but is still offered as a group switch",
        )


class DeployLlmConfigTests(unittest.TestCase):
    def test_general_llm_falls_back_field_by_field_to_the_steam_group(self):
        from otae_bot.config import settings
        from plugins.mcsm.deploy import llm_config

        with settings.override({}):
            self.assertIsNone(llm_config())
        steam = {"llm_api_key": "steam-key", "llm_base_url": "https://steam.test", "llm_model": "steam-model"}
        with settings.override({"plugins": {"plugins.steamInfo": steam}}):
            self.assertEqual(llm_config(), {"api_key": "steam-key", "base_url": "https://steam.test",
                                            "model": "steam-model"})
        with settings.override({"otae": {"llm": {"api_key": "general", "model": "m"}},
                                "plugins": {"plugins.steamInfo": steam}}):
            self.assertEqual(llm_config(), {"api_key": "general", "base_url": "https://steam.test", "model": "m"})


MCSM_SCENARIOS = r"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

mc = plugin('plugins.mcsm.handlers')
G = '100'
PANEL = 'http://panel.test:23333'
INSTANCES = [
    {'instanceUuid': 'uuid-aaaa-1111', 'config': {'nickname': 'Survival'}, 'status': 3},
    {'instanceUuid': 'uuid-bbbb-2222', 'config': {'nickname': 'Lobby'}, 'status': 0},
]


def starts(sent, prefix, count=1):
    # 含尖括号的文案按 str 发出、经 Satori XML 解析，只比前缀
    return len(sent) == count and sent[-1].startswith(prefix)


def sent_to(channel):
    return [content for target, content in default_account.protocol.sent if target == channel]


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.daemons = [{'uuid': 'daemon-1', 'remarks': 'Node'}]
        self.actions = []
        self.deleted = []

    async def get_daemon_list(self):
        return list(self.daemons)

    async def get_daemon_instances(self, daemon_id):
        return list(INSTANCES)

    async def find_instance_daemon(self, uuid):
        return 'daemon-1' if uuid.startswith('uuid-') else None

    async def get_instance_detail(self, uuid, daemon_id):
        return {'instanceUuid': uuid, 'status': 3, 'config': {'nickname': 'Survival'}}

    async def _instance_action(self, action, uuid, daemon_id):
        self.actions.append((action, uuid))
        return {'status': 200}

    async def send_command(self, uuid, daemon_id, command):
        self.actions.append(('command', command))
        return {'status': 200}

    async def get_instance_output(self, uuid, daemon_id, size=0):
        return '[INFO] line one\n[INFO] line two'

    async def delete_instance(self, uuid, daemon_id, delete_files=False):
        self.deleted.append((uuid, delete_files))

    async def list_supported_docker_daemons(self):
        image = {'RepoTags': ['eclipse-temurin:21-jre']}
        return [{'uuid': 'daemon-1', 'remarks': 'Node', '_dockerImages': [image]},
                {'uuid': 'daemon-2', 'remarks': 'Other', '_dockerImages': [image]}]


async def dialog(user, first, *answers, group=G):
    # 第一条指令在后台等待 prompt，之后的消息逐条回答
    before = len(sent_in(group))
    task = asyncio.create_task(say(first, user=user, group=group))
    for answer in answers:
        await asyncio.sleep(0.1)
        await say(answer, user=user, group=group)
    await asyncio.wait_for(task, 5)
    return sent_in(group)[before:]


def sent_in(group):
    return sent_to(group)


async def main():
    no_render = patch.object(mc, 'run_image_render', AsyncMock(side_effect=RuntimeError('no render')))
    no_render.start()
    superusers = patch.object(mc, 'SUPERUSERS', ['8'])
    superusers.start()

    # 未绑定面板
    assert starts(await say('/mcsm'), '本群尚未关联 MCSM 面板\n发送 /mcsm bind ')
    for text in ('/mcsm help', '/mcsm 帮助'):
        assert await say(text) == ['发送 /help mcsm 获取 MCSM 详细指令说明。'], text
    assert starts(await say('/mcsm status x'), '本群尚未关联 MCSM 面板\n请发送 /mcsm bind ')
    assert starts(await say('/mcsm bind ' + PANEL, user='1'), '权限不足：')
    assert await say('/mcsm unbindpanel', user='8') == ['本群尚未关联 MCSM 面板']

    # 面板绑定：群里记录地址并私信索要 API Key
    sent = await say('/mcsm bind ' + PANEL, user='8')
    assert sent[-1] == '面板地址已记录\n面板: ' + PANEL + '\n请前往机器人私聊窗口发送对应的 API Key。', sent
    assert sent_to('private:8')[-1].startswith('请直接回复本条私聊消息以提供 MCSM API Key'), sent_to('private:8')
    # 私聊监听：群消息与无待确认的私聊不处理
    assert await say('hello', user='8') == []
    assert await say('随便说点', private=True, user='5') == []
    assert await say('short', private=True, user='8') == ['API Key 格式无效\n密钥长度过短，请重新输入完整的 API Key。\n若要放弃本次操作，请回复“取消”。']
    empty = FakeClient()
    empty.daemons = []
    with patch.object(mc, 'MCSMClient', lambda **kwargs: empty):
        sent = await say('key-1234567890', private=True, user='8')
    assert sent == ['正在核对 API Key...', '面板下暂无在线节点\n请确认面板网络与节点状态无误。\n未保存当前密钥，请重新发送有效 Key，或回复“取消”。'], sent
    assert await say('取消', private=True, user='8') == ['MCSM 面板绑定已取消']
    assert not mc._store.has_panel(G) and mc._store.get_pending_key('8') is None
    assert await say('取消', private=True, user='8') == []
    await say('/mcsm bind ' + PANEL, user='8')
    with patch.object(mc, 'MCSMClient', FakeClient):
        sent = await say('API Key: key-1234567890', private=True, user='8')
    assert sent[0] == '正在核对 API Key...' and starts(sent, 'MCSM 面板绑定已完成\n面板: ' + PANEL + '\n可用节点: 1', 2), sent
    assert mc._store.has_panel(G)
    assert starts(await say('/mcsm bind ' + PANEL, user='8'), '本群已关联面板')

    client = FakeClient()
    with patch.object(mc, 'get_client', lambda group_id: client):
        # 批量绑定向导：群里提示、私信列出候选，私聊回复编号
        sent = await say('/mcsm bind daemon-1', user='8')
        assert sent[1:] == ['绑定向导已私信送达\n候选实例清单及关联结果已发送至私聊窗口。'], sent
        assert sent[0] == sent_to('private:8')[-1] and sent[0].startswith('MCSM 实例批量绑定向导\n目标群：100'), sent
        assert await say('9', private=True, user='8') == ['未能匹配到有效实例\n未找到 9']
        await say('/mcsm bind daemon-1', user='8')
        assert await say('cancel', private=True, user='8') == ['实例批量关联已取消']
        assert await say('1', private=True, user='8') == []
        await say('/mcsm bind daemon-1', user='8')
        sent = await say('1=survival 2', private=True, user='8')
        assert sent == ['批量绑定完成：共 2 个实例添加成功\n成功绑定:\nsurvival (uuid-aaa)\nLobby (uuid-bbb)'], sent
        assert starts(await say('/mcsm bind nope', user='8'), '未检索到目标节点')
        assert starts(await say('/mcsm bind', user='8'), '参数格式错误')
        assert await say('/mcsm bind uuid-cccc x', user='1') == ['权限不足：该指令仅限当前群 MCSM 管理员、QQ 群管理员、群主或 SUPERUSER 运行。']
        assert await say('/mcsm bind uuid-cccc survival', user='8') == ['别名冲突：别名 survival 已被使用。']
        assert await say('/mcsm bind uuid-aaaa-1111 other', user='8') == ['实例已绑定：该实例已命名为 survival。']
        sent = await say('/mcsm bind uuid-cccc-3333 third', user='8')
        assert sent[0] == '正在检索实例归属节点...' and sent[1].startswith('实例绑定成功\n别名: third\nUUID: uuid-cccc-3333'), sent
        sent = await say('/mcsm bind zzz fourth', user='8')
        assert sent[0] == '正在检索实例归属节点...' and sent[1].startswith('未检索到指定实例'), sent

        # 列表、状态、模糊匹配
        for text in ('/mcsm', '/mcsm list', '/mcsm 列表 全部'):
            sent = await say(text, user='8')
            assert len(sent) == 1 and sent[0].startswith('MCSM 本群实例\n面板: ' + PANEL), (text, sent)
        sent = await say('/mcsm status survival')
        assert len(sent) == 1 and sent[0].startswith('MCSM  survival'), sent
        assert starts(await say('/mcsm info nothing'), '未检索到实例：nothing')
        sent = await say('/mcsm survival')
        assert len(sent) == 1 and sent[0].startswith('MCSM  survival'), sent
        assert starts(await say('/mcsm 乱七八糟'), '未知指令：乱七八糟')

        # 实例操作
        assert starts(await say('/mcsm start', user='8'), '指令格式：/mcsm open ')
        assert await say('/mcsm stop survival', user='1') == ['权限不足：该指令仅限当前群 MCSM 管理员、QQ 群管理员、群主或 SUPERUSER 运行。']
        assert await say('/mcsm stop nothing', user='8') == ['未检索到实例：nothing。']
        for text, action, name in (('/mcsm start survival', 'open', '开启'), ('/mcsm 启动 survival', 'open', '开启'),
                                   ('/mcsm stop survival', 'stop', '关闭'), ('/mcsm 停止 survival', 'stop', '关闭'),
                                   ('/mcsm restart survival', 'restart', '重启'), ('/mcsm 重启 survival', 'restart', '重启'),
                                   ('/mcsm kill survival', 'kill', '强制终止'), ('/mcsm 强杀 survival', 'kill', '强制终止')):
            op = mc.OPERATION_NAMES.get(action, action)
            sent = await say(text, user='8')
            assert sent == ['正在对实例 survival 执行「' + op + '」操作...',
                            '操作成功执行\n实例 survival 的「' + op + '」已完成。'], (text, sent)
        assert client.actions[-1] == ('kill', 'uuid-aaaa-1111'), client.actions
        with patch.object(mc, '_asyncio', SimpleNamespace(sleep=AsyncMock())):
            sent = await say('/mcsm cmd survival say hi', user='8')
        assert len(sent) == 2 and sent[1].startswith('survival 控制台输出'), sent
        assert client.actions[-1] == ('command', 'say hi'), client.actions
        # 缺命令：只发错误提示，不再多发「操作失败」
        sent = await say('/mcsm cmd survival', user='8')
        assert sent == ['正在对实例 survival 执行「' + mc.OPERATION_NAMES.get('command', 'command') + '」操作...',
                        '请输入要在控制台执行的具体命令。'], sent

        # 日志
        assert starts(await say('/mcsm log', user='8'), '命令格式：/mcsm log ')
        assert starts(await say('/mcsm 日志 survival -n 0', user='8'), '命令格式：/mcsm log ')
        sent = await say('/mcsm log survival -a', user='8')
        assert len(sent) == 1 and sent[0].startswith('survival 控制台日志'), sent

        # 隐藏、管理员
        assert await say('/mcsm hide survival', user='8') == ['实例已设定为隐藏\n实例: survival']
        assert starts(await say('/mcsm info survival', user='1'), '未检索到实例：survival')
        assert await say('/mcsm 显示 survival', user='8') == ['实例已设定为取消隐藏\n实例: survival']
        assert starts(await say('/mcsm admin foo', user='8'), 'admin 子命令格式错误')
        assert await say('/mcsm admin list') == ['本群 MCSM 管理员\n  8']  # 绑定面板者即管理员
        assert starts(await say('/mcsm admin add', user='8'), '未指定目标成员')
        assert await say('/mcsm admin add 123', user='8') == ['MCSM 群管理员已更新\n已授权群管理员：123']
        assert await say('/mcsm admin list') == ['本群 MCSM 管理员\n  8\n  123']
        assert await say('/mcsm admin remove 123', user='8') == ['MCSM 群管理员已更新\n已解除群管理员：123']

        # 部署：参数错误、预检中选择节点（取消/选中）
        assert starts(await say('/mcsm deploy', user='8'), 'deploy 指令参数有误')
        deploy = '/mcsm deploy srv https://example.com/pack.zip --port 25570 --dry-run'
        sent = await dialog('8', deploy, 'cancel')
        assert sent[0] == '开始部署流程：正在解析资源链接并检测节点与环境...', sent
        assert sent[1].startswith('请选择用于部署服务的 Docker 节点：') and sent[2] == 'Docker 节点选择已取消', sent
        assert len(sent) == 3, sent
        real_prompt = mc.prompt
        with patch.object(mc, 'prompt', lambda *args, timeout: real_prompt(*args, timeout=0.3)):
            sent = await dialog('8', deploy)
        assert len(sent) == 4 and '超时' in sent[2] and sent[3] == 'Docker 节点选择已取消', sent
        sent = await dialog('8', deploy, '2')
        assert len(sent) == 3 and sent[2].startswith('MCSM Docker 部署预检通过\n别名: srv\n节点: Other'), sent

        # 解绑、删除
        assert starts(await say('/mcsm unbind', user='8'), '指令格式：/mcsm unbind')
        assert await say('/mcsm unbind third', user='8') == ['实例关联已解除\n实例: third']
        assert await say('/mcsm unbind third', user='8') == ['未找到别名为 third 的实例。']
        assert await say('/mcsm delete Lobby --files', user='8') == ['实例已彻底删除\n别名: Lobby\n实例: uuid-bbb\n文件: 已请求删除']
        assert client.deleted == [('uuid-bbbb-2222', True)], client.deleted

    # 图片渲染可用时发图
    no_render.stop()
    sent = await say('/mcsm admin list')
    assert len(sent) == 1 and sent[0].startswith('<img'), sent
    no_render.start()

    assert await say('/mcsm 解绑面板', user='8') == ['MCSM 面板解绑成功\n已清除当前群聊的所有实例关联数据。']
    assert not mc._store.has_panel(G)
    no_render.stop()
    superusers.stop()
    print('ALL-OK')

run(main())
"""


class McsmDispatchTests(unittest.TestCase):
    def test_mcsm_command_and_private_dialogs_through_the_real_event_path(self):
        from tests.support.dispatch import run_scenarios

        result = run_scenarios(MCSM_SCENARIOS)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-6000:])
        self.assertIn("ALL-OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
