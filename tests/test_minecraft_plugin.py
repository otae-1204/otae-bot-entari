from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from dns.resolver import LifetimeTimeout


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class MinecraftPluginTests(unittest.TestCase):
    def test_saved_identifiers_are_case_insensitive(self):
        data_source = _load_module(
            "minecraft_casefold_data_source_for_test",
            "plugins/minecraft_plugin/data_source.py",
        )
        with TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            os.chdir(tmp)
            try:
                manager = data_source.MinecraftDataManager()
                self.assertTrue(manager.add_group_server("10001", "short-address", "nfwc2"))
                self.assertTrue(manager.update_server_nickname("10001", "nfwc2", "survival"))

                self.assertEqual(
                    manager.get_server_by_identifier("10001", "NFWC2")["address"],
                    "nfwc2",
                )
                self.assertEqual(
                    manager.get_server_by_nickname("10001", "SURVIVAL")["address"],
                    "nfwc2",
                )
            finally:
                os.chdir(cwd)

    def test_dns_timeout_returns_error_without_traceback(self):
        ping_module = _load_module(
            "minecraft_dns_timeout_ping_for_test",
            "plugins/minecraft_plugin/ping.py",
        )
        timeout = LifetimeTimeout(timeout=3, errors=[])

        with patch.object(ping_module, "_lookup_and_status", side_effect=timeout):
            result = asyncio.run(ping_module.ping("NFWC2", "java"))

        self.assertEqual(result["status"], "error")
        self.assertIn("域名解析", result["data"])
        self.assertNotIn("Traceback", result["data"])

    def test_ping_command_resolves_saved_identifier_first(self):
        source = (ROOT / "plugins/minecraft_plugin/handlers.py").read_text(encoding="utf-8")
        start = source.index("# 优先按已保存的地址、名称或昵称查找")
        end = source.index("r = await ping(command_args", start)
        lookup_source = source[start:end]

        self.assertIn("get_server_by_identifier(group_id, command_args)", lookup_source)
        self.assertNotIn("get_server_by_nickname(group_id, command_args)", lookup_source)



