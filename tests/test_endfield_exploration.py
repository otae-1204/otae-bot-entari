from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from lxml import html

from plugins.endfield import handlers
from plugins.endfield.account.client import EndfieldAPIError
from plugins.endfield.account.exploration import draw, version
from plugins.endfield.account.exploration.models import (
    CollectionProgress,
    ExplorationRegion,
)
from plugins.endfield.account.exploration.service import build_exploration_view
from plugins.endfield.account.exploration.thumbnails import (
    MapThumbnail,
    MapTile,
    match_thumbnails,
)
from plugins.endfield.account.store import EndfieldRole, EndfieldStore
from plugins.endfield.catalog.commands import parse_command
from plugins.endfield.providers.akedata import AKEDATA_TIMEOUT_SECONDS

FIXTURE = Path(__file__).parent / "fixtures/endfield/exploration/screenshot.json"


def detail_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["detail"]


def build(detail=None):
    return build_exploration_view(
        detail_fixture() if detail is None else detail, uid="****4321", server_name="1"
    )


def production_detail():
    """Simulate card/detail as served: domain_N IDs, English or bare-ID names."""

    def level(level_id, name):
        return {
            "levelId": level_id,
            "name": name,
            "trchestCount": {"count": 1, "total": 2},
        }

    return {
        "domain": [
            {
                "domainId": "domain_1",
                "name": "Valley IV",
                "levels": [
                    level("map01_lv001", "The Hub"),
                    level("map01_lv002", "Valley Pass"),
                ],
            },
            {
                "domainId": "domain_2",
                "name": {"en": "Wuling"},
                "levels": [
                    level("indie_dg007", "indie_dg007"),
                    level("map02_lv005", "Test Area"),
                    level("indie_dg016", "indie_dg016"),
                    level("map02_lv099", "Frontier Pass"),
                ],
            },
            {
                "domainId": "domain_3",
                "name": "New Domain",
                "levels": [level("map03_lv001", "New Level")],
            },
        ]
    }


