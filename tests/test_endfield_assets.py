from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_endfield_modules():
    pkg_name = "endfield_asset_test"
    if f"{pkg_name}.asset_urls" in sys.modules:
        return sys.modules[f"{pkg_name}.asset_urls"]
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(ROOT / "plugins/endfield")]
    sys.modules[pkg_name] = pkg
    _load_module(f"{pkg_name}.models", "plugins/endfield/models.py")
    return _load_module(f"{pkg_name}.asset_urls", "plugins/endfield/asset_urls.py")


def _load_draw():
    pkg_name = "endfield_asset_test"
    _load_endfield_modules()
    if f"{pkg_name}.draw" in sys.modules:
        return sys.modules[f"{pkg_name}.draw"]
    try:
        _load_module(f"{pkg_name}.client", "plugins/endfield/client.py")
        return _load_module(f"{pkg_name}.draw", "plugins/endfield/draw.py")
    except Exception:
        sys.modules.pop(f"{pkg_name}.draw", None)
        raise


asset_urls = _load_endfield_modules()
models = sys.modules["endfield_asset_test.models"]
OperatorView = models.OperatorView
SkillView = models.SkillView
EffectView = models.EffectView
WARFARIN_STATIC_BASE = asset_urls.WARFARIN_STATIC_BASE
AKEDATA_SPRITES_BASE = asset_urls.AKEDATA_SPRITES_BASE


def _operator(**overrides) -> OperatorView:
    values = dict(
        name="提弗洛斯",
        slug="干员/提弗洛斯",
        operator_id="chr_0034_typhoea",
        english_name="Typhoeus",
    )
    values.update(overrides)
    return OperatorView(**values)


class EndfieldAssetUrlTests(unittest.TestCase):
    def test_operator_icon_urls_include_warfarin_and_akedata(self):
        urls = asset_urls.operator_icon_urls("chr_0034_typhoea", "https://assets.fz.wiki/missing.png")
        self.assertEqual(urls[0], "https://assets.fz.wiki/missing.png")
        self.assertIn(f"{WARFARIN_STATIC_BASE}/charicon/icon_chr_0034_typhoea.webp", urls)
        self.assertIn(f"{AKEDATA_SPRITES_BASE}/charremoteicon/icon_chr_0034_typhoea.png", urls)

    def test_portrait_urls_prefer_primary_then_warfarin(self):
        urls = asset_urls.operator_portrait_urls("chr_0034_typhoea", "https://assets.fz.wiki/illust.png")
        self.assertEqual(
            urls,
            (
                "https://assets.fz.wiki/illust.png",
                f"{WARFARIN_STATIC_BASE}/characterportrait/chr_0034_typhoea.webp",
            ),
        )

    def test_skill_icon_urls_keep_http_and_expand_ids(self):
        urls = asset_urls.skill_icon_urls("https://assets.fz.wiki/glyph.png", "sk_typhoea_s1")
        self.assertEqual(urls[0], "https://assets.fz.wiki/glyph.png")
        self.assertIn(f"{WARFARIN_STATIC_BASE}/skillicon/sk_typhoea_s1.webp", urls)
        self.assertIn(f"{AKEDATA_SPRITES_BASE}/skillicon/sk_typhoea_s1.png", urls)

    def test_item_icon_urls_cover_weapon_and_equipment(self):
        urls = asset_urls.item_icon_urls("wpn_claym_0001", "")
        self.assertIn(f"{WARFARIN_STATIC_BASE}/itemicon/wpn_claym_0001.webp", urls)
        self.assertIn(f"{AKEDATA_SPRITES_BASE}/itemiconbig/wpn_claym_0001.png", urls)

    def test_unique_urls_drops_blank_and_duplicates(self):
        self.assertEqual(asset_urls.unique_urls("", "a", "a", "b"), ("a", "b"))


