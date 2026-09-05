from __future__ import annotations

import re
import unittest
from unittest import mock

import plugins.endfield.handlers as endfield
from plugins.endfield.account.detail import draw as account_detail_draw
from plugins.endfield.account.detail import models as account_detail_models
from plugins.endfield.catalog import commands as commands_module
from plugins.endfield.account.store import EndfieldRole
from plugins.endfield.account.draw import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    FONT_ASSETS,
    PROFILE_CANVAS_HEIGHT,
    PROFILE_CANVAS_WIDTH,
    UI_ASSETS,
    _crisis_team,
    _prepare_assets,
    _crisis_html,
    _indie_html,
    _profession_icon_url,
    _profile_html,
    _property_icon_url,
)
from plugins.endfield.account.models import AccountUiPayload


def fixture_payload() -> AccountUiPayload:
    character = {
        "id": "char-a",
        "level": 90,
        "potentialLevel": 2,
        "charData": {
            "name": "测试干员",
            "rarity": {"value": "6"},
            "property": {"value": "灼热"},
            "avatarRtUrl": "https://assets.invalid/char.png",
            "avatarSqUrl": "https://assets.invalid/char-square.png",
            "illustrationUrl": "https://assets.invalid/illustration.png",
            "skills": [
                {"id": "normal", "name": "普通攻击", "type": {"key": "skill_type_normal_attack"}},
                {"id": "skill-a", "name": "战技", "type": {"key": "skill_type_normal_skill"}},
                {"id": "skill-b", "name": "连携技", "type": {"key": "skill_type_combo_skill"}},
                {"id": "skill-c", "name": "终结技", "type": {"key": "skill_type_ultimate_skill"}},
            ],
        },
        "userSkills": {
            "normal": {"level": 1},
            "skill-a": {"level": 9},
            "skill-b": {"level": 10},
            "skill-c": {"level": 9},
        },
        "weapon": {
            "level": 90,
            "breakthroughLevel": 4,
            "refineLevel": 4,
            "weaponData": {
                "name": "测试武器",
                "iconUrl": "https://assets.invalid/weapon.png",
                "skills": [
                    {"key": "weapon-attr-a", "value": "主能力提升"},
                    {"key": "weapon-attr-b", "value": "攻击提升"},
                    {"key": "weapon-skill-c", "value": "武器技能"},
                ],
            },
        },
    }
    character["charData"]["profession"] = {"key": "profession_assault", "value": "Assault"}
    character["charData"]["property"] = {"key": "char_property_fire", "value": "Fire"}
    detail = {
        "base": {
            "roleId": "secret-role-id",
            "name": "测试账号",
            "level": 60,
            "worldLevel": 7,
            "avatarUrl": "https://assets.invalid/avatar.png",
            "mainMission": {"description": "志同道合"},
            "charNum": 26,
            "weaponNum": 54,
            "docNum": 222,
        },
        "chars": [character],
        "config": {"charIds": ["char-a"]},
        "achieve": {"achieveMedals": [], "display": {}},
        "domain": [{"name": "四号谷地", "level": 12}],
    }
    crisis = {
        "status": {"name": "重燃测试作战", "highest": 44},
        "history": {
            "bestRecord": {
                "id": "record-a",
                "indicatorCount": 44,
                "passTs": "356",
                "chars": [{"charId": "char-a", "level": 90, "potentialLevel": 0}],
            }
        },
        "indicators": [],
    }
    crisis_record = {
        "id": "record-a",
        "chars": [
            {
                "charId": "char-a",
                "level": 90,
                "potentialLevel": 0,
                "avatarUrl": "https://assets.invalid/record-char.png",
                "weapon": {
                    "level": 80,
                    "icon": "https://assets.invalid/record-weapon.png",
                    "weaponTerms": [9, 9, 6],
                },
                "equips": {
                    "bodyEquip": {"icon": "https://assets.invalid/record-equip.png"}
                },
            }
        ],
    }
    indie = {
        "name": "死寂争鸣",
        "dungeonGroups": [
            {
                "hardDungeon": {
                    "name": "忿鼓咆声·苦难",
                    "recommendLevel": 90,
                    "isPass": True,
                    "desc": "关卡描述",
                    "feature": "敌人属性提升。",
                    "enemies": [],
                }
            }
        ],
        "isInActivity": True,
    }
    return AccountUiPayload(detail, crisis, (indie,), crisis_record)


