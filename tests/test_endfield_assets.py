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
    if "." in name:
        importlib.import_module(name.rpartition(".")[0])
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_endfield_modules():
    pkg_name = "endfield_asset_test"
    if f"{pkg_name}.providers.assets" in sys.modules:
        return sys.modules[f"{pkg_name}.providers.assets"]
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(ROOT / "plugins/endfield")]
    sys.modules[pkg_name] = pkg
    _load_module(f"{pkg_name}.catalog.models", "plugins/endfield/catalog/models.py")
    return _load_module(f"{pkg_name}.providers.assets", "plugins/endfield/providers/assets.py")


def _load_draw():
    pkg_name = "endfield_asset_test"
    _load_endfield_modules()
    if f"{pkg_name}.rendering.cards" in sys.modules:
        return sys.modules[f"{pkg_name}.rendering.cards"]
    try:
        _load_module(f"{pkg_name}.providers.warfarin", "plugins/endfield/providers/warfarin.py")
        return _load_module(f"{pkg_name}.rendering.cards", "plugins/endfield/rendering/cards.py")
    except Exception:
        sys.modules.pop(f"{pkg_name}.rendering.cards", None)
        raise


asset_urls = _load_endfield_modules()
models = sys.modules["endfield_asset_test.catalog.models"]
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
    def test_operator_icon_urls_akedata_only(self):
        urls = asset_urls.operator_icon_urls("chr_0034_typhoea", "https://assets.fz.wiki/missing.png")
        self.assertEqual(
            urls,
            (
                f"{AKEDATA_SPRITES_BASE}/charremoteicon/icon_chr_0034_typhoea.png",
                f"{AKEDATA_SPRITES_BASE}/charicon/icon_chr_0034_typhoea.png",
            ),
        )

    def test_portrait_urls_akedata_only(self):
        urls = asset_urls.operator_portrait_urls("chr_0034_typhoea", "https://assets.fz.wiki/illust.png")
        self.assertEqual(urls, (f"{AKEDATA_SPRITES_BASE}/characterportrait/chr_0034_typhoea.png",))

    def test_skill_icon_urls_akedata_only(self):
        urls = asset_urls.skill_icon_urls("https://assets.fz.wiki/glyph.png", "icon_skill_typhoea_01")
        self.assertEqual(urls, (f"{AKEDATA_SPRITES_BASE}/skillicon/icon_skill_typhoea_01.png",))

    def test_skill_icon_urls_skip_synthetic_fz_ids(self):
        urls = asset_urls.skill_icon_urls("https://assets.fz.wiki/glyph.png", "fz_skill_2")
        self.assertEqual(urls, ())

    def test_skill_icon_key_from_warfarin_url(self):
        self.assertEqual(
            asset_urls.skill_icon_key(f"{WARFARIN_STATIC_BASE}/skillicon/icon_talent_typhoea_01.webp"),
            "icon_talent_typhoea_01",
        )

    def test_item_icon_urls_akedata_only(self):
        urls = asset_urls.item_icon_urls("wpn_claym_0001", "https://assets.fz.wiki/item.png")
        self.assertEqual(
            urls,
            (
                f"{AKEDATA_SPRITES_BASE}/itemiconbig/wpn_claym_0001.png",
                f"{AKEDATA_SPRITES_BASE}/itemicon/wpn_claym_0001.png",
            ),
        )

    def test_static_sprite_url_rewrites_warfarin(self):
        self.assertEqual(
            asset_urls.static_sprite_url(f"{WARFARIN_STATIC_BASE}/termicon/icon_term_ba_airborne.webp"),
            f"{AKEDATA_SPRITES_BASE}/termicon/icon_term_ba_airborne.png",
        )

    def test_unique_urls_drops_blank_and_duplicates(self):
        self.assertEqual(asset_urls.unique_urls("", "a", "a", "b"), ("a", "b"))

    def test_profession_and_element_icons_akedata_only(self):
        self.assertEqual(
            asset_urls.profession_icon_urls("突击", "https://assets.fz.wiki/job.png"),
            (f"{AKEDATA_SPRITES_BASE}/charprofessionicon/icon_profession_8_s.png",),
        )
        self.assertEqual(
            asset_urls.element_icon_urls("自然", "https://assets.fz.wiki/elem.png"),
            (f"{AKEDATA_SPRITES_BASE}/elementicon/icon_charattrtype_nature.png",),
        )
        self.assertEqual(asset_urls.weapon_type_icon_urls("施术单元", "https://assets.fz.wiki/wpn.png"), ())


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
            ("talent", f"{WARFARIN_STATIC_BASE}/skillicon/talent.webp"),
        )

    def test_akedata_growth_icons_match_category_and_talent_slot(self):
        view = _operator(
            round_icon_url="https://assets.fz.wiki/round.png",
            skills=[
                SkillView("fz_1", "追猎荒原", icon_id="https://assets.fz.wiki/s1.png", category="普攻"),
                SkillView("fz_2", "风矢穿林", icon_id="https://assets.fz.wiki/s2.png", category="战技"),
                SkillView("fz_3", "冰山呼告", icon_id="https://assets.fz.wiki/s3.png", category="终结技"),
            ],
            talents=[
                EffectView("t1", "猎物清点", "desc", "天赋", icon_url="https://assets.fz.wiki/t1.png"),
                EffectView("t2", "T2 荒原律动", "desc", "天赋", icon_url="https://assets.fz.wiki/t2.png"),
            ],
            potentials=[EffectView("p1", "P1", "desc", "潜能", icon_url="https://assets.fz.wiki/p.png")],
        )
        asset_urls.apply_akedata_growth_icons(
            view,
            {
                "skillGroupMap": {
                    "combo": {"skillGroupType": 3, "icon": "icon_combo_skill_typhoea_01"},
                    "atk": {"skillGroupType": 0, "icon": "icon_attack_funnel"},
                    "skill": {"skillGroupType": 1, "icon": "icon_skill_typhoea_01"},
                    "ult": {"skillGroupType": 2, "icon": "icon_ultimate_skill_typhoea_01"},
                },
                "talentNodeMap": {
                    "chr_0034_typhoea_passive_skill_0_1": {
                        "passiveSkillNodeInfo": {
                            "iconId": "icon_talent_typhoea_01",
                            "talentEffectId": "chr_0034_typhoea_talent_1_1",
                        }
                    },
                    "chr_0034_typhoea_passive_skill_1_2": {
                        "passiveSkillNodeInfo": {
                            "iconId": "icon_talent_typhoea_02",
                            "talentEffectId": "chr_0034_typhoea_talent_2_2",
                        }
                    },
                    "charBreak20": {"passiveSkillNodeInfo": {"iconId": "", "talentEffectId": ""}},
                },
            },
        )
        self.assertEqual(view.skills[0].icon_fallbacks, ("icon_attack_funnel",))
        self.assertEqual(view.skills[1].icon_fallbacks, ("icon_skill_typhoea_01",))
        self.assertEqual(view.skills[2].icon_fallbacks, ("icon_ultimate_skill_typhoea_01",))
        self.assertEqual(view.talents[0].icon_fallbacks, ("icon_talent_typhoea_01",))
        self.assertEqual(view.talents[1].icon_fallbacks, ("icon_talent_typhoea_02",))
        self.assertFalse(asset_urls.operator_needs_asset_donor(view))

    def test_operator_needs_donor_ignores_potential_remote_icons(self):
        view = _operator(
            round_icon_url="https://assets.fz.wiki/round.png",
            skills=[SkillView("s1", "追猎荒原", icon_id="icon_attack_funnel")],
            talents=[EffectView("t1", "猎物清点", "desc", "天赋", icon_url="icon_talent_typhoea_01")],
            potentials=[EffectView("p1", "P1", "desc", "潜能", icon_url="https://assets.fz.wiki/p.png")],
        )
        self.assertFalse(asset_urls.operator_needs_asset_donor(view))


