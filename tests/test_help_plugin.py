"""Keep the help topic map honest about the images that actually ship.

``TOPIC_MAP`` used to advertise ``mcping``, ``online`` and ``broadcast``, but no
such PNG was ever committed, so those branches could never resolve and silently
degraded to the generic topic list.  These tests fail loudly when a topic is
registered without its image (or when an image is orphaned).
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

from tests.support.dispatch import run_scenarios

ROOT = Path(__file__).resolve().parents[1]
HELP_IMAGE_DIR = ROOT / "assets/image/help"


def _load_help_handlers():
    """Import help_plugin.handlers without pulling in the Entari plugin scope."""
    module_name = "help_plugin_handlers_for_test"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "plugins/help_plugin/handlers.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class HelpTopicMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handlers = _load_help_handlers()
        cls.topics = cls.handlers.TOPIC_MAP
        cls.present = {path.stem for path in HELP_IMAGE_DIR.glob("*.png")}

    def test_every_topic_target_has_an_image(self):
        missing = sorted({name for name in self.topics.values() if name not in self.present})
        self.assertEqual(
            missing, [], f"help topics point at images that do not ship: {missing}"
        )

    def test_resolve_image_works_for_every_alias(self):
        for alias in self.topics:
            resolved = self.handlers._resolve_image(alias)
            self.assertIsNotNone(resolved, f"/help {alias} does not resolve to an image")
            self.assertTrue(resolved.exists(), f"/help {alias} resolved to a missing file")

    def test_main_and_endfield_topics_resolve(self):
        self.assertEqual(self.topics["main"], "main")
        self.assertEqual(self.topics["ef"], "endfield")
        self.assertEqual(self.topics["终末地"], "endfield")

    def test_arknights_topics_resolve_to_the_ak_card(self):
        for alias in ("ak", "明日方舟", "arknights", "森空岛"):
            self.assertEqual(self.topics[alias], "ak", alias)
        # TEXT_TOPICS is checked before TOPIC_MAP; leaving "ak" there would
        # silently shadow the card and answer with plain text again.
        self.assertNotIn("ak", self.handlers.TEXT_TOPICS)

    def test_available_topics_are_either_text_or_image(self):
        """`/help list` may advertise text-only topics, but never a broken image topic."""
        for topic in self.handlers._available_topics():
            if topic in self.handlers.TEXT_TOPICS:
                continue  # answered with text, no image expected
            self.assertIsNotNone(
                self.handlers._resolve_image(topic),
                f"advertised topic {topic} is neither a text topic nor a resolvable image",
            )


HELP_SCENARIOS = r"""
from pathlib import Path
from unittest.mock import AsyncMock, patch

help_plugin = plugin('plugins.help_plugin.handlers')
png = Path('help-test.png').resolve()
png.write_bytes(b'\x89PNG\r\n\x1a\nfake')
IMAGE = ['<img src="' + png.as_uri() + '"/>']


async def main():
    with patch.object(help_plugin, 'cached_help_image', AsyncMock(return_value=png)) as cached:
        # 无主题发主帮助图，图片主题按 TOPIC_MAP 渲染；四个指令名都能用
        assert await say('/help') == IMAGE and cached.await_args.args == ('main',)
        assert await say('/Help EF') == IMAGE and cached.await_args.args == ('endfield',)
        assert await say('/帮助 ak') == IMAGE and cached.await_args.args == ('ak',)
        count = cached.await_count
        # 文字主题原样按字符串发送（与以前一样经 Satori XML 解析）
        sent = await say('/h q')
        assert len(sent) == 1 and sent[0].startswith('HYW 搜索问答') and '</问题>' in sent[0], sent
        assert (await say('/help grok'))[0].startswith('Grok Bot 问答')
        assert (await say('/help 功能'))[0].startswith('本群插件开关')
        assert (await say('/help 更新日志'))[0].startswith('更新日志查询')
        assert (await say('/help 签到'))[0].startswith('签到：')
        # 列表与未知主题都回主题目录，只发一条
        for text in ('/help list', '/help 列表', '/help nope'):
            sent = await say(text)
            assert len(sent) == 1 and sent[0].startswith('可用帮助主题：\n  /help ak'), (text, sent)
        assert cached.await_count == count
    with patch.object(help_plugin, 'cached_help_image', AsyncMock(return_value=None)):
        sent = await say('/help')
        assert len(sent) == 1 and sent[0].startswith('可用帮助主题：'), sent
    with patch.object(help_plugin, '_available_topics', return_value=[]):
        empty = ['暂无可用的帮助图，请联系管理员检查 assets/image/help/ 目录。']
        assert await say('/help list') == empty
        assert await say('/help nope') == empty
    print('ALL-OK')

run(main())
"""


class HelpDispatchTests(unittest.TestCase):
    def test_help_command_through_the_real_event_path(self):
        result = run_scenarios(HELP_SCENARIOS)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-6000:])
        self.assertIn("ALL-OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
