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

    def test_removed_dead_topics_stay_removed(self):
        """These never had assets; re-adding the alias without the image regresses."""
        for alias in ("mcping", "ping", "p", "online", "ol", "broadcast", "bc"):
            if alias in self.topics:
                self.assertIn(
                    self.topics[alias],
                    self.present,
                    f"/help {alias} is registered but its image is missing",
                )

    def test_main_and_endfield_topics_resolve(self):
        self.assertEqual(self.topics["main"], "main")
        self.assertEqual(self.topics["ef"], "endfield")
        self.assertEqual(self.topics["终末地"], "endfield")

    def test_available_topics_are_either_text_or_image(self):
        """`/help list` may advertise text-only topics, but never a broken image topic."""
        for topic in self.handlers._available_topics():
            if topic in self.handlers.TEXT_TOPICS:
                continue  # answered with text, no image expected
            self.assertIsNotNone(
                self.handlers._resolve_image(topic),
                f"advertised topic {topic} is neither a text topic nor a resolvable image",
            )


if __name__ == "__main__":
    unittest.main()
