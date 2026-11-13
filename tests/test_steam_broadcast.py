from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from arclet.letoderea import Subscriber

from otae_bot.accounts import runtime
from otae_bot.accounts.ready import get_bot
from otae_bot.groups.features import GroupFeatureStore, GroupScope
from otae_bot.pipeline import feature_gate
from plugins.steamInfo import handlers
from tests.support.satori_accounts import make_account, pin_event_loop

GROUP = "100"


class Disabled:
    def __init__(self):
        self.parents, self.saved = set(), 0

    def is_disabled(self, parent_id):
        return parent_id in self.parents

    def add(self, parent_id):
        self.parents.add(parent_id)

    def save(self):
        self.saved += 1


@pytest.fixture
def steam(monkeypatch):
    disabled = Disabled()
    entry = {"type": "start", "player": {"steamid": "1", "gameextrainfo": "Factorio"}}
    monkeypatch.setattr(handlers, "disable_parent_data", disabled)
    monkeypatch.setattr(handlers, "config", SimpleNamespace(steam_broadcast_type="none"))
    monkeypatch.setattr(handlers, "steam_info_data", SimpleNamespace(compare=lambda *_: [entry]))
    monkeypatch.setattr(handlers, "_display_name", lambda *_: "Alice")
    directory = tempfile.TemporaryDirectory()
    store = GroupFeatureStore(Path(directory.name) / "switches.json")
    monkeypatch.setattr(feature_gate, "feature_store", store)
    runtime.clear_account()
    yield SimpleNamespace(disabled=disabled, store=store)
    runtime.clear_account()
    directory.cleanup()


def broadcast(*accounts, before=None, as_job=False):
    """Bring (self_id, platform, groups[, reject]) accounts online, run one Steam broadcast.

    Returns (result, {self_id: [(channel, content), ...]}).
    """
    async def scenario():
        with pin_event_loop():
            bots = []
            for self_id, platform, groups, *reject in accounts:
                bot = make_account(self_id, platform=platform, guilds=groups)
                bot.protocol.reject = reject[0] if reject else None
                runtime.set_account(bot)
                bots.append(bot)
            await runtime.refresh_guilds()
            if before is not None:
                before(bots)
            if as_job:
                async def job():
                    return await handlers.broadcast_steam_info(GROUP, {"response": {"players": []}})

                job.__module__ = "plugins.steamInfo.handlers"
                result = await Subscriber(job).handle({})
            else:
                result = await handlers.broadcast_steam_info(GROUP, {"response": {"players": []}})
            return result, {bot.self_id: bot.protocol.sent for bot in bots}

    return asyncio.run(scenario())


QQ = ("9", "llonebot", [GROUP])
IR = ("ir-7f3a", "ir_web", ["folder-root"])  # The Ir panel: "guilds" are folders, not QQ groups.


def test_broadcast_goes_to_the_group_through_an_account_in_it(steam):
    def check_default(bots):
        assert get_bot() is bots[-1]  # Ir logged in last: it is the default account.

    result, sent = broadcast(QQ, IR, before=check_default)
    assert result is True
    assert sent == {"9": [(GROUP, "Alice 开始玩 Factorio 了")], "ir-7f3a": []}


def test_without_an_account_in_the_group_the_broadcast_waits_and_is_not_disabled(steam):
    # Ir would have answered with an error the disable rule recognises.
    result, sent = broadcast(("ir-7f3a", "ir_web", ["folder-root"], RuntimeError("message_create failed")))
    assert result is False
    assert sent == {"ir-7f3a": []}
    assert not steam.disabled.parents


def test_switched_off_group_is_skipped_but_counts_as_done(steam):
    steam.store.set_enabled(GroupScope("llonebot", "9", GROUP), "steamInfo", False)
    result, sent = broadcast(QQ, as_job=True)
    assert result is True and sent == {"9": []}
    assert not steam.disabled.parents


def test_kicked_group_disables_its_broadcast(steam):
    result, _ = broadcast(("9", "llonebot", [GROUP], RuntimeError("发送失败：已被移出该群")))
    assert result is False
    assert steam.disabled.parents == {GROUP} and steam.disabled.saved == 1


