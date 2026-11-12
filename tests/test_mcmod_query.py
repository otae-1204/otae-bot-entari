"""/mod、/模组、/资料 through the real Entari event path."""

from __future__ import annotations

import unittest

from tests.support.dispatch import run_scenarios

SCENARIOS = r'''
from unittest.mock import AsyncMock, patch
import httpx

mcmod = plugin('plugins.McModQuery.handlers')
HTML = ('<div class="head"><a href="https://www.mcmod.cn/class/1.html">暮色<em>森林</em></a></div>'
        '<div class="head"><a href="https://www.mcmod.cn/class/2.html">A&amp;B</a></div>'
        '<div class="head"><a href="https://www.mcmod.cn/item/3.html">物品</a></div>')
MOD = 'MC 百科相关模组结果如下（仅展示前 2 项）：\n暮色森林：\nhttps://www.mcmod.cn/class/1.html\nA&amp;B：\nhttps://www.mcmod.cn/class/2.html\n'


async def main():
    with patch.object(mcmod, '_fetch_search_html', AsyncMock(return_value=HTML)) as fetch:
        # 结果是纯文本（标题里的 & 被转义，而不是当 Satori XML 解析）
        for text in ('/mod 暮色', '/模组 暮色'):
            assert await say(text) == [MOD], text
            assert fetch.await_args.args == ('暮色',) and fetch.await_args.kwargs == {}
        sent = await say('/资料 铁锭')
        assert sent == [MOD.replace('模组', '资料')], sent
        assert fetch.await_args.args == ('铁锭',)
        assert fetch.await_args.kwargs == {'filter_args': '&filter=3&mold=0'}
        # 参数是单个 str：缺参数或多出一个词都匹配不上，不回复也不查询
        count = fetch.await_count
        for text in ('/mod', '/模组 暮色 1', '/资料'):
            assert await say(text) == [], text
        assert fetch.await_count == count
    with patch.object(mcmod, '_fetch_search_html', AsyncMock(return_value='<html></html>')):
        assert await say('/mod x') == ['未找到相关搜索结果，建议更换关键词重试。']
    with patch.object(mcmod, '_fetch_search_html', AsyncMock(side_effect=httpx.ConnectTimeout('t'))):
        assert await say('/mod x') == ['MC 百科搜索请求超时，请稍后再试。']
        assert await say('/资料 x') == ['MC 百科搜索请求超时，请稍后再试。']
    with patch.object(mcmod, '_fetch_search_html', AsyncMock(side_effect=httpx.ConnectError('t'))):
        assert await say('/mod x') == ['MC 百科搜索请求失败，请稍后重试。']
        assert await say('/资料 x') == ['MC 百科搜索请求失败，请稍后重试。']
    print('ALL-OK')

run(main())
'''


class McModDispatchTests(unittest.TestCase):
    def test_commands_through_the_real_event_path(self):
        result = run_scenarios(SCENARIOS)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-6000:])
        self.assertIn("ALL-OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