class EndfieldAccountUiTests(unittest.TestCase):
    def test_payload_reads_responses_and_respects_display_order(self):
        payload = fixture_payload()
        response_payload = AccountUiPayload.from_responses(
            {"data": {"detail": payload.detail}},
            {"data": {"crisisContract": payload.crisis_contract}},
            {"data": {"indieHard": {"indieHardGroups": list(payload.indie_hard_groups)}}},
            {"data": {"recordDetail": payload.crisis_record}},
        )
        self.assertEqual(response_payload.base["name"], "测试账号")
        self.assertEqual(response_payload.displayed_characters()[0]["id"], "char-a")
        self.assertEqual(response_payload.active_indie_group()["name"], "死寂争鸣")
        self.assertEqual(response_payload.crisis_record["id"], "record-a")

    def test_profile_html_matches_fixed_canvas_and_displays_role_id(self):
        payload = fixture_payload()
        page = _profile_html(
            payload,
            {"https://assets.invalid/char-square.png": "square-portrait"},
        )
        self.assertIn(f"width: {CANVAS_WIDTH}px", page)
        self.assertIn(f"height: {CANVAS_HEIGHT}px", page)
        self.assertIn(f"width: {PROFILE_CANVAS_WIDTH}px", page)
        self.assertIn(f"height: {PROFILE_CANVAS_HEIGHT}px", page)
        self.assertIn('class="profile-stage"', page)
        self.assertIn("@font-face", page)
        self.assertIn("EndfieldCN", page)
        self.assertIn("EndfieldHUD", page)
        self.assertIn("测试账号", page)
        self.assertIn("四号谷地", page)
        self.assertIn("secret-role-id", page)
        self.assertIn("square-portrait", page)
        self.assertNotIn("https://assets.invalid/char.png", page)
        self.assertIn("profile-profession-8.png", page)
        self.assertIn("profile-property-fire.png", page)
        self.assertIn("potential-2.png", page)
        payload.detail["chars"][0]["potentialLevel"] = 0
        zero_potential_page = _profile_html(payload, {})
        self.assertIn("profile-potential-0.png", zero_potential_page)
        self.assertIn("profile-mission.png", page)
        self.assertIn("profile-medal-deco.png", page)
        self.assertIn("profile-medal-detail.png", page)
        self.assertIn("profile-medal-caption.png", page)
        self.assertIn("profile-medal-label.png", page)
        self.assertEqual(page.count("profile-medal-entry.png"), 1)
        self.assertNotIn('<button class="avatar-more"', page)
        self.assertNotIn('<div class="profile-actions">', page)

    def test_indie_html_contains_selected_hard_dungeon(self):
        payload = fixture_payload()
        group = payload.active_indie_group()
        dungeon = group["dungeonGroups"][0]["hardDungeon"]
        page = _indie_html(group, [dungeon], dungeon, {})
        self.assertIn("忿鼓咆声·苦难", page)
        self.assertIn("LV.90", page)
        self.assertIn("已通过", page)

    def test_crisis_html_contains_result_and_squad(self):
        payload = fixture_payload()
        character = _crisis_team(
            payload, payload.crisis_contract["history"]["bestRecord"]
        )[0]
        page = _crisis_html(
            payload,
            payload.crisis_contract,
            [character],
            [],
            {
                "https://assets.invalid/record-char.png": "snapshot-portrait",
                "https://assets.invalid/record-weapon.png": "snapshot-weapon",
                "https://assets.invalid/record-equip.png": "snapshot-equip",
            },
        )
        self.assertIn("行动成功", page)
        self.assertIn("05:56", page)
        self.assertIn('<span class="level-label">LV</span><b>90</b>', page)
        self.assertNotIn("测试干员</strong>", page)
        self.assertIn("potential-0.png", page)
        self.assertIn("contract-role-watermark.png", page)
        self.assertIn("contract-total.png", page)
        self.assertEqual(page.count(">9</b>"), 2)
        self.assertEqual(page.count(">6</b>"), 1)
        self.assertIn("snapshot-weapon", page)
        self.assertIn("snapshot-equip", page)
        self.assertNotIn("https://assets.invalid/weapon.png", page)
        self.assertNotIn("行动结束", page)
        self.assertNotIn('class="action-buttons"', page)
        self.assertNotIn('class="crisis-footer"', page)
        self.assertNotIn('class="operator-badges"', page)
        self.assertIn('class="hud-icon stat-watermark"', page)

    def test_pages_use_local_game_ui_assets_without_text_placeholders(self):
        payload = fixture_payload()
        profile = _profile_html(payload, {})
        group = payload.active_indie_group()
        dungeon = group["dungeonGroups"][0]["hardDungeon"]
        indie = _indie_html(group, [dungeon], dungeon, {})
        crisis = _crisis_html(payload, payload.crisis_contract, [payload.characters[0]], [], {})

        self.assertIn('class="profile-bg"', profile)
        self.assertNotIn("profile-personal.png", profile)
        self.assertNotIn("common-close.png", profile)
        self.assertIn("dungeon-more.png", indie)
        self.assertIn("contract-total.png", crisis)
        self.assertNotIn("common-confirm.png", crisis)
        for page in (profile, indie, crisis):
            for placeholder in ("•••", "↻", "✓　完成", "↗"):
                self.assertNotIn(placeholder, page)

    def test_crisis_header_image_is_rendered(self):
        payload = fixture_payload()
        payload.crisis_contract["status"]["headerImage"] = "https://assets.invalid/header.png"
        page = _crisis_html(
            payload,
            payload.crisis_contract,
            [payload.characters[0]],
            [],
            {"https://assets.invalid/header.png": "data:image/png;base64,header"},
        )
        self.assertIn('class="crisis-header-art"', page)
        self.assertIn("data:image/png;base64,header", page)

    def test_warfarin_semantic_icon_urls_match_endfield_keys(self):
        self.assertEqual(
            _profession_icon_url("profession_assault"),
            "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites/charprofessionicon/icon_profession_8_s.png",
        )
        self.assertEqual(
            _property_icon_url("char_property_fire"),
            "https://data.akedata.wiki/public/images/assets/beyond/dynamicassets/gameplay/ui/sprites/elementicon/icon_charattrtype_fire.png",
        )


class EndfieldAccountUiAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_assets_registers_local_ui_fonts_and_icons(self):
        prepared = await _prepare_assets([], inline=False)
        for virtual_url, path, _weight in FONT_ASSETS.values():
            if str(path).startswith("C:/Windows/") and not path.exists():
                self.assertNotIn(virtual_url, prepared.urls)
                self.assertNotIn(virtual_url, prepared.resources)
                continue  # Optional Windows HUD fonts have CSS fallbacks on Linux.
            self.assertTrue(path.exists())
            self.assertIn(virtual_url, prepared.urls)
            self.assertIn(virtual_url, prepared.resources)
        for virtual_url, path in UI_ASSETS.values():
            self.assertTrue(path.exists())
            self.assertIn(virtual_url, prepared.urls)
            self.assertIn(virtual_url, prepared.resources)


class EndfieldAccountDetailDrawTests(unittest.TestCase):
    def build_view(self, **overrides):
        equip = account_detail_models.AccountEquipView(
            slot_label="护甲",
            name="测试护甲",
            icon_url="https://assets.invalid/equip.png",
            rarity=5,
            type_label="护甲",
            level_label="70",
            suit_name="测试套组",
        )
        operator = account_detail_models.AccountOperatorView(
            name="测试干员",
            rarity=6,
            level=90,
            evolve_phase=4,
            potential_level=5,
            profession="近卫",
            element="寒冷",
            element_color="#2f86c4",
            weapon_type="单手剑",
            portrait_url="https://assets.invalid/portrait.png",
            skills=(
                account_detail_models.AccountSkillView(
                    "普通攻击", "https://assets.invalid/skill.png", 9, 12, "普通攻击",
                    "物理", "#969a99",
                ),
                account_detail_models.AccountSkillView(
                    "战技", "https://assets.invalid/skill.png", 10, 12, "战技",
                    "灼热", "#ec654d",
                ),
                account_detail_models.AccountSkillView(
                    "连携技", "https://assets.invalid/skill.png", 11, 12, "连携技",
                    "自然", "#77c92f",
                ),
                account_detail_models.AccountSkillView(
                    "终结技", "https://assets.invalid/skill.png", 12, 12, "终结技",
                    "电磁", "#f0cf2f", True,
                ),
            ),
            weapon=account_detail_models.AccountWeaponView(
                name="测试武器", rarity=6, level=90, potential_level=5, breakthrough_level=4,
                gem_name="测试基质", gem_icon_url="https://assets.invalid/gem.png",
            ),
            equips=(equip, None, None, equip),
        )
        defaults = {
            "nickname": "测试管理员",
            "uid": "****1234",
            "server_name": "China",
            "level": 60,
            "world_level": 7,
            "main_mission": "余晖未却",
            "saved_at": "2026-07-20 09:00",
            "stats": (account_detail_models.AccountStatView("干员", "28", "已获得"),),
            "operators": (operator,),
        }
        defaults.update(overrides)
        return account_detail_models.AccountDetailView(**defaults)

    def render(self, view, icon_map=None):
        return account_detail_draw._render_account_detail_html(view, icon_map or {})

    def test_uses_the_plugin_visual_language(self):
        page = self.render(self.build_view())
        self.assertIn('"Microsoft YaHei"', page)
        self.assertIn("linear-gradient(135deg,#f8f9f6,#e6eaeb)", page)
        self.assertIn("#ffd000", page)
        self.assertIn("width:1550px", page)
        self.assertNotIn("@font-face", page)
        self.assertNotIn("endfield-account.local", page)

    def test_renders_identity_line_and_account_stats(self):
        page = self.render(self.build_view())
        self.assertIn("UID ****1234 · China · 权限等级 60 · 探索等级 7 · 主线「余晖未却」", page)
        self.assertIn("1 位干员", page)
        self.assertIn("数据更新 2026-07-20 09:00", page)

    def test_renders_progression_weapon_and_five_slots_per_operator(self):
        page = self.render(self.build_view())
        self.assertEqual(page.count('class="account-operator'), 1)
        # four equipment slots plus the tactical item, empty holes included
        self.assertEqual(len(re.findall(r'class="account-slot[ "]', page)), 5)
        self.assertEqual(page.count("account-slot empty"), 3)
        self.assertIn("Lv.90", page)
        self.assertIn("潜能 5", page)
        self.assertNotIn("精炼", page)
        self.assertNotIn("突破", page)
        self.assertNotIn("精英", page)
        self.assertIn("grid-template-columns:320px 210px 340px", page)
        self.assertIn("测试套组 ×2", page)
        self.assertIn("rarity-6", page)
        self.assertIn("--element-color:", page)
        self.assertNotIn("account-gem", page)
        self.assertNotIn("测试基质", page)
        self.assertNotIn("https://assets.invalid/gem.png", account_detail_draw._account_detail_icon_urls(self.build_view()))

    def test_renders_game_style_skill_damage_backgrounds(self):
        page = self.render(self.build_view())
        self.assertIn('style="--skill-color:#969a99"', page)
        self.assertIn('style="--skill-color:#ec654d"', page)
        self.assertIn('class="account-skill ultimate" style="--skill-color:#f0cf2f"', page)
        self.assertIn("box-shadow:inset 0 4px 0 #20262a", page)
        self.assertNotIn(".account-operator.rarity-6", page)
        self.assertIn("conic-gradient(transparent 0 120deg,var(--skill-color) 120deg 240deg", page)
        self.assertIn(".account-skill.ultimate .account-skill-icon::before", page)
        self.assertIn("技能 · 普攻 / 战技 / 连携 / 终结", page)

    def test_renders_lv9_and_three_mastery_polygon_states(self):
        page = self.render(self.build_view())
        self.assertEqual(page.count('class="account-skill-mastery mastery-0"'), 1)
        self.assertEqual(page.count('class="account-skill-mastery mastery-1"'), 1)
        self.assertEqual(page.count('class="account-skill-mastery mastery-2"'), 1)
        self.assertEqual(page.count('class="account-skill-mastery mastery-3"'), 1)
        self.assertIn(".mastery-1 .mastery-left", page)
        self.assertIn(".mastery-2 .mastery-bottom", page)
        self.assertIn(".mastery-3 .mastery-unit", page)
        self.assertIn("width:48px; height:25px", page)
        self.assertIn("width:23px; height:23px", page)
        self.assertIn("transform:translateX(-1.5px)", page)
        self.assertEqual(page.count('<svg viewBox="130 170 560 560"'), 4)
        self.assertIn('class="mastery-unit mastery-left" points="156.01,449.99', page)
        self.assertIn('class="mastery-unit mastery-bottom" points="597.00,704.61', page)
        self.assertIn('class="mastery-unit mastery-right" points="596.99,195.39', page)
        self.assertNotIn("clip-path:polygon", page)
        self.assertNotIn("<b>9</b>", page)

    def test_skill_level_labels_follow_lv9_and_m1_to_m3(self):
        self.assertEqual(
            [account_detail_draw._skill_level_label(level) for level in (9, 10, 11, 12)],
            ["Lv9", "M1", "M2", "M3"],
        )

    def test_lv1_to_lv8_use_plain_level_badges(self):
        for level in range(1, 9):
            marker = account_detail_draw._skill_progress_marker(level, 0)
            self.assertIn('class="account-skill-level"', marker)
            self.assertIn(f">Lv{level}<", marker)
            self.assertNotIn("<svg", marker)
        self.assertIn("<svg", account_detail_draw._skill_progress_marker(9, 0))

    def test_renders_skill_icons_from_the_prepared_asset_map(self):
        view = self.build_view()
        page = self.render(view, {"https://assets.invalid/skill.png": "https://endfield.local/assets/skill"})
        self.assertIn("https://endfield.local/assets/skill", page)
        self.assertEqual(
            account_detail_draw._account_detail_icon_urls(view).count("https://assets.invalid/skill.png"), 4
        )

    def test_uses_prepared_assets_instead_of_remote_urls(self):
        view = self.build_view()
        page = self.render(view, {"https://assets.invalid/portrait.png": "https://endfield.local/assets/abc"})
        self.assertIn("https://endfield.local/assets/abc", page)
        self.assertNotIn("https://assets.invalid/portrait.png", page)

    def test_missing_values_render_placeholders_without_fabricating_zero(self):
        bare = account_detail_models.AccountOperatorView(name="空白干员")
        page = self.render(self.build_view(operators=(bare,), level=None, world_level=None))
        self.assertIn("Lv.--", page)
        self.assertIn("未装备武器", page)
        self.assertIn("未成套", page)
        self.assertIn("权限等级 --", page)

    def test_escapes_operator_names(self):
        operator = account_detail_models.AccountOperatorView(name="<b>&测试")
        page = self.render(self.build_view(operators=(operator,)))
        self.assertIn("&lt;b&gt;&amp;测试", page)
        self.assertNotIn("<b>&测试", page)

    def test_compact_rows_only_above_threshold(self):
        operator = account_detail_models.AccountOperatorView(name="干员")
        threshold = account_detail_models.COMPACT_THRESHOLD
        roomy = self.render(self.build_view(operators=(operator,) * threshold))
        self.assertIn("grid-template-columns:320px", roomy)
        self.assertIn("width:84px; height:84px", roomy)
        tight = self.render(self.build_view(operators=(operator,) * (threshold + 1)))
        self.assertIn("grid-template-columns:300px", tight)
        self.assertIn("width:68px; height:68px", tight)

    def test_empty_roster_renders_notice(self):
        page = self.render(self.build_view(operators=()))
        self.assertIn("该账号暂无可展示的干员数据", page)
        self.assertNotIn('class="account-operator', page)


