"""Help card framework: spec parsing, art gallery, staleness of the shipped images."""

from __future__ import annotations

import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from otae_bot.help_images import help_image_choices, pick_help_image, variant_dir  # noqa: E402
from otae_bot.infrastructure.rendering import help_cards  # noqa: E402

RERENDER = "run: python scripts/render_help_cards.py --write"


def _page(**overrides) -> dict:
    raw = {
        "id": "demo",
        "file": "demo.png",
        "title": "演示 <帮助>",
        "art": ["school"],
        "columns": [[{"heading": "分区", "items": ["/demo <参数>  说明 & 更多"]}]],
    }
    raw.update(overrides)
    return raw


class HelpSpecTests(unittest.TestCase):
    def test_items_accept_two_space_strings_and_objects(self):
        self.assertEqual(help_cards.parse_item("/ef 签到 [账号]  森空岛签到"),
                         help_cards.HelpItem("/ef 签到 [账号]", "森空岛签到"))
        self.assertEqual(help_cards.parse_item("只有说明的一行"), help_cards.HelpItem("只有说明的一行"))
        self.assertEqual(
            help_cards.parse_item({"command": "/ef 绑定", "description": "绑定", "badge": "仅私聊"}),
            help_cards.HelpItem("/ef 绑定", "绑定", "仅私聊"),
        )
        self.assertEqual(help_cards.parse_item({"cmd": "/x", "desc": "叉"}), help_cards.HelpItem("/x", "叉"))
        for bad in ({"desc": "没有命令"}, "", 3):
            with self.assertRaises(help_cards.HelpSpecError):
                help_cards.parse_item(bad)

    def test_shipped_specs_parse_and_every_page_has_known_art(self):
        pages = help_cards.load_pages()
        gallery = help_cards.load_gallery()
        targets = help_cards.plan_targets(pages, gallery)
        self.assertEqual({target.page.id for target in targets if target.primary}, {page.id for page in pages})
        for art in gallery.artworks.values():
            self.assertTrue(art.path.is_file(), art.path)

    def test_every_shipped_help_image_is_generated_from_a_spec(self):
        files = {page.file for page in help_cards.load_pages()}
        shipped = {path.name for path in help_cards.HELP_IMAGE_DIR.glob("*.png")}
        self.assertEqual(sorted(shipped - files), [], "hand-made help images bypass the renderer")

    def test_shipped_images_are_up_to_date(self):
        targets = help_cards.plan_targets(help_cards.load_pages(), help_cards.load_gallery())
        stale = [str(target.path.relative_to(ROOT)) for target in help_cards.stale_targets(targets)]
        self.assertEqual(stale, [], f"help images are stale or missing; {RERENDER}")
        orphans = [str(path.relative_to(ROOT)) for path in help_cards.orphan_variants(targets)]
        self.assertEqual(orphans, [], f"variant images no page uses; {RERENDER}")

    def test_shipped_images_use_the_theme_width(self):
        width = help_cards.HelpTheme().page_width
        for target in help_cards.plan_targets(help_cards.load_pages(), help_cards.load_gallery()):
            with Image.open(target.path) as image:
                self.assertEqual(image.width, width, target.path)

    def test_unknown_art_is_reported_with_the_page(self):
        gallery = help_cards.load_gallery()
        page = help_cards.parse_page(_page(art=["missing-art"]))
        with self.assertRaisesRegex(help_cards.HelpSpecError, "demo.*missing-art"):
            gallery.resolve(page)

    def test_page_without_art_uses_gallery_default(self):
        gallery = help_cards.load_gallery()
        page = help_cards.parse_page(_page(art=None))
        self.assertEqual([art.id for art in gallery.resolve(page)], list(gallery.default))

    def test_extra_art_becomes_a_variant(self):
        gallery = help_cards.load_gallery()
        page = help_cards.parse_page(_page(art=["school", "apple"]))
        primary, variant = help_cards.plan_targets([page], gallery, Path("help"))
        self.assertEqual(primary.path, Path("help/demo.png"))
        self.assertEqual(variant.path, Path("help/variants/demo/apple.png"))
        self.assertFalse(variant.primary)

    def test_digest_tracks_text_and_art_but_not_the_variant_list(self):
        gallery = help_cards.load_gallery()
        school = gallery.artworks["school"]
        base = help_cards.spec_digest(help_cards.parse_page(_page()), school)
        more_art = help_cards.spec_digest(help_cards.parse_page(_page(art=["school", "apple"])), school)
        edited = help_cards.spec_digest(help_cards.parse_page(_page(title="改过的标题")), school)
        other_art = help_cards.spec_digest(help_cards.parse_page(_page()), gallery.artworks["apple"])
        self.assertEqual(base, more_art)
        self.assertNotEqual(base, edited)
        self.assertNotEqual(base, other_art)

    def test_html_escapes_text_and_never_reaches_the_network(self):
        gallery = help_cards.load_gallery()
        page = help_cards.parse_page(_page(
            subtitle='<script>alert(1)</script>',
            columns=[[{"heading": "A&B", "access": "需绑定", "notes": ["<注>"],
                       "items": [{"cmd": "/x <y>", "desc": "说明", "badge": "仅私聊"}]}]],
        ))
        document = help_cards.render_html(page, gallery.artworks["okhands"])
        self.assertNotIn("<script>", document)
        self.assertIn("&lt;script&gt;", document)
        self.assertIn("/x &lt;y&gt;", document)
        self.assertIn('class="badge">仅私聊<', document)
        self.assertIn("object-position:50.0% 20.0%", document)
        self.assertNotIn("http://", document)
        self.assertNotIn("https://", document)

    def test_layout_problems_are_reported(self):
        theme = help_cards.HelpTheme()
        good = {"width": theme.page_width, "height": 900, "escaped": [], "titleClear": True,
                "footClear": True, "fontsLoaded": True, "brokenImages": [], "externalRequests": []}
        self.assertEqual(help_cards.layout_problems(good, theme), [])
        bad = dict(good, escaped=["/ef 很长"], footClear=False, fontsLoaded=False,
                   height=theme.max_height + 1, brokenImages=["okhands"])
        self.assertEqual(len(help_cards.layout_problems(bad, theme)), 5)

    def test_gallery_rejects_bad_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.png").write_bytes(b"x")
            path = root / "gallery.json"
            for art, message in (
                ([{"id": "a", "file": "missing.png"}], "not found"),
                ([{"id": "a b", "file": "a.png"}], "letters"),
                ([{"id": "a", "file": "a.png"}, {"id": "a", "file": "a.png"}], "duplicate"),
                ([{"id": "a", "file": "a.png", "focus": [2, 0]}], "fractions"),
            ):
                path.write_text(json.dumps({"art": art}), encoding="utf-8")
                with self.subTest(message=message), self.assertRaisesRegex(help_cards.HelpSpecError, message):
                    help_cards.load_gallery(path)


class HelpImagePickTests(unittest.TestCase):
    def test_primary_only_and_missing_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "topic.png"
            self.assertIsNone(pick_help_image(primary))
            primary.write_bytes(b"png")
            self.assertEqual(pick_help_image(primary), primary)

    def test_variants_are_picked_randomly(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "topic.png"
            primary.write_bytes(b"png")
            variant_dir(primary).mkdir(parents=True)
            for name in ("b.png", "a.png", "notes.txt"):
                (variant_dir(primary) / name).write_bytes(b"x")
            choices = help_image_choices(primary)
            self.assertEqual([path.name for path in choices], ["topic.png", "a.png", "b.png"])
            picked = {pick_help_image(primary, random.Random(seed)).name for seed in range(40)}
            self.assertEqual(picked, {"topic.png", "a.png", "b.png"})


if __name__ == "__main__":
    unittest.main()
