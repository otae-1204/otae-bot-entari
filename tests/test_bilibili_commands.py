"""/bili 指令与 B 站链接预览经真实事件派发的场景。"""

from __future__ import annotations

import unittest

BILI_SCENARIOS = r"""
from unittest.mock import AsyncMock, patch
from arclet.entari import Text

bili = plugin('plugins.bilibilibot.handlers')
service = bili.service
HELP = 'B站订阅指令指南：'
KIND_ERROR = '订阅类型错误：仅支持 all、live、video 或 dynamic。'


async def main():
    follow = AsyncMock(return_value=(['UP 1'], ['UP 2：不存在']))
    unfollow = AsyncMock(return_value=(['UP 1'], []))
    refresh = AsyncMock(return_value=([], ['room:3：超时']))
    listing = AsyncMock(return_value=['订阅列表', '- UP 1'])
    preview = AsyncMock(return_value=None)
    segment = AsyncMock(return_value=Text('CARD'))
    with patch.object(service, 'follow', follow), patch.object(service, 'unfollow', unfollow), \
         patch.object(service, 'refresh', refresh), patch.object(service, 'list_subscriptions', listing), \
         patch.object(service, 'preview_link', preview), patch.object(service, 'card_to_segment', segment):
        # 帮助
        for text in ('/bili', '/bili help'):
            sent = await say(text)
            assert len(sent) == 1 and sent[0].startswith(HELP) and 'lottery_filter_uids' in sent[0], sent
        # 参数不足、类型错误
        # 文案按 str 发出（经 Satori XML 解析），保持原样
        assert await say('/bili follow live') == [
            '参数不足：格式为 /bili <follow|unfollow|refresh> <类型> <目标/></类型></follow|unfollow|refresh>']
        assert await say('/bili unfollow bad 1') == [KIND_ERROR]
        assert not follow.called and not unfollow.called and not refresh.called
        # follow 只回关注结果，不再刷新
        sent = await say('/bili FOLLOW live 1 2 BV1xx')
        assert sent == ['B站关注处理结果：\n成功:\n- UP 1\n失败:\n- UP 2：不存在'], sent
        follow.assert_awaited_once_with('live', ['1', '2', 'BV1xx'], 'group', '100')
        assert not refresh.called and not preview.called
        sent = await say('/bili unfollow all room:3', group='200')
        assert sent == ['B站取消关注处理结果：\n成功:\n- UP 1'], sent
        unfollow.assert_awaited_once_with('all', ['room:3'], 'group', '200')
        sent = await say('/bili refresh dynamic room:3')
        assert sent == ['B站信息刷新结果：\n失败:\n- room:3：超时'], sent
        refresh.assert_awaited_once_with('dynamic', ['room:3'])
        # 私聊：订阅者沿用原有判定（有频道号即按频道号）
        await say('/bili follow video 5', private=True, user='7')
        assert follow.await_args.args[2:] == ('group', 'private:7'), follow.await_args
        # list：默认、带类型、类型错误
        assert await say('/bili list') == ['订阅列表\n- UP 1']
        assert listing.await_args.args == ('group', '100', None), listing.await_args
        await say('/bili list video')
        assert listing.await_args.args == ('group', '100', 'video'), listing.await_args
        listing.side_effect = ValueError('kind')
        assert await say('/bili list xx') == [KIND_ERROR]
        # 未知子命令
        assert await say('/bili what') == ['未知子命令：请发送 /bili help 获取完整使用指南。']

        # 链接预览：规则两侧、解析不出、成功、异常
        assert await say('今天天气不错') == [] and not preview.called
        assert await say('/foo https://b23.tv/x') == [] and not preview.called
        assert await say('看看 https://b23.tv/abc') == []
        preview.assert_awaited_once_with('看看 https://b23.tv/abc')
        preview.return_value = object()
        assert await say('BV1xx411c7mD') == ['CARD']
        assert await say('https://www.bilibili.com/video/BV1') == ['CARD']
        preview.side_effect = RuntimeError('boom')
        assert await say('https://www.bilibili.com/video/BV2') == []
    print('ALL-OK')

run(main())
"""


class BiliDispatchTests(unittest.TestCase):
    def test_bili_command_and_link_preview_through_the_real_event_path(self):
        from tests.support.dispatch import run_scenarios

        result = run_scenarios(BILI_SCENARIOS)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-6000:])
        self.assertIn("ALL-OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
