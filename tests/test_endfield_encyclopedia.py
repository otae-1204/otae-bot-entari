"""图鉴第 1–4 期的测试：命令、分类、道具效果、索引缓存、别名、敌人、词条、档案与 handler 接线。

夹具按 §1 的 snapshot 方式注入：把 `repository.snapshot` 换成夹具快照，
`query_snapshot()` 就复用同一份表，测试不发任何请求。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from plugins.endfield.catalog import aliases as catalog_aliases
from plugins.endfield.catalog import commands
from plugins.endfield.catalog.models import ArchiveItemView, ArchiveSnapshotView
from plugins.endfield.catalog.views.common import _rich_text_visual
from plugins.endfield.encyclopedia import classify, index as encyclopedia_index
from plugins.endfield.encyclopedia import (
    enemies as encyclopedia_enemies,
    items as encyclopedia_items,
    props as encyclopedia_props,
    service as encyclopedia_service,
    terms as encyclopedia_terms,
)
from plugins.endfield.providers import repository
from plugins.endfield.providers.repository import AkeDataIncomplete, AkeSnapshot
from plugins.endfield.providers.warfarin import WarfarinAPIError
import plugins.endfield.handlers as endfield


FIXTURE = Path(__file__).parent / "fixtures/endfield_akedata_1_5_3.json"
ROOT = Path(__file__).resolve().parents[1]
HELP_IMAGE = ROOT / "assets/image/help/endfield.png"

ITEM_MOSS = "item_plant_1_moss"
ITEM_PROBE_MATERIAL = "item_equip_material_probe"
PROP_RATION = "item_tactic_ration"
PROP_MEDKIT = "item_consumable_medkit"
PROP_BROKEN = "item_tactic_broken"
ENEMY_PROBE = "eny_0001_probe"
ENEMY_SHIELD = "eny_0002_shield"
TERM_BURNING = "ba.burning"


_EMPTY_ALIAS_DATA = {
    "version": 2,
    "operator": {},
    "weapon": {},
    "equipment": {},
    "item": {},
    "prop": {},
    "enemy": {},
    "term": {},
    "archive_entry": {},
}


def _tables() -> dict:
    return json.loads(FIXTURE.read_bytes())


def _snapshot(*, shared: str = "fixture") -> AkeSnapshot:
    snapshot = AkeSnapshot("1.5.3@9885010-4", "public/1.5.3/9885010-4/TableCfg", shared)
    snapshot._tables = _tables()
    return snapshot


def _archive_snapshot() -> ArchiveSnapshotView:
    return ArchiveSnapshotView(
        items=[
            ArchiveItemView(
                item_id="nar_1",
                name="终末地物资",
                page_name="中枢档案",
                category_name="调查报告",
                group_id="grp_1",
                group_name="萨米维格",
                icon_url="",
            ),
            ArchiveItemView(
                item_id="nar_2",
                name="旧日回响",
                page_name="见闻辑录",
                category_name="电子档案",
                group_id="grp_1",
                group_name="萨米维格",
                icon_url="",
            ),
        ],
        version="1.5",
        fetched_at=1,
    )


class _SnapshotFixture:
    """把 `repository.snapshot` 换成夹具快照，并断言测试期间没有真实请求。"""

    def __init__(self, test: unittest.TestCase, *, shared: str = "fixture") -> None:
        self.test = test
        self.data = _snapshot(shared=shared)
        self.network = patch.object(
            repository, "_get", side_effect=AssertionError("Unexpected network request")
        )
        self.snapshot = patch.object(
            repository, "snapshot", AsyncMock(return_value=self.data)
        )

    def start(self) -> "_SnapshotFixture":
        self.network.start()
        self.snapshot.start()
        self.test.addCleanup(self.snapshot.stop)
        self.test.addCleanup(self.network.stop)
        self.test.addCleanup(encyclopedia_index.clear_index_caches)
        self.test.addCleanup(encyclopedia_terms.clear_caches)
        self.test.addCleanup(encyclopedia_enemies.akedata.clear_caches)
        # 测试自己读 self.data，与既有 endfield 测试同一形状。
        self.test.data = self.data
        return self


# ------------------------------------------------------------------ §4.6 / §7.1 命令


class EndfieldEncyclopediaCommandTests(unittest.TestCase):
    def test_scope_words_share_one_table(self):
        self.assertEqual(commands.ENCYCLOPEDIA_SCOPES, ("item", "prop", "enemy", "term", "archive_entry"))
        for scope in commands.ENCYCLOPEDIA_SCOPES:
            with self.subTest(scope=scope):
                self.assertIn(scope, commands.ENCYCLOPEDIA_SCOPE_ALIASES)
                self.assertIn(scope, commands.SCOPE_LABELS)
                self.assertIn(scope, commands.ENCYCLOPEDIA_SCOPE_WORDS)

    def test_food_alias_parses_as_prop_scope(self):
        command = commands.parse_command("食物 炝炒时蔬")
        self.assertEqual((command.action, command.scope, command.query), ("query", "prop", "炝炒时蔬"))

    def test_encyclopedia_range_words_parse(self):
        for text, scope in (
            ("物品", "item"),
            ("材料", "item"),
            ("道具", "prop"),
            ("敌人", "enemy"),
            ("怪物", "enemy"),
            ("词条", "term"),
            ("术语", "term"),
        ):
            with self.subTest(text=text):
                command = commands.parse_command(text)
                self.assertEqual((command.action, command.scope, command.query), ("query", scope, ""))

    def test_source_option_is_parsed_for_encyclopedia_scopes(self):
        command = commands.parse_command("道具 xx --source fz")
        self.assertEqual((command.scope, command.query, command.source), ("prop", "xx", "fz"))

    def test_bare_source_option_does_not_reject_unknown_scope(self):
        command = commands.parse_command("xx --source fz")
        self.assertEqual((command.action, command.scope, command.source), ("query", "all", "fz"))

    def test_currency_log_action_keeps_all_option_out_of_source_parsing(self):
        command = commands.parse_command("流水 2 --all")
        self.assertEqual(command.action, "currency_log")
        self.assertEqual(command.error, "")
        self.assertEqual(command.account_selector, "2")

    def test_archive_aliases_parse_as_archive_entry_scope(self):
        for text in ("档案 终末地", "报告 终末地", "档案库 终末地"):
            with self.subTest(text=text):
                command = commands.parse_command(text)
                self.assertEqual((command.scope, command.query), ("archive_entry", "终末地"))

    def test_archive_personal_actions_are_unchanged(self):
        self.assertEqual(commands.parse_command("档案").action, "archive_view")
        self.assertEqual(commands.parse_command("档案 刷新").action, "archive_refresh")
        progress = commands.parse_command("档案 收集 2")
        self.assertEqual(progress.action, "archive_progress")
        self.assertEqual(progress.account_selector, "2")

    def test_archive_entry_strips_source_and_rarity_options_from_query(self):
        command = commands.parse_command("档案 xx --source fz")
        self.assertEqual(command.scope, "archive_entry")
        self.assertEqual(command.source, "fz")
        self.assertEqual(command.query, "xx")
        self.assertNotIn("--source", command.query)

    def test_search_scope_reads_archive_entry_from_shared_aliases(self):
        command = commands.parse_command("搜索 档案 xx")
        self.assertEqual((command.action, command.scope, command.query), ("search", "archive_entry", "xx"))

    def test_normalize_alias_kind_accepts_encyclopedia_words(self):
        for word, kind in (
            ("物品", "item"),
            ("道具", "prop"),
            ("食物", "prop"),
            ("敌人", "enemy"),
            ("怪物", "enemy"),
            ("词条", "term"),
            ("术语", "term"),
            ("档案", "archive_entry"),
            ("报告", "archive_entry"),
        ):
            with self.subTest(word=word):
                self.assertEqual(commands.normalize_alias_kind(word), kind)

    def test_format_not_found_names_catalog_without_repeating_the_word(self):
        expected = {
            "item": "可以发送 /ef 物品 浏览物品目录",
            "item_catalog": "可以发送 /ef 物品 浏览物品目录",
            "prop": "可以发送 /ef 道具 浏览道具目录",
            "prop_catalog": "可以发送 /ef 道具 浏览道具目录",
            "enemy_catalog": "可以发送 /ef 敌人 浏览敌人目录",
            "term_catalog": "可以发送 /ef 词条 浏览词条目录",
        }
        for scope, tail in expected.items():
            with self.subTest(scope=scope):
                message = commands.format_not_found(scope, "xx")
                self.assertIn(tail, message)
                self.assertNotIn("目录目录", message)

    def test_format_not_found_falls_back_to_search_for_archive_entry(self):
        message = commands.format_not_found("archive_entry", "xx")
        self.assertIn("可以尝试 /ef 搜索 xx", message)

    def test_format_candidates_tail_lists_shipped_encyclopedia_scopes(self):
        message = commands.format_candidates(
            [commands.EndfieldCandidate("item", "k", "n", 90, "akedata")]
        )
        for word in ("物品", "道具", "敌人", "词条", "档案"):
            self.assertIn(f"/ef {word} <名称>", message)

    def test_format_help_and_source_cover_the_encyclopedia(self):
        help_text = commands.format_help()
        for usage in (
            "/ef 物品 <名称>",
            "/ef 道具 <名称>",
            "/ef 敌人 <名称>",
            "/ef 词条 <名称>",
            "/ef 档案 <名称>",
        ):
            self.assertIn(usage, help_text)
        self.assertIn("只使用 AkeData", commands.format_source())

    def test_choose_candidate_reports_a_numbered_list_within_the_ambiguity_margin(self):
        candidates = [
            commands.EndfieldCandidate("item", "k1", "同名", 90, "akedata"),
            commands.EndfieldCandidate("item", "k2", "同名", 85, "akedata"),
        ]
        selected, ambiguous = commands.choose_candidate(candidates)
        self.assertIsNone(selected)
        self.assertEqual([item.key for item in ambiguous], ["k1", "k2"])
        self.assertIn("1.", commands.format_candidates(ambiguous, interactive=True))


# ------------------------------------------------------------------ §4.1 / §4.4 分类与索引


class EndfieldEncyclopediaClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _SnapshotFixture(self).start()

    async def test_excluded_types_never_reach_the_item_or_prop_index(self):
        item_index = await encyclopedia_index.get_index(self.data, "item")
        prop_index = await encyclopedia_index.get_index(self.data, "prop")
        keys = {entry.key for entry in item_index.entries} | {entry.key for entry in prop_index.entries}
        tables = _tables()["ItemTable"]
        excluded = [
            item_id
            for item_id, row in tables.items()
            if row.get("type") in classify.EXCLUDED_TYPES
        ]
        self.assertTrue(excluded, "夹具必须含被排除类型，否则这条断言没有意义")
        for item_id in excluded:
            self.assertNotIn(item_id, keys)

    async def test_item_equip_prefix_stays_in_the_item_index_when_type_is_whitelisted(self):
        index = await encyclopedia_index.get_index(self.data, "item")
        entry = next(entry for entry in index.entries if entry.key == ITEM_PROBE_MATERIAL)
        self.assertEqual(entry.display_name, "装备用探针材料")
        self.assertEqual(_tables()["ItemTable"][ITEM_PROBE_MATERIAL]["type"], 8)

    async def test_types_outside_the_whitelist_do_not_enter_the_index(self):
        index = await encyclopedia_index.get_index(self.data, "item")
        keys = {entry.key for entry in index.entries}
        tables = _tables()["ItemTable"]
        for item_id in ("item_matrix_1_basic", "item_medal_t1_star", "item_weapon_t3_sword"):
            with self.subTest(item_id=item_id):
                self.assertNotIn(item_id, keys)
                self.assertNotIn(tables[item_id]["type"], classify.ITEM_TYPE_WHITELIST)

    async def test_use_item_ids_only_appear_in_the_prop_index(self):
        item_index = await encyclopedia_index.get_index(self.data, "item")
        prop_index = await encyclopedia_index.get_index(self.data, "prop")
        self.assertNotIn(PROP_RATION, {entry.key for entry in item_index.entries})
        self.assertIn(PROP_RATION, {entry.key for entry in prop_index.entries})

    async def test_item_scope_fallback_picks_a_prop_when_item_candidates_are_empty(self):
        command = commands.ParsedEndfieldCommand("query", scope="item", query="炝炒时蔬")
        fallback = await endfield._item_scope_fallback(command, [])
        self.assertIsInstance(fallback, list)
        self.assertEqual([item.key for item in fallback], [PROP_RATION])

    async def test_item_scope_fallback_is_not_called_when_item_candidates_exist(self):
        command = commands.ParsedEndfieldCommand("query", scope="item", query="苔藓样本")
        existing = [commands.EndfieldCandidate("item", ITEM_MOSS, "苔藓样本", 90, "akedata")]
        with patch.object(encyclopedia_service, "prop_candidates") as prop_candidates:
            fallback = await endfield._item_scope_fallback(command, existing)
        self.assertIsNone(fallback)
        self.assertEqual(prop_candidates.call_count, 0)

    async def test_item_scope_fallback_redirects_medals(self):
        command = commands.ParsedEndfieldCommand("query", scope="item", query="一星蚀刻章")
        fallback = await endfield._item_scope_fallback(command, [])
        self.assertEqual(fallback, "medal")

    async def test_item_obtain_way_is_localized(self):
        # 夹具里唯一带 obtainWayIds 的白名单类型行是战术物品；途径走 SystemJumpTable.desc。
        row = _tables()["ItemTable"][PROP_RATION]
        self.assertEqual(row["obtainWayIds"], ["item_obtain_equip"])
        view = await encyclopedia_items.build_item_view(self.data, PROP_RATION)
        self.assertEqual(view.obtain_ways, ("装备制造",))
        without_ways = await encyclopedia_items.build_item_view(self.data, ITEM_PROBE_MATERIAL)
        self.assertEqual(without_ways.type_name, "材料")
        self.assertEqual(without_ways.obtain_ways, ())

    async def test_item_catalog_candidate_carries_the_snapshot_revision(self):
        index = await encyclopedia_index.get_index(self.data, "item")
        candidates = encyclopedia_service.candidates(index, "item", encyclopedia_service.ALL_QUERY)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].kind, "item_catalog")
        self.assertEqual(candidates[0].key, "")
        self.assertEqual(candidates[0].score, 100)
        self.assertEqual(candidates[0].revision, self.data.revision)

    async def test_type_name_query_adds_a_catalog_candidate_with_reason_type(self):
        index = await encyclopedia_index.get_index(self.data, "item")
        candidates = encyclopedia_service.candidates(index, "item", "材料")
        catalog = [item for item in candidates if item.reason == "type"]
        self.assertEqual([item.kind for item in catalog], ["item_catalog"])
        self.assertEqual(catalog[0].key, "8")
        self.assertEqual(catalog[0].score, 100)

    async def test_prop_buckets_fall_back_to_other(self):
        self.assertEqual(classify.prop_bucket(48), "战术物品")
        self.assertEqual(classify.prop_bucket(52), "消耗品")
        self.assertEqual(classify.prop_bucket(55), "探测器")
        self.assertEqual(classify.prop_bucket(0), classify.PROP_OTHER_BUCKET)


# ------------------------------------------------------------------ §4.3 道具效果


class EndfieldEncyclopediaPropEffectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _SnapshotFixture(self).start()

    def test_placeholder_formats_percent_and_integer(self):
        actions = [
            {
                "buffBBData": {
                    "buffId": "buff_x",
                    "blackboard": [
                        {"key": "a", "value": 0.25, "valueStr": ""},
                        {"key": "b", "value": 3.0, "valueStr": ""},
                    ],
                }
            }
        ]
        lines = encyclopedia_props.render_effect_lines("造成{a:0%}伤害，持续{b:0}秒", actions)
        self.assertEqual(lines, ("造成25%伤害，持续3秒",))

    def test_missing_key_raises_and_never_formats_with_dashes(self):
        actions = [{"buffBBData": {"buffId": "buff_x", "blackboard": [{"key": "a", "value": 1.0}]}}]
        with self.assertRaises(encyclopedia_props.PropEffectIncomplete) as caught:
            encyclopedia_props.render_effect_lines("造成{a:0}点伤害，持续{b:0}秒", actions)
        self.assertNotIn("--", str(caught.exception))

    def test_two_partial_actions_fail_instead_of_merging_the_blackboards(self):
        actions = [
            {"buffBBData": {"buffId": "b1", "blackboard": [{"key": "a", "value": 1.0}]}},
            {"buffBBData": {"buffId": "b2", "blackboard": [{"key": "b", "value": 2.0}]}},
        ]
        with self.assertRaises(encyclopedia_props.PropEffectIncomplete):
            encyclopedia_props.render_effect_lines("造成{a:0}点伤害，持续{b:0}秒", actions)

    def test_null_value_with_a_value_str_still_fails(self):
        row = _tables()["UseItemTable"][PROP_BROKEN]
        blackboard = row["useActions"][0]["buffBBData"]["blackboard"]
        self.assertTrue(any(entry.get("value") is None for entry in blackboard))
        with self.assertRaises(encyclopedia_props.PropEffectIncomplete) as caught:
            encyclopedia_props.render_effect_lines(
                _tables()["I18nTextTable_CN"].get(
                    str(row["itemUseDesc"]["id"]), row["itemUseDesc"]["text"]
                ),
                row["useActions"],
            )
        self.assertNotIn("--", str(caught.exception))

    def test_extract_blackboard_reads_value_only(self):
        rows = [
            {"key": "value", "value": 0.25, "valueStr": "9"},
            {"key": "count", "value": None, "valueStr": "3"},
            {"key": "", "value": 1.0},
            {"key": "flag", "value": True},
        ]
        self.assertEqual(encyclopedia_props.extract_blackboard(rows), {"value": 0.25})

    async def test_effect_uses_the_action_blackboard_and_keeps_link_markup(self):
        view = await encyclopedia_props.build_prop_view(self.data, PROP_RATION)
        self.assertEqual(view.effect_lines, ("恢复25%生命，并获得3层护盾",))
        self.assertEqual(view.type_name, "战术物品")
        self.assertEqual(view.bucket, "战术物品")

    async def test_equip_row_fills_cooldown_and_a_missing_row_leaves_none(self):
        with_equip = await encyclopedia_props.build_prop_view(self.data, PROP_RATION)
        self.assertEqual(with_equip.cooldown, 3.0)
        self.assertEqual(with_equip.cast_time, 2.0)
        self.assertEqual(with_equip.charge_count, 3)
        self.assertEqual(with_equip.recover_upper_count, 1)
        without_equip = await encyclopedia_props.build_prop_view(self.data, PROP_MEDKIT)
        self.assertIsNone(without_equip.cooldown)
        self.assertIsNone(without_equip.cast_time)
        self.assertEqual(without_equip.bucket, "消耗品")

    async def test_zero_duration_is_stored_as_none(self):
        self.assertEqual(_tables()["UseItemTable"][PROP_RATION]["duration"], 0.0)
        view = await encyclopedia_props.build_prop_view(self.data, PROP_RATION)
        self.assertIsNone(view.duration)

    async def test_incomplete_prop_effect_never_reaches_the_drawer(self):
        drawer = AsyncMock(return_value=b"png")
        candidate = commands.EndfieldCandidate(
            "prop", PROP_BROKEN, "半成品口粮", 100, "akedata", revision=self.data.revision
        )
        with patch.object(endfield.encyclopedia_draw, "draw_prop_card", drawer):
            with self.assertRaises(encyclopedia_props.PropEffectIncomplete):
                await endfield._render_candidate(candidate, "akedata")
        self.assertEqual(drawer.await_count, 0)


# ------------------------------------------------------------------ §4.4 索引缓存


class EndfieldEncyclopediaIndexCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _SnapshotFixture(self).start()

    async def test_same_revision_reuses_the_same_index_object(self):
        first = await encyclopedia_index.get_index(self.data, "item")
        second = await encyclopedia_index.get_index(self.data, "item")
        self.assertIs(first, second)

    async def test_different_shared_revision_builds_its_own_index(self):
        first = await encyclopedia_index.get_index(self.data, "item")
        other = _snapshot(shared="fixture-2")
        second = await encyclopedia_index.get_index(other, "item")
        self.assertIsNot(first, second)
        self.assertEqual(first.revision, "1.5.3@9885010-4|fixture")
        self.assertEqual(second.revision, "1.5.3@9885010-4|fixture-2")

    async def test_item_index_does_not_read_skill_patch_table(self):
        tables = _tables()
        tables.pop("SkillPatchTable")
        snapshot = AkeSnapshot("1.5.3@9885010-4", "public/1.5.3/9885010-4/TableCfg", "no-skill-patch")
        snapshot._tables = tables
        index = await encyclopedia_index.get_index(snapshot, "item")
        self.assertEqual(len(index.entries), 2)

    async def test_supported_kinds_cover_the_four_ake_encyclopedia_kinds(self):
        self.assertEqual(
            set(encyclopedia_index.supported_kinds()), {"item", "prop", "enemy", "term"}
        )


# ------------------------------------------------------------------ §4.7 别名写入


class EndfieldEncyclopediaAliasTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "alias_data.json"
        self.path.write_text(json.dumps(_EMPTY_ALIAS_DATA, ensure_ascii=False), encoding="utf-8")
        self.patcher = patch.object(catalog_aliases, "ALIAS_DATA_PATH", self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(catalog_aliases.clear_alias_caches)
        catalog_aliases.clear_alias_caches()

    def test_lookup_creates_the_list_for_a_name_that_is_not_in_the_file(self):
        canonical, added = catalog_aliases.add_alias(
            "prop", "炝炒时蔬", "炒菜", lookup=lambda query: ("炝炒时蔬",)
        )
        self.assertEqual((canonical, added), ("炝炒时蔬", True))
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["prop"], {"炝炒时蔬": ["炒菜"]})
        catalog_aliases.clear_alias_caches()
        self.assertEqual(catalog_aliases.aliases_for("prop", "炝炒时蔬"), ("炒菜",))

    def test_adding_the_same_alias_twice_reports_no_change(self):
        lookup = lambda query: ("炝炒时蔬",)  # noqa: E731
        catalog_aliases.add_alias("prop", "炝炒时蔬", "炒菜", lookup=lookup)
        self.assertEqual(
            catalog_aliases.add_alias("prop", "炝炒时蔬", "炒菜", lookup=lookup),
            ("炝炒时蔬", False),
        )

    def test_empty_lookup_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "别名库中不存在正式名称"):
            catalog_aliases.add_alias("prop", "新名", "x", lookup=lambda query: ())

    def test_missing_lookup_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "别名库中不存在正式名称"):
            catalog_aliases.add_alias("prop", "新名", "x", lookup=None)

    def test_two_lookup_matches_ask_for_the_full_name(self):
        with self.assertRaisesRegex(ValueError, "正式名称不唯一"):
            catalog_aliases.add_alias("prop", "新名", "x", lookup=lambda query: ("甲", "乙"))

    def test_alias_equal_to_the_canonical_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "新别名不能与正式名称相同"):
            catalog_aliases.add_alias(
                "prop", "炝炒时蔬", "炝炒时蔬", lookup=lambda query: ("炝炒时蔬",)
            )

    def test_supported_kinds_include_the_five_encyclopedia_kinds(self):
        self.assertEqual(
            catalog_aliases.ENCYCLOPEDIA_KINDS,
            frozenset({"item", "prop", "enemy", "term", "archive_entry"}),
        )
        self.assertTrue(
            catalog_aliases.ENCYCLOPEDIA_KINDS <= catalog_aliases.SUPPORTED_KINDS
        )


class EndfieldEncyclopediaAliasHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _SnapshotFixture(self).start()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "alias_data.json"
        path.write_text(json.dumps(_EMPTY_ALIAS_DATA, ensure_ascii=False), encoding="utf-8")
        patcher = patch.object(catalog_aliases, "ALIAS_DATA_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(catalog_aliases.clear_alias_caches)
        catalog_aliases.clear_alias_caches()
        self.alias_path = path

    async def test_item_alias_uses_the_index_as_its_lookup(self):
        command = commands.ParsedEndfieldCommand(
            "alias", alias_action="add", args=("物品", "苔藓样本", "苔藓")
        )
        message = await endfield._handle_alias_command(command)
        self.assertEqual(message, "已添加物品别名：苔藓 → 苔藓样本")

    async def test_item_alias_rejects_a_name_that_is_not_in_the_index(self):
        command = commands.ParsedEndfieldCommand(
            "alias", alias_action="add", args=("物品", "不存在的物品", "x")
        )
        message = await endfield._handle_alias_command(command)
        self.assertIn("添加别名失败", message)
        self.assertIn("别名库中不存在正式名称", message)

    async def test_archive_entry_alias_uses_the_archive_snapshot_as_its_lookup(self):
        with patch.object(
            endfield.archive_store, "load_current_view", return_value=_archive_snapshot()
        ):
            command = commands.ParsedEndfieldCommand(
                "alias", alias_action="add", args=("档案", "终末地物资", "终末地")
            )
            message = await endfield._handle_alias_command(command)
        self.assertEqual(message, "已添加档案条目别名：终末地 → 终末地物资")

    async def test_archive_entry_alias_is_not_reported_as_unavailable(self):
        with patch.object(
            endfield.archive_store, "load_current_view", return_value=_archive_snapshot()
        ):
            command = commands.ParsedEndfieldCommand(
                "alias", alias_action="add", args=("档案", "不存在条目", "x")
            )
            message = await endfield._handle_alias_command(command)
        self.assertNotIn("尚未开放", message)
        self.assertIn("别名库中不存在正式名称", message)


# ------------------------------------------------------------------ §5 敌人


class EndfieldEncyclopediaEnemyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _SnapshotFixture(self).start()

    async def test_two_instances_sharing_one_attribute_template_yield_one_resistance_set(self):
        view = await encyclopedia_enemies.build_enemy_view(self.data, ENEMY_PROBE)
        self.assertEqual(view.resistance_template_count, 1)
        self.assertEqual([item.element for item in view.resistances], ["Physical", "Fire", "Pulse", "Cryst", "Natural"])
        self.assertEqual(view.resistances[1].percent, 90.0)
        self.assertEqual(view.resistances[1].color, "FF623D")
        self.assertNotIn("#", view.resistances[1].color)
        self.assertEqual(view.variant_count, 2)

    async def test_empty_attr_template_falls_back_to_the_template_id(self):
        enemies = _tables()["EnemyTable"]
        self.assertEqual(enemies["eny_0001_probe_b"]["attrTemplateId"], "")
        self.assertEqual(enemies["eny_0001_probe_b"]["templateId"], ENEMY_PROBE)
        view = await encyclopedia_enemies.build_enemy_view(self.data, ENEMY_PROBE)
        self.assertEqual(view.resistance_template_count, 1)

    async def test_a_second_attribute_template_clears_the_resistances(self):
        view = await encyclopedia_enemies.build_enemy_view(self.data, ENEMY_SHIELD)
        self.assertEqual(view.resistance_template_count, 2)
        self.assertEqual(view.resistances, ())
        self.assertEqual(view.variant_count, 2)

    async def test_nameless_ability_keeps_its_description(self):
        view = await encyclopedia_enemies.build_enemy_view(self.data, ENEMY_SHIELD)
        self.assertEqual(len(view.abilities), 1)
        self.assertEqual(view.abilities[0].name, "")
        self.assertEqual(view.abilities[0].description, "举盾时受到的物理伤害降低。")

    async def test_abilities_keep_the_declared_order(self):
        view = await encyclopedia_enemies.build_enemy_view(self.data, ENEMY_PROBE)
        self.assertEqual([item.name for item in view.abilities], ["探测波", ""])
        self.assertEqual(view.abilities[0].description, "周期性发射探测波。")

    async def test_display_type_falls_back_to_type_number(self):
        self.assertEqual(encyclopedia_enemies.DISPLAY_TYPE_NAMES, {})
        self.assertEqual(encyclopedia_enemies.display_type_name(0), "类型0")
        self.assertEqual(encyclopedia_enemies.display_type_name(None), "类型未知")
        view = await encyclopedia_enemies.build_enemy_view(self.data, ENEMY_PROBE)
        self.assertEqual((view.display_type, view.display_type_name), (0, "类型0"))

    async def test_distributions_are_localized_and_counted_by_raw_ids(self):
        view = await encyclopedia_enemies.build_enemy_view(self.data, ENEMY_PROBE)
        self.assertEqual(view.distributions, ("北部禁区",))
        self.assertEqual(view.distribution_count, 1)
        shield = await encyclopedia_enemies.build_enemy_view(self.data, ENEMY_SHIELD)
        self.assertEqual(shield.distributions, ("北部禁区", "通用副本"))
        self.assertEqual(shield.distribution_count, 2)

    async def test_index_groups_by_display_type_and_keeps_the_fixture_small(self):
        tables = _tables()
        self.assertLessEqual(len(tables["EnemyTable"]), 8)
        index = await encyclopedia_index.get_index(self.data, "enemy")
        self.assertEqual([entry.key for entry in index.entries], [ENEMY_PROBE, ENEMY_SHIELD])
        self.assertEqual(index.types, (("0", "类型0"), ("1", "类型1")))

    async def test_enemy_candidate_scores_by_name_and_template_id(self):
        index = await encyclopedia_index.get_index(self.data, "enemy")
        by_name = encyclopedia_service.candidates(index, "enemy", "游荡探针")
        self.assertEqual([item.key for item in by_name], [ENEMY_PROBE])
        by_id = encyclopedia_service.candidates(index, "enemy", ENEMY_PROBE)
        self.assertEqual(by_id[0].score, 100)


# ------------------------------------------------------------------ §6 词条


class EndfieldEncyclopediaTermTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _SnapshotFixture(self).start()

    async def test_short_name_hits_the_main_entry_and_full_name_opens_the_companion(self):
        index = await encyclopedia_index.get_index(self.data, "term")
        short = encyclopedia_service.candidates(index, "term", "燃烧")
        self.assertEqual([item.key for item in short], [TERM_BURNING])
        full = encyclopedia_service.candidates(index, "term", "干员受到燃烧")
        self.assertEqual([item.key for item in full], ["ba.burningonchar"])
        self.assertEqual(full[0].score, 100)

    async def test_companion_entries_are_hidden_unless_exactly_named(self):
        index = await encyclopedia_index.get_index(self.data, "term")
        entry = next(item for item in index.entries if item.key == "ba.burningonchar")
        self.assertFalse(entry.listed)
        self.assertTrue(classify.term_listed("法术异常 - 燃烧"))

    async def test_related_contains_the_companion_and_both_tag_forms(self):
        view = await encyclopedia_terms.build_term_view(self.data, TERM_BURNING)
        self.assertIn("ba.burningonchar", view.related)
        self.assertIn("ba.fire", view.related)

    async def test_hash_tag_references_also_enter_related(self):
        view = await encyclopedia_terms.build_term_view(self.data, "ba.spellstatus")
        self.assertTrue(view.related)
        for term_id in view.related:
            self.assertTrue(term_id.startswith("ba."))

    async def test_companion_of_an_unknown_stem_falls_to_inflict(self):
        existing = {str(key) for key in _tables()["HyperlinkTextTable"]}
        self.assertEqual(
            encyclopedia_terms.classify.term_companion("ba.crystonchar", existing),
            "ba.crystinflict",
        )
        self.assertEqual(encyclopedia_terms.classify.term_companion("ba.burningonchar", existing), TERM_BURNING)

    async def test_empty_rich_text_id_lands_in_the_other_group(self):
        index = await encyclopedia_index.get_index(self.data, "term")
        others = [entry for entry in index.entries if entry.group == ""]
        self.assertTrue(others)
        self.assertEqual(index.type_name(""), "其他")
        self.assertEqual(index.types[-1], ("", "其他"))

    async def test_fire_color_comes_from_rich_text_visual(self):
        styles = _tables()["RichTextStyleTable"]
        self.assertEqual(_rich_text_visual(styles["ba.fire"]), ("#ff8e59", ""))
        view = await encyclopedia_terms.build_term_view(self.data, TERM_BURNING)
        self.assertEqual(view.color, "#ff8e59")
        self.assertEqual((view.family_id, view.family), ("ba.fire", "灼热"))

    async def test_term_sources_are_built_once_per_revision(self):
        encyclopedia_terms.clear_caches()
        original = encyclopedia_terms._build_sources
        calls = []

        async def counted(snapshot):
            calls.append(snapshot.revision)
            return await original(snapshot)

        with patch.object(encyclopedia_terms, "_build_sources", counted):
            first = await encyclopedia_terms.term_sources(self.data)
            second = await encyclopedia_terms.term_sources(self.data)
        self.assertEqual(calls, [self.data.revision])
        self.assertIs(first, second)

    async def test_burning_sources_include_an_operator_and_a_weapon_skill(self):
        sources = await encyclopedia_terms.term_sources(self.data)
        names = {name for name, _ in sources[TERM_BURNING]}
        self.assertIn("莱万汀", names)
        weapon_sourced = {name for name, _ in sources["ba.vup"]}
        self.assertTrue({"塔尔11", "熔铸火焰", "寒夜幽影"} & weapon_sourced)
        view = await encyclopedia_terms.build_term_view(self.data, TERM_BURNING)
        self.assertEqual(view.source_total, len(sources[TERM_BURNING]))
        self.assertEqual([(item.name, item.skill) for item in view.sources], list(sources[TERM_BURNING]))

    async def test_source_limit_keeps_the_first_twelve_entries(self):
        self.assertEqual(encyclopedia_terms.SOURCE_LIMIT, 12)
        self.assertGreater(len(_tables()["HyperlinkTextTable"]), 12)

    async def test_term_html_has_no_image_tag_level_or_multiplier(self):
        view = await encyclopedia_terms.build_term_view(self.data, TERM_BURNING)
        document = endfield.encyclopedia_draw.render_term_html(view, {})
        for forbidden in ("<image=", "Lv1", "倍率", "源石技艺"):
            self.assertNotIn(forbidden, document)
        self.assertIn("燃烧", document)


# ------------------------------------------------------------------ §7 档案条目


class EndfieldEncyclopediaArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _SnapshotFixture(self).start()
        self.archive = patch.object(
            endfield.archive_store, "load_current_view", return_value=_archive_snapshot()
        )
        self.archive.start()
        self.addCleanup(self.archive.stop)
        self.addCleanup(endfield.encyclopedia_archives.clear_caches)

    async def test_archive_entry_is_never_wrapped_in_an_ake_snapshot(self):
        self.assertNotIn("archive_entry", endfield._AKE_SNAPSHOT_KINDS)
        self.assertNotIn("archive_entry", endfield._FZ_WHOLE_VIEW_FALLBACK_KINDS)

    async def test_archive_candidates_carry_the_archive_version(self):
        candidates = await endfield._resolve_archive_entry_candidates("终末地物资")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].kind, "archive_entry")
        self.assertEqual(candidates[0].revision, "1.5")
        self.assertEqual(candidates[0].source, "akedata")

    async def test_archive_candidates_are_empty_without_a_snapshot(self):
        with patch.object(endfield.archive_store, "load_current_view", return_value=None):
            self.assertEqual(await endfield._resolve_archive_entry_candidates("终末地物资"), [])

    async def test_missing_snapshot_sends_a_refresh_hint_for_the_explicit_scope(self):
        matcher = AsyncMock()
        with patch.object(endfield.archive_store, "load_current_view", return_value=None):
            await endfield._handle_command(
                matcher,
                None,
                commands.ParsedEndfieldCommand("query", scope="archive_entry", query="终末地"),
            )
        matcher.finish.assert_awaited_once_with("档案资料尚未就绪，先发送 /ef 档案 刷新")

    async def test_missing_snapshot_is_silent_for_the_all_scope(self):
        matcher = AsyncMock()
        with patch.object(endfield.archive_store, "load_current_view", return_value=None):
            await endfield._handle_command(
                matcher, None, commands.ParsedEndfieldCommand("query", scope="all", query="zzz")
            )
        matcher.finish.assert_awaited_once_with("未找到内容：zzz\n可以尝试 /ef 搜索 zzz")

    async def test_archive_card_renders_without_opening_an_ake_snapshot(self):
        drawer = AsyncMock(return_value=b"png")
        candidate = commands.EndfieldCandidate(
            "archive_entry", "nar_1", "终末地物资", 100, "akedata", revision="1.5"
        )
        with patch.object(repository, "snapshot", side_effect=AssertionError("must not open AkeSnapshot")):
            with patch.object(endfield.encyclopedia_draw, "draw_archive_entry_card", drawer):
                pages = await endfield._render_candidate(candidate, "")
        self.assertEqual(pages, (b"png",))
        self.assertEqual(drawer.await_count, 1)
        view = drawer.await_args.args[0]
        self.assertEqual((view.item_id, view.name, view.version), ("nar_1", "终末地物资", "1.5"))


# ------------------------------------------------------------------ §4.8 handler 接线与三态


class EndfieldEncyclopediaWiringTests(unittest.TestCase):
    def test_registries_are_consistent(self):
        for kind in endfield.CONTENT_RESOLVERS:
            with self.subTest(kind=kind):
                self.assertIn(kind, commands.SCOPE_LABELS)
                self.assertIn(kind, endfield.CONTENT_RENDERERS)
                self.assertTrue(endfield.source_order(kind))

    def test_fz_whole_view_fallback_is_a_subset_of_the_ake_snapshot_kinds(self):
        self.assertTrue(
            endfield._FZ_WHOLE_VIEW_FALLBACK_KINDS <= endfield._AKE_SNAPSHOT_KINDS
        )

    def test_every_ake_snapshot_kind_has_a_renderer(self):
        self.assertTrue(endfield._AKE_SNAPSHOT_KINDS <= set(endfield.CONTENT_RENDERERS))

    def test_catalogs_are_renderable_but_not_resolvable(self):
        for kind in ("item_catalog", "prop_catalog", "enemy_catalog", "term_catalog"):
            with self.subTest(kind=kind):
                self.assertIn(kind, endfield.CONTENT_RENDERERS)
                self.assertNotIn(kind, endfield.CONTENT_RESOLVERS)

    def test_encyclopedia_kinds_only_declare_akedata(self):
        for kind in ("item", "prop", "enemy", "term"):
            with self.subTest(kind=kind):
                self.assertEqual(set(endfield.SOURCE_CANDIDATE_RESOLVERS[kind]), {"akedata"})
        self.assertNotIn("archive_entry", endfield.SOURCE_CANDIDATE_RESOLVERS)
        self.assertEqual(endfield.source_order("archive_entry"), ("akedata",))

    def test_encyclopedia_scopes_are_registered_in_the_command_tables(self):
        for scope in commands.ENCYCLOPEDIA_SCOPES:
            with self.subTest(scope=scope):
                self.assertIn(scope, commands.SCOPE_LABELS)
                self.assertIn(scope, commands.ENCYCLOPEDIA_SCOPE_ALIASES)


class EndfieldEncyclopediaSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _SnapshotFixture(self).start()

    async def test_explicit_unsupported_source_is_rejected_before_collecting(self):
        matcher = AsyncMock()
        await endfield._handle_command(matcher, None, commands.parse_command("道具 xx --source fz"))
        matcher.finish.assert_awaited_once_with("该类资料只提供 AkeData")

    async def test_bare_source_option_is_not_rejected_by_the_encyclopedia_guard(self):
        matcher = AsyncMock()
        await endfield._handle_command(matcher, None, commands.parse_command("xx --source fz"))
        message = matcher.finish.await_args.args[0]
        self.assertNotIn("该类资料只提供 AkeData", message)

    async def test_fz_success_with_empty_list_beats_a_warfarin_failure(self):
        resolvers = {
            "fz": AsyncMock(return_value=[]),
            "warfarin": AsyncMock(side_effect=WarfarinAPIError("offline")),
        }
        with patch.dict(endfield.SOURCE_CANDIDATE_RESOLVERS, {"operator": resolvers}):
            candidates = await endfield._resolve_candidates_from_sources("operator", "q")
        self.assertEqual(candidates, [])

    async def test_every_source_failing_raises_the_last_error(self):
        resolvers = {
            "fz": AsyncMock(side_effect=WarfarinAPIError("first")),
            "warfarin": AsyncMock(side_effect=WarfarinAPIError("second")),
        }
        with patch.dict(endfield.SOURCE_CANDIDATE_RESOLVERS, {"operator": resolvers}):
            with self.assertRaisesRegex(WarfarinAPIError, "second"):
                await endfield._resolve_candidates_from_sources("operator", "q")

    async def test_akedata_failure_with_an_empty_fz_list_is_not_reported_as_unavailable(self):
        resolvers = {
            "akedata": AsyncMock(side_effect=AkeDataIncomplete("missing")),
            "fz": AsyncMock(return_value=[]),
        }
        with patch.dict(endfield.SOURCE_CANDIDATE_RESOLVERS, {"operator": resolvers}):
            candidates = await endfield._resolve_candidates_from_sources("operator", "q")
        self.assertEqual(candidates, [])

    async def test_scope_all_raises_when_the_only_akedata_kind_is_incomplete(self):
        async def resolve(kind, query, source="", rarity=""):
            if kind == "stage":
                raise AkeDataIncomplete("stage table missing")
            return []

        with patch.object(endfield, "_resolve_candidates_from_sources", resolve):
            with self.assertRaises(AkeDataIncomplete):
                await endfield._collect_candidates("all", "q")

    async def test_akedata_incomplete_reaches_the_user_as_unavailable(self):
        matcher = AsyncMock()
        with patch.object(
            endfield,
            "_resolve_candidates_from_sources",
            AsyncMock(side_effect=AkeDataIncomplete("missing")),
        ):
            await endfield._handle_command(matcher, None, commands.parse_command("物品 苔藓样本"))
        matcher.finish.assert_awaited_once_with("资料暂时不可用")


# ------------------------------------------------------------------ §4.9 渲染


class EndfieldEncyclopediaRenderTests(unittest.IsolatedAsyncioTestCase):
    def _resource(self, url: str):
        from otae_bot.infrastructure.http.client import HttpResource

        return HttpResource(b"png-bytes", "image/png", 200, url)

    async def _render_with_icon(self, *, icon_available: bool):
        from plugins.endfield.rendering import cards

        data = _snapshot()

        async def fetch_many(urls, **kwargs):
            resources = {url: (self._resource(url) if icon_available else None) for url in urls}
            return resources, {}

        with patch.object(repository, "snapshot", AsyncMock(return_value=data)):
            with patch.object(cards, "fetch_many_resilient", fetch_many):
                with patch.object(
                    endfield.encyclopedia_draw,
                    "screenshot_web_element",
                    AsyncMock(return_value=b"png-bytes"),
                ):
                    await endfield._CARD_CACHE.clear()
                    self.addCleanup(endfield._CARD_CACHE.clear)
                    # 命中计数是累计值，用增量判断这一条有没有写成品缓存。
                    before = (await endfield._CARD_CACHE.stats()).direct_hits
                    candidate = commands.EndfieldCandidate(
                        "item", ITEM_MOSS, "苔藓样本", 100, "akedata", revision=data.revision
                    )
                    first = await endfield._render_candidate(candidate, "akedata")
                    second = await endfield._render_candidate(candidate, "akedata")
                    after = (await endfield._CARD_CACHE.stats()).direct_hits
        return first, second, after - before

    async def test_item_card_renders_and_complete_results_are_cached(self):
        first, second, hits = await self._render_with_icon(icon_available=True)
        self.assertEqual(first, (b"png-bytes",))
        self.assertEqual(second, (b"png-bytes",))
        self.assertEqual(hits, 1)

    async def test_missing_icon_still_returns_pages_but_is_not_cached(self):
        first, second, hits = await self._render_with_icon(icon_available=False)
        self.assertEqual(first, (b"png-bytes",))
        self.assertEqual(second, (b"png-bytes",))
        self.assertEqual(hits, 0)

    async def test_four_card_renderers_are_registered(self):
        for kind in ("item", "prop", "enemy", "term"):
            with self.subTest(kind=kind):
                self.assertIn(kind, endfield.CONTENT_RENDERERS)
        self.assertIn("archive_entry", endfield.CONTENT_RENDERERS)

    async def test_catalog_drawer_returns_a_tuple_of_pages(self):
        from plugins.endfield.encyclopedia import draw as encyclopedia_draw

        with patch.object(
            encyclopedia_draw, "draw_catalog_card", AsyncMock(return_value=b"page")
        ):
            view = encyclopedia_service.build_archive_catalog(
                endfield.encyclopedia_archives.build_entries(_archive_snapshot()), "1.5"
            )
            pages = await encyclopedia_draw.draw_catalog_cards(view)
        self.assertEqual(pages, (b"page",))
        self.assertEqual(encyclopedia_draw.CATALOG_PAGE_BUDGETS, (24, 18, 12, 6))

    async def test_archive_entry_html_uses_only_snapshot_fields(self):
        from plugins.endfield.encyclopedia import draw as encyclopedia_draw

        entry = endfield.encyclopedia_archives.entry_view(_archive_snapshot(), "nar_1")
        document = encyclopedia_draw.render_archive_entry_html(entry, {})
        self.assertIn("终末地物资", document)
        self.assertIn("中枢档案", document)
        self.assertNotIn("描述", document)


# ------------------------------------------------------------------ §2.1 / §4.0 报告与帮助图


class EndfieldEncyclopediaReportTests(unittest.TestCase):
    def test_report_encyclopedia_makes_no_request(self):
        from scripts import inspect_endfield_akedata

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(
                json.dumps({"latest": "1.5.3@fixture"}), encoding="utf-8"
            )
            version_dir = root / "1.5.3@fixture"
            version_dir.mkdir()
            tables = _tables()
            for name in inspect_endfield_akedata.REPORT_TABLES:
                self.assertIn(name, tables, f"夹具缺少报告需要的表 {name}")
                (version_dir / f"{name}.json").write_text(
                    json.dumps(tables[name], ensure_ascii=False), encoding="utf-8"
                )
            buffer = io.StringIO()
            with patch("httpx.AsyncClient", side_effect=AssertionError("report must not build a client")):
                with contextlib.redirect_stdout(buffer):
                    exit_code = inspect_endfield_akedata.report_encyclopedia(root)
        self.assertEqual(exit_code, 0)
        output = buffer.getvalue()
        self.assertIn("ItemTypeTable", output)
        self.assertIn("EnemyTemplateDisplayInfoTable", output)
        self.assertIn("UseItemTable", output)

    def test_report_refuses_to_print_a_half_report(self):
        from scripts import inspect_endfield_akedata

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(
                json.dumps({"latest": "1.5.3@fixture"}), encoding="utf-8"
            )
            (root / "1.5.3@fixture").mkdir()
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = inspect_endfield_akedata.report_encyclopedia(root)
        self.assertEqual(exit_code, 1)
        self.assertIn("缺少表", buffer.getvalue())


class EndfieldEncyclopediaHelpTests(unittest.TestCase):
    """帮助图由 `scripts/render_help_cards.py` 从 `help_pages.json` 出图。

    实施文档 §4.0 写的是独立脚本 `render_endfield_help.py` 与 1075/RGBA；仓库在
    那之后合并了通用的 help_cards 框架（宽度 1325，`finalize_png` 固定转 RGB），
    出图来源与「宽度固定、高度随正文生长」这两条要求不变，故此处按框架的常量断言。
    """

    def test_shipped_help_image_matches_the_rendered_theme(self):
        from PIL import Image

        from otae_bot.infrastructure.rendering import help_cards

        theme = help_cards.HelpTheme()
        self.assertTrue(HELP_IMAGE.is_file(), "帮助图缺失，请跑 scripts/render_help_cards.py --write")
        with Image.open(HELP_IMAGE) as image:
            self.assertEqual(image.width, theme.page_width)
            self.assertGreaterEqual(image.height, 761)
            self.assertLessEqual(image.height, theme.max_height)

    def test_help_page_covers_the_encyclopedia_commands(self):
        from otae_bot.infrastructure.rendering import help_cards

        page = next(page for page in help_cards.load_pages() if page.id == "endfield")
        entries = {
            item.command: item
            for section in help_cards.iter_sections(page)
            for item in section.items
        }
        for command in (
            "/ef 物品 [名称]",
            "/ef 道具 [名称]",
            "/ef 敌人 [名称]",
            "/ef 词条 [名称]",
            "/ef 档案 <名称>",
        ):
            self.assertIn(command, entries)


if __name__ == "__main__":
    unittest.main()