class ExplorationParsingTests(unittest.TestCase):
    def test_aliases_and_account_selectors(self):
        for text in (
            "探索",
            "地区探索",
            "探索统计",
            "explore",
            "EXPLORATION",
            "账号 探索",
        ):
            with self.subTest(text=text):
                command = parse_command(text)
                self.assertEqual(
                    (command.action, command.account_selector), ("exploration", "")
                )
        for selector in ("2", "4321", "测试 管理员"):
            command = parse_command(f"探索 {selector}")
            self.assertEqual(
                (command.action, command.account_selector), ("exploration", selector)
            )
        self.assertEqual(parse_command("账号 探索 2").account_selector, "2")

    def test_screenshot_keeps_all_regions_and_six_column_order(self):
        view = build()
        # The API lists the oldest map first; the card reverses that, newest on top,
        # and keeps each region's own level order.
        domains = detail_fixture()["domain"]
        self.assertEqual([domain["name"] for domain in domains], ["四号谷地", "武陵"])
        self.assertEqual([region.name for region in view.regions], ["武陵", "四号谷地"])
        for region, domain in zip(view.regions, reversed(domains)):
            self.assertEqual(
                [level.level_id for level in region.levels],
                [level["levelId"] for level in domain["levels"]],
            )
        self.assertEqual(view.level_count, 13)
        self.assertEqual(view.regions[0].levels[3].name, "应龙关")
        self.assertEqual(
            [value.count for value in view.regions[0].levels[3].collections],
            [26, 16, 4, 0, 2, 8],
        )
        self.assertEqual(
            [value.total for value in view.regions[0].levels[3].collections],
            [26, 16, 4, 0, 2, 8],
        )
        self.assertEqual(view.regions[-1].levels[-1].name, "矿脉源区")

    def test_production_ids_take_packaged_chinese_names_newest_map_first(self):
        view = build(production_detail())
        self.assertEqual(
            [(region.region_id, region.name) for region in view.regions],
            [
                ("domain_3", "New Domain"),
                ("domain_2", "武陵"),
                ("domain_1", "四号谷地"),
            ],
        )
        self.assertEqual(
            [[level.name for level in region.levels] for region in view.regions],
            [
                # Absent from the packaged artwork: the API's own text stays.
                ["New Level"],
                ["首墩内部", "试验园区", "遂明", "Frontier Pass"],
                ["枢纽区", "谷地通道"],
            ],
        )
        # A parent without a known ID still takes the one map its levels share.
        detail = production_detail()
        detail["domain"][1]["domainId"] = "domain_wuling"
        self.assertEqual(build(detail).regions[1].name, "武陵")
        # Levels from two different maps name no parent.
        detail["domain"][1]["levels"].append({"levelId": "map01_lv003"})
        self.assertEqual(build(detail).regions[1].name, "Wuling")
        self.assertEqual(build(detail).regions[1].levels[-1].name, "阿伯莉采石场")
        doc = html.fromstring(draw.render_exploration_html(build(production_detail())))
        self.assertEqual(
            doc.xpath('//tr[@class="group"]//b/text()'),
            ["New Domain", "武陵", "四号谷地"],
        )
        self.assertEqual(
            [node.get("style") for node in doc.xpath("//tbody")],
            [
                "--region-color:#ffd000",
                "--region-color:#6bffff",
                "--region-color:#c1ff55",
            ],
        )

    def test_future_regions_and_names_are_not_hardcoded(self):
        detail = {
            "domain": [
                {
                    "domainId": "future",
                    "name": {"zh": "新地区", "en": "New"},
                    "levels": [
                        {
                            "levelId": "new",
                            "name": {"zh-CN": "新区域"},
                            "trchestCount": {"count": "4", "total": "12"},
                        },
                    ],
                }
            ]
        }
        view = build(detail)
        self.assertEqual(view.regions[0].name, "新地区")
        self.assertEqual(view.regions[0].levels[0].name, "新区域")
        self.assertEqual(
            view.regions[0].levels[0].collections[0], CollectionProgress(4, 12)
        )

    def test_zero_total_missing_field_and_zero_collected_remain_distinct(self):
        detail = {
            "domain": [
                {
                    "levels": [
                        {
                            "levelId": "a",
                            "trchestCount": {"count": 0, "total": 10},
                            "puzzleCount": {"count": 0, "total": 0},
                            "pieceCount": {"total": 2},
                            "equipTrchestCount": {"count": 1},
                        }
                    ]
                }
            ]
        }
        values = build(detail).regions[0].levels[0].collections
        self.assertFalse(values[0].absent)
        self.assertTrue(values[1].absent)
        self.assertEqual(values[2], CollectionProgress())
        self.assertEqual(values[3], CollectionProgress(None, 2))
        self.assertEqual(values[4], CollectionProgress(1, None))
        self.assertFalse(any(value.complete for value in values))

    def test_invalid_numbers_are_unknown_and_overcollection_is_not_clamped(self):
        for invalid in (-1, True, 2.5, "-1", "2.5", {}, [], "not-a-number"):
            with self.subTest(invalid=invalid):
                detail = {
                    "domain": [
                        {"levels": [{"trchestCount": {"count": invalid, "total": 10}}]}
                    ]
                }
                self.assertIsNone(
                    build(detail).regions[0].levels[0].collections[0].count
                )
        detail = {
            "domain": [
                {
                    "levels": [
                        {
                            "trchestCount": {"count": 12, "total": 10},
                            "trstarCount": {"count": 1, "total": 0},
                        }
                    ]
                }
            ]
        }
        view = build(detail)
        self.assertEqual(
            view.regions[0].levels[0].collections[0], CollectionProgress(12, 10)
        )
        self.assertTrue(view.regions[0].levels[0].collections[5].inconsistent)
        self.assertFalse(view.regions[0].levels[0].collections[5].absent)
        self.assertTrue(view.warnings)

    def test_legacy_counts_join_by_level_id_not_array_position(self):
        detail = {
            "domain": [
                {
                    "levels": [
                        {"levelId": "b", "trchestCount": {"total": 12}},
                        {"levelId": "a", "trchestCount": {"count": 3, "total": 9}},
                    ],
                    "collections": [
                        {"levelId": "a", "trchestCount": 99},
                        {"levelId": "b", "trchestCount": 4},
                        {"levelId": "c", "trchestCount": 5},
                    ],
                }
            ]
        }
        levels = build(detail).regions[0].levels
        self.assertEqual([level.level_id for level in levels], ["b", "a", "c"])
        self.assertEqual(
            [level.collections[0] for level in levels],
            [
                CollectionProgress(4, 12),
                CollectionProgress(3, 9),
                CollectionProgress(5, None),
            ],
        )

    def test_duplicate_level_ids_are_local_to_each_region(self):
        detail = {
            "domain": [
                {"name": "甲", "levels": [{"levelId": "same"}, {"levelId": "same"}]},
                {"name": "乙", "levels": [{"levelId": "same"}]},
            ]
        }
        view = build(detail)
        self.assertEqual([len(region.levels) for region in view.regions], [1, 1])
        self.assertTrue(view.warnings)

    def test_empty_and_malformed_data_degrade_without_fabricated_totals(self):
        for value in (
            None,
            {},
            {"domain": None},
            {"domain": []},
            {"domain": "invalid"},
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    build(value if value is not None else {"domain": None}).level_count,
                    0,
                )
        view = build(
            {
                "domain": [
                    None,
                    {"name": "尚未开放", "levels": None},
                    {"levels": [None, {"levelId": "ok"}]},
                ]
            }
        )
        self.assertEqual(len(view.regions), 2)
        self.assertEqual(view.level_count, 1)
        self.assertTrue(view.warnings)

    def test_snapshot_timestamp_is_beijing_save_time_not_request_time(self):
        view = build({"base": {"saveTime": "1"}, "currentTs": 999999})
        self.assertEqual(view.saved_at, "1970-01-01 08:00")
        self.assertEqual(build({"currentTs": 999999}).saved_at, "")
        self.assertEqual(
            build({"base": {"saveTime": "999999999999999999999"}}).saved_at, ""
        )


# Data rows, excluding the parent-region group rows.
DATA_ROWS = '//tbody/tr[not(@class="group")]'


