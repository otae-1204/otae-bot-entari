"""Preview-only tests; no plugin or account imports."""

import ast
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("tibo_ui_preview", HERE / "render.py")
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class PreviewTests(unittest.TestCase):
    def test_all_surfaces_and_state_variants_exist(self):
        pages = preview.pages()
        self.assertEqual(len(pages), 19)
        for suffix in (
            "overview",
            "feed",
            "status",
            "recent",
            "history",
            "subscription",
            "notification",
            "help",
            "empty",
            "error",
            "subscribed",
            "unsubscribed",
            "permission",
            "invalid",
        ):
            self.assertTrue(any(key.endswith("-" + suffix) for key in pages))
        for state in preview.STATUS:
            self.assertIn(preview.STATUS[state][0], preview.status(state))

    def test_every_document_is_marked_demo(self):
        for title, eyebrow, command, body in preview.pages().values():
            document = preview.document(
                title, preview.card(title, eyebrow, command, body)
            )
            self.assertIn(preview.DISCLAIMER, document)
            self.assertIn("演示数据 / 非实时", document)
            self.assertIn('lang="zh-CN"', document)
            self.assertNotIn("<script", document)

    def test_confirmed_marks_all_four_steps(self):
        self.assertEqual(preview.status("confirmed").count('class="step active"'), 4)

    def test_no_signal_does_not_fabricate_evidence(self):
        html = preview.status("unconfirmed")
        self.assertNotIn("<blockquote>", html)
        self.assertNotIn("16:30–17:30", html)
        self.assertNotIn('class="step active"', html)

    def test_preview_imports_no_bot_or_credentials(self):
        tree = ast.parse((HERE / "render.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for name in names:
                self.assertFalse(
                    name.startswith(("plugins", "otae_bot", "arclet", "sqlite3")), name
                )

    def test_html_build_runs_without_browser_and_produces_gallery(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            manifest = preview.build(output)
            self.assertEqual(len(manifest), 19)
            self.assertEqual(len(list(output.glob("*.html"))), 20)
            self.assertEqual(
                json.loads((output / "manifest.json").read_text(encoding="utf-8")),
                manifest,
            )
            self.assertTrue((output / "fonts/MiSans-Regular.ttf").is_file())
            self.assertIn("19 张", (output / "index.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