class EndfieldAssetFetchTests(unittest.TestCase):
    def test_prepare_assets_uses_24mb_limit(self):
        source = (ROOT / "plugins/endfield/rendering/cards.py").read_text(encoding="utf-8")
        self.assertIn("ASSET_FETCH_MAX_BYTES = 24 * 1024 * 1024", source)
        account = (ROOT / "plugins/endfield/account/draw.py").read_text(encoding="utf-8")
        self.assertIn("max_bytes=24 * 1024 * 1024", account)
        gacha = (ROOT / "plugins/endfield/gacha/assets.py").read_text(encoding="utf-8")
        self.assertIn("IMAGE_FETCH_MAX_BYTES = 24 * 1024 * 1024", gacha)
        self.assertIn("fetch_many_resilient", gacha)

    def test_resolve_asset_groups_falls_back_after_primary_miss(self):
        try:
            draw = _load_draw()
        except ModuleNotFoundError as exc:
            self.skipTest(f"draw 依赖缺失: {exc}")
        from otae_bot.infrastructure.http.client import HttpResource

        async def fake_fetch(urls, **_kwargs):
            resolved = {}
            for url in urls:
                if "/charicon/" in url:
                    resolved[url] = HttpResource(b"ok", "image/png", 200, url)
                else:
                    resolved[url] = None
            return resolved, {url: "http_status" for url, item in resolved.items() if item is None}

        with patch.object(draw, "fetch_many_resilient", side_effect=fake_fetch):
            _assets, mapped, sources = asyncio.run(
                draw._resolve_asset_groups(
                    {
                        "icon": (
                            f"{AKEDATA_SPRITES_BASE}/charremoteicon/icon_chr_0034_typhoea.png",
                            f"{AKEDATA_SPRITES_BASE}/charicon/icon_chr_0034_typhoea.png",
                        )
                    },
                    inline=True,
                )
            )
        self.assertIn("akedata.wiki", sources["icon"])
        self.assertTrue(mapped["icon"].startswith("data:image/png;base64,"))

    def test_nested_weapon_info_tags_render_inner_key(self):
        try:
            draw = _load_draw()
        except ModuleNotFoundError as exc:
            self.skipTest(f"draw 依赖缺失: {exc}")
        view = models.WeaponView(
            name="寒夜幽影",
            slug="umbra-of-frigid-eventide",
            title="武器/寒夜幽影",
            weapon_id="wpn_funnel_0019",
        )
        html = draw.render_weapon_rich_text(
            "<@ba.info>该效果无法叠加，<@ba.key>身形如风</>最多叠加4层。</>",
            view,
            {},
        )
        self.assertNotIn("&lt;@ba.key&gt;", html)
        self.assertNotIn("&lt;/&gt;", html)
        self.assertIn("info-note", html)
        self.assertIn("身形如风", html)

    def test_ultimate_meta_uses_cooldown_field(self):
        try:
            draw = _load_draw()
        except ModuleNotFoundError as exc:
            self.skipTest(f"draw 依赖缺失: {exc}")
        skill = SkillView(
            "ult",
            "冰山呼告",
            category="终结技",
            levels=[
                models.SkillLevelView(
                    "Lv9",
                    9,
                    values={"所需能量": "200", "冷却时间": "20s"},
                )
            ],
        )
        html = draw.skill_meta(skill)
        self.assertIn("20s", html)
        self.assertIn("200", html)


if __name__ == "__main__":
    unittest.main()
