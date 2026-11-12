"""peek 的五个指令 through the real Entari event path."""

from __future__ import annotations

import unittest

from tests.support.dispatch import run_scenarios

SCENARIOS = r'''
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx

peek = plugin('plugins.peek.handlers')
STATE = Path('data/peek/state.json')


class Response:
    def __init__(self, status=200, content=b'png-data'):
        self.status_code = status
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request('GET', 'http://pc/')
            raise httpx.HTTPStatusError('bad', request=request, response=httpx.Response(self.status_code, request=request))


def client(result, seen):
    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            seen.append(url)
            if isinstance(result, BaseException):
                raise result
            return result
    return Client


def one(sent):
    assert len(sent) == 1, sent
    return sent[0]


async def main():
    seen = []
    ok = client(Response(), seen)
    with patch.object(peek, 'owner', ['root']), \
         patch.object(peek, 'ashared_ssl_context', AsyncMock(return_value=None)), \
         patch.object(peek.httpx, 'AsyncClient', ok):
        # 不在白名单的群：/peek 不回复
        assert await say('/peek') == []
        # 只有 SuperUser 能改设置；参数是必填的，缺了就匹配不上
        for text in ('/add_whitelist 100', '/添加白名单 100', '/add_peek pc http://pc', '/添加窥视 pc http://pc',
                     '/del_peek pc', '/删除窥视 pc', '/set_default_peek pc', '/设置默认窥视 pc'):
            assert one(await say(text)) == '权限不足，无法执行该操作。', text
        assert await say('/add_whitelist', user='root') == []
        assert one(await say('/add_whitelist 100', user='root')) == '群号 100 已加入白名单。'
        assert one(await say('/add_whitelist_group 100', user='root')) == '群号 100 已在白名单列表中。'
        assert one(await say('/添加白名单 200', user='root')) == '群号 200 已加入白名单。'
        assert json.loads(STATE.read_text())['white_list'] == ['100', '200']
        # 白名单群里还没有目标
        assert one(await say('/peek')) == '当前群暂未配置默认 peek 地址。'
        assert one(await say('/peek pc')) == '未找到名为「pc」的监控目标。'
        # 添加目标：地址校验、连通性检查、第一个成为默认
        assert await say('/add_peek pc', user='root') == []
        assert one(await say('/add_peek pc ftp://pc', user='root')) == '请输入有效的 peek 地址。'
        with patch.object(peek.httpx, 'AsyncClient', client(httpx.ConnectError('down'), seen)):
            assert one(await say('/add_peek pc http://pc', user='root')) == '无法连通目标地址：http://pc'
        assert one(await say('/add_peek pc http://pc/', user='root')) == '已成功添加 pc: http://pc/\n已同步设为默认 peek 地址。'
        assert seen[-1] == 'http://pc/screenshot', seen
        assert one(await say('/添加窥视 pc http://other', user='root')) == '目标昵称「pc」已存在。'
        assert one(await say('/添加窥视 laptop https://laptop', user='root')) == '已成功添加 laptop: https://laptop'
        # 截图：默认目标与指定目标，文字 + 图片一条消息
        sent = one(await say('/peek'))
        assert re.fullmatch('Screenshot for pc<img src="data:image/png;base64,[^"]+"/>', sent), sent
        sent = one(await say('/窥视 laptop'))
        assert sent.startswith('Screenshot for laptop<img ') and seen[-1] == 'https://laptop/screenshot', sent
        for result in (httpx.ConnectError('down'), Response(503), Response(204)):
            with patch.object(peek.httpx, 'AsyncClient', client(result, seen)):
                assert one(await say('/peek')) == '获取屏幕截图失败，目标服务可能未启动。', result
        # 默认目标切换
        assert one(await say('/set_default_peek nope', user='root')) == '目标昵称「nope」不存在。'
        assert one(await say('/设置默认窥视 laptop', user='root')) == '已将默认 peek 目标切换为 laptop。'
        assert one(await say('/peek')).startswith('Screenshot for laptop')
        # 删除
        assert one(await say('/del_peek pc', user='root', group='200')) == '群组 200 尚未配置任何目标。'
        assert one(await say('/del_peek nope', user='root')) == '目标昵称「nope」不存在。'
        assert one(await say('/删除窥视 pc', user='root')) == '已移除 pc。'
        assert one(await say('/del_peek laptop', user='root')) == '已移除 laptop（包含默认地址）。'
        assert one(await say('/peek')) == '当前群暂未配置默认 peek 地址。'
        assert json.loads(STATE.read_text())['100'] == {'peek_list': {}}
        # 存下的地址格式不对
        peek.state.data['100'] = {'peek_list': {'bad': 'pc'}, 'def_peek_path': {'nickname': 'bad', 'peek_http_path': 'pc'}}
        assert one(await say('/peek')) == 'peek 地址格式不正确，须以 http:// 或 https:// 开头。'
        peek.state.data['100']['def_peek_path'] = {'nickname': 'bad'}
        assert one(await say('/peek')) == '当前群未设置默认 peek 地址，请先添加可用地址。'
    print('ALL-OK')

run(main())
'''


class PeekDispatchTests(unittest.TestCase):
    def test_commands_through_the_real_event_path(self):
        result = run_scenarios(SCENARIOS)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-6000:])
        self.assertIn("ALL-OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