class EndfieldAssetDonorTests(unittest.TestCase):
    def test_operator_needs_donor_when_fz_urls_present(self):
        view = _operator(
            icon_url="https://assets.fz.wiki/icon.png",
            portrait_url="https://assets.fz.wiki/illust.png",
            round_icon_url="https://assets.fz.wiki/round.png",
            skills=[SkillView("s1", "追猎荒原", icon_id="https://assets.fz.wiki/s1.png")],
            talents=[EffectView("t1", "猎物清点", "desc", "天赋", icon_url="https://assets.fz.wiki/t.png")],
        )
        self.assertTrue(asset_urls.operator_needs_asset_donor(view))

    def test_warfarin_operator_does_not_need_donor(self):
        view = _operator(
            icon_url=f"{WARFARIN_STATIC_BASE}/charicon/icon_chr_0034_typhoea.webp",
            round_icon_url=f"{WARFARIN_STATIC_BASE}/charroundicon/icon_round_chr_0034_typhoea.webp",
            portrait_url=f"{WARFARIN_STATIC_BASE}/characterportrait/chr_0034_typhoea.webp",
            skills=[SkillView("s1", "追猎荒原", icon_id="sk_s1")],
            talents=[EffectView("t1", "猎物清点", "desc", "天赋", icon_url=f"{WARFARIN_STATIC_BASE}/skillicon/t.webp")],
        )
        self.assertFalse(asset_urls.operator_needs_asset_donor(view))

    def test_apply_operator_asset_donor_fills_ids_and_skill_fallbacks(self):
        primary = _operator(
            operator_id="",
            icon_url="https://assets.fz.wiki/missing.png",
            portrait_url="https://assets.fz.wiki/illust.png",
            skills=[SkillView("fz_skill_1", "追猎荒原", icon_id="https://assets.fz.wiki/s1.png", category="普攻")],
            talents=[EffectView("t1", "猎物清点", "desc", "天赋", icon_url="https://assets.fz.wiki/t.png")],
        )
        donor = _operator(
            icon_url=f"{WARFARIN_STATIC_BASE}/charicon/icon_chr_0034_typhoea.webp",
            round_icon_url=f"{WARFARIN_STATIC_BASE}/charroundicon/icon_round_chr_0034_typhoea.webp",
            portrait_url=f"{WARFARIN_STATIC_BASE}/characterportrait/chr_0034_typhoea.webp",
            skills=[SkillView("sk_s1", "追猎荒原", icon_id="sk_typhoea_s1", category="普攻")],
            talents=[
                EffectView(
                    "t1",
                    "猎物清点",
                    "desc",
                    "天赋",
                    icon_url=f"{WARFARIN_STATIC_BASE}/skillicon/talent.webp",
                )
            ],
        )
        asset_urls.apply_operator_asset_donor(primary, donor)
        self.assertEqual(primary.operator_id, "chr_0034_typhoea")
        self.assertEqual(primary.icon_url, "https://assets.fz.wiki/missing.png")
        self.assertEqual(primary.round_icon_url, donor.round_icon_url)
        self.assertEqual(primary.skills[0].icon_fallbacks, ("sk_typhoea_s1",))
        self.assertEqual(
            primary.talents[0].icon_fallbacks,
            (f"{WARFARIN_STATIC_BASE}/skillicon/talent.webp",),
        )


class EndfieldAssetFetchTests(unittest.TestCase):
    def test_prepare_assets_uses_24mb_limit(self):
        source = (ROOT / "plugins/endfield/draw.py").read_text(encoding="utf-8")
        self.assertIn("ASSET_FETCH_MAX_BYTES = 24 * 1024 * 1024", source)
        account = (ROOT / "plugins/endfield/account_draw.py").read_text(encoding="utf-8")
        self.assertIn("max_bytes=24 * 1024 * 1024", account)

    def test_resolve_asset_groups_falls_back_after_primary_miss(self):
        try:
            draw = _load_draw()
        except ModuleNotFoundError as exc:
            self.skipTest(f"draw 依赖缺失: {exc}")
        from utils.http_client import HttpResource

        async def fake_fetch(urls, **_kwargs):
            resolved = {}
            for url in urls:
                if "warfarin" in url:
                    resolved[url] = HttpResource(b"ok", "image/webp", 200, url)
                else:
                    resolved[url] = None
            return resolved, {url: "http_status" for url, item in resolved.items() if item is None}

        with patch.object(draw, "fetch_many_resilient", side_effect=fake_fetch):
            _assets, mapped, sources = asyncio.run(
                draw._resolve_asset_groups(
                    {
                        "icon": (
                            "https://assets.fz.wiki/missing.png",
                            f"{WARFARIN_STATIC_BASE}/charicon/icon_chr_0034_typhoea.webp",
                        )
                    },
                    inline=True,
                )
            )
        self.assertIn("warfarin", sources["icon"])
        self.assertTrue(mapped["icon"].startswith("data:image/webp;base64,"))


if __name__ == "__main__":
    unittest.main()
