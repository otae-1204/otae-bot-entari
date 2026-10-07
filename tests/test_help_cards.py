"""Help card framework: spec parsing, art gallery, staleness of the shipped images."""

from __future__ import annotations

import io
import json
import os
import random
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from otae_bot.help_images import cover_visibility, image_ratio  # noqa: E402
from otae_bot.infrastructure.rendering import help_cards, help_runtime  # noqa: E402

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

    def test_only_the_first_artwork_is_shipped(self):
        """Variants are rendered on demand now, so one page ships exactly one PNG."""
        gallery = help_cards.load_gallery()
        page = help_cards.parse_page(_page(art=["school", "apple"]))
        targets = help_cards.plan_targets([page], gallery, Path("help"))
        self.assertEqual([target.path for target in targets], [Path("help/demo.png")])
        self.assertTrue(targets[0].primary)
        self.assertEqual(targets[0].art.id, "school")

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


class _FixedRng:
    """Stand-in for ``random.Random`` that always yields one artwork."""

    def __init__(self, art):
        self._art = art

    def choice(self, sequence):
        return self._art


def _png(width: int = 40, height: int = 30) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class HelpArtworkRatioTests(unittest.TestCase):
    """The whole gallery is usable; the standee window is the only gate."""

    def setUp(self):
        self.gallery = help_cards.load_gallery()
        self.pages = {page.id: page for page in help_cards.load_pages()}

    def _visibility(self, page, kind: str) -> dict[str, float]:
        height = help_cards.page_height(page)
        ratio = (help_cards.backdrop_ratio if kind == "backdrop" else help_cards.window_ratio)(height)
        return {
            art_id: cover_visibility(image_ratio(str(art.path)), ratio)
            for art_id, art in self.gallery.artworks.items()
        }

    def test_candidates_are_sorted_by_backdrop_match(self):
        """Ordering is by backdrop fit so a send is reproducible; the caller draws uniformly."""
        page = self.pages["main"]
        visibility = self._visibility(page, "backdrop")
        candidates = help_cards.ratio_candidates(page, self.gallery)
        scores = [visibility[art.id] for art in candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_the_whole_gallery_is_usable_on_every_page(self):
        """The blurred backdrop never filters: every shipped artwork stays available."""
        everything = set(self.gallery.artworks)
        self.assertEqual(len(everything), 10)
        for page in self.pages.values():
            with self.subTest(page=page.id):
                chosen = {art.id for art in help_cards.ratio_candidates(page, self.gallery)}
                self.assertEqual(chosen, everything, f"{page.id} dropped {everything - chosen}")

    def test_portrait_and_landscape_art_are_both_usable(self):
        """Both orientations reach the pool, so a page is not locked to one cluster."""
        chosen = help_cards.ratio_candidates(self.pages["main"], self.gallery)
        ratios = [image_ratio(str(art.path)) for art in chosen]
        self.assertTrue(any(r > 1.0 for r in ratios), "no landscape art usable")
        self.assertTrue(any(r < 1.0 for r in ratios), "no portrait art usable")

    def test_window_floor_drops_art_that_would_hide_the_subject(self):
        """A narrow standee window is the one thing that removes artwork from the pool."""
        page = self.pages["main"]
        theme = replace(help_cards.HelpTheme(), window_width=100)
        candidates = help_cards.ratio_candidates(page, self.gallery, theme)
        self.assertTrue(candidates)
        self.assertLess(len(candidates), len(self.gallery.artworks), "floor dropped nothing")
        for art in candidates:
            window = cover_visibility(image_ratio(str(art.path)), help_cards.window_ratio(779, theme))
            self.assertGreaterEqual(window, help_cards.WINDOW_VISIBILITY_FLOOR, art.id)
            self.assertLess(image_ratio(str(art.path)), 1.0, f"{art.id} is landscape but won anyway")

    def test_shipped_pages_keep_the_subject_in_the_window(self):
        """Whatever a page draws, the standee window still shows the subject."""
        for page in self.pages.values():
            with self.subTest(page=page.id):
                window = help_cards.window_ratio(help_cards.page_height(page))
                for art in help_cards.ratio_candidates(page, self.gallery):
                    visible = cover_visibility(image_ratio(str(art.path)), window)
                    self.assertGreaterEqual(visible, help_cards.WINDOW_VISIBILITY_FLOOR, f"{page.id}/{art.id}")

    def test_a_page_too_tall_for_the_floor_still_returns_candidates(self):
        """The floor is a guard, not a hard filter: an extreme page must still send art."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # page_height() reads only the height, so a 1 px wide stand-in keeps the write cheap.
            Image.new("RGB", (1, 6000)).save(root / "demo.png")
            page = help_cards.parse_page(_page())
            with patch.object(help_cards, "HELP_IMAGE_DIR", root):
                candidates = help_cards.ratio_candidates(page, self.gallery)
        self.assertTrue(candidates, "no artwork clears the floor here, so fall back to all")

    def test_page_height_falls_back_to_the_theme_when_nothing_ships(self):
        with tempfile.TemporaryDirectory() as directory:
            page = help_cards.parse_page(_page())
            with patch.object(help_cards, "HELP_IMAGE_DIR", Path(directory)):
                theme = help_cards.HelpTheme()
                self.assertEqual(help_cards.page_height(page, theme),
                                 theme.min_card_height + help_cards.CARD_HEIGHT_INSET)


class HelpRuntimeCacheTests(unittest.IsolatedAsyncioTestCase):
    """Rendering happens once per (page, artwork); later calls reuse the cached file."""

    async def _render_with(self, page, art, theme):
        return help_cards.finalize_png(_png(), help_cards.spec_digest(page, art, theme))

    def _settings(self, directory: str):
        return patch.dict(os.environ, {help_runtime.CACHE_PATH_ENV: directory})

    async def test_render_is_reused_from_the_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            with self._settings(directory):
                with patch.object(help_runtime, "_render", new=AsyncMock(side_effect=self._render_with)) as render:
                    first = await help_runtime.cached_help_image("main", rng=random.Random(0))
                    second = await help_runtime.cached_help_image("main", rng=random.Random(0))
            self.assertIsNotNone(first)
            self.assertEqual(first, second)
            self.assertEqual(render.await_count, 1, "second call must come from the cache")
            self.assertEqual(first.parent, Path(directory))

    async def test_different_artwork_caches_separately(self):
        page = help_runtime.find_page("main")
        candidates = help_cards.ratio_candidates(page, help_cards.load_gallery())
        with tempfile.TemporaryDirectory() as directory:
            with self._settings(directory):
                with patch.object(help_runtime, "_render", new=AsyncMock(side_effect=self._render_with)) as render:
                    paths = []
                    for art in candidates:
                        rng = _FixedRng(art)
                        first = await help_runtime.cached_help_image("main", rng=rng)
                        self.assertEqual(first, await help_runtime.cached_help_image("main", rng=rng))
                        paths.append(first)
            self.assertEqual(render.await_count, len(candidates), "one render per artwork")
            self.assertEqual(len(set(paths)), len(candidates), "each artwork keeps its own file")

    async def test_stale_cache_is_rendered_again(self):
        with tempfile.TemporaryDirectory() as directory:
            with self._settings(directory):
                with patch.object(help_runtime, "_render", new=AsyncMock(side_effect=self._render_with)) as render:
                    page = help_runtime.find_page("main")
                    art = help_cards.ratio_candidates(page, help_cards.load_gallery())[0]
                    cached = Path(directory) / f"{page.id}.{art.id}.png"
                    cached.write_bytes(help_cards.finalize_png(_png(), "outdated-digest"))
                    result = await help_runtime.cached_help_image("main", rng=_FixedRng(art))
                    self.assertEqual(render.await_count, 1, "stale cache must be re-rendered")
            self.assertNotEqual(help_cards.read_digest(result), "outdated-digest")

    async def test_render_failure_falls_back_to_the_shipped_image(self):
        with tempfile.TemporaryDirectory() as directory:
            with self._settings(directory):
                with patch.object(help_runtime, "_render", new=AsyncMock(side_effect=RuntimeError("no browser"))):
                    result = await help_runtime.cached_help_image("main")
            self.assertEqual(result, help_cards.HELP_IMAGE_DIR / "main.png")

    async def test_unknown_page_returns_the_shipped_image_or_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            with self._settings(directory):
                with patch.object(help_runtime, "_render", new=AsyncMock(side_effect=RuntimeError("x"))):
                    self.assertEqual(await help_runtime.cached_help_image("main"),
                                     help_cards.HELP_IMAGE_DIR / "main.png")
                self.assertIsNone(await help_runtime.cached_help_image("nope"))

    async def test_cache_dir_honours_the_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            with self._settings(directory):
                self.assertEqual(help_runtime.cache_dir(), Path(directory))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(help_runtime.CACHE_PATH_ENV, None)
            self.assertEqual(help_runtime.cache_dir(),
                             help_runtime.PROJECT_ROOT / help_runtime.DEFAULT_CACHE_PATH)


if __name__ == "__main__":
    unittest.main()
