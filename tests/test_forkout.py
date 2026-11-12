"""/forkrank 与「叉出去」走真实的事件派发：真实的 Satori 事件进来，只伪造协议的 HTTP 调用。"""

from __future__ import annotations

import unittest

from tests.support.dispatch import run_scenarios

SCENARIOS = r'''
from unittest.mock import AsyncMock, patch
from satori import At, Text

forkout = plugin('plugins.forkout.handlers')
account = default_account
account.protocol.answers['guild.member.get'] = lambda params: {
    'user': {'id': params['user_id'], 'name': 'QQ名' + params['user_id']}, 'nick': '群名片' + params['user_id'],
}

async def main():
    # 别名同样可用；本群没有记录时如实回答
    for text in ('/forkrank', '/叉排行', '/xr', '/XR'):
        assert await say(text) == ['本群暂无被叉记录。'], text

    # 不带 @ 也不带引用的「叉」不响应
    assert await say('/叉') == []

    with patch.object(forkout, '_download_avatar', AsyncMock(return_value=None)):
        sent = await say([Text('/叉 '), At('42')])
        sent += await say([Text('/X '), At('42')])
        sent += await say([Text('/forkout '), At('7')])
    assert len(sent) == 3 and all('<img' in item for item in sent), sent

    # 排行按次数排序，名字取群名片；其他群看不到这些记录
    ranking = await say('/forkrank 多余参数')
    assert ranking == ['=== 被叉排行榜 ===\n1ST 群名片42 - 被叉 2 次\n2ND 群名片7 - 被叉 1 次'], ranking
    assert await say('/xrank', group='200') == ['本群暂无被叉记录。']

    # 私聊里没有群，排行按私聊频道算（保持现有行为）
    assert await say('/forkrank', private=True) == ['本群暂无被叉记录。']

    # 群功能关闭后命令不响应
    from otae_bot.groups.features import feature_store, group_scope
    feature_store.set_enabled(group_scope(account, '100'), 'forkout', False)
    assert await say('/forkrank') == []
    feature_store.set_enabled(group_scope(account, '100'), 'forkout', True)
    print('ALL-OK')

run(main())
'''


class ForkoutDispatchTests(unittest.TestCase):
    def test_commands_through_the_real_event_path(self):
        result = run_scenarios(SCENARIOS)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-6000:])
        self.assertIn("ALL-OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