def test_other_send_errors_keep_the_broadcast(steam):
    result, _ = broadcast(("9", "llonebot", [GROUP], RuntimeError("timeout")))
    assert result is False
    assert not steam.disabled.parents


STEAM_SCENARIOS = r"""
import asyncio, base64, io
from unittest.mock import AsyncMock, patch
from arclet.entari import Image, Text
from PIL import Image as PILImage
from otae_bot.config.settings import Config as GlobalConfig

st = plugin('plugins.steamInfo.handlers')
GROUP_ONLY = '请在群聊或频道中使用 Steam 相关指令。'
SID = '76561198000000001'
real_prompt = st.prompt


def short_prompt(*args, timeout):
    return real_prompt(*args, timeout=0.3)


def sent_in(group):
    return [content for channel, content in default_account.protocol.sent if channel == group]


async def dialog(user, first, *answers, group='100'):
    # 第一条指令在后台等待 prompt，之后的消息逐条回答
    before = len(sent_in(group))
    task = asyncio.create_task(say(first, user=user, group=group))
    for answer in answers:
        await asyncio.sleep(0.1)
        await say(answer, user=user, group=group)
    await asyncio.wait_for(task, 5)
    return sent_in(group)[before:]


def png_data_url():
    buffer = io.BytesIO()
    PILImage.new('RGB', (4, 4)).save(buffer, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()


def starts(sent, prefix):
    # 含尖括号的文案按 str 发出、经 Satori XML 解析，只比前缀
    return len(sent) == 1 and sent[0].startswith(prefix)


async def fake_segment(*args, **kwargs):
    return Image(src='https://example.com/card.png')


async def main():
    # 定时任务的 id 与间隔不变
    job = st.timer._jobs['update_steam_info']
    assert job.max_instances == 1 and job.interval_seconds == st.config.steam_request_interval, job

    # 帮助：__plugin_meta__ 不存在，兼容层下抛 NameError，什么也不发
    for text in ('/steamhelp', '/steam帮助'):
        assert await say(text) == [], text

    # 私聊里一律提示到群里用
    for text in ('/steambind 1', '/steamunbind', '/steaminfo', '/steamgame x', '/steamupdate',
                 '/steamenable', '/steamdisable', '/steamnickname a', '/steamcheck'):
        assert await say(text, private=True, user='1') == [GROUP_ONLY], text
    assert await say('/steamcheck 多余', private=True, user='1') == []

    # 绑定、更新、昵称、解绑
    assert starts(await say('/steambind abc', user='1'), '账号输入有误，格式：/steambind ')
    assert starts(await say('/steambind', user='1'), '账号输入有误，格式：/steambind ')
    assert await say('/steamnickname 甲', user='1') == ['尚未绑定账号，请先通过 /steambind 绑定 Steam ID 后再设置昵称。']
    assert await say('/steambind ' + SID, user='1') == ['Steam ID 绑定成功：' + SID]
    assert await say('/绑定steam 39734273', user='1') == ['Steam ID 更新成功：' + SID]
    assert starts(await say('/steamnickname', user='1'), '格式错误：/steamnickname ')
    assert await say('/steam昵称 甲', user='1') == ['Steam 播报昵称已设置为：甲']
    assert await say('/steambind ' + SID, user='2') == ['Steam ID 绑定成功：' + SID]
    assert await say('/steamunbind', user='2') == ['Steam ID 解绑成功。']
    assert await say('/解绑steam', user='2') == ['当前未绑定任何 Steam ID。']

    # 开关播报
    assert await say('/steamdisable') == ['已停用本群 Steam 游戏动态播报。']
    assert st.disable_parent_data.is_disabled('100')
    assert await say('/禁用steam') == ['已停用本群 Steam 游戏动态播报。']
    assert await say('/steamenable') == ['已开启本群 Steam 游戏动态播报。']
    assert not st.disable_parent_data.is_disabled('100')
    assert await say('/启用steam') == ['已开启本群 Steam 游戏动态播报。']

    # 个人名片：@ 未绑定成员、输入有误、本人未绑定、成功出图
    # 带 @ 时处理函数读 At.target（Satori 的 At 只有 id）抛 AttributeError，不回复（迁移前后相同）
    assert await say([Text('/steaminfo '), st.At('9')], user='1') == []
    assert await say('/steam信息 abc', user='1') == ['输入有误：请输入有效的 Steam ID 或好友代码。']
    assert starts(await say('/steaminfo', user='3'), '你尚未绑定 Steam ID，请先使用：/steambind ')
    player = {'background': None, 'avatar': None, 'player_name': 'A', 'description': '',
              'recent_2_week_play_time': '', 'game_data': []}
    with patch.object(st, 'get_user_data', AsyncMock(return_value=player)) as get_user_data, \
         patch.object(st, '_render_image_segment', fake_segment):
        sent = await say('/steaminfo', user='1')
        assert sent == ['<img src="https://example.com/card.png"/>'], sent
        get_user_data.assert_awaited_once()
        assert get_user_data.await_args.args[0] == int(SID)
        sent = await say('/steaminfo 39734273', user='3')
        assert sent == ['<img src="https://example.com/card.png"/>'], sent

    # 群状态：带参数时不响应
    users = AsyncMock(return_value={'response': {'players': []}})
    parent = AsyncMock(return_value=(None, '群'))
    with patch.object(st, 'get_steam_users_info', users), patch.object(st, '_resolve_parent_info', parent), \
         patch.object(st, '_render_image_segment', fake_segment):
        for text in ('/steamcheck', '/查看steam', '/查steam'):
            assert await say(text) == ['<img src="https://example.com/card.png"/>'], text
        assert parent.await_args.args[0] is default_account
        assert await say('/steamcheck 多余') == []

    # 更新群信息
    msg = '格式要求：消息需同时包含群名文本与头像图片。'
    assert await say('/steamupdate 新群名') == [msg]
    assert starts(await say('/更新群信息'), '格式要求')
    assert await say([Text('/steam_update_parent 新群名 '), Image(src=png_data_url())]) == ['群信息已成功更新。']
    assert st.parent_data.get('100')[1].startswith('新群名'), st.parent_data.get('100')[1]

    # 缓存管理：仅超管
    assert await say('/steamcache', user='1') == ['权限不足：仅机器人超级管理员可使用此命令。']
    old_superusers = GlobalConfig.SUPERUSERS
    GlobalConfig.SUPERUSERS = ['8']
    try:
        assert starts(await say('/steam缓存', user='8'), '格式说明：')
        assert starts(await say('/steamcache foo', user='8'), '格式说明：')
        assert starts(await say('/steamcache alias', user='8'), '格式说明：')
        with patch.object(st, 'get_steam_app_list', AsyncMock(return_value=[1, 2])):
            assert await say('/steamcache refresh', user='8') == ['Steam 应用列表已刷新完成，共缓存 2 款应用。']
        with patch.object(st, 'write_steam_app_alias') as write_alias:
            assert await say('/steamcache alias set 别名 10 游戏 名', user='8') == ['alias set: 别名 -&gt; 游戏 名 (10)']
            write_alias.assert_called_once_with('别名', {'appid': 10, 'name': '游戏 名'})
            assert starts(await say('/steamcache alias set 别名 x 游戏', user='8'), '格式说明：')
        with patch.object(st, 'delete_steam_app_alias', return_value=False):
            assert await say('/steamcache alias del 别名', user='8') == ['alias not found: 别名']
        with patch.object(st, 'read_steam_app_aliases', return_value={}):
            assert await say('/steamcache alias list', user='8') == ['alias cache: no matched items']
        assert starts(await say('/steamcache alias bogus', user='8'), '格式说明：')
        with patch.object(st, 'write_app_lookup_cache') as write_lookup:
            assert await say('/steamcache lookup set 词 20 名', user='8') == ['lookup set: 词 -&gt; 名 (20)']
        with patch.object(st, 'delete_app_lookup_cache', return_value=True):
            assert await say('/steamcache lookup del 词', user='8') == ['lookup deleted: 词']
        assert await say('/steamcache lookup list', user='8') == ['lookup cache: no matched items']
        assert starts(await say('/steamcache lookup bogus', user='8'), '格式说明：')
        assert await say('/steamcache ambiguous list', user='8') == ['ambiguous cache: no matched items']
        with patch.object(st, 'delete_app_ambiguous_cache', return_value=False):
            assert await say('/steamcache ambiguous del 词', user='8') == ['ambiguous not found: 词']
        assert starts(await say('/steamcache ambiguous set', user='8'), '格式说明：')
    finally:
        GlobalConfig.SUPERUSERS = old_superusers

    # 游戏时长榜
    assert starts(await say('/steamgame', user='1'), '格式错误：/steamgame ')
    with patch.object(st, '_steam_api_keys', return_value=''):
        assert await say('/steam游戏 x', user='1') == ['未配置 Steam API Key，暂无法查询玩家游戏数据。']
    apps = [{'appid': 10, 'name': '甲游戏'}, {'appid': 20, 'name': '乙游戏'}]
    with patch.object(st, '_steam_api_keys', return_value='key'), \
         patch.object(st, 'is_steam_app_list_cache_stale', return_value=False), \
         patch.object(st, 'find_unambiguous_steam_app', AsyncMock(return_value=None)), \
         patch.object(st, 'read_app_ambiguous_cache', return_value=[]), \
         patch.object(st, 'suggest_steam_game_names', AsyncMock(return_value=[])), \
         patch.object(st, 'append_app_ambiguous_cache') as append_cache, \
         patch.object(st, 'get_steam_users_info', AsyncMock(return_value={'response': {'players': []}})), \
         patch.object(st, 'get_owned_game', AsyncMock(return_value=None)):
        assert await say('/steam时长 x', group='200', user='1') == ['当前群聊尚未有成员绑定 Steam ID。']
        prompt_text = st._game_candidate_prompt('游戏', apps)
        with patch.object(st, 'resolve_steam_app_candidates', AsyncMock(return_value=apps)), \
             patch('plugins.steamInfo.steam.write_app_ambiguous_cache'):
            # 多候选：选序号后出结果；无效序号与超时结束查询
            sent = await dialog('1', '/steamgame 游戏', '2')
            assert sent == [prompt_text, '已绑定的 1 位成员中，暂无关于 乙游戏 的公开游玩时长数据。'], sent
            append_cache.assert_called_once()
            sent = await dialog('1', '/steamgame 游戏', '9')
            assert sent == [prompt_text, '序号或名称选择无效，请重新发起查询。'], sent
            with patch.object(st, 'prompt', short_prompt):
                sent = await dialog('1', '/steamgame 游戏')
            assert len(sent) == 3 and sent[0] == prompt_text and '超时' in sent[1], sent
            assert sent[2] == '等待选择超时，本次查询已自动取消。', sent
            # 回复游戏名：再确认
            with patch.object(st, 'find_steam_app', AsyncMock(return_value=None)):
                sent = await dialog('1', '/steamgame 游戏', '丙游戏')
                assert sent == [prompt_text, '未检索到游戏：丙游戏'], sent
        # 单个候选：确认 yes/no/无效
        confirm = '已找到游戏：甲游戏（AppID: 10）。请回复 yes 或 no 进行确认：'
        with patch.object(st, 'resolve_steam_app_candidates', AsyncMock(return_value=[])), \
             patch.object(st, 'suggest_steam_game_names', AsyncMock(return_value=['甲游戏'])), \
             patch.object(st, 'find_steam_app', AsyncMock(return_value=apps[0])):
            assert await dialog('1', '/steamgame 甲', 'no') == [confirm, '已取消当前游戏查询。']
            assert await dialog('1', '/steamgame 甲', 'maybe') == [confirm, '输入无效，查询已取消，请重新发送指令。']
            sent = await dialog('1', '/steamgame 甲', 'yes')
            assert sent == [confirm, '已绑定的 1 位成员中，暂无关于 甲游戏 的公开游玩时长数据。'], sent
        with patch.object(st, 'resolve_steam_app_candidates', AsyncMock(return_value=[])), \
             patch.object(st, 'find_steam_app', AsyncMock(return_value=None)):
            assert await say('/steamgame 没有', user='1') == ['未检索到游戏：没有']
    print('ALL-OK')

run(main())
"""


def test_steam_commands_through_the_real_event_path():
    from tests.support.dispatch import run_scenarios

    result = run_scenarios(STEAM_SCENARIOS)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-6000:]
    assert "ALL-OK" in result.stdout
