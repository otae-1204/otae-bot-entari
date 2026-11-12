"""/wiki、/查wiki through the real Entari event path."""

from __future__ import annotations

import unittest

from tests.support.dispatch import run_scenarios

SCENARIOS = r'''
import io
import re
from unittest.mock import AsyncMock, patch
import httpx
from PIL import Image as PILImage

wiki = plugin('plugins.McWikiQuery.handlers')
SEARCH = '<html><body><a data-serp-pos="0" href="/w/Stone">Stone</a></body></html>'


def png(height):
    buffer = io.BytesIO()
    PILImage.new('RGB', (2, height)).save(buffer, format='PNG')
    return buffer.getvalue()


class Response:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request('GET', 'https://zh.minecraft.wiki/')
            raise httpx.HTTPStatusError('bad', request=request, response=httpx.Response(self.status_code, request=request))


def client(result, seen):
    class Client:
        def __init__(self, **kwargs):
            seen.append(kwargs)

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


def quoted(sent, text):
    # 每条回复都引用触发消息
    return re.fullmatch('<quote id="incoming-[0-9]+"/>' + re.escape(text), sent) is not None


async def main():
    seen = []
    with patch.object(wiki, 'ashared_ssl_context', AsyncMock(return_value=None)), \
         patch.object(wiki, 'system_proxy', return_value={'http': None}):
        with patch.object(wiki.httpx, 'AsyncClient', client(Response(SEARCH), seen)), \
             patch.object(wiki, 'screenshot_web_element', AsyncMock(return_value=png(10))) as shot:
            for text in ('/wiki 石头', '/查wiki 石头'):
                sent = await say(text)
                assert len(sent) == 2, sent
                assert quoted(sent[0], '正在生成 Wiki 页面截图，请稍候……'), sent
                assert re.fullmatch('<quote id="incoming-[0-9]+"/><img src="data:image/png;base64,[^"]+"/>', sent[1]), sent
            assert shot.await_args.args == ('https://zh.minecraft.wiki/w/Stone?variant=zh', '#content, .mw-body')
            assert '%E7%9F%B3%E5%A4%B4' in seen[-1], seen
            # 参数是单个 str：缺参数或多出一个词都匹配不上，不回复
            assert await say('/wiki') == []
            assert await say('/wiki a b') == []
        # 很高的截图切成多张图放在同一条消息里
        with patch.object(wiki.httpx, 'AsyncClient', client(Response(SEARCH), seen)), \
             patch.object(wiki, 'screenshot_web_element', AsyncMock(return_value=png(wiki.WIKI_IMAGE_CHUNK_HEIGHT + 1))):
            sent = await say('/wiki 石头')
            assert len(sent) == 2 and sent[1].count('<img ') == 2, sent
        with patch.object(wiki.httpx, 'AsyncClient', client(Response(SEARCH), seen)), \
             patch.object(wiki, 'screenshot_web_element', AsyncMock(side_effect=RuntimeError('no browser'))):
            sent = await say('/wiki 石头')
            assert len(sent) == 2 and quoted(sent[1], '页面截取失败，请稍后重试。'), sent
        cases = [
            (Response('<html></html>'), '未找到与「石头」相关的内容。'),
            (Response('', 503), 'Wiki 搜索请求异常：HTTP 503'),
            (httpx.ConnectTimeout('slow'), 'Wiki 检索超时，请稍后重试。'),
            (httpx.ConnectError('down'), 'Wiki 搜索网络异常，请确认网络连接或稍后再试。'),
            (IndexError('parse'), 'Wiki 结果解析失败，请稍后再试。'),
        ]
        for result, reply in cases:
            with patch.object(wiki.httpx, 'AsyncClient', client(result, seen)):
                sent = await say('/wiki 石头')
                assert len(sent) == 1 and quoted(sent[0], reply), (reply, sent)
    print('ALL-OK')

run(main())
'''


class McWikiDispatchTests(unittest.TestCase):
    def test_commands_through_the_real_event_path(self):
        result = run_scenarios(SCENARIOS)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-6000:])
        self.assertIn("ALL-OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