class ExplorationRenderingTests(unittest.TestCase):
    def test_table_headers_and_row_values_follow_the_reference(self):
        doc = html.fromstring(
            draw.render_exploration_html(replace(build(), version="1.5"))
        )
        # Each parent region's head row carries the six column labels with icons.
        heads = doc.xpath('//tr[@class="group"]')
        for head in heads:
            self.assertEqual(
                [
                    node.text_content()
                    for node in head.xpath('.//span[@class="group-col"]')
                ],
                ["储藏箱", "醚质", "工业点数", "维修灵感点", "装备模板箱", "塔晶"],
            )
            self.assertEqual(len(head.xpath('.//img[@class="collection-icon"]')), 6)
        row = doc.xpath(DATA_ROWS)[3]
        self.assertEqual(row.xpath("./td/strong/text()"), ["26", "16", "4", "2", "8"])
        self.assertEqual(
            row.xpath("./td/small/text()"), ["/ 26", "/ 16", "/ 4", "/ 2", "/ 8"]
        )
        self.assertEqual(row.xpath('./td[@class="absent"]/text()'), ["—"])
        self.assertEqual(len(doc.xpath(DATA_ROWS)), 13)
        self.assertEqual(len(doc.xpath("//table")), 1)
        self.assertFalse(doc.xpath("//thead"))
        # Zebra rows restart under each group head.
        self.assertEqual(
            [row.get("class") for row in doc.xpath(DATA_ROWS)],
            [None, "alt"] * 5 + [None] + [None, "alt"],
        )
        self.assertEqual(doc.xpath('//span[@class="game-version"]/text()'), ["V1.5"])
        # The header badge carries the version; the footer no longer repeats it.
        self.assertEqual(
            [
                node.text_content()
                for node in doc.xpath('//div[@class="header-bottom"]/span')
            ],
            ["游戏版本 V1.5"],
        )
        self.assertNotIn("V1.5", doc.xpath("//footer")[0].text_content())
        # Each group's side bar takes its parent region's theme colour.
        self.assertEqual(
            [node.get("style") for node in doc.xpath("//tbody")],
            ["--region-color:#6bffff", "--region-color:#c1ff55"],
        )
        self.assertFalse(
            doc.xpath('//*[@class="region-head" or @class="region-number"]')
        )
        # Each parent region is named once, in a group row above its rows.
        self.assertEqual(
            doc.xpath('//tr[@class="group"]//b/text()'), ["武陵", "四号谷地"]
        )
        self.assertEqual(
            doc.xpath('//tr[@class="group"]//em/text()'), ["11 个地区", "2 个地区"]
        )
        self.assertFalse(doc.xpath('//small[@class="area-parent"]'))

    def test_summary_marks_left_out_cells_and_the_legend_explains_once(self):
        view = build()
        levels = list(view.regions[0].levels)
        values = list(levels[2].collections)
        values[3] = CollectionProgress(None, 5)
        levels[2] = replace(levels[2], collections=tuple(values))
        values = list(levels[3].collections)
        values[0] = CollectionProgress(19, 26)
        levels[3] = replace(levels[3], collections=tuple(values))
        regions = (replace(view.regions[0], levels=tuple(levels)), *view.regions[1:])
        doc = html.fromstring(
            draw.render_exploration_html(replace(view, regions=regions))
        )
        summary = doc.xpath('//div[@class="exploration-summary"]')[0]
        # One row: the wider overall tile first, then the six category tiles.
        self.assertFalse(summary.xpath(".//h2"))
        overall = summary.xpath('./div[starts-with(@class,"overall-cell")]')[0]
        tiles = summary.xpath('./div[starts-with(@class,"stat-cell")]')
        self.assertEqual(len(tiles), 6)
        self.assertNotIn("已收满", summary.text_content())
        # Overall progress leaves out the area with an unknown cell; the flag sits
        # in the label row, the percentage below it.
        self.assertEqual(
            overall.xpath('.//span[@class="overall-label"]//text()'), ["收集度", "?1"]
        )
        self.assertEqual(
            overall.xpath('.//b[@class="overall-value"]//text()'), ["99", "%"]
        )
        # By default a thin badge-yellow track under the text fills the same share.
        self.assertEqual(overall.get("class"), "overall-cell badge-progress")
        self.assertEqual(
            overall.xpath('./span[@class="overall-badge"]/i/@style'), ["width:99%"]
        )
        # The track spans the text column (no negative inset) on a visible grey.
        badge_css = re.search(r"\.overall-badge\{([^}]*)\}", draw._css()).group(1)
        self.assertIn("height:3px", badge_css)
        self.assertIn("background:#4a5258", badge_css)
        self.assertNotIn("margin:8px -6px", badge_css)
        # The gauge variant sweeps the same share of its arc instead.
        with patch.object(draw, "OVERALL_PROGRESS", "arc"):
            gauge = html.fromstring(
                draw.render_exploration_html(replace(view, regions=regions))
            ).xpath('//div[@class="overall-cell arc-progress"]')[0]
        self.assertFalse(gauge.xpath(".//span[@class='overall-edge']"))
        self.assertEqual(
            gauge.xpath('.//circle[@class="on"]/@stroke-dasharray'),
            [f"{draw.ARC_LENGTH * 99 / 100:.2f} 200"],
        )

        def overall_for(style, source):
            with patch.object(draw, "OVERALL_PROGRESS", style):
                doc = html.fromstring(draw.render_exploration_html(source))
            return doc.xpath(f'//div[@class="overall-cell {style}-progress"]')[0]

        partial = replace(view, regions=regions)
        # "spine" fills the left spine upwards; "edge" fills the tile's top hairline.
        spine = overall_for("spine", partial)
        self.assertEqual(
            spine.xpath('./span[@class="overall-spine"]/i/@style'), ["height:99%"]
        )
        edge = overall_for("edge", partial)
        self.assertEqual(
            edge.xpath('./span[@class="overall-edge"]/i/@style'), ["width:99%"]
        )
        # "chip" draws no bar: a chip on the figure's line names the state.
        level = view.regions[0].levels[0]
        unknown = replace(
            view,
            regions=(
                replace(
                    view.regions[0],
                    levels=(
                        replace(level, collections=(CollectionProgress(None, 5),) * 6),
                    ),
                ),
            ),
        )
        for source, state, text in (
            (partial, "short", "未收满"),
            (build(), "full", "已收满"),
            (unknown, "unknown", "数据不全"),
        ):
            chip = overall_for("chip", source)
            self.assertEqual(
                chip.xpath(f'./b/em[@class="overall-chip {state}"]/text()'), [text]
            )
            self.assertFalse(chip.xpath(".//i"))
        self.assertEqual(
            tiles[0].xpath('.//b[@class="stat-value"]//text()'), ["675", "/ 682"]
        )
        # The known part is still summed; the flag counts the cells left out.
        self.assertEqual(
            tiles[3].xpath('.//b[@class="stat-value"]//text()'), ["5", "/ 5", "?1"]
        )
        # No grey note lines under the values any more.
        self.assertFalse(doc.xpath('//div[contains(@class,"stat-cell")]/i'))
        self.assertIn(
            "概况合计未计入 N 个数据不全的地区",
            doc.xpath('//div[@class="legend"]')[0].text_content(),
        )
        plain = html.fromstring(draw.render_exploration_html(build()))
        self.assertFalse(plain.xpath('//em[@class="flag"]'))
        self.assertNotIn(
            "未计入", plain.xpath('//div[@class="legend"]')[0].text_content()
        )

    def test_long_summary_sums_shrink_and_move_the_flag_to_its_own_line(self):
        view = build()
        done, rest = view.regions[0].levels[:2]
        done = replace(
            done,
            collections=(CollectionProgress(1200, 1200), CollectionProgress(682, 682))
            + done.collections[2:],
        )
        rest = replace(
            rest,
            collections=(CollectionProgress(None, 5), CollectionProgress(None, 3))
            + rest.collections[2:],
        )
        regions = (replace(view.regions[0], levels=(done, rest)),)
        doc = html.fromstring(
            draw.render_exploration_html(replace(view, regions=regions))
        )
        tiles = doc.xpath('//div[starts-with(@class,"stat-cell")]')
        # Nine digits with a flag: smaller figures, flag on its own line.
        self.assertEqual(tiles[0].get("class"), "stat-cell compact stacked")
        self.assertEqual(
            tiles[0].xpath('./span[@class="stat-flags"]/em/text()'), ["?1"]
        )
        self.assertFalse(tiles[0].xpath('.//b[@class="stat-value"]/em'))
        # Seven digits: smaller figures, flag still beside them.
        self.assertEqual(tiles[1].get("class"), "stat-cell compact")
        self.assertEqual(
            tiles[1].xpath('.//b[@class="stat-value"]//text()'),
            ["682", "/ 682", "?1"],
        )
        # Short sums keep the regular size.
        self.assertEqual(
            [tile.get("class") for tile in tiles[2:]], ["stat-cell"] * 4
        )

    def test_html_escapes_identity_and_region_names(self):
        view = replace(
            build(),
            nickname='<script>alert("x")</script>',
            uid='<img src="x">',
            version="<iframe>",
            regions=(
                ExplorationRegion(
                    "x",
                    "<b>地区</b>",
                    (replace(build().regions[0].levels[0], name="<b>地区</b>"),),
                ),
            ),
        )
        doc = html.fromstring(draw.render_exploration_html(view))
        self.assertFalse(doc.xpath("//script|//iframe|//img[@src='x']"))
        self.assertIn("<b>地区</b>", doc.text_content())
        self.assertEqual(
            doc.xpath('//span[@class="group-name"]//text()'),
            ["<b>地区</b>", "1 个地区"],
        )
        self.assertIn("V<iframe>", doc.text_content())

    def test_pagination_keeps_all_rows_and_empty_regions(self):
        original = build()
        rows = tuple(
            replace(original.regions[0].levels[0], level_id=str(i), name=f"区域{i}")
            for i in range(75)
        )
        view = replace(
            original,
            regions=(
                ExplorationRegion("future", "未来", rows),
                ExplorationRegion("empty", "未解锁", ()),
            ),
        )
        pages = draw.paginate_exploration(view)
        self.assertEqual(len(pages), 4)
        self.assertEqual(
            [
                row.level_id
                for page in pages
                for region in page.regions
                for row in region.levels
            ],
            [str(i) for i in range(75)],
        )
        self.assertEqual(pages[-1].regions[-1].name, "未解锁")
        self.assertTrue(all(page.level_count <= 24 for page in pages))
        self.assertEqual(len(draw.paginate_exploration(replace(view, regions=()))), 1)
        last_page = html.fromstring(
            draw.render_exploration_html(pages[-1], page_number=4, page_count=4)
        )
        self.assertIn("未解锁暂无地区明细", last_page.text_content())
        badge = last_page.xpath('//span[@class="version-label"]')[0]
        self.assertEqual(badge.text_content(), "版本未知")
        self.assertEqual(
            last_page.xpath('//span[@class="page-label"]/text()'), ["04 / 04"]
        )
        self.assertEqual(
            last_page.xpath('//span[@class="overall-label"]/text()'), ["本页收集度"]
        )
        # Regions without a theme colour fall back to the signature yellow.
        self.assertEqual(
            last_page.xpath("//tbody/@style"), ["--region-color:#ffd000"] * 2
        )


class ExplorationVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_manifest_game_version_and_missing_or_invalid_labels(self):
        for latest, expected in (
            ("1.5.3@10506507-7", "1.5"),
            ("2.0.1@next", "2.0"),
            (None, ""),
            ("invalid", ""),
        ):
            with (
                self.subTest(latest=latest),
                patch.object(
                    version,
                    "fetch_akedata_manifest",
                    AsyncMock(return_value={"latest": latest}),
                ),
            ):
                self.assertEqual(await version.fetch_exploration_version(), expected)

    async def test_cold_manifest_lookup_is_not_cut_short_of_its_http_timeout(self):
        bounds = []
        real_timeout = asyncio.timeout

        def recorded_timeout(delay):
            bounds.append(delay)
            return real_timeout(delay)

        async def cold_manifest():
            await asyncio.sleep(0.01)
            return {"latest": "1.5.3@10506507-7"}

        with (
            patch.object(version.asyncio, "timeout", recorded_timeout),
            patch.object(version, "fetch_akedata_manifest", cold_manifest),
        ):
            self.assertEqual(await version.fetch_exploration_version(), "1.5")
        # A 5 s bound used to turn a slow but healthy fetch into "版本未知".
        self.assertEqual(len(bounds), 1)
        self.assertGreater(bounds[0], AKEDATA_TIMEOUT_SECONDS)

    async def test_lookup_failure_keeps_exploration_available(self):
        for error in (RuntimeError("invalid manifest"), TimeoutError()):
            with (
                self.subTest(error=error),
                patch.object(
                    version, "fetch_akedata_manifest", AsyncMock(side_effect=error)
                ),
            ):
                self.assertEqual(await version.fetch_exploration_version(), "")


class ExplorationAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        artwork = patch.object(
            draw, "fetch_exploration_thumbnails", AsyncMock(return_value={})
        )
        artwork.start()
        self.addCleanup(artwork.stop)
        await handlers._ACCOUNT_PAGE_CACHE.clear()
        self.roles = [
            EndfieldRole(
                1, 1, "caller", "binding", "12344321", "1", "甲", "China", False
            ),
            EndfieldRole(
                2, 1, "caller", "binding", "12348765", "1", "乙", "China", True
            ),
        ]

    async def asyncTearDown(self):
        await handlers._ACCOUNT_PAGE_CACHE.clear()

    async def run_command(
        self,
        text="探索",
        *,
        group=True,
        roles=None,
        response=None,
        error=None,
        repeat=1,
        game_version="1.5",
    ):
        roles = self.roles if roles is None else roles
        store = Mock()
        store.list_roles.side_effect = lambda user: roles if user == "caller" else []
        store.resolve_role.side_effect = lambda user, selector: (
            EndfieldStore.resolve_role(store, user, selector)
        )
        store.decrypt_token.return_value = "fake-token"
        fetch = AsyncMock(
            return_value=detail_fixture() if response is None else response,
            side_effect=error,
        )
        render = AsyncMock(return_value=(b"page1", b"page2"))
        send = AsyncMock()
        matcher = AsyncMock()
        with (
            patch.object(handlers, "account_store", store),
            patch.object(handlers, "event_user_id", return_value="caller"),
            patch.object(handlers, "is_group", return_value=group),
            patch.object(handlers.CredentialCipher, "from_env", return_value=Mock()),
            patch.object(handlers.official_client, "card_detail", fetch),
            patch.object(
                handlers.ownership_stats_service, "persist_detail", AsyncMock()
            ),
            patch.object(handlers, "draw_exploration_cards", render),
            patch.object(
                handlers,
                "fetch_exploration_version",
                AsyncMock(return_value=game_version),
            ),
            patch.object(handlers, "_finish_pngs", send),
        ):
            for _ in range(repeat):
                await handlers._handle_command(
                    matcher, SimpleNamespace(), parse_command(text)
                )
        return matcher, store, fetch, render, send

    async def test_full_dispatch_uses_primary_account_and_one_detail_fetch(self):
        _, store, fetch, render, send = await self.run_command()
        store.resolve_role.assert_called_once_with("caller", "")
        fetch.assert_awaited_once_with("fake-token", self.roles[1])
        self.assertEqual(render.await_args.args[0].level_count, 13)
        self.assertEqual(render.await_args.args[0].uid, "****8765")
        self.assertEqual(render.await_args.args[0].version, "1.5")
        self.assertEqual(send.await_args.args[1], (b"page1", b"page2"))

    async def test_selected_account_and_private_uid(self):
        _, _, fetch, render, _ = await self.run_command("账号 探索 1", group=False)
        self.assertEqual(fetch.await_args.args[1], self.roles[0])
        self.assertEqual(render.await_args.args[0].uid, "12344321")

    async def test_binding_and_invalid_selector_do_not_fetch(self):
        for text, roles, keyword in (
            ("探索", [], "绑定"),
            ("探索 99", self.roles, "编号"),
            ("探索 不属于我的账号", self.roles, "编号"),
        ):
            with self.subTest(text=text):
                matcher, _, fetch, render, send = await self.run_command(
                    text, roles=roles
                )
                self.assertIn(keyword, matcher.finish.await_args.args[0])
                fetch.assert_not_awaited()
                render.assert_not_awaited()
                send.assert_not_awaited()

    async def test_api_failure_returns_existing_error_path(self):
        error = EndfieldAPIError("查询终末地档案", message="凭据失效")
        matcher, _, _, render, send = await self.run_command(error=error)
        self.assertIn("凭据失效", matcher.finish.await_args.args[0])
        render.assert_not_awaited()
        send.assert_not_awaited()

    async def test_same_snapshot_reuses_images_but_still_fetches_current_data(self):
        _, _, fetch, render, send = await self.run_command(repeat=2)
        self.assertEqual(fetch.await_count, 2)
        self.assertEqual(render.await_count, 1)
        self.assertEqual(send.await_count, 2)

    async def test_changed_counts_and_chat_visibility_do_not_reuse_old_images(self):
        await self.run_command()
        modified = deepcopy(detail_fixture())
        modified["domain"][0]["levels"][0]["trchestCount"]["count"] = 4
        _, _, _, render, _ = await self.run_command(response=modified)
        render.assert_awaited_once()
        _, _, _, private_render, _ = await self.run_command(group=False)
        private_render.assert_awaited_once()

    async def test_changed_or_unavailable_version_does_not_reuse_old_images(self):
        await self.run_command()
        _, _, _, render, _ = await self.run_command(game_version="2.0")
        render.assert_awaited_once()
        self.assertEqual(render.await_args.args[0].version, "2.0")
        _, _, _, render, _ = await self.run_command(game_version="")
        render.assert_awaited_once()
        self.assertEqual(render.await_args.args[0].version, "")

    async def test_height_limit_retries_with_smaller_pages_without_dropping_rows(self):
        view = build()
        capture = AsyncMock(
            side_effect=[
                RuntimeError("Screenshot element height 13000 exceeds limit 12000"),
                b"p1",
                b"p2",
            ]
        )
        with patch.object(draw, "_draw_gallery_catalog", capture):
            result = await draw.draw_exploration_cards(view)
        self.assertEqual(result, (b"p1", b"p2"))
        docs = [
            html.fromstring(call.args[0].html) for call in capture.await_args_list[1:]
        ]
        self.assertEqual(sum(len(doc.xpath(DATA_ROWS)) for doc in docs), 13)
        self.assertTrue(all(doc.xpath('//tr[@class="group"]') for doc in docs))

    async def test_non_height_render_errors_are_not_hidden(self):
        with (
            patch.object(
                draw,
                "_draw_gallery_catalog",
                AsyncMock(side_effect=RuntimeError("browser disconnected")),
            ) as capture,
            self.assertRaisesRegex(RuntimeError, "browser disconnected"),
        ):
            await draw.draw_exploration_cards(build())
        capture.assert_awaited_once()


