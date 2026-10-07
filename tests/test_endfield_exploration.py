from __future__ import annotations

import hashlib
import json
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

FIXTURE = Path(__file__).parent / "fixtures/endfield/exploration/screenshot.json"


def detail_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["detail"]


def build(detail=None):
    return build_exploration_view(
        detail_fixture() if detail is None else detail, uid="****4321", server_name="1"
    )


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
        self.assertEqual([region.name for region in view.regions], ["武陵", "四号谷地"])
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


class ExplorationRenderingTests(unittest.TestCase):
    def test_table_headers_and_row_values_follow_the_reference(self):
        doc = html.fromstring(
            draw.render_exploration_html(replace(build(), version="1.5"))
        )
        self.assertEqual(
            [node.text_content() for node in doc.xpath("//thead")[0].xpath(".//th")],
            ["地区", "储藏箱", "醚质", "工业点数", "维修灵感点", "装备模板箱", "塔晶"],
        )
        row = doc.xpath("//tbody/tr")[3]
        self.assertEqual(row.xpath("./td/strong/text()"), ["26", "16", "4", "2", "8"])
        self.assertEqual(
            row.xpath("./td/small/text()"), ["/26", "/16", "/4", "/2", "/8"]
        )
        self.assertEqual(row.xpath('./td[@class="absent"]/text()'), ["—"])
        self.assertEqual(len(doc.xpath("//tbody/tr")), 13)
        self.assertEqual(len(doc.xpath("//table|//thead")), 2)
        self.assertEqual(doc.xpath('//span[@class="game-version"]/text()'), ["V1.5"])
        self.assertEqual(
            doc.xpath('//div[@class="header-bottom"]/span[1]/text()'), ["13 个地区"]
        )
        self.assertFalse(
            doc.xpath('//*[@class="region-head" or @class="region-number"]')
        )
        self.assertEqual(
            doc.xpath('//small[@class="area-parent"]/text()'),
            ["武陵"] * 11 + ["四号谷地"] * 2,
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
            doc.xpath('//small[@class="area-parent"]/text()'), ["<b>地区</b>"]
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
        last_page = html.fromstring(draw.render_exploration_html(pages[-1]))
        self.assertIn("未解锁暂无地区明细", last_page.text_content())
        self.assertIn("版本未知", last_page.text_content())


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
        self.assertEqual(sum(len(doc.xpath("//tbody/tr")) for doc in docs), 13)
        self.assertTrue(all(doc.xpath("//thead") for doc in docs))

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
        row = doc.xpath("//tbody/tr")[4]
        self.assertEqual(len(row.xpath(".//img")), 4)
        self.assertEqual(
            row.xpath('.//div[@class="area-name"]/span[last()]/span/text()'), ["藏剑谷"]
        )
        self.assertEqual(len(doc.xpath("//tbody/tr")), 13)
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
        self.assertEqual(len(doc.xpath('//img[@class="collection-icon"]')), 6)
        self.assertTrue(
            all(
                node.get("src", "").startswith("data:image/png;base64,")
                for node in doc.xpath('//img[@class="collection-icon"]')
            )
        )


if __name__ == "__main__":
    unittest.main()