class EndfieldAccountDetailPaginationTests(unittest.IsolatedAsyncioTestCase):
    def build_view(self, count: int):
        return account_detail_models.AccountDetailView(
            nickname="测试管理员",
            uid="****1234",
            level=60,
            saved_at="2026-07-27 09:00",
            stats=(account_detail_models.AccountStatView("干员", str(count), "已获得"),),
            operators=tuple(
                account_detail_models.AccountOperatorView(name=f"干员{index}")
                for index in range(count)
            ),
        )

    def test_pages_stay_within_budget_and_keep_every_operator(self):
        view = self.build_view(130)

        pages = account_detail_draw._paginate_operators(view, 60)

        self.assertEqual(len(pages), 3)
        for page in pages:
            self.assertLessEqual(page.page_operator_count, 60)
        names = [operator.name for page in pages for operator in page.operators]
        self.assertEqual(names, [operator.name for operator in view.operators])

    def test_only_the_first_page_carries_the_account_stats(self):
        pages = account_detail_draw._paginate_operators(self.build_view(130), 60)

        self.assertTrue(pages[0].stats)
        self.assertTrue(all(not page.stats for page in pages[1:]))
        first = account_detail_draw._render_account_detail_html(pages[0], {})
        later = account_detail_draw._render_account_detail_html(pages[1], {})
        self.assertIn('<div class="stat-strip">', first)
        self.assertNotIn('<div class="stat-strip">', later)

    def test_every_page_reports_the_whole_roster(self):
        pages = account_detail_draw._paginate_operators(self.build_view(130), 60)

        for index, page in enumerate(pages, start=1):
            self.assertEqual(page.operator_count, 130)
            self.assertEqual((page.page_number, page.page_count), (index, 3))
            html = account_detail_draw._render_account_detail_html(page, {})
            self.assertIn("130 位干员", html)
            self.assertIn(f"第 {index} / 3 张", html)

    def test_row_density_stays_identical_across_pages(self):
        # Density follows the whole roster, so a short final page must not resize its rows.
        pages = account_detail_draw._paginate_operators(self.build_view(130), 60)

        self.assertTrue(all(page.compact for page in pages))
        widths = {
            account_detail_draw._render_account_detail_html(page, {}).count("width:68px; height:68px")
            > 0
            for page in pages
        }
        self.assertEqual(widths, {True})

    def test_page_marker_is_absent_when_a_single_image_suffices(self):
        html = account_detail_draw._render_account_detail_html(self.build_view(3), {})

        self.assertIn("3 位干员", html)
        self.assertNotIn("第 1 / ", html)
        self.assertNotIn("本张", html)

    async def test_one_image_while_the_roster_fits(self):
        renderer = mock.AsyncMock(return_value=b"single")

        with mock.patch.object(account_detail_draw, "draw_account_detail_card", renderer):
            pages = await account_detail_draw.draw_account_detail_cards(self.build_view(10))

        self.assertEqual(pages, (b"single",))
        self.assertEqual(renderer.await_count, 1)

    async def test_splits_a_large_roster_without_waiting_for_an_overflow(self):
        # 120 operators would still render as one image, but a ~12000px one.
        renderer = mock.AsyncMock(side_effect=[b"page-1", b"page-2"])

        with mock.patch.object(account_detail_draw, "draw_account_detail_card", renderer):
            pages = await account_detail_draw.draw_account_detail_cards(self.build_view(120))

        self.assertEqual(pages, (b"page-1", b"page-2"))
        self.assertEqual(renderer.await_count, 2)
        self.assertEqual(renderer.await_args_list[0].args[0].page_count, 2)
        self.assertEqual(renderer.await_args_list[0].args[0].roster_count, 120)

    async def test_a_roster_within_the_limit_still_splits_when_it_overflows(self):
        overflow = RuntimeError("Screenshot element height 12997px exceeds limit 12000px")
        # Unusually tall rows: 50 operators are under the limit yet still blow the ceiling.
        renderer = mock.AsyncMock(side_effect=[overflow, overflow, b"page-1", b"page-2"])

        with mock.patch.object(account_detail_draw, "draw_account_detail_card", renderer):
            pages = await account_detail_draw.draw_account_detail_cards(self.build_view(50))

        self.assertEqual(pages, (b"page-1", b"page-2"))
        self.assertEqual(renderer.await_args_list[0].args[0].page_count, 1)
        self.assertEqual(renderer.await_args_list[-1].args[0].page_count, 2)

    async def test_a_non_height_render_error_is_not_swallowed(self):
        renderer = mock.AsyncMock(side_effect=RuntimeError("browser crashed"))

        with mock.patch.object(account_detail_draw, "draw_account_detail_card", renderer):
            with self.assertRaisesRegex(RuntimeError, "browser crashed"):
                await account_detail_draw.draw_account_detail_cards(self.build_view(10))

        self.assertEqual(renderer.await_count, 1)

    async def test_a_budget_that_still_overflows_falls_through_to_a_smaller_one(self):
        overflow = RuntimeError("Screenshot element height 12997px exceeds limit 12000px")
        # The 60-per-page split still overflows; the 40-per-page split fits.
        renderer = mock.AsyncMock(side_effect=[overflow, b"a", b"b", b"c"])

        with mock.patch.object(account_detail_draw, "draw_account_detail_card", renderer):
            pages = await account_detail_draw.draw_account_detail_cards(self.build_view(100))

        self.assertEqual(pages, (b"a", b"b", b"c"))
        self.assertEqual(renderer.await_args_list[-1].args[0].page_count, 3)


class EndfieldAccountDetailRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await endfield._ACCOUNT_PAGE_CACHE.clear()

    async def asyncTearDown(self):
        await endfield._ACCOUNT_PAGE_CACHE.close()

    def setUp(self):
        self.roles = [
            EndfieldRole(1, 1, "qq", "b1", "1770431209", "1", "甲", "China", True),
            EndfieldRole(2, 1, "qq", "b2", "1770431888", "1", "乙", "China", False),
        ]

    def patched(self, *, group: bool, roles):
        store = mock.Mock()
        store.list_roles.return_value = roles
        store.resolve_role.side_effect = lambda _user, selector: next(
            (role for role in roles if role.nickname == selector), None
        )
        store.decrypt_token.return_value = "token"
        return (
            mock.patch.object(endfield, "account_store", store),
            mock.patch.object(endfield, "is_group", return_value=group),
            mock.patch.object(endfield.CredentialCipher, "from_env", return_value=mock.Mock()),
            mock.patch.object(
                endfield.official_client, "card_detail", mock.AsyncMock(return_value={"base": {"name": "甲"}})
            ),
            mock.patch.object(
                endfield.official_client, "currency_balances", mock.AsyncMock(return_value={1: 7, 2: 80, 3: 9})
            ),
            mock.patch.object(
                endfield, "draw_account_detail_cards", mock.AsyncMock(return_value=(b"png",))
            ),
            mock.patch.object(endfield, "_finish_png", mock.AsyncMock()),
            store,
        )

    async def run_accounts(self, *, group: bool, roles, selector: str = "", reply=None):
        store_patch, group_patch, cipher_patch, detail_patch, currency_patch, draw_patch, finish_patch, store = self.patched(
            group=group, roles=roles
        )
        command = commands_module.ParsedEndfieldCommand("accounts", account_selector=selector)
        matcher = mock.AsyncMock()
        prompt = mock.AsyncMock(return_value=reply)
        with store_patch, group_patch, cipher_patch, detail_patch as detail, currency_patch as currency, draw_patch as draw, finish_patch:
            with mock.patch.object(endfield, "prompt_silently", prompt):
                await endfield._handle_accounts(
                    matcher, "qq", command, mock.Mock(), group=group
                )
        return matcher, prompt, detail, currency, draw, store

    async def test_single_account_renders_detail_without_prompting(self):
        matcher, prompt, detail, currency, draw, _ = await self.run_accounts(group=False, roles=self.roles[:1])
        prompt.assert_not_awaited()
        detail.assert_awaited_once()
        currency.assert_awaited_once()
        draw.assert_awaited_once()

    async def test_private_chat_reveals_uid_and_group_masks_it(self):
        _, _, _, _, draw, _ = await self.run_accounts(group=False, roles=self.roles[:1])
        self.assertEqual(draw.await_args.args[0].uid, "1770431209")
        _, _, _, _, masked_draw, _ = await self.run_accounts(group=True, roles=self.roles[:1])
        self.assertEqual(masked_draw.await_args.args[0].uid, "****1209")

    async def test_multiple_accounts_prompt_with_detail_hint(self):
        _, prompt, detail, _, _, _ = await self.run_accounts(group=False, roles=self.roles)
        prompt.assert_awaited_once()
        listing = prompt.await_args.args[0]
        self.assertIn("回复编号查看该账号详情", listing)
        self.assertIn("1. 甲", listing)
        self.assertIn("2. 乙", listing)
        detail.assert_not_awaited()

    async def test_group_listing_hides_full_uid(self):
        _, prompt, _, _, _, _ = await self.run_accounts(group=True, roles=self.roles)
        listing = prompt.await_args.args[0]
        self.assertIn("****1209", listing)
        self.assertNotIn("1770431209", listing)

    async def test_reply_number_selects_the_matching_account(self):
        reply = mock.Mock()
        reply.extract_plain_text.return_value = "2"
        _, _, detail, _, _, _ = await self.run_accounts(group=False, roles=self.roles, reply=reply)
        self.assertEqual(detail.await_args.args[1].nickname, "乙")

    async def test_reply_nickname_falls_back_to_resolve_role(self):
        reply = mock.Mock()
        reply.extract_plain_text.return_value = "乙"
        _, _, detail, _, _, store = await self.run_accounts(group=False, roles=self.roles, reply=reply)
        store.resolve_role.assert_called_with("qq", "乙")
        self.assertEqual(detail.await_args.args[1].nickname, "乙")

    async def test_explicit_selector_skips_the_prompt(self):
        _, prompt, detail, _, _, _ = await self.run_accounts(group=False, roles=self.roles, selector="乙")
        prompt.assert_not_awaited()
        self.assertEqual(detail.await_args.args[1].nickname, "乙")

    async def test_unknown_selector_reports_guidance(self):
        matcher, _, detail, _, _, _ = await self.run_accounts(group=False, roles=self.roles, selector="丙")
        matcher.finish.assert_awaited_once_with("未找到对应账号，请使用 /ef 账号 查看编号。")
        detail.assert_not_awaited()

    async def test_missing_binding_reports_bind_hint(self):
        matcher, _, detail, _, _, _ = await self.run_accounts(group=False, roles=[])
        matcher.finish.assert_awaited_once_with("尚未绑定终末地账号。使用 /ef 绑定 开始绑定。")
        detail.assert_not_awaited()

    async def test_cancelled_reply_stops_without_rendering(self):
        reply = mock.Mock()
        reply.extract_plain_text.return_value = "取消"
        matcher, _, detail, _, _, _ = await self.run_accounts(group=False, roles=self.roles, reply=reply)
        matcher.finish.assert_awaited_once_with("已取消账号查询。")
        detail.assert_not_awaited()

    async def test_detail_fetch_claims_the_role_task_lock(self):
        with mock.patch.object(endfield.ROLE_TASKS, "claim") as claim:
            await self.run_accounts(group=False, roles=self.roles[:1])
        claim.assert_called_once_with(self.roles[0])


if __name__ == "__main__":
    unittest.main()