class ExplorationThumbnailTests(unittest.TestCase):
    def metadata(self):
        tree = {
            "code": 0,
            "data": {
                "maps": [
                    {
                        "id": "map02",
                        "name": "武陵",
                        "levels": [{"id": "map02_lv006", "name": "藏剑谷"}],
                    }
                ]
            },
        }
        prefix = "assets/beyond/dynamicassets/gameplay/ui/textures/levelmap/levelmapchunks/map02lv006/l_map02_lv006_"
        files = {f"{prefix}{x}_{y}.png": {} for x in (1, 2) for y in (1, 2)}
        return tree, {"datasets": {"images": {"files": files}}}

    def test_scoped_name_matching_and_all_tiles_in_bottom_up_coordinates(self):
        tree, index = self.metadata()
        view = build()
        result = match_thumbnails(view, tree, index)
        key = (view.regions[0].region_id, view.regions[0].levels[4].level_id)
        self.assertEqual(list(result), [key])
        thumb = result[key]
        self.assertEqual((thumb.columns, thumb.rows, len(thumb.tiles)), (2, 2, 4))
        self.assertEqual(
            [(tile.column, tile.row) for tile in thumb.tiles],
            [(0, 1), (0, 0), (1, 1), (1, 0)],
        )
        self.assertEqual(view, build())  # Artwork never changes official statistics.

    def test_exact_id_can_match_renamed_level(self):
        tree, index = self.metadata()
        region = build().regions[0]
        level = replace(region.levels[4], level_id="map02_lv006", name="新名称")
        view = replace(build(), regions=(replace(region, levels=(level,)),))
        self.assertEqual(len(match_thumbnails(view, tree, index)), 1)

    def test_region_id_takes_priority_over_conflicting_region_name(self):
        tree, index = self.metadata()
        tree["data"]["maps"].append({"id": "other", "name": "武陵", "levels": []})
        region = replace(build().regions[0], region_id="map02")
        view = replace(build(), regions=(region,))
        self.assertEqual(len(match_thumbnails(view, tree, index)), 1)

    def test_domain_ids_reach_their_map_tree_parent_without_guessing(self):
        tree, index = self.metadata()
        level = replace(build().regions[0].levels[4], level_id="x", name="藏剑谷")
        # English parent names as card/detail sends them; only the ID alias links.
        for region_id, expected in (("domain_2", 1), ("domain_1", 0)):
            with self.subTest(region_id=region_id):
                region = ExplorationRegion(region_id, "Wuling", (level,))
                view = replace(build(), regions=(region,))
                self.assertEqual(len(match_thumbnails(view, tree, index)), expected)
        # The alias finds the parent; a duplicated level name still never guesses.
        tree["data"]["maps"][0]["levels"].append({"id": "another", "name": "藏剑谷"})
        region = ExplorationRegion("domain_2", "Wuling", (level,))
        view = replace(build(), regions=(region,))
        self.assertEqual(match_thumbnails(view, tree, index), {})

    def test_sprite_only_maps_and_complete_texture_fallback(self):
        tree, index = self.metadata()
        textures = index["datasets"]["images"]["files"]
        sprites = {path.replace("/textures/", "/sprites/"): {} for path in textures}
        files = index["datasets"]["images"]["files"] = dict(sprites)
        for mode in ("sprites-only", "both", "incomplete-sprites"):
            with self.subTest(mode=mode):
                if mode == "both":
                    files.update(textures)
                elif mode == "incomplete-sprites":
                    files.pop(next(iter(sprites)))
                result = match_thumbnails(build(), tree, index)
                self.assertEqual(len(result), 1)
                thumb = next(iter(result.values()))
                self.assertEqual(len(thumb.tiles), 4)
                family = "textures" if mode == "incomplete-sprites" else "sprites"
                self.assertTrue(all(f"/{family}/" in tile.url for tile in thumb.tiles))

    def test_partial_sprite_and_texture_sets_are_never_combined(self):
        tree, index = self.metadata()
        files = index["datasets"]["images"]["files"]
        missing = list(files)[-1]
        files.pop(missing)
        files[missing.replace("/textures/", "/sprites/")] = {}
        self.assertEqual(match_thumbnails(build(), tree, index), {})

    def test_ambiguous_names_and_wrong_parent_never_guess(self):
        tree, index = self.metadata()
        parent = tree["data"]["maps"][0]
        parent["levels"].append({"id": "another", "name": "藏剑谷"})
        self.assertEqual(match_thumbnails(build(), tree, index), {})
        parent["levels"].pop()
        parent["name"] = "另一个地区"
        self.assertEqual(match_thumbnails(build(), tree, index), {})

    def test_incomplete_grid_and_untrusted_paths_are_ignored(self):
        tree, index = self.metadata()
        files = index["datasets"]["images"]["files"]
        files.pop(next(iter(files)))
        files["https://evil.test/map02_lv006_1_1.png"] = {}
        self.assertEqual(match_thumbnails(build(), tree, index), {})

    def test_html_places_complete_art_before_name_and_missing_tile_falls_back(self):
        tree, index = self.metadata()
        view = build()
        thumbs = match_thumbnails(view, tree, index)
        urls = {
            tile.url: "data:image/png;base64,AA=="
            for thumb in thumbs.values()
            for tile in thumb.tiles
        }
        doc = html.fromstring(
            draw.render_exploration_html(view, thumbnails=thumbs, assets=urls)
        )
        row = doc.xpath(DATA_ROWS)[4]
        self.assertEqual(len(row.xpath(".//img")), 4)
        self.assertEqual(
            row.xpath('.//div[@class="area-name"]/span[last()]/span/text()'), ["藏剑谷"]
        )
        self.assertEqual(len(doc.xpath(DATA_ROWS)), 13)
        urls.pop(next(iter(urls)))
        doc = html.fromstring(
            draw.render_exploration_html(view, thumbnails=thumbs, assets=urls)
        )
        self.assertFalse(doc.xpath('//span[contains(@class,"map-thumb")]//img'))
        self.assertEqual(len(doc.xpath('//span[contains(@class,"map-missing")]')), 13)


class ExplorationThumbnailAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_reference_regions_use_official_icons_without_network(self):
        from plugins.endfield.account.exploration import thumbnails

        with patch.object(thumbnails, "fetch_json", AsyncMock()) as fetch:
            result = await thumbnails.fetch_exploration_thumbnails(build())
        self.assertEqual(len(result), 13)
        self.assertTrue(all(thumb.official for thumb in result.values()))
        self.assertTrue(
            all(
                thumb.tiles[0].url.startswith("data:image/png;base64,")
                for thumb in result.values()
            )
        )
        fetch.assert_not_awaited()

    async def test_production_ids_use_official_icons_without_network(self):
        from plugins.endfield.account.exploration import thumbnails
        from plugins.endfield.account.exploration.artwork import artwork_url

        view = build(production_detail())
        known = replace(
            view,
            regions=tuple(
                replace(
                    region,
                    levels=tuple(
                        level
                        for level in region.levels
                        if not level.level_id.startswith(("map03", "map02_lv099"))
                    ),
                )
                for region in view.regions
            ),
        )
        with patch.object(thumbnails, "fetch_json", AsyncMock()) as fetch:
            result = await thumbnails.fetch_exploration_thumbnails(known)
        fetch.assert_not_awaited()
        self.assertEqual(
            list(result),
            [
                ("domain_2", "indie_dg007"),
                ("domain_2", "map02_lv005"),
                ("domain_2", "indie_dg016"),
                ("domain_1", "map01_lv001"),
                ("domain_1", "map01_lv002"),
            ],
        )
        self.assertTrue(all(thumb.official for thumb in result.values()))
        # 首墩内部 shares 首墩's artwork, as in the official component.
        self.assertEqual(
            result["domain_2", "indie_dg007"].tiles[0].url,
            artwork_url("regions/map02_lv004.png"),
        )
        # Raw English names under an unknown parent: the unique level ID still hits.
        raw = replace(
            known,
            regions=(
                ExplorationRegion(
                    "domain_9",
                    "Wuling",
                    tuple(
                        replace(level, name=level.level_id)
                        for level in known.regions[1].levels
                    ),
                ),
            ),
        )
        self.assertEqual(
            list(thumbnails.match_official_thumbnails(raw)),
            [
                ("domain_9", "indie_dg007"),
                ("domain_9", "map02_lv005"),
                ("domain_9", "indie_dg016"),
            ],
        )

    async def test_new_region_falls_back_without_replacing_official_artwork(self):
        from plugins.endfield.account.exploration import thumbnails

        original = build()
        parent = original.regions[0]
        future = replace(parent.levels[0], level_id="map02_lv099", name="未来谷")
        view = replace(
            original,
            regions=(
                replace(parent, levels=(*parent.levels, future)),
                *original.regions[1:],
            ),
        )
        tree, index = ExplorationThumbnailTests().metadata()
        tree["data"]["maps"][0]["levels"] = [{"id": "map02_lv099", "name": "未来谷"}]
        index["datasets"]["images"]["files"] = {
            path.replace("map02lv006", "map02lv099").replace(
                "map02_lv006", "map02_lv099"
            ): {}
            for path in index["datasets"]["images"]["files"]
        }
        with patch.object(
            thumbnails, "fetch_json", AsyncMock(side_effect=(tree, index))
        ) as fetch:
            result = await thumbnails.fetch_exploration_thumbnails(view)
        self.assertEqual(len(result), 14)
        self.assertEqual(sum(thumb.official for thumb in result.values()), 13)
        self.assertFalse(result[parent.region_id, future.level_id].official)
        self.assertEqual(fetch.await_count, 2)

    async def test_metadata_outage_returns_placeholders_without_cacheable_success(self):
        from plugins.endfield.account.exploration import thumbnails
        from plugins.endfield.rendering.health import track_render_health

        with (
            patch.object(thumbnails, "match_official_thumbnails", return_value={}),
            patch.object(thumbnails, "fetch_json", AsyncMock(side_effect=TimeoutError)),
            track_render_health() as health,
        ):
            self.assertEqual(await thumbnails.fetch_exploration_thumbnails(build()), {})
            self.assertFalse(health.complete)

    async def test_malformed_metadata_does_not_break_statistics_rendering(self):
        from plugins.endfield.account.exploration import thumbnails
        from plugins.endfield.rendering.health import track_render_health

        for payload in (
            [],
            {"code": 0, "data": None},
            {"code": 0, "data": {"maps": []}},
        ):
            with (
                self.subTest(payload=payload),
                patch.object(thumbnails, "match_official_thumbnails", return_value={}),
                patch.object(thumbnails, "fetch_json", AsyncMock(return_value=payload)),
                track_render_health() as health,
            ):
                self.assertEqual(
                    await thumbnails.fetch_exploration_thumbnails(build()), {}
                )
                self.assertFalse(health.complete)

    async def test_empty_view_does_not_request_metadata(self):
        from plugins.endfield.account.exploration import thumbnails

        with patch.object(thumbnails, "fetch_json", AsyncMock()) as fetch:
            self.assertEqual(
                await thumbnails.fetch_exploration_thumbnails(build({})), {}
            )
            fetch.assert_not_awaited()

    async def test_prepared_html_uses_shared_resources_and_health_for_missing_tiles(
        self,
    ):
        from plugins.endfield.rendering import cards
        from plugins.endfield.rendering.health import track_render_health

        view = build()
        key = (view.regions[0].region_id, view.regions[0].levels[0].level_id)
        thumb = MapThumbnail(1, 1, (MapTile("https://example.test/map.png", 0, 0),))
        with (
            patch.object(
                cards, "fetch_many_resilient", AsyncMock(return_value=({}, {}))
            ),
            patch.object(draw, "note_remote_assets", AsyncMock()),
            track_render_health() as health,
        ):
            prepared = await draw.prepare_exploration_html(
                view, thumbnails={key: thumb}
            )
            self.assertFalse(health.complete)
            self.assertFalse(
                html.fromstring(prepared.html).xpath(
                    '//span[contains(@class,"map-thumb")]//img'
                )
            )
            self.assertEqual(prepared.resources, {})


class ExplorationOfficialArtworkTests(unittest.TestCase):
    def test_packaged_images_keep_source_hashes_and_all_six_headers(self):
        from plugins.endfield.account.exploration.artwork import (
            ARTWORK_DIR,
            artwork_manifest,
        )
        from plugins.endfield.account.exploration.models import COLLECTION_COLUMNS

        manifest = artwork_manifest()
        self.assertEqual(
            list(manifest["collections"]), [key for key, _ in COLLECTION_COLUMNS]
        )
        for filename, info in manifest["files"].items():
            with self.subTest(filename=filename):
                self.assertEqual(
                    hashlib.sha256((ARTWORK_DIR / filename).read_bytes()).hexdigest(),
                    info["sha256"],
                )
        doc = html.fromstring(draw.render_exploration_html(build()))
        # Six in the summary, then six in each of the two group heads.
        self.assertEqual(len(doc.xpath('//img[@class="collection-icon"]')), 18)
        self.assertTrue(
            all(
                node.get("src", "").startswith("data:image/png;base64,")
                for node in doc.xpath('//img[@class="collection-icon"]')
            )
        )


if __name__ == "__main__":
    unittest.main()