MINECRAFT_SCENARIOS = r"""
import io
from unittest.mock import AsyncMock, patch
from PIL import Image as PILImage

mc = plugin('plugins.minecraft_plugin.handlers')
buffer = io.BytesIO()
PILImage.new('RGB', (2, 2)).save(buffer, format='PNG')
PNG = buffer.getvalue()
drawn = []


async def fake_render(func, *args):
    drawn.append((func.__name__, args))
    return io.BytesIO(PNG)


pings = []


async def fake_ping(address, kind='java'):
    pings.append(address)
    return {'status': 'success', 'data': {'players': ['Steve'], 'favicon': None, 'game_version': '1.21', 'max_players': 20}}


NAMES = {
    'ping': ['ping', 'Ping', 'PING', 'p', 'P'],
    'addserver': ['addserver', 'Addserver', 'add_server', 'as', '添加服务器'],
    'addservernickname': ['addservernickname', 'asnn', 'asn', '添加服务器昵称'],
    'removeserver': ['removeserver', 'Removeserver', 'remove_server', 'rs', 'delserver', 'del_server', 'ds', '删除服务器'],
    'removeservernickname': ['removeservernickname', 'rsnn', 'rsn', '删除服务器昵称'],
    'updateservername': ['updateservername', 'Updateservername', 'update_server_name', 'usn', 'USN', 'update_name',
                         'updatename', 'un', 'editname', 'edit_name', 'en', '修改服务器名称'],
    'updateserveraddress': ['updateserveraddress', 'Updateserveraddress', 'update_server_address', 'usa', 'USA',
                            'updateaddress', 'update_address', 'ua', 'UA', 'editaddress', 'edit_address', 'ea', '修改服务器地址'],
    'pinglist': ['pinglist', 'Pinglist', 'ping_list', 'pl', 'PL', '服务器列表'],
    'addbroadcast': ['addbroadcast', 'Addbroadcast', 'add_broadcast', 'ab', '添加播报服务器'],
    'removebroadcast': ['removebroadcast', 'Removebroadcast', 'remove_broadcast', 'rb', '删除播报服务器'],
    'updatebroadcastaddress': ['updatebroadcastaddress', 'Updatebroadcastaddress', 'update_broadcast_address', 'uba', 'UBA',
                               'updatebroadcast', 'Updatebroadcast', 'ub', 'UB', 'editbroadcastaddress',
                               'edit_broadcast_address', 'eba', '修改播报服务器地址'],
    'updatebroadcastname': ['updatebroadcastname', 'Updatebroadcastname', 'update_broadcast_name', 'ubn', 'UBN',
                            'updatebroadcastnickname', 'ubnn', 'editbroadcastname', 'edit_broadcast_name', 'ebn', '修改播报服务器名称'],
    'broadcastlist': ['broadcastlist', 'Broadcastlist', 'broadcast_list', 'bl', 'BL', '播报服务器列表'],
    'broadcastinterval': ['broadcastinterval', 'Broadcastinterval', 'broadcast_interval', 'bi', 'BI', '播报间隔'],
    'online': ['online', 'Online', 'ol', '在线', '排行榜', 'rank'],
}
# 各指令不带参数时的回复（空群）；按 str 发出，尖括号经 Satori XML 解析，保持原样
EMPTY = {
    'ping': '当前群尚未添加任何服务器，可发送 /addserver <名称> <地址> 进行添加。</地址></名称>',
    'addserver': None,
    'addservernickname': None,
    'removeserver': None,
    'removeservernickname': None,
    'updateservername': None,
    'updateserveraddress': None,
    'pinglist': '当前群尚未保存任何服务器。',
    'addbroadcast': None,
    'removebroadcast': None,
    'updatebroadcastaddress': None,
    'updatebroadcastname': None,
    'broadcastlist': '当前群尚未开启任何服务器的播报。',
    'broadcastinterval': None,
    'online': '暂无玩家时长记录。\n（数据将在玩家断开连接离开服务器时自动统计）',
}


async def main():
    with patch.object(mc, 'ping', fake_ping), patch.object(mc, 'run_image_render', fake_render):
        first = {}
        for key, names in NAMES.items():
            for name in names:
                sent = await say('/' + name, group='500')
                assert len(sent) == 1, (name, sent)
                first.setdefault(key, sent[0])
                assert sent[0] == first[key], (name, sent, first[key])
        for key, text in EMPTY.items():
            if text is not None:
                assert first[key] == text, (key, first[key])
        assert first['addserver'].startswith('格式错误：/addserver'), first
        assert first['removeserver'].startswith('格式错误：/removeserver'), first
        assert first['addbroadcast'].startswith('格式错误：/addbroadcast'), first
        assert first['broadcastinterval'].startswith('当前群播报间隔：'), first

        # 服务器增删改
        assert await say('/addserver srv1 a.example.net') == ['服务器添加成功：srv1（a.example.net）']
        assert await say('/addserver srv1 a.example.net') == ['添加失败：服务器已存在或地址格式有误。']
        assert (await say('/addserver onlyname'))[0].startswith('格式错误：/addserver')
        assert await say('/asn srv1 nick1') == ['已成功为服务器 srv1 设置昵称：nick1']
        assert await say('/asn nosuch x') == ['设置昵称失败：未找到对应的服务器名称。']
        # 昵称以列表保存，列表原样拼进文本（保持原样）
        assert await say('/pl') == ["本群已保存的服务器列表：\n  srv1 (['nick1']) - a.example.net"]
        # ping：单个服务器、按昵称、直接地址、多个服务器
        assert len(await say('/ping')) == 1 and drawn[-1][0] == 'draw_server_players'
        assert drawn[-1][1][0]['address'] == 'a.example.net' and drawn[-1][1][0]['nickname'] == ['nick1'], drawn[-1]
        sent = await say('/ping NICK1')
        assert len(sent) == 1 and sent[0].startswith('<img') and drawn[-1][0] == 'draw_server_players', (sent, drawn[-1])
        sent = await say('/p other.host')
        assert len(sent) == 1 and drawn[-1][0] == '_draw_ping_result' and drawn[-1][1][0]['address'] == 'other.host', drawn[-1]
        assert pings[-1] == 'other.host'
        assert await say('/as srv2 b.example.net') == ['服务器添加成功：srv2（b.example.net）']
        sent = await say('/ping')
        assert len(sent) == 1 and drawn[-1][0] == '_draw_server_list_from_responses', drawn[-1]
        assert [r['name'] for r in drawn[-1][1][0]] == ['srv1', 'srv2'] and drawn[-1][1][1] == '100', drawn[-1]
        assert await say('/usn srv1 srvA') == ['服务器名称更新成功。']
        assert await say('/usn nosuch x') == ['更新失败：未找到指定的服务器。']
        assert (await say('/usn srvA'))[0].startswith('格式错误：/updateservername')
        assert await say('/usa srvA c.example.net') == ['服务器地址更新成功。']
        assert await say('/usa nosuch x') == ['更新失败：未找到指定的服务器。']
        assert await say('/rsn srvA') == ['已成功清除服务器 srvA 的昵称。']
        assert await say('/rsn nosuch') == ['清除昵称失败：未找到指定的服务器。']
        assert await say('/rs srv2') == ['已成功删除服务器：srv2']
        assert await say('/rs srv2') == ['删除失败：未找到指定的服务器。']
        assert await say('/pl') == ['本群已保存的服务器列表：\n  srvA - c.example.net']

        # 播报服务器
        assert await say('/ab srvA') == ['已成功将 srvA 加入播报名单。']
        assert await say('/ab newsrv d.example.net') == ['已成功将 newsrv（d.example.net）添加到播报名单。']
        sent = await say('/ab unknown')
        assert len(sent) == 1 and sent[0].startswith('未在已存列表中找到 unknown'), sent
        sent = await say('/bl')
        assert len(sent) == 1 and sent[0].startswith('本群启用的播报服务器列表：\n当前轮询间隔：'), sent
        assert (await say('/uba newsrv'))[0].startswith('格式错误：/updatebroadcastaddress')
        assert await say('/uba newsrv e.example.net') == ['播报服务器 newsrv 的地址已更新。']
        assert (await say('/ubn newsrv'))[0].startswith('格式错误：/updatebroadcastname')
        assert await say('/ubn newsrv newer') == ['播报服务器名称已成功更新。']
        assert await say('/rb newer') == ['已成功将 newer 从播报名单中移除。']

        # 播报间隔
        assert await say('/bi abc') == ['时间格式错误，支持 60s、5m、1h 或纯数字（单位：分钟）。']
        assert await say('/bi 30s') == ['播报间隔范围须在 60 秒至 24 小时之间。']
        assert await say('/bi 25h') == ['播报间隔范围须在 60 秒至 24 小时之间。']
        assert await say('/bi 10m') == ['本群播报间隔已调整为：10分钟']
        sent = await say('/bi')
        assert sent[0].startswith('当前群播报间隔：10分钟\n全局预设间隔：'), sent
        assert (await say('/bl'))[0].startswith('本群启用的播报服务器列表：\n当前轮询间隔：10分钟'), sent
        sent = await say('/bi 重置')
        assert len(sent) == 1 and sent[0].startswith('已恢复为全局默认播报间隔：'), sent

        # 在线排行榜
        assert await say('/online nosuch') == ['未查询到服务器：nosuch']
        top = [{'player_id': 'Steve', 'formatted_time': '1h 0m'}]
        dm = mc.data_manager
        with patch.object(dm, 'get_top_players', return_value=top) as by_server, \
             patch.object(dm, 'get_server_player_gametimes', return_value={'Steve': 3600}), \
             patch.object(dm, 'get_top_players_all_servers', return_value=top) as all_servers, \
             patch.object(dm, 'get_all_servers_player_gametimes', return_value={'Steve': 3600, 'Alex': 1}):
            sent = await say('/ol srvA -a')
            assert len(sent) == 1 and drawn[-1][0] == 'draw_player_leaderboard', drawn[-1]
            assert by_server.call_args.kwargs == {'limit': 0} and by_server.call_args.args[1] == 'c.example.net'
            info = drawn[-1][1][0]
            assert info['name'] == 'srvA' and info['data']['playtimes'] == {'Steve': '1h 0m'} and info['data']['game_version'] == '1.21', info
            sent = await say('/rank')
            assert len(sent) == 1 and all_servers.call_args.kwargs == {'limit': 10}
            info = drawn[-1][1][0]
            assert info['name'] == '全部服务器' and info['data']['online_players'] == 2, info
    print('ALL-OK')

run(main())
"""


class MinecraftDispatchTests(unittest.TestCase):
    def test_minecraft_commands_through_the_real_event_path(self):
        from tests.support.dispatch import run_scenarios

        result = run_scenarios(MINECRAFT_SCENARIOS)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-6000:])
        self.assertIn("ALL-OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
