from __future__ import annotations

import copy
import json
import unittest
from hashlib import md5
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

from plugins.endfield import handlers as end
from plugins.endfield.account.challenge.i18n import build_challenge_locale
from plugins.endfield.account.detail.names import build_account_detail_name_map
from plugins.endfield.account.detail.service import build_account_detail_view
from plugins.endfield.catalog.models import (
    EquipmentView,
    OperatorView,
    SkillView,
    TermStyleView,
    WeaponSkillLevelView,
    WeaponSkillView,
    WeaponView,
)
from plugins.endfield.rendering import cards


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/endfield_mission_i18n.json").read_text()
)
ENGLISH = FIXTURE["I18nTextTable_EN"]["3186512559126454343"]


def names():
    return build_account_detail_name_map(
        {"chr_0005_chen": {}, "chr_0032_lizhiyan": {}},
        {},
        {},
        {},
        {},
        FIXTURE["I18nTextTable_CN"],
        text_table=FIXTURE["TextTable"],
    )


class AccountFollowupTests(unittest.IsolatedAsyncioTestCase):
    def test_main_mission_resolves_raw_hashed_and_text_reference_ids(self):
        descriptions = [
            {"id": "e11m8", "description": ENGLISH},
            {"id": md5(b"e11m8").hexdigest(), "description": ENGLISH},
            {"description": {"key": "e11m8_name", "value": ENGLISH}},
            {"description": {"id": 3186512559126454343, "text": ENGLISH}},
            {"description": "e11m8_name"},
        ]
        for mission in descriptions:
            with self.subTest(mission=mission):
                view = build_account_detail_view(
                    {"base": {"mainMission": mission}}, uid="x", name_map=names()
                )
                self.assertEqual(view.main_mission, "余晖未却")

    def test_hash_ids_are_sorted_using_the_original_character_number(self):
        chars = [
            {
                "level": 90,
                "charData": {
                    "id": md5(key.encode()).hexdigest(),
                    "name": key,
                    "rarity": {"value": "6"},
                },
            }
            for key in ("chr_0005_chen", "chr_0032_lizhiyan")
        ]
        before = copy.deepcopy(chars)
        view = build_account_detail_view({"chars": chars}, uid="x", name_map=names())
        self.assertEqual(
            [row.name for row in view.operators], ["chr_0032_lizhiyan", "chr_0005_chen"]
        )
        self.assertEqual(chars, before)

    async def run_account(self, mission, locale):
        role = SimpleNamespace(
            masked_uid="masked",
            role_id="test-role",
            server_id="1",
            nickname="test",
            server_name="国服",
        )
        detail = {"base": {"mainMission": mission}}
        original = copy.deepcopy(detail)
        with (
            patch.object(end.account_store, "decrypt_token", return_value="test-only"),
            patch.object(
                end, "_card_detail_with_snapshot", AsyncMock(return_value=detail)
            ),
            patch.object(
                end.official_client, "currency_balances", AsyncMock(return_value={})
            ),
            patch.object(
                end, "fetch_account_detail_name_map", AsyncMock(return_value=names())
            ),
            patch.object(end, "fetch_challenge_locale", locale),
            patch.object(
                end, "_render_account_pages", AsyncMock(return_value=(b"png",))
            ) as render,
            patch.object(end, "_finish_pngs", AsyncMock()),
        ):
            await end._render_account_detail(None, role, None, group=True)
            view = render.call_args.args[3]
        self.assertEqual(detail, original)
        return view

    async def test_bare_english_mission_uses_shared_cn_en_locale_without_id(self):
        locale = AsyncMock(
            return_value=build_challenge_locale(
                FIXTURE["I18nTextTable_CN"], FIXTURE["I18nTextTable_EN"]
            )
        )
        view = await self.run_account({"id": "", "description": ENGLISH}, locale)
        self.assertEqual(view.main_mission, "余晖未却")
        locale.assert_awaited_once()

    async def test_cn_or_resolved_mission_does_not_load_english_tables(self):
        for mission in (
            {"description": "余晖未却"},
            {"id": "e11m8", "description": ENGLISH},
        ):
            locale = AsyncMock(side_effect=AssertionError("Unnecessary locale request"))
            view = await self.run_account(mission, locale)
            self.assertEqual(view.main_mission, "余晖未却")
            locale.assert_not_awaited()

    async def test_locale_failure_keeps_original_mission_and_account_page(self):
        view = await self.run_account(
            {"description": ENGLISH}, AsyncMock(side_effect=RuntimeError("outage"))
        )
        self.assertEqual(view.main_mission, ENGLISH)


def rich_icon_pages():
    # Code-native fixture with AKE's 44:36 canvas, no image generator/network.
    icon = "data:image/svg+xml," + quote(
        '<svg xmlns="http://www.w3.org/2000/svg" width="44" height="36"><path fill="#8eae20" d="M22 5L36 19L22 33L8 19Z"/></svg>'
    )
    tag = "ba.naturalinflict"
    desc = f"目标受到<#{tag}>自然附着</>。"
    styles = {tag: TermStyleView(tag, "#8eae20", "term")}
    operator = OperatorView(
        "测试干员",
        "operator",
        "chr_1",
        skills=[SkillView("s", "测试技能", category="战技", description=desc)],
    )
    weapon = WeaponView(
        "测试武器",
        "weapon",
        "武器/测试",
        skills=[WeaponSkillView("测试技能", desc, [WeaponSkillLevelView(1)])],
        rich_text_links={tag: {"iconPath": "term"}},
    )
    equipment = EquipmentView(
        "测试装备", "装备/测试", suit_description=desc, term_styles=styles
    )
    return {
        "operator": cards._render_html(operator, "", {}, {}, styles, {tag: icon}),
        "weapon": cards._render_weapon_html(weapon, "", {"term": icon}),
        "equipment": cards._render_equipment_html(equipment, "", {}, {tag: icon}),
    }


class RichIconFollowupTests(unittest.IsolatedAsyncioTestCase):
    def test_all_rich_text_pages_share_font_relative_css(self):
        for kind, html in rich_icon_pages().items():
            with self.subTest(kind=kind):
                self.assertEqual(html.count(cards.TERM_ICON_CSS), 1)
                self.assertNotIn('style="width:11px;height:11px"', html)
                self.assertIn('class="term-icon"', html)
        self.assertEqual(cards.weapon_term_icon("missing", {}), "")
        self.assertEqual(cards.term_image("", ""), "")

    async def test_browser_icon_size_tracks_text_and_preserves_sprite_aspect(self):
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                for kind, html in rich_icon_pages().items():
                    await page.set_content(html)
                    for size in (13, 20):
                        with self.subTest(kind=kind, font_size=size):
                            metrics = await page.locator(
                                "img.term-icon"
                            ).first.evaluate(
                                """(img, size) => {
                                img.parentElement.style.fontSize = size + 'px';
                                const style = getComputedStyle(img), rect = img.getBoundingClientRect();
                                return {width:rect.width, height:rect.height, font:parseFloat(style.fontSize), align:parseFloat(style.verticalAlign)};
                            }""",
                                size,
                            )
                            self.assertAlmostEqual(
                                metrics["height"] / metrics["font"], 1.25, places=2
                            )
                            self.assertAlmostEqual(
                                metrics["width"] / metrics["height"], 44 / 36, places=2
                            )
                            self.assertAlmostEqual(
                                metrics["align"] / metrics["font"], -0.25, places=2
                            )
            finally:
                await browser.close()
