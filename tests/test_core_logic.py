from __future__ import annotations

import os
import sys
import unittest
import importlib.util
import json
import asyncio
import logging
import tarfile
import time
import types
import sqlite3
import zipfile
from urllib.parse import unquote, urlparse
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.disable(logging.CRITICAL)

def _load_mcsm_store_class():
    spec = importlib.util.spec_from_file_location("mcsm_store_for_test", ROOT / "plugins/mcsm/store.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.MCSMStore


def _load_mcsm_draw_module():
    return _load_module("mcsm_draw_for_test", "plugins/mcsm/draw.py")


def _load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_minecraft_broadcast_module(name: str):
    previous_entari_native = sys.modules.get("utils.entari_native")
    previous_arclet_entari = sys.modules.get("arclet.entari")
    previous_arclet = sys.modules.get("arclet")
    previous_pkg = sys.modules.get("plugins.minecraft_plugin")
    previous_ping = sys.modules.get("plugins.minecraft_plugin.ping")

    entari_native = types.ModuleType("utils.entari_native")
    entari_native.listen_notice = lambda *args, **kwargs: types.SimpleNamespace(
        handle=lambda: (lambda func: func),
        finish=lambda *a, **kw: None,
    )
    entari_native.get_bot = lambda: None
    entari_native.Pred = lambda func: func
    entari_native.ChainMsg = types.SimpleNamespace(text=lambda text: types.SimpleNamespace(send=lambda *a, **kw: None))
    entari_native.SendDest = lambda *args, **kwargs: (args, kwargs)
    entari_native.account_adapter_name = lambda bot: ""
    entari_native.timer = types.SimpleNamespace(
        scheduled_job=lambda *args, **kwargs: (lambda func: func)
    )

    arclet = previous_arclet or types.ModuleType("arclet")
    arclet_entari = types.ModuleType("arclet.entari")
    arclet_entari.Account = object
    arclet_entari.Event = object

    sys.modules["utils.entari_native"] = entari_native
    sys.modules["arclet"] = arclet
    sys.modules["arclet.entari"] = arclet_entari
    pkg = previous_pkg or types.ModuleType("plugins.minecraft_plugin")
    pkg.__path__ = [str(ROOT / "plugins/minecraft_plugin")]
    sys.modules["plugins.minecraft_plugin"] = pkg
    ping_module = types.ModuleType("plugins.minecraft_plugin.ping")
    async def fake_ping(*args, **kwargs):
        return {"status": "error", "data": {}}
    ping_module.ping = fake_ping
    sys.modules["plugins.minecraft_plugin.ping"] = ping_module
    try:
        return _load_module(f"plugins.minecraft_plugin.{name}", "plugins/minecraft_plugin/broadcast.py")
    finally:
        if previous_entari_native is None:
            sys.modules.pop("utils.entari_native", None)
        else:
            sys.modules["utils.entari_native"] = previous_entari_native
        if previous_arclet_entari is None:
            sys.modules.pop("arclet.entari", None)
        else:
            sys.modules["arclet.entari"] = previous_arclet_entari
        if previous_arclet is None:
            sys.modules.pop("arclet", None)
        else:
            sys.modules["arclet"] = previous_arclet
        if previous_pkg is None:
            sys.modules.pop("plugins.minecraft_plugin", None)
        else:
            sys.modules["plugins.minecraft_plugin"] = previous_pkg
        if previous_ping is None:
            sys.modules.pop("plugins.minecraft_plugin.ping", None)
        else:
            sys.modules["plugins.minecraft_plugin.ping"] = previous_ping


def _load_steam_data_source():
    pkg_name = "steam_info_for_test"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(ROOT / "plugins/steamInfo")]
    sys.modules[pkg_name] = pkg
    models = _load_module(f"{pkg_name}.models", "plugins/steamInfo/models.py")
    sys.modules[f"{pkg_name}.models"] = models
    return _load_module(f"{pkg_name}.data_source", "plugins/steamInfo/data_source.py")


def _load_steam_module():
    pkg_name = "steam_module_for_test"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(ROOT / "plugins/steamInfo")]
    sys.modules[pkg_name] = pkg
    models = _load_module(f"{pkg_name}.models", "plugins/steamInfo/models.py")
    sys.modules[f"{pkg_name}.models"] = models
    return _load_module(f"{pkg_name}.steam", "plugins/steamInfo/steam.py")


def _load_steam_draw_module():
    pkg_name = "steam_draw_for_test"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(ROOT / "plugins/steamInfo")]
    sys.modules[pkg_name] = pkg
    models = _load_module(f"{pkg_name}.models", "plugins/steamInfo/models.py")
    sys.modules[f"{pkg_name}.models"] = models
    return _load_module(f"{pkg_name}.draw", "plugins/steamInfo/draw.py")


def _load_bili_new_module(module_name: str):
    pkg_name = f"bilibilibot_new_for_test_{module_name}"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(ROOT / "plugins/bilibilibot")]
    sys.modules[pkg_name] = pkg
    models = _load_module(f"{pkg_name}.models", "plugins/bilibilibot/models.py")
    sys.modules[f"{pkg_name}.models"] = models
    if module_name == "models":
        return models
    return _load_module(f"{pkg_name}.{module_name}", f"plugins/bilibilibot/{module_name}.py")


async def _run_scheduled_cleanup_case(path: Path):
    from utils.temp_files import schedule_temp_file_cleanup

    path.write_text("temp", encoding="utf-8")
    schedule_temp_file_cleanup(path, delay_seconds=0)
    await asyncio.sleep(0.05)


def _image_file_path(segment) -> Path:
    src = str(getattr(segment, "src", "") or "")
    parsed = urlparse(src)
    if parsed.scheme != "file":
        raise AssertionError(f"expected file image src, got {src!r}")
    file_path = unquote(parsed.path)
    if file_path.startswith("/") and len(file_path) > 2 and file_path[2] == ":":
        file_path = file_path[1:]
    return Path(file_path)


class CoreLogicTests(unittest.TestCase):
    def test_entari_account_adapter_name_handles_object_string_and_missing(self):
        entari_native = _load_module("entari_native_adapter_for_test", "utils/entari_native.py")

        class AdapterObject:
            def get_name(self):
                return "llonebot"

        class AccountWithObject:
            adapter = AdapterObject()

        class AccountWithString:
            adapter = "llonebot-string"

        class AccountWithoutAdapter:
            pass

        self.assertEqual(entari_native.account_adapter_name(AccountWithObject()), "llonebot")
        self.assertEqual(entari_native.account_adapter_name(AccountWithString()), "llonebot-string")
        self.assertEqual(entari_native.account_adapter_name(AccountWithoutAdapter()), "")

    def test_entari_on_ready_clears_account_on_disconnect(self):
        entari_native = _load_module("entari_native_lifecycle_for_test", "utils/entari_native.py")
        captured = []

        def fake_listen(event_type):
            def decorator(func):
                captured.append(func)
                return func
            return decorator

        entari_native.listen = fake_listen
        entari_native.runtime.clear_account()

        class Account:
            self_id = "10000"
            id = "10000"

        calls = []
        entari_native.on_ready(lambda account: calls.append(account))

        asyncio.run(captured[0](types.SimpleNamespace(
            status=entari_native.LoginStatus.ONLINE,
            account=Account(),
        )))
        self.assertIsNotNone(entari_native.runtime.get_account())
        self.assertEqual(len(calls), 1)

        asyncio.run(captured[0](types.SimpleNamespace(
            status=entari_native.LoginStatus.DISCONNECT,
            account=Account(),
        )))
        self.assertIsNone(entari_native.runtime.get_account())

    def test_entari_handler_resolves_injected_async_provider(self):
        entari_native = _load_module(
            "entari_native_inject_for_test", "utils/entari_native.py"
        )

        event = types.SimpleNamespace(guild=types.SimpleNamespace(id="10001"))
        session = types.SimpleNamespace(event=event)
        account = types.SimpleNamespace(self_id="20002")
        calls = []

        async def get_target(event, bot):
            calls.append((event, bot))
            return entari_native.SendDest("10001", "10001", True)

        async def handler(target=entari_native.inject(get_target)):
            return target

        result = asyncio.run(
            entari_native._call_handler(handler, None, session, account, None)
        )

        self.assertIsInstance(result, entari_native.SendDest)
        self.assertEqual(result.parent_id, "10001")
        self.assertEqual(calls, [(event, account)])

    def test_no_direct_adapter_get_name_calls_remain(self):
        offenders = []
        for base in (ROOT / "plugins", ROOT / "utils"):
            for path in base.rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                if ".adapter.get_name(" in text:
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_bili_store_initializes_and_deduplicates_subscriptions(self):
        bili_store = _load_bili_new_module("store")
        bili_models = sys.modules[bili_store.__package__ + ".models"]
        with TemporaryDirectory() as tmp:
            store = bili_store.BiliStore(Path(tmp) / "bilibili.db", Path(tmp) / "missing.db")
            self.assertEqual(store.db_path, Path(tmp) / "bilibili.db")
            store.upsert_target(bili_models.TargetInfo("video", "123", name="UP", latest_id="BV1xx411c7mD"))
            self.assertTrue(store.add_subscription("video", "123", "group", "456"))
            self.assertFalse(store.add_subscription("video", "123", "group", "456"))
            rows = store.subscriptions_for_subscriber("group", "456", "video")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1].name, "UP")
            self.assertTrue(store.remove_subscription("video", "123", "group", "456"))
            self.assertFalse(store.remove_subscription("video", "123", "group", "456"))
            store.close()

    def test_bili_store_migrates_legacy_live_video_dynamic_only(self):
        bili_store = _load_bili_new_module("store")
        with TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(legacy)
            conn.executescript(
                """
                CREATE TABLE up(up_uid TEXT, up_name TEXT, latest_update INTEGER);
                CREATE TABLE liver(liver_uid TEXT, liver_name TEXT, is_live INTEGER, live_room TEXT);
                CREATE TABLE dynamic(uid TEXT, u_name TEXT, pin_id_str TEXT, latest_timestamp INTEGER);
                CREATE TABLE telegram(season_id TEXT, telegram_title TEXT, episode INTEGER, is_finish INTEGER);
                CREATE TABLE up_follower(up_uid TEXT, user_id TEXT, group_id TEXT);
                CREATE TABLE liver_follower(liver_uid TEXT, user_id TEXT, group_id TEXT);
                CREATE TABLE dynamic_follower(uid TEXT, user_id TEXT, group_id TEXT);
                CREATE TABLE telegram_follower(season_id TEXT, user_id TEXT, group_id TEXT);
                INSERT INTO up VALUES('11', 'Video UP', 100);
                INSERT INTO liver VALUES('22', 'Live UP', 1, '222');
                INSERT INTO dynamic VALUES('33', 'Dynamic UP', '', 300);
                INSERT INTO telegram VALUES('44', 'Bangumi', 1, 0);
                INSERT INTO up_follower VALUES('11', NULL, '900');
                INSERT INTO liver_follower VALUES('22', '901', NULL);
                INSERT INTO dynamic_follower VALUES('33', NULL, '900');
                INSERT INTO telegram_follower VALUES('44', NULL, '900');
                """
            )
            conn.commit()
            conn.close()

            store = bili_store.BiliStore(Path(tmp) / "new.db", legacy)
            self.assertEqual(store.get_meta("legacy_migrated"), "1")
            self.assertEqual(store.get_target("video", "11").name, "Video UP")
            self.assertTrue(store.get_target("live", "22").is_live)
            self.assertEqual(store.get_target("dynamic", "33").latest_ts, 300)
            self.assertEqual(len(store.subscriptions_for_subscriber("group", "900")), 2)
            store.close()

    def test_bili_client_link_parsing(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient()

        async def run():
            live = await client.parse_link("https://live.bilibili.com/12345")
            video = await client.parse_link("鐪嬬湅 https://www.bilibili.com/video/BV1xx411c7mD")
            bare = await client.parse_link("BV1xx411c7mD")
            return live, video, bare

        live, video, bare = asyncio.run(run())
        self.assertEqual((live.kind, live.value), ("live", "12345"))
        self.assertEqual((video.kind, video.value), ("video", "BV1xx411c7mD"))
        self.assertEqual((bare.kind, bare.value), ("video", "BV1xx411c7mD"))

    def test_bili_client_risk_retry_refreshes_cookie_then_succeeds(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient()
        calls = []

        async def fake_get_json(url, *, params=None, cookies=None):
            calls.append((url, params))
            if len(calls) == 1:
                return {"code": -352, "message": "椋庢帶鏍￠獙澶辫触"}
            return {
                "code": 0,
                "data": {
                    "list": {
                        "vlist": [
                            {
                                "bvid": "BV1xx411c7mD",
                                "title": "ok",
                                "created": 123,
                                "pic": "https://example.com/cover.jpg",
                            }
                        ]
                    }
                },
            }

        async def fake_refresh_risk():
            client.cookies.set("buvid3", "risk-cookie", domain=".bilibili.com")

        async def fake_refresh_wbi():
            client.img_key = "a" * 32
            client.sub_key = "b" * 32

        async def fake_video_detail(_):
            return bili_client.BiliCard(
                "video",
                "ok",
                author="Detail UP",
                description="desc",
                avatar_url="https://example.com/avatar.jpg",
            )

        client.img_key = "a" * 32
        client.sub_key = "b" * 32
        client._get_json = fake_get_json
        client.refresh_risk_cookies = fake_refresh_risk
        client.refresh_wbi_keys = fake_refresh_wbi
        client.video_by_bvid = fake_video_detail

        card = asyncio.run(client.latest_video("135116630"))
        self.assertEqual(card.item_id, "BV1xx411c7mD")
        self.assertEqual(card.author, "Detail UP")
        self.assertEqual(card.avatar_url, "https://example.com/avatar.jpg")
        self.assertEqual(len(calls), 2)
        self.assertEqual(client.cookies.get("buvid3"), "risk-cookie")

    def test_bili_client_risk_retry_failure_message_mentions_cookie(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient()

        async def fake_get_json(url, *, params=None, cookies=None):
            return {"code": -352, "message": "椋庢帶鏍￠獙澶辫触"}

        async def noop():
            return None

        client.img_key = "a" * 32
        client.sub_key = "b" * 32
        client._get_json = fake_get_json
        client.refresh_risk_cookies = noop
        client.refresh_wbi_keys = noop
        async def fail_rss(_):
            raise bili_client.BiliAPIError("rss failed")
        client._rsshub_first_item = fail_rss

        with self.assertRaisesRegex(Exception, "BILI_SESSDATA/BILI_BUVID3"):
            asyncio.run(client.latest_video("135116630"))

    def test_bili_client_login_cookies_are_preserved_after_risk_refresh(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient(sessdata="sess", buvid3="login-buvid")
        self.assertEqual(client.cookies.get("SESSDATA"), "sess")
        self.assertEqual(client.cookies.get("buvid3"), "login-buvid")

    def test_bili_client_dm_img_params_are_signed_for_video_list(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient(
            dm_img_list="[]",
            dm_img_str="dm-str",
            dm_cover_img_str="cover-str",
        )
        seen_params = {}

        async def fake_get_json(url, *, params=None, cookies=None):
            seen_params.update(params or {})
            return {
                "code": 0,
                "data": {"list": {"vlist": []}},
            }

        client.img_key = "a" * 32
        client.sub_key = "b" * 32
        client._wbi_updated_at = 9999999999
        client._get_json = fake_get_json
        asyncio.run(client.latest_video("135116630"))
        self.assertEqual(seen_params["dm_img_list"], "[]")
        self.assertEqual(seen_params["dm_img_str"], "dm-str")
        self.assertEqual(seen_params["dm_cover_img_str"], "cover-str")
        self.assertIn("w_rid", seen_params)

    def test_bili_client_video_falls_back_to_rsshub_after_http_412(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient()

        async def fake_json_retry(*args, **kwargs):
            raise bili_client.BiliAPIError("Bilibili HTTP 412: arc/search")

        async def fake_rss(route):
            self.assertEqual(route, "/bilibili/user/video/135116630")
            return {
                "title": "RSS Video",
                "author": "RSS UP",
                "description": "desc",
                "cover_url": "https://example.com/cover.jpg",
                "link": "https://www.bilibili.com/video/BV1xx411c7mD",
                "published_at": 123,
            }

        client.img_key = "a" * 32
        client.sub_key = "b" * 32
        client._get_json_with_risk_retry = fake_json_retry
        client._rsshub_first_item = fake_rss
        async def fake_video_detail(_):
            return bili_client.BiliCard(
                "video",
                "RSS Video",
                avatar_url="https://example.com/avatar.jpg",
            )
        client.video_by_bvid = fake_video_detail
        card = asyncio.run(client.latest_video("135116630"))
        self.assertEqual(card.title, "RSS Video")
        self.assertEqual(card.author, "RSS UP")
        self.assertEqual(card.avatar_url, "https://example.com/avatar.jpg")
        self.assertEqual(card.item_id, "BV1xx411c7mD")

    def test_bili_client_video_rsshub_without_bv_still_has_item_id(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient()

        async def fake_json_retry(*args, **kwargs):
            raise bili_client.BiliAPIError("Bilibili HTTP 412: arc/search")

        async def fake_rss(route):
            return {
                "title": "RSS Video",
                "author": "RSS UP",
                "description": "desc",
                "cover_url": "",
                "link": "https://example.com/posts/abc123",
                "published_at": 123,
            }

        client.img_key = "a" * 32
        client.sub_key = "b" * 32
        client._wbi_updated_at = 9999999999
        client._get_json_with_risk_retry = fake_json_retry
        client._rsshub_first_item = fake_rss
        card = asyncio.run(client.latest_video("135116630"))
        self.assertEqual(card.item_id, "abc123")

    def test_bili_client_video_falls_back_to_dynamic_archive(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient()

        async def fake_json_retry(*args, **kwargs):
            raise bili_client.BiliAPIError("Bilibili HTTP 412: arc/search")

        async def fake_rss_video(uid, primary_error):
            raise bili_client.BiliAPIError("video rss down")

        async def fake_dynamic_items(uid):
            return [
                {
                    "id_str": "1206332625750110740",
                    "modules": {
                        "module_author": {"name": "UP", "face": "", "pub_ts": 456},
                        "module_dynamic": {
                            "desc": {"text": "posted video"},
                            "major": {
                                "type": "MAJOR_TYPE_ARCHIVE",
                                "archive": {
                                    "title": "Dynamic Video",
                                    "cover": "https://example.com/cover.jpg",
                                    "jump_url": "https://www.bilibili.com/video/BV1xx411c7mD",
                                },
                            },
                        },
                    },
                }
            ]

        async def fake_video_detail(_):
            return bili_client.BiliCard(
                "video",
                "Detail Video",
                author="Detail UP",
                avatar_url="https://example.com/avatar.jpg",
                published_at=789,
            )

        client.img_key = "a" * 32
        client.sub_key = "b" * 32
        client._get_json_with_risk_retry = fake_json_retry
        client._rsshub_latest_video = fake_rss_video
        client.dynamic_items = fake_dynamic_items
        client.video_by_bvid = fake_video_detail
        card = asyncio.run(client.latest_video("135116630"))
        self.assertEqual(card.card_type, "video")
        self.assertEqual(card.badge, "VIDEO")
        self.assertEqual(card.item_id, "BV1xx411c7mD")
        self.assertEqual(card.author, "UP")
        self.assertEqual(card.avatar_url, "https://example.com/avatar.jpg")

    def test_bili_client_video_dynamic_fallback_uses_dynamic_rss_bv(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient()

        async def fake_json_retry(*args, **kwargs):
            raise bili_client.BiliAPIError("Bilibili HTTP 412")

        async def fake_rss(route):
            if route == "/bilibili/user/video/135116630":
                raise bili_client.BiliAPIError("video rss down")
            self.assertEqual(route, "/bilibili/user/dynamic/135116630")
            return {
                "title": "posted video",
                "author": "RSS UP",
                "description": "new video https://www.bilibili.com/video/BV1xx411c7mD",
                "cover_url": "",
                "link": "https://t.bilibili.com/123456",
                "published_at": 456,
            }

        async def fake_video_detail(_):
            return bili_client.BiliCard("video", "Detail Video", avatar_url="https://example.com/avatar.jpg")

        client.img_key = "a" * 32
        client.sub_key = "b" * 32
        client._get_json_with_risk_retry = fake_json_retry
        client._rsshub_first_item = fake_rss
        client.video_by_bvid = fake_video_detail
        card = asyncio.run(client.latest_video("135116630"))
        self.assertEqual(card.item_id, "BV1xx411c7mD")
        self.assertEqual(card.card_type, "video")
        self.assertEqual(card.badge, "VIDEO")

    def test_bili_client_video_dynamic_fallback_reports_no_video_item(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient()

        async def fake_dynamic_items(uid):
            return [
                {
                    "id_str": "dynamic-1",
                    "modules": {
                        "module_author": {"name": "UP", "pub_ts": 456},
                        "module_dynamic": {"desc": {"text": "plain dynamic"}, "major": {"type": "MAJOR_TYPE_DRAW"}},
                    },
                }
            ]

        client.dynamic_items = fake_dynamic_items
        with self.assertRaisesRegex(Exception, "no video item"):
            asyncio.run(client._dynamic_latest_video("135116630"))

    def test_bili_client_dynamic_falls_back_to_rsshub_after_risk(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient()

        async def fake_json_retry(*args, **kwargs):
            raise bili_client.BiliRiskControlError("dynamic 135116630")

        async def fake_rss(route):
            self.assertEqual(route, "/bilibili/user/dynamic/135116630")
            return {
                "title": "RSS Dynamic",
                "author": "RSS UP",
                "description": "dynamic desc",
                "cover_url": "https://example.com/dynamic.jpg",
                "link": "https://t.bilibili.com/123456",
                "published_at": 456,
            }

        client._get_json_with_risk_retry = fake_json_retry
        client._rsshub_first_item = fake_rss
        items = asyncio.run(client.dynamic_items("135116630"))
        card = client._dynamic_card_from_item(items[0], "135116630")
        self.assertEqual(card.title, "dynamic desc")
        self.assertEqual(card.cover_url, "https://example.com/dynamic.jpg")

    def test_bili_client_rss_parser_extracts_item_and_cover(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient()
        author = "\u82b1\u56ed\u305f\u3048"
        xml = f"""<?xml version="1.0" encoding="UTF-8" ?>
        <rss><channel><title>{author} \u7684 Bilibili \u6295\u7a3f\u89c6\u9891</title><item>
        <title>Video Title</title>
        <link>https://www.bilibili.com/video/BV1xx411c7mD</link>
        <pubDate>Sun, 24 May 2026 10:00:00 GMT</pubDate>
        <description><![CDATA[<p>Hello</p><img src="https://example.com/a.jpg"/>]]></description>
        </item></channel></rss>"""
        item = client._parse_rss_first_item(xml)
        self.assertEqual(item["title"], "Video Title")
        self.assertEqual(item["author"], author)
        self.assertEqual(item["cover_url"], "https://example.com/a.jpg")
        self.assertIn("Hello", item["description"])

    def test_bili_client_dynamic_archive_extracts_bv_as_item_id(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient()
        item = {
            "id_str": "1206332625750110740",
            "modules": {
                "module_author": {"name": "UP", "face": "https://example.com/avatar.jpg", "pub_ts": 100},
                "module_dynamic": {
                    "desc": {"text": "posted video"},
                    "major": {
                        "type": "MAJOR_TYPE_ARCHIVE",
                        "archive": {
                            "title": "Test Video",
                            "cover": "https://example.com/cover.jpg",
                            "jump_url": "https://www.bilibili.com/video/BV1xx411c7mD",
                        },
                    },
                },
            },
        }
        card = client._dynamic_card_from_item(item, "135116630")
        self.assertEqual(card.item_id, "BV1xx411c7mD")
        self.assertEqual(card.title, "Test Video")

    def test_bili_client_clean_rsshub_author_title(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient()
        author = "\u82b1\u56ed\u305f\u3048"
        self.assertEqual(client._clean_rsshub_author_title(f"{author} \u7684 Bilibili \u6295\u7a3f\u89c6\u9891"), author)
        self.assertEqual(client._clean_rsshub_author_title(f"{author} \u7684 bilibili \u52a8\u6001"), author)
        self.assertEqual(client._clean_rsshub_author_title("Plain Name"), "Plain Name")

    def test_bili_client_rsshub_tries_backup_instance(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient(rsshub_base_urls=["https://primary.example", "https://backup.example"])
        calls = []

        class FakeResponse:
            def __init__(self, url):
                self.url = url
                self.text = """<rss><channel><title>UP</title><item><title>ok</title><link>https://x</link></item></channel></rss>"""

            def raise_for_status(self):
                if "primary" in self.url:
                    raise Exception("primary down")

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url):
                calls.append(url)
                return FakeResponse(url)

        with patch.object(bili_client.httpx, "AsyncClient", FakeAsyncClient):
            item = asyncio.run(client._rsshub_first_item("/route"))
        self.assertEqual(item["title"], "ok")
        self.assertGreaterEqual(len(calls), 2)

    def test_bili_client_rsshub_all_failures_are_compacted(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient(rsshub_base_urls=["https://one.example", "https://two.example", "https://three.example", "https://four.example"])

        async def fail_fetch(base_url, route):
            raise bili_client.BiliAPIError(f"{base_url}: down")

        client.rsshub_base_urls = ["https://one.example", "https://two.example", "https://three.example", "https://four.example"]
        client._rsshub_fetch_first_item = fail_fetch
        with self.assertRaisesRegex(Exception, "all RSSHub instances unavailable"):
            try:
                asyncio.run(client._rsshub_first_item("/route"))
            except Exception as exc:
                msg = str(exc)
                self.assertIn("https://one.example", msg)
                self.assertIn("https://two.example", msg)
                self.assertIn("https://three.example", msg)
                self.assertIn("and 1 more", msg)
                self.assertNotIn("https://four.example: down; https://four.example", msg)
                raise

    def test_bili_client_rsshub_configured_urls_precede_defaults_and_dedupe(self):
        bili_client = _load_bili_new_module("client")
        client = bili_client.BiliClient(rsshub_base_urls=["https://custom.example/", "https://rss.materium.io"])
        self.assertEqual(client.rsshub_base_urls[0], "https://custom.example")
        self.assertEqual(client.rsshub_base_urls.count("https://rss.materium.io"), 1)
        self.assertIn("https://rsshub.app", client.rsshub_base_urls)

    def test_bili_service_all_follow_keeps_live_when_video_dynamic_fail(self):
        bili_store = _load_bili_new_module("store")
        bili_service = _load_bili_new_module("service")
        bili_models = sys.modules[bili_store.__package__ + ".models"]

        class FakeClient:
            async def resolve_live_target(self, value):
                return bili_models.TargetInfo("live", value, name="Live UP", room_id="100")

            async def resolve_video_target(self, value):
                raise RuntimeError("risk")

            async def resolve_dynamic_target(self, value):
                raise RuntimeError("risk")

        with TemporaryDirectory() as tmp:
            store = bili_store.BiliStore(Path(tmp) / "bilibili.db", Path(tmp) / "missing.db")
            service = bili_service.BiliService(store, FakeClient())
            ok, failed = asyncio.run(service.follow("all", ["135116630"], "group", "900"))
            self.assertEqual(len(ok), 1)
            self.assertEqual(len(failed), 2)
            self.assertEqual(len(store.subscriptions_for_subscriber("group", "900", "live")), 1)
            store.close()

    def test_bili_service_check_video_refines_uid_name_and_empty_avatar(self):
        bili_store = _load_bili_new_module("store")
        bili_service = _load_bili_new_module("service")
        bili_models = sys.modules[bili_store.__package__ + ".models"]

        class FakeClient:
            async def latest_video(self, uid):
                return bili_models.BiliCard(
                    "video",
                    "New Video",
                    author="鑺卞洯銇熴亪",
                    avatar_url="https://example.com/avatar.jpg",
                    item_id="BV1xx411c7mD",
                    published_at=100,
                )

        with TemporaryDirectory() as tmp:
            store = bili_store.BiliStore(Path(tmp) / "bilibili.db", Path(tmp) / "missing.db")
            store.upsert_target(bili_models.TargetInfo("video", "135116630", name="135116630", latest_id="old"))
            store.add_subscription("video", "135116630", "group", "900")
            service = bili_service.BiliService(store, FakeClient())
            sent = []

            async def fake_broadcast(kind, uid, card):
                sent.append(card)

            service.broadcast = fake_broadcast
            asyncio.run(service.check_video())
            target = store.get_target("video", "135116630")
            self.assertEqual(target.name, "鑺卞洯銇熴亪")
            self.assertEqual(target.avatar_url, "https://example.com/avatar.jpg")
            self.assertEqual(sent[0].author, "鑺卞洯銇熴亪")
            store.close()

    def test_bili_service_check_video_does_not_override_custom_name(self):
        bili_store = _load_bili_new_module("store")
        bili_service = _load_bili_new_module("service")
        bili_models = sys.modules[bili_store.__package__ + ".models"]

        class FakeClient:
            async def latest_video(self, uid):
                return bili_models.BiliCard("video", "New Video", author="鑺卞洯銇熴亪", item_id="BV1xx411c7mD", published_at=100)

        with TemporaryDirectory() as tmp:
            store = bili_store.BiliStore(Path(tmp) / "bilibili.db", Path(tmp) / "missing.db")
            store.upsert_target(bili_models.TargetInfo("video", "135116630", name="鑷畾涔夊悕", latest_id="old"))
            store.add_subscription("video", "135116630", "group", "900")
            service = bili_service.BiliService(store, FakeClient())
            service.broadcast = lambda *args, **kwargs: asyncio.sleep(0)
            asyncio.run(service.check_video())
            self.assertEqual(store.get_target("video", "135116630").name, "鑷畾涔夊悕")
            store.close()

    def test_bili_service_check_video_repeated_failures_are_throttled(self):
        bili_store = _load_bili_new_module("store")
        bili_service = _load_bili_new_module("service")
        bili_models = sys.modules[bili_store.__package__ + ".models"]

        class FakeClient:
            async def latest_video(self, uid):
                raise RuntimeError("same failure")

        with TemporaryDirectory() as tmp:
            store = bili_store.BiliStore(Path(tmp) / "bilibili.db", Path(tmp) / "missing.db")
            store.upsert_target(bili_models.TargetInfo("video", "135116630", name="UP"))
            store.add_subscription("video", "135116630", "group", "900")
            service = bili_service.BiliService(store, FakeClient())
            with patch.object(bili_service.logger, "warning") as warning, patch.object(bili_service.logger, "debug") as debug:
                asyncio.run(service.check_video())
                asyncio.run(service.check_video())
            self.assertEqual(warning.call_count, 1)
            self.assertEqual(debug.call_count, 1)
            store.close()

    def test_bili_service_check_dynamic_skips_video_dynamic_when_video_subscribed(self):
        bili_store = _load_bili_new_module("store")
        bili_service = _load_bili_new_module("service")
        bili_models = sys.modules[bili_store.__package__ + ".models"]

        class FakeClient:
            def _dynamic_card_from_item(self, item, uid):
                return bili_models.BiliCard(
                    "dynamic",
                    "Video Dynamic",
                    author="UP",
                    url="https://www.bilibili.com/video/BV1xx411c7mD",
                    item_id="BV1xx411c7mD",
                    published_at=100,
                )

            async def dynamic_items(self, uid):
                return [{"id_str": "dynamic-1"}]

        with TemporaryDirectory() as tmp:
            store = bili_store.BiliStore(Path(tmp) / "bilibili.db", Path(tmp) / "missing.db")
            store.upsert_target(bili_models.TargetInfo("video", "135116630", name="UP"))
            store.upsert_target(bili_models.TargetInfo("dynamic", "135116630", name="UP", latest_ts=1))
            store.add_subscription("video", "135116630", "group", "900")
            store.add_subscription("dynamic", "135116630", "group", "900")
            service = bili_service.BiliService(store, FakeClient())
            sent = []

            async def fake_broadcast(kind, uid, card):
                sent.append(card)

            service.broadcast = fake_broadcast
            asyncio.run(service.check_dynamic())
            self.assertEqual(sent, [])
            self.assertTrue(store.has_seen("dynamic", "135116630", "BV1xx411c7mD"))
            self.assertEqual(store.get_target("dynamic", "135116630").latest_id, "BV1xx411c7mD")
            store.close()

    def test_bili_service_check_dynamic_sends_video_dynamic_without_video_subscription(self):
        bili_store = _load_bili_new_module("store")
        bili_service = _load_bili_new_module("service")
        bili_models = sys.modules[bili_store.__package__ + ".models"]

        class FakeClient:
            def _dynamic_card_from_item(self, item, uid):
                return bili_models.BiliCard(
                    "dynamic",
                    "Video Dynamic",
                    author="UP",
                    url="https://www.bilibili.com/video/BV1xx411c7mD",
                    item_id="BV1xx411c7mD",
                    published_at=100,
                )

            async def dynamic_items(self, uid):
                return [{"id_str": "dynamic-1"}]

        with TemporaryDirectory() as tmp:
            store = bili_store.BiliStore(Path(tmp) / "bilibili.db", Path(tmp) / "missing.db")
            store.upsert_target(bili_models.TargetInfo("dynamic", "135116630", name="UP", latest_ts=1))
            store.add_subscription("dynamic", "135116630", "group", "900")
            service = bili_service.BiliService(store, FakeClient())
            sent = []

            async def fake_broadcast(kind, uid, card):
                sent.append((kind, card.item_id))

            service.broadcast = fake_broadcast
            asyncio.run(service.check_dynamic())
            self.assertEqual(sent, [("dynamic", "BV1xx411c7mD")])
            store.close()

    def test_bili_draw_card_generates_png_without_remote_images(self):
        bili_draw = _load_bili_new_module("draw")
        bili_models = sys.modules[bili_draw.__package__ + ".models"]
        card = bili_models.BiliCard(
            "video",
            "A very long title " * 10,
            author="UP",
            description="description " * 40,
            badge="VIDEO",
            uid="123",
            item_id="BV1xx411c7mD",
        )
        png = asyncio.run(bili_draw.draw_bili_card(card))
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_bili_draw_centered_text_position_is_inside_box_center(self):
        bili_draw = _load_bili_new_module("draw")
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (200, 80))
        draw = ImageDraw.Draw(image)
        box = (40, 20, 140, 52)
        x, y = bili_draw._centered_text_position(draw, box, "VIDEO", bili_draw.FONT_BADGE)
        bbox = draw.textbbox((x, y), "VIDEO", font=bili_draw.FONT_BADGE)
        text_center = ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
        box_center = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        self.assertLess(abs(text_center[0] - box_center[0]), 1.0)
        self.assertLess(abs(text_center[1] - box_center[1]), 1.0)

    def _assert_valid_mcsm_png(self, output):
        from PIL import Image

        image = Image.open(BytesIO(output.getvalue()))
        self.assertIn(image.mode, ("RGB", "RGBA"))
        self.assertGreaterEqual(image.size[0], 900)
        self.assertGreaterEqual(image.size[1], 250)

    def test_mcsm_draw_core_cards_generate_valid_pngs(self):
        draw = _load_mcsm_draw_module()
        overview = draw.draw_panel_overview(
            {
                "daemon-1": {
                    "name": "node-a",
                    "uuid": "daemon-1",
                    "online": 1,
                    "total": 2,
                    "instances": [
                        {
                            "uuid": "inst-1",
                            "name": "Survival Server With A Long Display Name",
                            "alias": "survival",
                            "status": 3,
                            "hidden": False,
                        },
                        {
                            "uuid": "inst-2",
                            "name": "Creative",
                            "alias": "",
                            "status": 0,
                            "hidden": True,
                        },
                    ],
                },
                "daemon-2": {
                    "name": "node-b",
                    "uuid": "daemon-2",
                    "online": 0,
                    "total": 0,
                    "instances": [],
                },
            },
            "http://panel.example",
            is_superuser=True,
            show_all=True,
        )
        empty_overview = draw.draw_panel_overview(
            {},
            "http://panel.example",
            is_superuser=True,
            show_all=False,
        )
        status = draw.draw_status(
            "survival",
            {
                "status": 3,
                "instanceName": "Survival",
                "started": 1,
                "space": 17520,
                "processInfo": {"cpu": 3, "memory": 4824},
                "info": {"currentPlayers": 0, "maxPlayers": 20, "version": "1.21.1"},
                "config": {
                    "nickname": "Survival",
                    "type": "minecraft/java",
                    "startCommand": "java -Xms2G -Xmx8G -XX:+UseG1GC -XX:+ParallelRefProcEnabled -jar paper-server-preview.jar nogui",
                    "lastDatetime": 1779290968000,
                    "endTime": 0,
                    "docker": {
                        "memory": 8192,
                        "maxSpace": 20480,
                        "ports": [
                            {"protocol": "tcp", "hostPort": 51568, "containerPort": 25565},
                            {"protocol": "udp", "hostPort": 51568, "containerPort": 25565},
                        ],
                    },
                    "eventTask": {"autoRestart": True},
                },
            },
            {
                "uuid": "inst-1-very-long-uuid-value-for-two-column-layout-check",
                "daemonId": "daemon-1-very-long-daemon-value-for-two-column-layout-check",
                "admins": ["10001", "10002"],
            },
        )
        legacy_status = draw.draw_status(
            "legacy",
            {
                "status": 0,
                "instanceName": "Legacy Server",
                "space": None,
                "processInfo": {},
                "info": {"currentPlayers": -1, "maxPlayers": -1},
                "config": {
                    "type": "java",
                    "lastDatetime": "2026-05-21 02:09:28",
                    "pingConfig": {"port": 25565},
                },
            },
            {"uuid": "inst-legacy", "daemonId": "daemon-legacy", "admins": []},
        )
        console = draw.draw_console_output(
            "survival",
            "say hello",
            "\n".join(f"[12:{i:02d}] long log line with value {i} and extra text" for i in range(80)),
        )
        admin = draw.draw_admin_list("survival", ["10001", "10002"])
        success = draw.draw_notice("操作成功", ["实例已启动"], level="success")
        error = draw.draw_error("获取面板数据失败: timeout")

        for output in [overview, empty_overview, status, legacy_status, console, admin, success, error]:
            self._assert_valid_mcsm_png(output)

    def test_mcsm_status_metric_icons_use_local_assets_with_fallback(self):
        draw = _load_mcsm_draw_module()

        for name in ("cpu.png", "memory.png", "disk.png"):
            self.assertTrue((draw.METRIC_ICON_DIR / name).is_file())

        status = draw.draw_status(
            "survival",
            {
                "status": 3,
                "instanceName": "Survival",
                "space": 1024,
                "processInfo": {"cpu": 12, "memory": 512},
                "config": {"docker": {"memory": 1024, "maxSpace": 2048}},
            },
            {"uuid": "inst-1", "daemonId": "daemon-1", "admins": []},
        )
        self._assert_valid_mcsm_png(status)

        with TemporaryDirectory() as tmp:
            draw.METRIC_ICON_DIR = Path(tmp)
            draw._METRIC_ICON_CACHE.clear()
            fallback_status = draw.draw_status(
                "survival",
                {
                    "status": 3,
                    "instanceName": "Survival",
                    "space": 1024,
                    "processInfo": {"cpu": 12, "memory": 512},
                    "config": {"docker": {"memory": 1024, "maxSpace": 2048}},
                },
                {"uuid": "inst-1", "daemonId": "daemon-1", "admins": []},
            )
        self._assert_valid_mcsm_png(fallback_status)

    def test_mcsm_console_command_output_uses_only_new_lines(self):
        draw = _load_mcsm_draw_module()

        before = "\n".join(
            [
                "[12:00:00 INFO]: server started",
                "[12:00:01 INFO]: old list output",
            ]
        )
        after = "\n".join(
            [
                before,
                "> list",
                "[12:00:02 INFO]: There are 2 of a max of 20 players online",
                "Steve, Alex",
            ]
        )

        result = draw.extract_command_output(before, after, "list")
        rendered = draw.render_console_text(result, command="list", show_command=True, empty_text="(empty)")

        self.assertNotIn("old list output", rendered)
        self.assertNotIn("> list", rendered)
        self.assertEqual(rendered.splitlines()[0], ">list")
        self.assertIn("There are 2", rendered)
        self.assertIn("Steve, Alex", rendered)
        rows, _, _ = draw._console_display_rows(result, command="list")
        rows = [(">list", draw.TEXT)] + rows
        self.assertEqual(rows[0], (">list", draw.TEXT))

    def test_mcsm_console_command_output_falls_back_to_last_prompt(self):
        draw = _load_mcsm_draw_module()

        output = "\n".join(
            [
                "[12:00:00 INFO]: old",
                "> list",
                "[12:00:01 INFO]: old command output",
                "> list",
                "[12:00:02 INFO]: fresh command output",
            ]
        )

        result = draw.extract_command_output("missing before snapshot", output, "list")

        self.assertNotIn("old command output", result)
        self.assertNotIn("> list", result)
        self.assertIn("fresh command output", result)

    def test_mcsm_console_command_output_accepts_echo_variants(self):
        draw = _load_mcsm_draw_module()

        variants = [">list", "> list", "$list", "$ list", "list"]
        for marker in variants:
            with self.subTest(marker=marker):
                output = "\n".join(
                    [
                        "[12:00:00 INFO]: old command output",
                        marker,
                        "[12:00:01 INFO]: fresh command output",
                    ]
                )
                result = draw.extract_command_output("missing before snapshot", output, "list")
                rendered = draw.render_console_text(result, command="list", show_command=True, empty_text="(empty)")

                self.assertNotIn("old command output", rendered)
                self.assertEqual(rendered.splitlines()[0], ">list")
                self.assertIn("fresh command output", rendered)

    def test_mcsm_console_command_output_does_not_return_full_after_without_boundary(self):
        draw = _load_mcsm_draw_module()

        output = "\n".join(
            [
                "[12:00:00 INFO]: old command output",
                "[12:00:01 INFO]: still old",
            ]
        )

        result = draw.extract_command_output("missing before snapshot", output, "list")
        rendered = draw.render_console_text(result, command="list", show_command=True, empty_text="(empty)")

        self.assertEqual(result, "")
        self.assertEqual(rendered, ">list\n(empty)")

    def test_mcsm_console_log_entries_keep_continuations_and_limit_entries(self):
        draw = _load_mcsm_draw_module()

        raw = "\n".join(
            [f"[12:00:{i:02d} INFO]: entry {i}\ncontinuation {i}" for i in range(12)]
        )

        entries = draw.parse_console_entries(raw)
        text = draw.render_console_text(raw, max_entries=10)

        self.assertEqual(len(entries), 12)
        self.assertEqual(entries[0]["lines"], ["[12:00:00 INFO]: entry 0", "continuation 0"])
        self.assertNotIn("[12:00:00 INFO]: entry 0", text)
        self.assertNotIn("[12:00:01 INFO]: entry 1", text)
        self.assertIn("entry 2", text)
        self.assertIn("continuation 11", text)

        console = draw.draw_console_output("survival", "", raw, max_entries=10)
        self._assert_valid_mcsm_png(console)

    def test_mcsm_console_log_level_entries_start_new_entries(self):
        draw = _load_mcsm_draw_module()

        raw = "\n".join(
            [
                "ATA DATA - NOISE: UNKNOWN",
                "[20:07:45] [pool-5-thread-1/ERROR] [updatechecklib]: Failed to retrieve update JSON for CoreTweaks:",
                "https://raw.githubusercontent.com/makamys/GTNewHorizons/master/updatejson/update.json",
                "[20:07:45] [pool-5-thread-1/INFO] [updatechecklib]: Found 0 updates.",
                "[20:07:45] [pool-5-thread-1/INFO] [updatechecklib]: Wrote update check results to updates.html",
                "[ERROR] close command timed out and instance state was restored",
                "[ERROR] restart state error, restart plan canceled",
            ]
        )

        entries = draw.parse_console_entries(raw)

        self.assertEqual(len(entries), 6)
        self.assertEqual(entries[1]["lines"][1], "https://raw.githubusercontent.com/makamys/GTNewHorizons/master/updatejson/update.json")
        self.assertEqual(entries[-2]["lines"], ["[ERROR] close command timed out and instance state was restored"])
        self.assertEqual(entries[-1]["lines"], ["[ERROR] restart state error, restart plan canceled"])

    def test_mcsm_console_log_mode_counts_plain_top_level_records(self):
        draw = _load_mcsm_draw_module()

        raw = "\n".join(
            [
                "ATA DATA - NOISE: UNKNOWN",
                "[20:07:45] [pool-5-thread-1/ERROR] [updatechecklib]: Failed to retrieve update JSON for CoreTweaks:",
                "https://raw.githubusercontent.com/makamys/GTNewHorizons/master/updatejson/update.json",
                "[20:07:45] [pool-5-thread-1/INFO] [updatechecklib]: Found 0 updates.",
                "[20:07:45] [pool-5-thread-1/INFO] [updatechecklib]: Wrote update check results to updates.html",
                "[ERROR] close command timed out and instance state was restored",
                "[ERROR] restart state error, restart plan canceled",
                "list",
                "[12:32:45] [Server thread/INFO]: There are 0/200 players online:",
                "[12:32:45] [Server thread/INFO]:",
            ]
        )

        entries = draw.parse_console_entries(raw, mode="log")
        entry_lines = [entry["lines"] for entry in entries]

        self.assertEqual(len(entries), 8)
        self.assertEqual(entries[0]["lines"][1], "https://raw.githubusercontent.com/makamys/GTNewHorizons/master/updatejson/update.json")
        self.assertIn(["[ERROR] restart state error, restart plan canceled"], entry_lines)
        self.assertIn(["list"], entry_lines)
        self.assertIn(["[12:32:45] [Server thread/INFO]: There are 0/200 players online:"], entry_lines)
        self.assertIn(">list", draw.render_console_text(raw, max_entries=10, mode="log"))
        self.assertNotIn(">ATA DATA", draw.render_console_text(raw, max_entries=10, mode="log"))
        rows, _, _ = draw._console_display_rows(raw, max_entries=10, display_line_limit=None, mode="log")
        self.assertIn((">list", draw.TEXT), rows)

    def test_mcsm_console_log_mode_drops_leading_partial_fragment(self):
        draw = _load_mcsm_draw_module()

        raw = "\n".join(
            ["inecraft:the_end"]
            + [f"[12:00:{i:02d}] [Server thread/INFO]: complete record {i}" for i in range(10)]
        )

        entries = draw.parse_console_entries(raw, mode="log")
        text = draw.render_console_text(raw, max_entries=10, mode="log")

        self.assertEqual(len(entries), 10)
        self.assertNotIn("inecraft:the_end", text)
        self.assertIn("[12:00:00] [Server thread/INFO]: complete record 0", text)
        self.assertIn("[12:00:09] [Server thread/INFO]: complete record 9", text)

    def test_mcsm_console_log_mode_does_not_pad_when_only_six_complete_records(self):
        draw = _load_mcsm_draw_module()

        raw = "\n".join(
            ["inecraft:the_end"]
            + [f"[19:55:58] [Server thread/INFO]: shutdown record {i}" for i in range(5)]
            + ["[INFO] 实例已停止。"]
        )

        entries = draw.parse_console_entries(raw, mode="log")

        self.assertEqual(len(entries), 6)
        self.assertEqual(entries[0]["lines"], ["[19:55:58] [Server thread/INFO]: shutdown record 0"])
        self.assertEqual(entries[-1]["lines"], ["[INFO] 实例已停止。"])

    def test_mcsm_console_log_mode_keeps_stack_continuations(self):
        draw = _load_mcsm_draw_module()

        raw = "\n".join(
            [
                "[ERROR] exception happened",
                "    at com.example.Main.run(Main.java:1)",
                "Caused by: java.lang.IllegalStateException",
                "... 12 more",
                "list",
            ]
        )

        entries = draw.parse_console_entries(raw, mode="log")

        self.assertEqual(len(entries), 2)
        self.assertEqual(len(entries[0]["lines"]), 4)
        self.assertEqual(entries[1]["lines"], ["list"])

    def test_mcsm_console_log_mode_limits_to_last_ten_records(self):
        draw = _load_mcsm_draw_module()

        raw = "\n".join(f"plain command {i}" for i in range(12))
        text = draw.render_console_text(raw, max_entries=10, mode="log")
        lines = text.splitlines()

        self.assertNotIn("plain command 0", lines)
        self.assertNotIn("plain command 1", lines)
        self.assertIn(">plain command 2", lines)
        self.assertIn(">plain command 11", lines)

    def test_mcsm_console_log_mode_supports_all_and_custom_entry_limits(self):
        draw = _load_mcsm_draw_module()

        raw = "\n".join(f"[12:00:{i:02d}] [Server thread/INFO]: entry {i}" for i in range(12))
        all_text = draw.render_console_text(raw, max_entries=None, mode="log")
        five_text = draw.render_console_text(raw, max_entries=5, mode="log")

        self.assertIn("entry 0", all_text)
        self.assertIn("entry 11", all_text)
        self.assertNotIn("entry 6", five_text)
        self.assertIn("entry 7", five_text)
        self.assertIn("entry 11", five_text)

    def test_mcsm_console_level_only_logs_limit_to_last_ten(self):
        draw = _load_mcsm_draw_module()

        raw = "\n".join(f"[ERROR] line {i}" for i in range(12))
        text = draw.render_console_text(raw, max_entries=10)
        lines = text.splitlines()

        self.assertNotIn("[ERROR] line 0", lines)
        self.assertNotIn("[ERROR] line 1", lines)
        self.assertIn("[ERROR] line 2", lines)
        self.assertIn("[ERROR] line 11", lines)

    def test_mcsm_log_path_does_not_truncate_before_parsing(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")
        start = source.index("async def _cmd_log")
        end = source.index("# ── hide / unhide", start)
        log_source = source[start:end]

        self.assertNotIn("raw = raw[-7000:]", log_source)
        self.assertIn('get_instance_output(info["uuid"], info["daemonId"], size=2048)', log_source)
        self.assertIn("render_console_text(raw, max_entries=log_limit, mode=\"log\"", log_source)
        self.assertIn("await _finish_image_or_text(", log_source)
        self.assertIn("draw_console_output,", log_source)
        self.assertIn("display_line_limit=None", log_source)
        self.assertIn('mode="log"', log_source)

    def test_mcsm_log_route_parses_all_and_custom_limits_source_paths(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")
        start = source.index("def _parse_log_args")
        end = source.index("async def _cmd_log", start)
        parser_source = source[start:end]
        route_start = source.index("if subcmd in (\"log\", \"日志\")")
        route_end = source.index("# ── hide / unhide", route_start)
        route_source = source[route_start:route_end]

        self.assertIn("LOG_DEFAULT_ENTRIES = 10", source)
        self.assertIn("LOG_MAX_ENTRIES = 200", source)
        self.assertIn("arg in (\"-a\", \"--all\")", parser_source)
        self.assertIn("arg.startswith(\"-n\")", parser_source)
        self.assertIn("arg.startswith(\"--num=\")", parser_source)
        self.assertIn("parsed > LOG_MAX_ENTRIES", parser_source)
        self.assertIn("alias, log_limit, log_error = _parse_log_args(parts)", route_source)
        self.assertIn("log_limit=log_limit", route_source)

    def test_mcsm_console_row_limit_keeps_entry_boundaries(self):
        draw = _load_mcsm_draw_module()

        first_entry = "[12:00:00 INFO]: first entry\n" + "\n".join(f"continuation {i}" for i in range(90))
        later_entries = "\n".join(f"[12:00:{i:02d} INFO]: entry {i}" for i in range(1, 10))
        raw = first_entry + "\n" + later_entries

        rows, omitted_entries, omitted_lines = draw._console_display_rows(raw, max_entries=10)
        rendered_lines = [line for line, _ in rows]

        self.assertGreaterEqual(omitted_entries, 1)
        self.assertGreater(omitted_lines, 0)
        self.assertNotIn("continuation 89", rendered_lines[:1])
        self.assertEqual(rendered_lines[0], "[12:00:01 INFO]: entry 1")

    def test_mcsm_console_log_preserve_entries_disables_visual_line_limit(self):
        draw = _load_mcsm_draw_module()
        from PIL import Image

        raw = "\n".join(
            "[12:00:{:02d} INFO]: entry {}\n{}".format(i, i, "\n".join(f"continuation {i}-{j}" for j in range(9)))
            for i in range(10)
        )

        limited_rows, limited_omitted_entries, _ = draw._console_display_rows(raw, max_entries=10)
        preserved_rows, preserved_omitted_entries, preserved_omitted_lines = draw._console_display_rows(
            raw,
            max_entries=10,
            display_line_limit=None,
        )
        preserved_text = "\n".join(line for line, _ in preserved_rows)

        self.assertGreater(limited_omitted_entries, 0)
        self.assertEqual(preserved_omitted_entries, 0)
        self.assertEqual(preserved_omitted_lines, 0)
        self.assertIn("[12:00:00 INFO]: entry 0", preserved_text)
        self.assertIn("continuation 9-8", preserved_text)
        self.assertGreater(len(preserved_rows), draw.CONSOLE_DISPLAY_LINES)

        png = draw.draw_console_output("survival", "", raw, max_entries=10, display_line_limit=None)
        image = Image.open(png)
        self.assertGreater(image.height, 154 + 58 + draw.CONSOLE_DISPLAY_LINES * draw.CONSOLE_LINE_H)

    def test_mcsm_console_wraps_long_text_by_pixel_width(self):
        draw = _load_mcsm_draw_module()
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (1, 1))
        drawer = ImageDraw.Draw(image)
        max_px = draw.CARD_W - draw.PADDING * 2
        long_cn = "[ERROR] " + "关闭命令已发出但长时间未能关闭实例，可能是实例关闭命令错误或实例进程假死导致，" * 5
        long_url = "https://raw.githubusercontent.com/makamys/GTNewHorizons/master/updatejson/update.json" * 2

        for wrapped in draw._wrap_console_text(drawer, long_cn, max_px) + draw._wrap_console_text(drawer, long_url, max_px):
            self.assertLessEqual(draw._text_width(drawer, wrapped, draw.FONT_MONO), max_px + 1)

    def test_mcsm_status_summary_resource_fields(self):
        draw = _load_mcsm_draw_module()

        official = draw.status_summary(
            {
                "space": 17520,
                "processInfo": {"cpu": 3, "memory": 4824},
                "config": {"docker": {"memory": 8192, "maxSpace": 20480}},
            },
            {"admins": []},
        )
        self.assertEqual(official["cpu"], "3%")
        self.assertEqual(official["memory"], "4.71 GiB / 8.00 GiB")
        self.assertEqual(official["disk"], "17.11 GiB / 20.00 GiB")
        self.assertAlmostEqual(official["cpu_ratio"], 0.03)
        self.assertAlmostEqual(official["memory_ratio"], 4824 / 8192)
        self.assertAlmostEqual(official["disk_ratio"], 17520 / 20480)

        compatible = draw.status_summary(
            {
                "space": 0,
                "resources": {"cpu": 0.27, "memory": 512, "space": 1024},
                "docker": {"memory": 1024, "maxSpace": 2048},
                "config": {},
            },
            {"admins": []},
        )
        self.assertEqual(compatible["cpu"], "27%")
        self.assertEqual(compatible["memory"], "512 MiB / 1.00 GiB")
        self.assertEqual(compatible["disk"], "1.00 GiB / 2.00 GiB")
        self.assertAlmostEqual(compatible["cpu_ratio"], 0.27)
        self.assertAlmostEqual(compatible["memory_ratio"], 0.5)
        self.assertAlmostEqual(compatible["disk_ratio"], 0.5)

        variants = draw.status_summary(
            {
                "processInfo": {"cpuUsage": 18, "memoryUsage": 2048, "totalMemory": 4096},
                "info": {"spaceUsage": 3072},
                "resources": {
                    "memory": {"used": 1024, "total": 2048},
                    "disk": {"used": 4096, "total": 8192},
                },
                "config": {},
            },
            {"admins": []},
        )
        self.assertEqual(variants["cpu"], "18%")
        self.assertEqual(variants["memory"], "2.00 GiB / 4.00 GiB")
        self.assertEqual(variants["disk"], "3.00 GiB / 8.00 GiB")
        self.assertAlmostEqual(variants["cpu_ratio"], 0.18)
        self.assertAlmostEqual(variants["memory_ratio"], 0.5)
        self.assertAlmostEqual(variants["disk_ratio"], 3072 / 8192)

        byte_memory = draw.status_summary(
            {
                "processInfo": {"memoryUsage": 5058666496},
                "config": {"docker": {"memory": 8192}},
            },
            {"admins": []},
        )
        self.assertEqual(byte_memory["memory"], "4.71 GiB / 8.00 GiB")
        self.assertAlmostEqual(byte_memory["memory_ratio"], (5058666496 / 1024 / 1024) / 8192)

        nested_byte_memory = draw.status_summary(
            {
                "resources": {
                    "memory": {"used": 1073741824, "total": 2147483648},
                },
                "config": {},
            },
            {"admins": []},
        )
        self.assertEqual(nested_byte_memory["memory"], "1.00 GiB / 2.00 GiB")
        self.assertAlmostEqual(nested_byte_memory["memory_ratio"], 0.5)

        disk_usage_bytes = draw.status_summary(
            {
                "diskUsage": 17520 * 1024 * 1024,
                "docker": {"maxSpace": 20480},
                "config": {},
            },
            {"admins": []},
        )
        self.assertEqual(disk_usage_bytes["disk"], "17.11 GiB / 20.00 GiB")
        self.assertAlmostEqual(disk_usage_bytes["disk_ratio"], 17520 / 20480)

        nested_storage = draw.status_summary(
            {
                "resources": {
                    "storage": {"used": 1073741824, "total": 2147483648},
                },
                "config": {},
            },
            {"admins": []},
        )
        self.assertEqual(nested_storage["disk"], "1.00 GiB / 2.00 GiB")
        self.assertAlmostEqual(nested_storage["disk_ratio"], 0.5)

        missing = draw.status_summary({"space": 0, "config": {}}, {"admins": []})
        self.assertEqual(missing["cpu"], "N/A")
        self.assertEqual(missing["memory"], "N/A")
        self.assertEqual(missing["disk"], "N/A")
        self.assertIsNone(missing["cpu_ratio"])
        self.assertIsNone(missing["memory_ratio"])
        self.assertIsNone(missing["disk_ratio"])

    def test_mcsm_status_detail_merges_runtime_snapshot_resources(self):
        draw = _load_mcsm_draw_module()

        detail = {
            "status": 3,
            "instanceName": "Detail Name",
            "space": 0,
            "processInfo": {},
            "config": {
                "nickname": "Detail Nickname",
                "docker": {"memory": 8192, "maxSpace": 20480},
            },
        }
        snapshot = {
            "status": 0,
            "instanceUuid": "inst-1",
            "instanceName": "Snapshot Name",
            "space": 17520,
            "processInfo": {"cpu": 3, "memory": 4824},
        }
        merged = draw.merge_status_detail(detail, snapshot)
        summary = draw.status_summary(merged, {"admins": []})

        self.assertEqual(merged["status"], 0)
        self.assertEqual(summary["display_name"], "Detail Nickname")
        self.assertEqual(summary["cpu"], "3%")
        self.assertEqual(summary["memory"], "4.71 GiB / 8.00 GiB")
        self.assertEqual(summary["disk"], "17.11 GiB / 20.00 GiB")
        self.assertAlmostEqual(summary["cpu_ratio"], 0.03)
        self.assertAlmostEqual(summary["memory_ratio"], 4824 / 8192)
        self.assertAlmostEqual(summary["disk_ratio"], 17520 / 20480)

        detail_without_disk = {
            "status": 3,
            "instanceName": "Detail Name",
            "processInfo": {},
            "config": {"nickname": "Detail Nickname", "docker": {"memory": 8192}},
        }
        disk_snapshot = {
            "status": 3,
            "instanceUuid": "inst-1",
            "diskUsage": 17520 * 1024 * 1024,
            "docker": {"maxSpace": 20480},
        }
        merged_disk = draw.merge_status_detail(detail_without_disk, disk_snapshot)
        disk_summary = draw.status_summary(merged_disk, {"admins": []})

        self.assertEqual(disk_summary["display_name"], "Detail Nickname")
        self.assertEqual(disk_summary["disk"], "17.11 GiB / 20.00 GiB")
        self.assertAlmostEqual(disk_summary["disk_ratio"], 17520 / 20480)

    def test_mcsm_client_instance_list_parses_paginated_data(self):
        mcsm_client = _load_module("mcsm_client_list_for_test", "plugins/mcsm/client.py")

        class FakeClient(mcsm_client.MCSMClient):
            async def _get(self, path, params=None):
                self.path = path
                self.params = params
                return {
                    "status": 200,
                    "data": {
                        "maxPage": 1,
                        "pageSize": 50,
                        "data": [{"instanceUuid": "inst-1", "processInfo": {"cpu": 3}}],
                    },
                }

        client = FakeClient("panel.example", "key")
        result = asyncio.run(client.get_daemon_instances("daemon-1"))

        self.assertEqual(client.path, "/api/service/remote_service_instances")
        self.assertEqual(client.params["daemonId"], "daemon-1")
        self.assertEqual(client.params["page"], 1)
        self.assertEqual(client.params["page_size"], 50)
        self.assertEqual(client.params["instance_name"], "")
        self.assertEqual(client.params["status"], "")
        self.assertEqual(result, [{"instanceUuid": "inst-1", "processInfo": {"cpu": 3}}])

    def test_mcsm_client_instance_list_empty_data(self):
        mcsm_client = _load_module("mcsm_client_empty_list_for_test", "plugins/mcsm/client.py")

        class FakeClient(mcsm_client.MCSMClient):
            async def _get(self, path, params=None):
                return {"status": 200, "data": {"data": []}}

        result = asyncio.run(FakeClient("panel.example", "key").get_daemon_instances("daemon-1"))

        self.assertEqual(result, [])

    def test_mcsm_client_instance_list_raises_panel_error(self):
        mcsm_client = _load_module("mcsm_client_list_error_for_test", "plugins/mcsm/client.py")

        class FakeClient(mcsm_client.MCSMClient):
            async def _get(self, path, params=None):
                return {"status": 400, "data": {"message": "TypeError: missing status"}}

        with self.assertRaises(mcsm_client.MCSMAPIError) as ctx:
            asyncio.run(FakeClient("panel.example", "key").get_daemon_instances("daemon-1"))

        self.assertIn("获取节点实例失败", str(ctx.exception))
        self.assertIn("TypeError: missing status", str(ctx.exception))

    def test_mcsm_client_find_instance_daemon_uses_list_snapshots(self):
        mcsm_client = _load_module("mcsm_client_find_daemon_for_test", "plugins/mcsm/client.py")

        class FakeClient(mcsm_client.MCSMClient):
            def __init__(self):
                super().__init__("panel.example", "key")
                self.instance_calls = []

            async def get_daemon_list(self):
                return [{"uuid": "daemon-bad"}, {"uuid": "daemon-good"}]

            async def get_daemon_instances(self, daemon_id):
                self.instance_calls.append(daemon_id)
                if daemon_id == "daemon-bad":
                    raise mcsm_client.MCSMAPIError("HTTP 500 instance does not exist")
                return [{"instanceUuid": "target-inst"}]

            async def get_instance_detail(self, uuid, daemon_id):
                raise AssertionError("detail probe should not run after list snapshot match")

        client = FakeClient()
        result = asyncio.run(client.find_instance_daemon("target-inst"))

        self.assertEqual(result, "daemon-good")
        self.assertEqual(client.instance_calls, ["daemon-bad", "daemon-good"])

    def test_mcsm_client_find_instance_daemon_detail_fallback_ignores_probe_errors(self):
        mcsm_client = _load_module("mcsm_client_find_daemon_fallback_for_test", "plugins/mcsm/client.py")

        class FakeClient(mcsm_client.MCSMClient):
            async def get_daemon_list(self):
                return [{"uuid": "daemon-bad"}, {"uuid": "daemon-good"}]

            async def get_daemon_instances(self, daemon_id):
                return []

            async def get_instance_detail(self, uuid, daemon_id):
                if daemon_id == "daemon-bad":
                    raise mcsm_client.MCSMAPIError("HTTP 500 instance does not exist")
                return {"instanceUuid": uuid}

        result = asyncio.run(FakeClient("panel.example", "key").find_instance_daemon("target-inst"))

        self.assertEqual(result, "daemon-good")

    def test_mcsm_dm_key_results_use_explicit_unimessage_send(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")
        start = source.index("async def _finish_dm_image_or_text")
        end = source.index("async def _finish_notice", start)
        dm_finish_source = source[start:end]

        self.assertIn("async def _finish_dm_image_or_text", source)
        self.assertIn("await message.send(target, bot)", source)
        self.assertIn("stop_session()", dm_finish_source)
        self.assertIn("await ChainMsg.text(fallback).send()", dm_finish_source)
        self.assertNotIn("await matcher.send(", dm_finish_source)
        self.assertNotIn("await matcher.finish()", dm_finish_source)
        self.assertIn("await _finish_dm_notice(\n        dm_key_handler,\n        bot,\n        user_id,", source)
        self.assertNotIn("_finish_notice(dm_key_handler", source)

    def test_mcsm_dm_key_handler_does_not_call_eventhook_send_or_finish(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")
        start = source.index("async def handle_dm_key")
        end = source.index("async def _is_dm_for_pending_bind", start)
        handler_source = source[start:end]

        self.assertNotIn("dm_key_handler.send(", handler_source)
        self.assertNotIn("dm_key_handler.finish(", handler_source)
        self.assertIn('await ChainMsg.text("正在验证 API Key...").send()', handler_source)
        self.assertIn("stop_session()", handler_source)
        for text in (
            "API Key too short",
            "Reply cancel",
            "validation failed",
            "No available daemon",
            "Check the key",
            "Use /mcsm bind <daemon_id> in DM",
        ):
            self.assertNotIn(text, handler_source)

    def test_mcsm_private_batch_bind_and_group_admin_source_paths(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")

        self.assertIn("_pending_bind_sessions", source)
        self.assertIn("dm_bind_handler = listen_message", source)
        self.assertIn("await message.send(target, bot)", source)
        self.assertIn("MCSM 批量绑定选择", source)
        self.assertIn("async def _is_group_manager", source)
        self.assertIn("guild_member_get", source)
        self.assertIn("/mcsm admin add @某人", source)
        self.assertNotIn("_store.add_admin(group_id, target,", source)

    def test_mcsm_delete_instance_command_is_wired(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")

        self.assertIn('if subcmd in ("delete", "del", "remove", "删除")', source)
        self.assertIn("async def _cmd_delete_instance", source)
        self.assertIn("await client.delete_instance(uuid, daemon_id_value, delete_files=delete_files)", source)
        self.assertIn("_store.unbind_instance(group_id, alias)", source)
        self.assertIn("delete | admin", source)

    def test_mcsm_list_uses_only_bound_instances_source_paths(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")
        start = source.index("async def _cmd_list")
        end = source.index("async def _cmd_status", start)
        list_source = source[start:end]

        self.assertNotIn("get_all_instances", list_source)
        self.assertIn("get_daemon_instances", list_source)
        self.assertIn("暂无本群实例", list_source)
        self.assertIn("面板已绑定；使用 /mcsm bind <节点ID>", source)
        self.assertNotIn("现在可在群内使用 /mcsm list 查看实例列表", source)
        self.assertIn("status = _mcsm_status_code(", list_source)
        self.assertIn("status_text = _mcsm_status_text(", list_source)
        self.assertIn("if status == 3:", list_source)

    def test_mcsm_list_status_text_helpers(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")
        start = source.index("def _mcsm_status_code")
        end = source.index("async def _extract_at_users", start)
        namespace = {
            "STATUS_MAP": {-1: "BUSY", 0: "STOPPED", 3: "RUNNING"},
            "STATUS_EMOJI": {-1: "WAIT", 0: "STOP", 3: "RUN"},
        }
        exec(source[start:end], namespace)

        self.assertEqual(namespace["_mcsm_status_code"]("3"), 3)
        self.assertEqual(namespace["_mcsm_status_code"](3), 3)
        self.assertEqual(namespace["_mcsm_status_code"]("bad"), -1)
        self.assertEqual(namespace["_mcsm_status_text"]("3"), "RUN RUNNING")
        self.assertEqual(namespace["_mcsm_status_text"](99), "❓ UNKNOWN(99)")

    def test_mcsm_overview_uses_dark_stat_pills(self):
        source = (ROOT / "plugins/mcsm/draw.py").read_text(encoding="utf-8")
        start = source.index("def draw_panel_overview")
        overview_source = source[start:]

        self.assertIn("def _overview_stat_pill", source)
        self.assertIn("_overview_stat_pill(draw, x, stat_y", overview_source)
        self.assertNotIn("x = _pill(draw, x, stat_y", overview_source)

    def test_mcsm_store_panel_admin_and_hidden(self):
        MCSMStore = _load_mcsm_store_class()
        with TemporaryDirectory() as tmp:
            store = MCSMStore(str(Path(tmp) / "bindings.json"))
            store.set_panel_url("10001", "http://panel.example/")
            store.set_api_key("10001", "secret-key")
            self.assertEqual(store.get_group_instances("10001"), {})
            store.set_owner("10001", "246")
            store.bind_instance("10001", "survival", "uuid-1", "daemon-1")

            self.assertTrue(store.has_panel("10001"))
            self.assertEqual(store.get_panel("10001"), ("http://panel.example", "secret-key"))
            self.assertEqual(store.get_owner("10001"), "246")
            self.assertIn("246", store.get_admins("10001"))
            self.assertFalse(store.get_instance("10001", "survival").get("hidden"))
            self.assertNotIn("admins", store.get_instance("10001", "survival"))

            self.assertTrue(store.add_admin("10001", "135"))
            self.assertFalse(store.add_admin("10001", "135"))
            self.assertTrue(store.check_instance_permission("10001", "survival", "135"))

            self.assertTrue(store.set_hidden("10001", "survival", True))
            self.assertEqual(store.get_visible_instances("10001"), {})
            store.clear_panel("10001")
            self.assertFalse(store.has_panel("10001"))
            self.assertEqual(store.get_group_instances("10001"), {})
            self.assertIn("135", store.get_admins("10001"))

    def test_mcsm_client_get_all_instances_keeps_partial_results(self):
        mcsm_client = _load_module("mcsm_client_for_test", "plugins/mcsm/client.py")

        class FakeClient(mcsm_client.MCSMClient):
            async def _ensure_daemons(self):
                return [
                    {"uuid": "daemon-1", "remarks": "node-a"},
                    {"uuid": "daemon-2", "remarks": "node-b"},
                ]

            async def _get_instances_by_daemon(self, daemon_id, page=1, page_size=50):
                if daemon_id == "daemon-2":
                    raise mcsm_client.MCSMAPIError("node offline")
                return [{"instanceUuid": "inst-1", "config": {"nickname": "survival"}}]

        result = asyncio.run(FakeClient("panel.example", "key").get_all_instances())

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["_daemonId"], "daemon-1")
        self.assertEqual(result[0]["_daemonName"], "node-a")

    def test_mcsm_client_daemon_cache_refresh_and_error_message(self):
        mcsm_client = _load_module("mcsm_client_cache_for_test", "plugins/mcsm/client.py")

        class FakeClient(mcsm_client.MCSMClient):
            def __init__(self):
                super().__init__("panel.example", "key")
                self.calls = 0

            async def get_daemon_list(self):
                self.calls += 1
                self._daemon_cache = [{"uuid": f"daemon-{self.calls}"}]
                return self._daemon_cache

        client = FakeClient()

        self.assertEqual(asyncio.run(client._ensure_daemons()), [{"uuid": "daemon-1"}])
        self.assertEqual(asyncio.run(client._ensure_daemons()), [{"uuid": "daemon-1"}])
        self.assertEqual(client.calls, 1)
        self.assertEqual(asyncio.run(client.refresh_daemon_list()), [{"uuid": "daemon-2"}])
        client.clear_daemon_cache()
        self.assertIsNone(client._daemon_cache)

        self.assertEqual(
            mcsm_client.MCSMClient._api_error_message({"data": {"message": "bad daemon"}}),
            "bad daemon",
        )
        self.assertEqual(mcsm_client.MCSMClient._api_error_message({"status": 500}), "status=500")

    def test_mcsm_deploy_parse_accepts_optional_port_and_options(self):
        deploy = _load_module("mcsm_deploy_parse_for_test", "plugins/mcsm/deploy.py")

        parsed = deploy.parse_deploy_args([
            "survival",
            "https://example.com/server.zip",
            "--port",
            "25566",
            "--node",
            "上海",
            "--image",
            "java21",
            "--mem",
            "3072",
            "--dry-run",
        ])

        self.assertFalse(parsed.errors)
        self.assertEqual(parsed.options.alias, "survival")
        self.assertEqual(parsed.options.port, 25566)
        self.assertEqual(parsed.options.node, "上海")
        self.assertEqual(parsed.options.image, "java21")
        self.assertEqual(parsed.options.memory_mb, 3072)
        self.assertTrue(parsed.options.dry_run)

        missing_port = deploy.parse_deploy_args(["survival", "https://example.com/server.zip"])
        self.assertEqual(missing_port.errors, [])
        self.assertEqual(missing_port.options.port, 0)

    def test_mcsm_deploy_auto_port_alias_prefix(self):
        deploy = _load_module("mcsm_deploy_alias_prefix_for_test", "plugins/mcsm/deploy.py")

        self.assertEqual(deploy.apply_auto_port_alias("survival", 56666), "56666-survival")
        self.assertEqual(deploy.apply_auto_port_alias("51565-survival", 56666), "56666-survival")
        self.assertEqual(deploy.apply_auto_port_alias("12345-51565-survival", 56666), "56666-51565-survival")
        self.assertEqual(deploy.apply_auto_port_alias("", 56666), "56666-instance")

    def test_mcsm_deploy_auto_port_helpers(self):
        deploy = _load_module("mcsm_deploy_auto_port_for_test", "plugins/mcsm/deploy.py")

        self.assertTrue(deploy.is_frp_instance({"config": {"nickname": "00-FRP-MCs"}}))
        self.assertTrue(deploy.is_frp_instance({"instanceName": "01-FRP-Others"}))
        self.assertTrue(deploy.is_frp_instance({"name": "frp-client"}))
        self.assertFalse(deploy.is_frp_instance({"config": {"nickname": "survival"}}))

        toml_text = """
[[proxies]]
name = "mc-1"
remotePort = 59900

[[proxies]]
name = "mc-2"
remote_port = 59901

[[allowPorts]]
start = 59910
end = 59912
"""
        self.assertEqual(deploy.extract_frp_candidate_ports(toml_text), {59900, 59901, 59910, 59911, 59912})
        self.assertEqual(
            deploy.extract_frp_candidate_ports('allowPorts = ["59920-59921", "59922"]'),
            {59920, 59921, 59922},
        )
        template_text = """
{{- range $_, $v := parseNumberRangePair "51565-51580" "51565-51580" }}
[[proxies]]
name = "MC-{{ $v.First }}"
type = "tcp"
localPort = {{ $v.First }}
remotePort = {{ $v.Second }}
{{- end }}

{{- range $_, $v := parseNumberRangePair "52100-52113,52115-52125" "52100-52113,52115-52125" }}
[[proxies]]
name = "MC-{{ $v.First }}"
type = "tcp"
localPort = {{ $v.First }}
remotePort = {{ $v.Second }}
{{- end }}
"""
        expected_template_ports = set(range(51565, 51581)) | set(range(52100, 52114)) | set(range(52115, 52126))
        self.assertEqual(deploy.extract_frp_candidate_ports(template_text), expected_template_ports)
        self.assertEqual(
            deploy.extract_frp_candidate_ports('{{ range $_, $v := parseNumberRangePair "53000-53002" }}'),
            {53000, 53001, 53002},
        )
        self.assertEqual(
            deploy.extract_instance_host_ports({"config": {"docker": {"ports": ["59900:25565/tcp"]}}}),
            {59900},
        )
        self.assertEqual(
            deploy.extract_instance_host_ports({"config": {"docker": {"ports": ["0.0.0.0:59901:25565/tcp"]}}}),
            {59901},
        )
        occupied = deploy.running_instance_host_ports(
            [
                {"status": "3", "config": {"docker": {"ports": ["59900:25565/tcp"]}}},
                {"status": 0, "config": {"docker": {"ports": ["59901:25565/tcp"]}}},
            ]
        )
        self.assertEqual(occupied, {59900})
        with patch.object(deploy.random, "choice", lambda values: values[0]):
            self.assertEqual(deploy.choose_deploy_port({59900, 59901}, occupied), 59901)
            self.assertEqual(deploy.choose_deploy_port({59900}, occupied), 0)

    def test_mcsm_deploy_find_instance_toml_paths_scans_nested_pages_and_limits(self):
        deploy = _load_module("mcsm_deploy_toml_bfs_for_test", "plugins/mcsm/deploy.py")

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def list_instance_files(self, uuid, daemon_id, target="/", page=0, page_size=200):
                self.calls.append((target, page, page_size))
                if target == "/" and page == 0:
                    return [{"name": "frp", "type": 0}, {"name": "logs", "type": 0}]
                if target == "/frp" and page == 0:
                    return [{"name": "templates", "type": 0}]
                if target == "/frp/templates" and page in {0, 1, 2, 3}:
                    return [{"name": f"ignore-{page}.txt", "type": 1}]
                if target == "/frp/templates" and page == 4:
                    return [{"name": "frpc.toml", "type": 1}]
                if target == "/logs" and page == 0:
                    return [{"name": "old.log", "type": 1}]
                return []

        client = FakeClient()
        paths = asyncio.run(deploy.find_instance_toml_paths(client, "inst-1", "daemon-1"))

        self.assertEqual(paths, ["/frp/templates/frpc.toml"])
        self.assertIn(("/frp/templates", 4, 200), client.calls)

        limited_client = FakeClient()
        limited_paths = asyncio.run(
            deploy.find_instance_toml_paths(limited_client, "inst-1", "daemon-1", max_depth=1)
        )
        self.assertEqual(limited_paths, [])
        self.assertNotIn(("/frp/templates", 0, 200), limited_client.calls)

    def test_mcsm_deploy_detect_start_command_prefers_scripts_then_jar(self):
        deploy = _load_module("mcsm_deploy_detect_for_test", "plugins/mcsm/deploy.py")

        command, source = deploy.detect_start_command(
            [{"name": "server.jar"}, {"name": "启动.sh"}],
            memory_mb=4096,
        )
        self.assertEqual(command, "sh ./'启动.sh'")
        self.assertIn("启动.sh", source)

        jar_command, jar_source = deploy.detect_start_command(
            [{"name": "paper-1.21.jar"}],
            memory_mb=3072,
        )
        self.assertEqual(jar_command, "java -Xms1G -Xmx3072M -jar paper-1.21.jar nogui")
        self.assertIn("paper-1.21.jar", jar_source)

        server_jar_command, server_jar_source = deploy.detect_start_command(
            [{"fileName": "Start.bat"}, {"fileName": "server.jar"}],
            memory_mb=2048,
        )
        self.assertEqual(server_jar_command, "java -Xms1G -Xmx2048M -jar server.jar nogui")
        self.assertIn("server.jar", server_jar_source)

        nested_command, nested_source = deploy.detect_start_command(
            ["Start.bat", {"fileName": "server.jar"}],
            memory_mb=2048,
            base_path="server files",
        )
        self.assertEqual(nested_command, "java -Xms1G -Xmx2048M -jar 'server files/server.jar' nogui")
        self.assertIn("server files/server.jar", nested_source)

        explicit, explicit_source = deploy.detect_start_command([], explicit_command="sh ./custom.sh")
        self.assertEqual(explicit, "sh ./custom.sh")
        self.assertEqual(explicit_source, "用户指定")

        bat_command, bat_source = deploy.detect_start_command([{"name": "Start.bat"}])
        self.assertEqual(bat_command, "")
        self.assertIn("Docker Linux", bat_source)
        self.assertIn("Start.bat", bat_source)

        jsr_command, jsr_source = deploy.detect_start_command([{"name": "server.jsr"}])
        self.assertEqual(jsr_command, "")
        self.assertIn("server.jsr", jsr_source)
        self.assertIn("server.jar", jsr_source)

        missing_command, missing_source = deploy.detect_start_command([{"displayName": "readme.txt"}])
        self.assertEqual(missing_command, "")
        self.assertIn("readme.txt", missing_source)

    def test_mcsm_deploy_image_selection_helpers(self):
        deploy = _load_module("mcsm_deploy_images_for_test", "plugins/mcsm/deploy.py")
        images = [
            {"RepoTags": ["ubuntu:22.04"]},
            {"RepoTags": ["eclipse-temurin:17-jre"]},
            {"RepoTags": ["eclipse-temurin:21-jre"]},
        ]

        self.assertEqual(deploy.image_display_name(deploy.choose_default_java_images(images)[0]), "eclipse-temurin:21-jre")
        self.assertEqual(deploy.image_display_name(deploy.find_images(images, "java17")[0]), "eclipse-temurin:17-jre")
        self.assertEqual(deploy.java_runtime_image_label({"RepoTags": ["eclipse-temurin:21-jre"]}), "jre21")
        self.assertEqual(deploy.java_runtime_image_label({"RepoTags": ["openjdk:17-jdk"]}), "jdk17")
        self.assertEqual(deploy.java_runtime_image_label({"RepoTags": ["openjdk:8"]}), "jdk8")
        self.assertEqual(deploy.java_runtime_image_label({"RepoTags": ["realitylink-to-onebot:1.1.1"]}), "")
        self.assertEqual(
            deploy.java_runtime_image_labels(
                [
                    {"RepoTags": ["eclipse-temurin:21-jre"]},
                    {"RepoTags": ["realitylink-to-onebot:1.1.1"]},
                    {"RepoTags": ["openjdk:17-jdk"]},
                    {"RepoTags": ["openjdk:17-jdk"]},
                ]
            ),
            ["jre21", "jdk17"],
        )

    def test_mcsm_deploy_extract_uuid_and_redacts_sensitive_text(self):
        deploy = _load_module("mcsm_deploy_uuid_for_test", "plugins/mcsm/deploy.py")

        self.assertEqual(
            deploy.extract_created_instance_uuid({"data": {"instance": {"instanceUuid": "inst-1"}}}),
            "inst-1",
        )
        redacted = deploy.redact_sensitive_text(
            "https://panel.example/path?apikey=abcdef1234567890&rkey=qq-secret token sk-1234567890abcdef "
            "Bearer llm-secret-token-123456"
        )
        self.assertIn("https://<redacted-url>", redacted)
        self.assertNotIn("abcdef1234567890", redacted)
        self.assertNotIn("qq-secret", redacted)
        self.assertNotIn("sk-1234567890abcdef", redacted)
        self.assertNotIn("llm-secret-token-123456", redacted)

        summary = deploy.redact_deploy_summary(
            {
                "url": "https://multimedia.qfile.qq.com/download?rkey=qq-secret",
                "nested": ["apikey=abcdef1234567890"],
            }
        )
        self.assertEqual(summary["url"], "https://<redacted-url>")
        self.assertNotIn("abcdef1234567890", summary["nested"][0])

    def test_mcsm_qflash_download_url_candidates_include_raw_direct_url(self):
        qflash = _load_module("mcsm_qflash_url_candidates_for_test", "plugins/mcsm/qflash.py")
        item = qflash.QFlashArchive(
            name="server pack.zip",
            size=480 * 1024 * 1024,
            fileset_id="fileset",
            expired_time=0,
            physical_id="physical",
            cli_fileid="cli",
            download_url="https://multimedia.qfile.qq.com/download?rkey=secret&fname=remote.zip",
        )

        candidates = qflash.qflash_download_url_candidates(item)

        self.assertEqual(len(candidates), 2)
        self.assertIn("filename=server+pack.zip", candidates[0])
        self.assertEqual(candidates[1], item.download_url)

    def test_mcsm_client_redacts_sensitive_text(self):
        mcsm_client = _load_module("mcsm_client_redact_for_test", "plugins/mcsm/client.py")

        text = (
            "GET https://panel.example/api?apikey=abcdef1234567890&rkey=qq-secret "
            "Authorization: Bearer llm-secret-token-123456 password=upload-secret "
            "0123456789abcdef0123456789abcdef"
        )
        redacted = mcsm_client.redact_sensitive_text(text)

        self.assertIn("https://<redacted-url>", redacted)
        self.assertNotIn("abcdef1234567890", redacted)
        self.assertNotIn("qq-secret", redacted)
        self.assertNotIn("llm-secret-token-123456", redacted)
        self.assertNotIn("upload-secret", redacted)
        self.assertNotIn("0123456789abcdef0123456789abcdef", redacted)

    def test_mcsm_deploy_gateway_timeout_detection(self):
        deploy = _load_module("mcsm_deploy_timeout_for_test", "plugins/mcsm/deploy.py")

        self.assertTrue(deploy.is_extract_gateway_timeout_error("面板返回 HTTP 504: Gateway Time-out"))
        self.assertTrue(deploy.is_extract_gateway_timeout_error("504 Gateway Timeout"))
        self.assertFalse(deploy.is_extract_gateway_timeout_error("HTTP 400: target is required"))

    def test_mcsm_deploy_upload_permission_repair_detection(self):
        deploy = _load_module("mcsm_deploy_upload_repair_for_test", "plugins/mcsm/deploy.py")

        self.assertTrue(deploy.is_upload_permission_error("HTTP 500: Access denied: No file found"))
        self.assertTrue(deploy.is_upload_permission_error("permission denied: EACCES"))
        self.assertTrue(deploy.is_upload_permission_error("EPERM operation not permitted"))
        self.assertFalse(deploy.is_upload_permission_error("connection refused"))
        self.assertTrue(deploy.is_permission_repair_instance({"config": {"nickname": "0-AAA卡权限解决脚本"}}))
        self.assertTrue(deploy.is_permission_repair_instance({"nickname": "0-AAA-permission-repair"}))
        self.assertFalse(deploy.is_permission_repair_instance({"nickname": "0-AAA普通脚本"}))

    def test_mcsm_deploy_eula_remediation_detection(self):
        deploy = _load_module("mcsm_deploy_eula_detection_for_test", "plugins/mcsm/deploy.py")

        self.assertTrue(deploy.needs_eula_remediation(log_text="You need to agree to the EULA in order to run the server."))
        self.assertTrue(deploy.needs_eula_remediation(error="Handle stopped", log_text=""))
        self.assertFalse(deploy.needs_eula_remediation(error="Handle stopped", log_text="java.lang.Exception"))
        self.assertEqual(deploy.remediation_summary(["写入 eula", "", "重启"]), "写入 eula；重启")

    def test_mcsm_deploy_flow_wires_eula_auto_remediation(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")

        self.assertIn("async def _auto_remediate_deploy_start", source)
        self.assertIn('client.write_instance_file(uuid, daemon_id_value, "eula.txt", EULA_REMEDIATION_TEXT)', source)
        self.assertIn("needs_eula_remediation", source)
        self.assertIn("await _auto_remediate_deploy_start(", source)
        self.assertIn("自动修复:", source)

    def test_mcsm_deploy_failure_summary_includes_transfer_state(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")

        self.assertIn('summary["transfer_status"] = "已下载到 Bot 临时目录"', source)
        self.assertIn('summary["transfer_status"] = "已上传到 daemon"', source)
        self.assertIn('summary["extract_status"] = "解压接口已返回成功"', source)
        self.assertIn('summary["cleanup_status"] = "已删除上传压缩包"', source)
        self.assertIn("redact_mcsm_sensitive_text(cleanup_exc)", source)
        self.assertIn("权限修复状态:", source)
        self.assertIn("远程直链安装:", source)
        self.assertIn('summary["remote_install_retry_status"]', source)

    def test_mcsm_deploy_failure_auto_deletes_created_instance(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")
        start = source.index("async def _cmd_deploy")
        end = source.index("@dm_bind_handler.handle()", start)
        deploy_source = source[start:end]

        self.assertIn("async def _cleanup_failed_deploy_instance", source)
        self.assertIn("client.delete_instance(uuid, daemon_id_value, delete_files=False)", source)
        self.assertIn('summary["deploy_cleanup_status"]', source)
        self.assertIn('if summary.get("deploy_cleanup_status"):', source)
        self.assertIn('uuid = ""', deploy_source)
        self.assertIn('did = ""', deploy_source)
        self.assertIn("if uuid and did:", deploy_source)
        self.assertIn("await _cleanup_failed_deploy_instance(client, uuid, did, summary)", deploy_source)
        self.assertIn("raise RuntimeError(start_source)", deploy_source)
        self.assertNotIn('await _finish_deploy_failure("璇嗗埆鍚姩鍛戒护", start_source, summary)', deploy_source)

    def test_mcsm_deploy_upload_permission_repair_is_wired(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")
        start = source.index("async def _cmd_deploy")
        end = source.index("@dm_bind_handler.handle()", start)
        deploy_source = source[start:end]

        self.assertIn("async def _retry_upload_after_permission_repair", source)
        self.assertIn("is_permission_repair_instance", source)
        self.assertIn("client.start_instance(repair_uuid, daemon_id_value)", source)
        self.assertIn("except Exception as upload_exc:", deploy_source)
        self.assertIn("if is_upload_permission_error(upload_exc):", deploy_source)
        self.assertIn("uploaded_name = await _retry_upload_after_permission_repair(client, uuid, did, local_file, summary)", deploy_source)

    def test_mcsm_deploy_retries_remote_install_after_upload_failure(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")
        start = source.index("async def _cmd_deploy")
        end = source.index("@dm_bind_handler.handle()", start)
        deploy_source = source[start:end]

        self.assertIn("async def _retry_remote_install_after_upload_failure", source)
        self.assertIn('summary["remote_install_retry_status"] = "上传失败后尝试 daemon 远程下载直链安装"', source)
        self.assertIn('summary["transfer_status"] = "Bot 上传失败，已改用 daemon 远程下载直链安装"', source)
        self.assertIn("remote_install_fallback_succeeded = False", deploy_source)
        self.assertIn("await _retry_remote_install_after_upload_failure(", deploy_source)
        self.assertIn("if not remote_install_fallback_succeeded:", deploy_source)
        self.assertIn('lines.append("安装方式: daemon 远程下载直链")', deploy_source)

    def test_mcsm_deploy_refreshes_qflash_direct_urls_for_large_packages(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")
        start = source.index("async def _cmd_deploy")
        end = source.index("@dm_bind_handler.handle()", start)
        deploy_source = source[start:end]

        self.assertIn("refresh_qflash_archive", source)
        self.assertIn("async def _refresh_qflash_download", source)
        self.assertIn("async def _retry_large_qflash_remote_install", source)
        self.assertIn('os.getenv("MCSM_DEPLOY_LARGE_PACKAGE_MB", "200")', source)
        self.assertIn("await preflight_qflash_archive(selected)", source)
        self.assertIn("fresh = await _refresh_qflash_download(options.url, selected, summary)", source)
        self.assertIn("qflash_download_url_candidates", source)
        self.assertIn("for index, install_url in enumerate(install_urls, 1):", source)
        self.assertIn('summary["remote_install_url_variant"]', source)
        self.assertIn("remote_retry_succeeded, install_url, qflash_archive = await _retry_large_qflash_remote_install(", deploy_source)
        self.assertIn("qflash_archive = await _refresh_qflash_download(options.url, qflash_archive, summary)", deploy_source)
        self.assertIn("options.url,", deploy_source)
        self.assertIn("qflash_archive,", deploy_source)
        self.assertIn('summary["qflash_refresh_count"]', source)
        self.assertIn("闪传直链刷新:", source)

    def test_mcsm_deploy_does_not_report_session_stop_as_failure(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")
        start = source.index("async def _cmd_deploy")
        end = source.index("@dm_bind_handler.handle()", start)
        deploy_source = source[start:end]

        self.assertIn("from arclet.letoderea.exceptions import _ExitException", source)
        self.assertIn('await _finish_notice(mcsm, "MCSM Docker 部署完成"', deploy_source)
        success_index = deploy_source.index('await _finish_notice(mcsm, "MCSM Docker 部署完成"')
        exit_index = deploy_source.index("except _ExitException:", success_index)
        generic_index = deploy_source.index("except Exception as exc:", exit_index)
        self.assertIn("return", deploy_source[success_index:exit_index])
        self.assertLess(exit_index, generic_index)
        self.assertIn("raise", deploy_source[exit_index:generic_index])

    def test_mcsm_deploy_progress_messages_are_compact(self):
        source = (ROOT / "plugins/mcsm/__init__.py").read_text(encoding="utf-8")
        daemon_start = source.index("def _daemon_label")
        daemon_end = source.index("async def _select_deploy_daemon", daemon_start)
        daemon_source = source[daemon_start:daemon_end]
        start = source.index("async def _cmd_deploy")
        end = source.index("@dm_bind_handler.handle()", start)
        deploy_source = source[start:end]

        self.assertIn("java_runtime_image_labels", daemon_source)
        self.assertIn("JDK/JRE:", daemon_source)
        self.assertNotIn("image_display_name(image) for image in images[:3]", daemon_source)
        self.assertEqual(deploy_source.count("await mcsm.send("), 4)
        self.assertIn("开始部署：解析下载链接、检测节点与镜像。", deploy_source)
        self.assertIn("正在创建实例并安装压缩包。", deploy_source)
        self.assertIn("远程安装失败，切换 Bot 中转上传并解压。", deploy_source)
        self.assertIn("正在识别启动命令并启动实例。", deploy_source)
        self.assertIn("if not options.port:", deploy_source)
        self.assertIn("allocated_port, port_source = await _auto_deploy_port(client, did)", deploy_source)
        self.assertIn("options.port = allocated_port", deploy_source)
        self.assertIn("final_alias = apply_auto_port_alias(options.alias, allocated_port)", deploy_source)
        self.assertIn('summary["alias_change"]', deploy_source)
        self.assertIn('summary["port_source"] = port_source', deploy_source)
        auto_port_index = deploy_source.index("allocated_port, port_source = await _auto_deploy_port(client, did)")
        alias_prefix_index = deploy_source.index("final_alias = apply_auto_port_alias(options.alias, allocated_port)")
        alias_exists_index = deploy_source.index("if _store.alias_exists(group_id, options.alias):")
        image_index = deploy_source.index('stage = "选择 Docker 镜像"')
        self.assertLess(auto_port_index, alias_prefix_index)
        self.assertLess(alias_prefix_index, alias_exists_index)
        self.assertLess(alias_exists_index, image_index)
        self.assertIn("TOML:", source)
        self.assertNotIn("正在解析下载链接...", deploy_source)
        self.assertNotIn("正在检测可用 Docker 节点...", deploy_source)
        self.assertNotIn("正在选择 Docker 镜像...", deploy_source)
        self.assertNotIn("正在上传压缩包到 daemon...", deploy_source)
        self.assertNotIn("正在解压压缩包...", deploy_source)

    def test_mcsm_deploy_waits_for_background_extract(self):
        deploy = _load_module("mcsm_deploy_wait_extract_for_test", "plugins/mcsm/deploy.py")

        class FakeClient:
            def __init__(self):
                self.calls = 0

            async def list_instance_files(self, uuid, daemon_id, target="/", page=0, page_size=200):
                self.calls += 1
                if self.calls < 2:
                    return []
                return [{"name": "server.jar"}]

        async def no_sleep(_seconds):
            return None

        client = FakeClient()
        command, source = asyncio.run(
            deploy.wait_for_deploy_start_command(
                client,
                "inst-1",
                "daemon-1",
                2048,
                "",
                wait_seconds=10,
                interval_seconds=5,
                sleep_func=no_sleep,
            )
        )
        self.assertEqual(command, "java -Xms1G -Xmx2048M -jar server.jar nogui")
        self.assertIn("server.jar", source)

    def test_mcsm_deploy_scans_page_zero_and_type_zero_directories(self):
        deploy = _load_module("mcsm_deploy_page_zero_for_test", "plugins/mcsm/deploy.py")

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def list_instance_files(self, uuid, daemon_id, target="/", page=0, page_size=200):
                self.calls.append((target, page, page_size))
                if target == "/":
                    return [{"name": "server-files", "type": 0}]
                if target == "/server-files":
                    return [{"name": "server.jar", "type": 1}]
                return []

        client = FakeClient()
        command, source = asyncio.run(
            deploy.detect_deploy_start_command(client, "inst-1", "daemon-1", 2048, "")
        )
        self.assertEqual(command, "java -Xms1G -Xmx2048M -jar server-files/server.jar nogui")
        self.assertIn("server.jar", source)
        self.assertEqual(client.calls, [("/", 0, 200), ("/server-files", 0, 200)])

    def test_mcsm_deploy_detects_archive_start_command_from_zip_root(self):
        deploy = _load_module("mcsm_deploy_archive_zip_root_for_test", "plugins/mcsm/deploy.py")

        with TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "server.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("Start.bat", "java -jar server.jar")
                archive.writestr("server.jar", b"jar")

            command, source = deploy.detect_archive_start_command(archive_path, memory_mb=2048)

        self.assertEqual(command, "java -Xms1G -Xmx2048M -jar server.jar nogui")
        self.assertIn("压缩包根目录", source)
        self.assertIn("server.jar", source)

    def test_mcsm_deploy_detects_archive_start_command_from_zip_subdir(self):
        deploy = _load_module("mcsm_deploy_archive_zip_subdir_for_test", "plugins/mcsm/deploy.py")

        with TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "server.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("server-files/server.jar", b"jar")

            command, source = deploy.detect_archive_start_command(archive_path, memory_mb=3072)

        self.assertEqual(command, "java -Xms1G -Xmx3072M -jar server-files/server.jar nogui")
        self.assertIn("压缩包子目录", source)
        self.assertIn("server-files/server.jar", source)

    def test_mcsm_deploy_detects_archive_start_command_from_tar_gz(self):
        deploy = _load_module("mcsm_deploy_archive_targz_for_test", "plugins/mcsm/deploy.py")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "启动.sh"
            script.write_text("java -jar server.jar", encoding="utf-8")
            archive_path = root / "server.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(script, arcname="启动.sh")

            command, source = deploy.detect_archive_start_command(archive_path, memory_mb=2048)

        self.assertEqual(command, "sh ./'启动.sh'")
        self.assertIn("压缩包根目录", source)

    def test_mcsm_deploy_archive_fallback_when_api_scan_empty(self):
        deploy = _load_module("mcsm_deploy_archive_fallback_for_test", "plugins/mcsm/deploy.py")

        command, source, used = deploy.apply_archive_start_fallback(
            "",
            "未找到启动脚本或 Jar 文件；根目录已扫描: 未返回文件列表；子目录: 无子目录",
            "java -Xms1G -Xmx2048M -jar server.jar nogui",
            "压缩包根目录 Jar 文件 server.jar",
        )

        self.assertTrue(used)
        self.assertEqual(command, "java -Xms1G -Xmx2048M -jar server.jar nogui")
        self.assertIn("压缩包根目录", source)
        self.assertIn("API 扫描结果", source)

    def test_mcsm_deploy_archive_fallback_keeps_api_command(self):
        deploy = _load_module("mcsm_deploy_archive_fallback_keep_for_test", "plugins/mcsm/deploy.py")

        command, source, used = deploy.apply_archive_start_fallback(
            "sh ./start.sh",
            "启动脚本 start.sh",
            "java -jar server.jar",
            "压缩包根目录 Jar 文件 server.jar",
        )

        self.assertFalse(used)
        self.assertEqual(command, "sh ./start.sh")
        self.assertEqual(source, "启动脚本 start.sh")

    def test_mcsm_deploy_background_extract_timeout_reports_source(self):
        deploy = _load_module("mcsm_deploy_wait_timeout_for_test", "plugins/mcsm/deploy.py")

        class FakeClient:
            async def list_instance_files(self, uuid, daemon_id, target="/", page=0, page_size=200):
                return []

        async def no_sleep(_seconds):
            return None

        command, source = asyncio.run(
            deploy.wait_for_deploy_start_command(
                FakeClient(),
                "inst-1",
                "daemon-1",
                2048,
                "",
                wait_seconds=5,
                interval_seconds=5,
                sleep_func=no_sleep,
            )
        )
        self.assertEqual(command, "")
        self.assertIn("后台解压等待超时", source)

    def test_mcsm_qflash_extracts_code_and_builds_signed_headers(self):
        qflash = _load_module("mcsm_qflash_headers_for_test", "plugins/mcsm/qflash.py")

        self.assertEqual(qflash.extract_qflash_code("https://qfile.qq.com/q/XJz5hqnGuc"), "XJz5hqnGuc")
        self.assertTrue(qflash.is_qflash_url("https://qfile.qq.com/q/XJz5hqnGuc"))
        self.assertFalse(qflash.is_qflash_url("https://example.com/server.zip"))

        with patch.object(qflash.random, "randint", return_value=1234), patch.object(qflash.time, "time", return_value=1780632000):
            headers = qflash.build_qflash_headers(
                qflash.GET_FILESET_ID_BY_CODE,
                {"code": "XJz5hqnGuc", "scene_type": 0},
                "https://qfile.qq.com/q/XJz5hqnGuc",
            )

        expected_body = '{"code":"XJz5hqnGuc","scene_type":0}'
        expected_sig = qflash.hmac.new(
            qflash.SIGN_KEY,
            (expected_body + "1234" + "1780632000").encode("utf-8"),
            qflash.hashlib.sha1,
        ).hexdigest()
        self.assertEqual(headers["x-qq-ar-signature"], expected_sig)
        self.assertIn("0x93eb", headers["x-oidb"])

    def test_mcsm_qflash_resolves_archive_download_url(self):
        qflash = _load_module("mcsm_qflash_resolver_for_test", "plugins/mcsm/qflash.py")

        class FakeResolver(qflash.QFlashResolver):
            def __init__(self):
                super().__init__()
                self.calls = []

            async def _rpc(self, api, body, referer):
                self.calls.append((api, body, referer))
                if api == qflash.GET_FILESET_ID_BY_CODE:
                    return {"retcode": 0, "data": {"fileset_id": "fs-1"}}
                if api == qflash.GET_FILESET:
                    return {"retcode": 0, "data": {"fileset": {"expired_time": "1781833652"}}}
                if api == qflash.GET_FILE_LIST:
                    return {
                        "retcode": 0,
                        "data": {
                            "file_lists": [
                                {
                                    "file_list": [
                                        {
                                            "name": "readme.txt",
                                            "is_dir": False,
                                            "file_size": "10",
                                            "physical": {"id": "phys-txt"},
                                        },
                                        {
                                            "name": "server.tar.gz",
                                            "is_dir": False,
                                            "file_size": "2048",
                                            "cli_fileid": "cli-zip",
                                            "physical": {"id": "phys-zip"},
                                        },
                                    ]
                                }
                            ]
                        },
                    }
                if api == qflash.BATCH_DOWNLOAD:
                    if body["download_info"][0]["batch_id"] != "phys-zip":
                        raise AssertionError(body["download_info"][0]["batch_id"])
                    return {"retcode": 0, "data": {"download_rsp": [{"url": "https://multimedia.qfile.qq.com/download?x=1"}]}}
                raise AssertionError(api)

        resolver = FakeResolver()
        archives = asyncio.run(resolver.resolve_archives("https://qfile.qq.com/q/XJz5hqnGuc"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].name, "server.tar.gz")
        self.assertEqual(archives[0].size, 2048)
        self.assertEqual(archives[0].download_url, "https://multimedia.qfile.qq.com/download?x=1")
        self.assertEqual([call[0] for call in resolver.calls], [
            qflash.GET_FILESET_ID_BY_CODE,
            qflash.GET_FILESET,
            qflash.GET_FILE_LIST,
            qflash.BATCH_DOWNLOAD,
        ])

    def test_mcsm_qflash_rejects_share_without_archive(self):
        qflash = _load_module("mcsm_qflash_no_archive_for_test", "plugins/mcsm/qflash.py")

        class FakeResolver(qflash.QFlashResolver):
            async def _rpc(self, api, body, referer):
                if api == qflash.GET_FILESET_ID_BY_CODE:
                    return {"retcode": 0, "data": {"fileset_id": "fs-1"}}
                if api == qflash.GET_FILESET:
                    return {"retcode": 0, "data": {"fileset": {}}}
                if api == qflash.GET_FILE_LIST:
                    return {"retcode": 0, "data": {"file_lists": [{"file_list": [{"name": "readme.txt", "physical": {"id": "p"}}]}]}}
                raise AssertionError(api)

        with self.assertRaises(qflash.QFlashError) as ctx:
            asyncio.run(FakeResolver().resolve_archives("https://qfile.qq.com/q/XJz5hqnGuc"))
        self.assertIn("没有可部署的压缩包", str(ctx.exception))

    def test_mcsm_client_docker_deploy_payloads(self):
        mcsm_client = _load_module("mcsm_client_deploy_for_test", "plugins/mcsm/client.py")

        class FakeClient(mcsm_client.MCSMClient):
            def __init__(self):
                super().__init__("panel.example", "key")
                self.calls = []

            async def _post(self, path, params=None, json_data=None, timeout=30):
                self.calls.append(("POST", path, params, json_data, timeout))
                return {"status": 200, "data": {"instanceUuid": "inst-1"}}

            async def _put(self, path, params=None, json_data=None, timeout=30):
                self.calls.append(("PUT", path, params, json_data, timeout))
                if path == "/api/files/" and json_data and "text" not in json_data:
                    return {"status": 200, "data": {"text": "eula=false\n"}}
                return {"status": 200}

            async def _delete(self, path, params=None, json_data=None, timeout=30):
                self.calls.append(("DELETE", path, params, json_data, timeout))
                return {"status": 200}

            async def get_instance_detail(self, uuid, daemon_id):
                return {"config": {"nickname": "survival", "startCommand": "old"}}

        client = FakeClient()
        asyncio.run(client.create_docker_instance("daemon-1", "survival", "eclipse-temurin:21-jre", "sh ./start.sh", 25566, 3072))
        method, path, params, payload, _timeout = client.calls[-1]
        self.assertEqual((method, path), ("POST", "/api/instance"))
        self.assertEqual(params["daemonId"], "daemon-1")
        self.assertNotIn("config", payload)
        self.assertEqual(payload["processType"], "docker")
        self.assertEqual(payload["cwd"], "/data")
        self.assertEqual(payload["docker"]["image"], "eclipse-temurin:21-jre")
        self.assertEqual(payload["docker"]["ports"], ["25566:25565/tcp"])
        self.assertEqual(payload["docker"]["memory"], 3072)

        asyncio.run(client.install_instance_from_url("inst-1", "daemon-1", "https://example.com/server.zip"))
        _method, path, params, payload, timeout = client.calls[-1]
        self.assertEqual(path, "/api/protected_instance/install_instance")
        self.assertEqual(params["uuid"], "inst-1")
        self.assertEqual(params["daemonId"], "daemon-1")
        self.assertEqual(payload["targetUrl"], "https://example.com/server.zip")
        self.assertEqual(timeout, 60)

        asyncio.run(client.update_instance_start_command("inst-1", "daemon-1", "sh ./启动.sh"))
        method, path, params, payload, _timeout = client.calls[-1]
        self.assertEqual((method, path), ("PUT", "/api/instance"))
        self.assertEqual(params["uuid"], "inst-1")
        self.assertEqual(params["daemonId"], "daemon-1")
        self.assertEqual(payload["startCommand"], "sh ./启动.sh")

        with self.assertRaises(mcsm_client.MCSMAPIError) as ctx:
            asyncio.run(client.create_docker_instance("", "survival", "eclipse-temurin:21-jre", "sh ./start.sh", 25566))
        self.assertIn("缺少 daemonId", str(ctx.exception))

        asyncio.run(client.delete_instance("inst-1", "daemon-1", delete_files=True))
        method, path, params, payload, _timeout = client.calls[-1]
        self.assertEqual((method, path), ("DELETE", "/api/instance"))
        self.assertEqual(params["daemonId"], "daemon-1")
        self.assertNotIn("uuid", params)
        self.assertEqual(payload["uuids"], ["inst-1"])
        self.assertTrue(payload["deleteFile"])

    def test_mcsm_client_file_transfer_payloads(self):
        mcsm_client = _load_module("mcsm_client_file_transfer_for_test", "plugins/mcsm/client.py")

        class FakeClient(mcsm_client.MCSMClient):
            def __init__(self):
                super().__init__("panel.example", "key")
                self.calls = []
                self._daemon_cache = [{"uuid": "daemon-1", "ip": "10.0.0.8"}]

            async def _post(self, path, params=None, json_data=None, timeout=30):
                self.calls.append(("POST", path, params, json_data, timeout))
                if path == "/api/files/upload":
                    return {"status": 200, "data": {"addr": "localhost:24444", "password": "up-token"}}
                return {"status": 200}

            async def _put(self, path, params=None, json_data=None, timeout=30):
                self.calls.append(("PUT", path, params, json_data, timeout))
                if path == "/api/files/" and json_data and "text" not in json_data:
                    return {"status": 200, "data": {"text": "eula=false\n"}}
                return {"status": 200}

            async def _delete(self, path, params=None, json_data=None, timeout=30):
                self.calls.append(("DELETE", path, params, json_data, timeout))
                return {"status": 200}

        client = FakeClient()
        self.assertEqual(
            client._build_upload_url({"addr": "localhost:24444", "password": "up-token"}, "daemon-1"),
            "http://10.0.0.8:24444/upload/up-token",
        )
        self.assertEqual(
            client._build_upload_url({"addr": "192.168.1.20:24444", "password": "pw"}, "daemon-1"),
            "http://192.168.1.20:24444/upload/pw",
        )

        config = asyncio.run(client.get_upload_config("inst-1", "daemon-1", "/"))
        method, path, params, payload, _timeout = client.calls[-1]
        self.assertEqual((method, path), ("POST", "/api/files/upload"))
        self.assertEqual(params["uuid"], "inst-1")
        self.assertEqual(params["daemonId"], "daemon-1")
        self.assertEqual(params["upload_dir"], "/")
        self.assertEqual(payload, {})
        self.assertEqual(config["password"], "up-token")

        asyncio.run(client.extract_instance_archive("inst-1", "daemon-1", "server.zip"))
        method, path, params, payload, timeout = client.calls[-1]
        self.assertEqual((method, path), ("POST", "/api/files/compress"))
        self.assertEqual(params["uuid"], "inst-1")
        self.assertEqual(params["daemonId"], "daemon-1")
        self.assertEqual(payload["type"], 2)
        self.assertEqual(payload["source"], "/server.zip")
        self.assertEqual(payload["targets"], "/")
        self.assertEqual(timeout, 120)

        asyncio.run(client.delete_instance_file("inst-1", "daemon-1", "server.zip"))
        method, path, params, payload, _timeout = client.calls[-1]
        self.assertEqual((method, path), ("DELETE", "/api/files"))
        self.assertEqual(params["uuid"], "inst-1")
        self.assertEqual(params["daemonId"], "daemon-1")
        self.assertEqual(payload["targets"], ["/server.zip"])

        content = asyncio.run(client.read_instance_file("inst-1", "daemon-1", "eula.txt"))
        method, path, params, payload, _timeout = client.calls[-1]
        self.assertEqual((method, path), ("PUT", "/api/files/"))
        self.assertEqual(params["uuid"], "inst-1")
        self.assertEqual(params["daemonId"], "daemon-1")
        self.assertEqual(payload["target"], "/eula.txt")
        self.assertNotIn("text", payload)
        self.assertEqual(content, "eula=false\n")

        asyncio.run(client.read_instance_file("inst-1", "daemon-1", "config/frps.toml"))
        method, path, params, payload, _timeout = client.calls[-1]
        self.assertEqual((method, path), ("PUT", "/api/files/"))
        self.assertEqual(params["uuid"], "inst-1")
        self.assertEqual(params["daemonId"], "daemon-1")
        self.assertEqual(payload["target"], "/config/frps.toml")
        self.assertNotIn("text", payload)

        asyncio.run(client.write_instance_file("inst-1", "daemon-1", "eula.txt", "eula=true\n"))
        method, path, params, payload, _timeout = client.calls[-1]
        self.assertEqual((method, path), ("PUT", "/api/files/"))
        self.assertEqual(params["uuid"], "inst-1")
        self.assertEqual(params["daemonId"], "daemon-1")
        self.assertEqual(payload["target"], "/eula.txt")
        self.assertEqual(payload["text"], "eula=true\n")

    def test_mcsm_client_file_list_uses_root_target(self):
        mcsm_client = _load_module("mcsm_client_file_list_for_test", "plugins/mcsm/client.py")

        class FakeClient(mcsm_client.MCSMClient):
            def __init__(self):
                super().__init__("panel.example", "key")
                self.calls = []

            async def _get(self, path, params=None):
                self.calls.append(("GET", path, params))
                return {"status": 200, "data": [{"name": "start.sh"}]}

        client = FakeClient()
        files = asyncio.run(client.list_instance_files("inst-1", "daemon-1"))
        self.assertEqual(files, [{"name": "start.sh"}])
        method, path, params = client.calls[-1]
        self.assertEqual((method, path), ("GET", "/api/files/list"))
        self.assertEqual(params["uuid"], "inst-1")
        self.assertEqual(params["daemonId"], "daemon-1")
        self.assertEqual(params["target"], "/")
        self.assertEqual(params["page"], 0)
        self.assertEqual(params["page_size"], 200)
        self.assertEqual(params["file_name"], "")

        asyncio.run(client.list_instance_files("inst-1", "daemon-1", target="   ", page=-1, page_size="bad"))
        _method, _path, params = client.calls[-1]
        self.assertEqual(params["target"], "/")
        self.assertEqual(params["page"], 0)
        self.assertEqual(params["page_size"], 200)
        self.assertEqual(params["file_name"], "")

        asyncio.run(client.list_instance_files("inst-1", "daemon-1", page="bad"))
        _method, _path, params = client.calls[-1]
        self.assertEqual(params["page"], 0)
        self.assertEqual(params["file_name"], "")

    def test_mcsm_client_file_list_accepts_paginated_data(self):
        mcsm_client = _load_module("mcsm_client_file_list_paginated_for_test", "plugins/mcsm/client.py")

        class FakeClient(mcsm_client.MCSMClient):
            async def _get(self, path, params=None):
                return {"status": 200, "data": {"items": [{"name": "启动.sh", "type": 1}], "page": 0}}

        client = FakeClient("panel.example", "key")
        files = asyncio.run(client.list_instance_files("inst-1", "daemon-1"))
        self.assertEqual(files, [{"name": "启动.sh", "type": 1}])

    def test_mcsm_client_file_list_sends_empty_file_name_filter(self):
        mcsm_client = _load_module("mcsm_client_file_list_file_name_for_test", "plugins/mcsm/client.py")

        class FakeClient(mcsm_client.MCSMClient):
            def __init__(self):
                super().__init__("panel.example", "key")
                self.calls = []

            async def _get(self, path, params=None):
                self.calls.append((path, params))
                if params and "file_name" in params and params["file_name"] == "":
                    return {
                        "status": 200,
                        "data": {
                            "items": [
                                {"name": "servers", "type": 0},
                                {"name": "frpc.toml", "type": 1},
                            ],
                            "page": 0,
                            "pageSize": 100,
                            "total": 2,
                        },
                    }
                return {"status": 200, "data": {"items": [], "page": 0, "pageSize": 100, "total": 0}}

        client = FakeClient()
        files = asyncio.run(client.list_instance_files("inst-1", "daemon-1"))

        self.assertEqual(files, [{"name": "servers", "type": 0}, {"name": "frpc.toml", "type": 1}])
        _path, params = client.calls[-1]
        self.assertEqual(params["file_name"], "")

    def test_mcsm_client_file_list_accepts_common_nested_shapes(self):
        mcsm_client = _load_module("mcsm_client_file_list_nested_for_test", "plugins/mcsm/client.py")

        payloads = [
            {"status": 200, "data": {"items": [{"name": "server.jar"}]}},
            {"status": 200, "data": {"files": [{"file_name": "paper.jar"}]}},
            {"status": 200, "data": {"children": [{"basename": "start.sh"}]}},
            {"status": 200, "data": {"data": {"items": [{"displayName": "purpur.jar"}]}}},
        ]

        class FakeClient(mcsm_client.MCSMClient):
            def __init__(self):
                super().__init__("panel.example", "key")
                self.index = 0

            async def _get(self, path, params=None):
                payload = payloads[self.index]
                self.index += 1
                return payload

        client = FakeClient()
        self.assertEqual(asyncio.run(client.list_instance_files("inst-1", "daemon-1")), [{"name": "server.jar"}])
        self.assertEqual(asyncio.run(client.list_instance_files("inst-1", "daemon-1")), [{"file_name": "paper.jar"}])
        self.assertEqual(asyncio.run(client.list_instance_files("inst-1", "daemon-1")), [{"basename": "start.sh"}])
        self.assertEqual(asyncio.run(client.list_instance_files("inst-1", "daemon-1")), [{"displayName": "purpur.jar"}])

    def test_plugin_data_migrates_legacy_assets_json(self):
        with TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            os.chdir(tmp)
            try:
                legacy = Path("assets/json")
                legacy.mkdir(parents=True)
                (legacy / "demo.json").write_text('{"value": 42}', encoding="utf-8")

                import utils.plugin_data as plugin_data_module

                plugin_data_module.JSON_PATH = str(legacy.absolute()) + "/"
                data = plugin_data_module.Plugin_Data("demo")
                self.assertEqual(data.plugin_data, {"value": 42})
                self.assertTrue(Path("data/demo/data.json").exists())

                data.plugin_data["value"] = 43
                data.save_plugin_data()
                self.assertIn('"value": 43', Path("data/demo/data.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(cwd)

    def test_schedule_temp_file_cleanup_removes_file_later(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "temp.txt"
            asyncio.run(_run_scheduled_cleanup_case(path))
            self.assertFalse(path.exists())

    def test_parse_ark_invite_raw(self):
        ark_module = _load_module("request_ark_for_test", "plugins/request_handler/ark.py")
        raw = json.dumps({
            "config": {"token": "token-1"},
            "meta": {
                "news": {
                    "title": "group invite",
                    "jumpUrl": (
                        "mqqapi://card/show_pslcard?groupcode=123456"
                        "&groupname=%E6%B5%8B%E8%AF%95%E7%BE%A4"
                        "&senderuin=246&msgseq=flag-9"
                    ),
                },
            },
        }, ensure_ascii=False)

        info = ark_module.parse_ark_invite_raw(raw)
        self.assertEqual(info["group_code"], "123456")
        self.assertEqual(info["group_name"], "group invite")
        self.assertEqual(info["inviter_uin"], "246")
        self.assertEqual(info["msgseq"], "flag-9")
        self.assertEqual(info["token"], "token-1")
        self.assertIsNone(ark_module.parse_ark_invite_raw('{"meta": {"news": {"title": "normal card"}}}'))

    def test_mcwiki_url_builders_encode_keywords(self):
        wiki_module = _load_module("mcwiki_for_test", "plugins/McWikiQuery/__init__.py")

        search_url = wiki_module._wiki_search_url("\u7ea2\u77f3 torch")
        article_url = wiki_module._wiki_article_url("/w/\u7ea2\u77f3")

        self.assertIn("search=%E7%BA%A2%E7%9F%B3+torch", search_url)
        self.assertEqual(article_url, "https://zh.minecraft.wiki/w/\u7ea2\u77f3?variant=zh")

    def test_mcwiki_search_result_parser_handles_empty_html(self):
        wiki_module = _load_module("mcwiki_parser_for_test", "plugins/McWikiQuery/__init__.py")

        self.assertIsNone(wiki_module._extract_first_result_path(""))
        self.assertIsNone(wiki_module._extract_first_result_path("   "))
        self.assertIsNone(wiki_module._extract_first_result_path("<html><body></body></html>"))
        self.assertEqual(
            wiki_module._extract_first_result_path(
                '<a data-serp-pos="0" href="/w/绾㈢煶">绾㈢煶</a>'
            ),
            "/w/绾㈢煶",
        )

    def test_mcwiki_http_client_follows_redirects(self):
        wiki_module = _load_module("mcwiki_client_for_test", "plugins/McWikiQuery/__init__.py")

        self.assertEqual(
            wiki_module._wiki_client_kwargs("http://127.0.0.1:7890"),
            {
                "timeout": 15,
                "follow_redirects": True,
                "proxy": "http://127.0.0.1:7890",
            },
        )

        self.assertEqual(
            wiki_module._wiki_client_kwargs(None),
            {"timeout": 15, "follow_redirects": True},
        )

    def test_mcwiki_image_segment_uses_temp_file(self):
        wiki_module = _load_module("mcwiki_image_for_test", "plugins/McWikiQuery/__init__.py")

        segment = wiki_module._image_segment_from_png(b"png-data")
        path = _image_file_path(segment)
        try:
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), b"png-data")
        finally:
            path.unlink(missing_ok=True)

    def test_mcwiki_splits_tall_images(self):
        wiki_module = _load_module("mcwiki_split_image_for_test", "plugins/McWikiQuery/__init__.py")
        from PIL import Image as PILImage

        source = PILImage.new("RGB", (8, 21), "white")
        buf = BytesIO()
        source.save(buf, format="PNG")

        segments = wiki_module._image_segments_from_png(buf.getvalue(), max_height=10)
        paths = [_image_file_path(segment) for segment in segments]
        try:
            self.assertEqual(len(paths), 3)
            sizes = []
            for path in paths:
                with PILImage.open(path) as image:
                    sizes.append(image.size)
            self.assertEqual(sizes, [(8, 10), (8, 10), (8, 1)])
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

    def test_mcwiki_reply_message_uses_event_message_id(self):
        wiki_module = _load_module("mcwiki_reply_for_test", "plugins/McWikiQuery/__init__.py")

        event = types.SimpleNamespace(msg_id="msg-1")
        message = wiki_module._reply_message(event, "hello")

        self.assertEqual(wiki_module._event_message_id(event), "msg-1")
        self.assertEqual(message[0].id, "msg-1")
        self.assertEqual(str(message[1]), "hello")

    def test_mcwiki_reply_message_falls_back_to_nested_message_id(self):
        wiki_module = _load_module("mcwiki_reply_nested_for_test", "plugins/McWikiQuery/__init__.py")

        event = types.SimpleNamespace(message=types.SimpleNamespace(id="nested-msg"))
        message = wiki_module._reply_message(event, "hello")

        self.assertEqual(wiki_module._event_message_id(event), "nested-msg")
        self.assertEqual(message[0].id, "nested-msg")

    def test_mcwiki_reply_message_without_id_still_sends_content(self):
        wiki_module = _load_module("mcwiki_reply_missing_for_test", "plugins/McWikiQuery/__init__.py")

        message = wiki_module._reply_message(types.SimpleNamespace(), "hello")

        self.assertEqual(len(message), 1)
        self.assertEqual(str(message[0]), "hello")

    def test_mcmod_search_url_encodes_keyword(self):
        mcmod_module = _load_module("mcmod_for_test", "plugins/McModQuery/__init__.py")

        self.assertEqual(
            mcmod_module._search_url("\u66ae\u8272 forest", filter_args="&filter=3&mold=0"),
            "https://search.mcmod.cn/s?key=%E6%9A%AE%E8%89%B2+forest&filter=3&mold=0",
        )

    def test_peek_helpers_use_stable_image_paths(self):
        peek_module = _load_module("peek_helpers_for_test", "plugins/peek/__init__.py")

        self.assertEqual(peek_module._join_endpoint("http://example.com/", "/screenshot"), "http://example.com/screenshot")
        segment = peek_module._image_segment_from_bytes(b"png-data")
        path = _image_file_path(segment)
        try:
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), b"png-data")
        finally:
            path.unlink(missing_ok=True)

    def test_image_utils_playwright_proxy_keeps_scheme(self):
        import utils.image_utils as image_utils

        with patch.object(image_utils, "SYSTEM_PROXY", {"http": "http://127.0.0.1:7890/"}):
            self.assertEqual(
                image_utils._playwright_proxy_conf(),
                {"server": "http://127.0.0.1:7890"},
            )

        with patch.object(image_utils, "SYSTEM_PROXY", {"http": "127.0.0.1:7890"}):
            self.assertEqual(
                image_utils._playwright_proxy_conf(),
                {"server": "http://127.0.0.1:7890"},
            )

    def test_web_image_builders_keeps_legacy_keyword_names(self):
        import utils.image_utils as image_utils

        async def fake_screenshot(web_url, selector):
            self.assertEqual(web_url, "https://example.com/wiki")
            self.assertEqual(selector, "body")
            return b"png-data"

        with TemporaryDirectory() as tmp:
            with patch.object(image_utils, "TEMP_PATH", tmp):
                with patch.object(image_utils, "screenshot_web_element", fake_screenshot):
                    asyncio.run(
                        image_utils.WebImageBuilders(
                            fillName="wiki",
                            webUrl="https://example.com/wiki",
                        )
                    )
            self.assertEqual((Path(tmp) / "wiki.png").read_bytes(), b"png-data")

    def test_image_utils_uses_dedicated_browser_executor(self):
        import utils.image_utils as image_utils

        class FakeLoop:
            def __init__(self):
                self.executor = None
                self.args = None

            async def run_in_executor(self, executor, func, *args):
                self.executor = executor
                self.args = args
                return func(*args)

        def fake_sync(*args):
            return b"png-data"

        async def run_case():
            await image_utils.close_browser()
            loop = FakeLoop()
            with patch("asyncio.get_running_loop", return_value=loop):
                with patch.object(image_utils, "_screenshot_web_element_sync", fake_sync):
                    result = await image_utils.screenshot_web_element("https://example.com")
            executor = loop.executor
            self.assertEqual(result, b"png-data")
            self.assertIsNotNone(executor)
            await image_utils.close_browser()
            return executor

        executor = asyncio.run(run_case())
        self.assertTrue(executor._shutdown)

    def test_minecraft_data_manager_crud_and_rank(self):
        data_source = _load_module("minecraft_data_source_for_test", "plugins/minecraft_plugin/data_source.py")
        with TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            os.chdir(tmp)
            try:
                manager = data_source.MinecraftDataManager()
                self.assertTrue(manager.add_group_server("10001", "survival-server", "mc.example.net"))
                self.assertFalse(manager.add_group_server("10001", "duplicate-server", "mc.example.net"))

                self.assertTrue(manager.update_server_nickname("10001", "survival-server", "survival"))
                self.assertEqual(manager.get_server_by_nickname("10001", "survival")["address"], "mc.example.net")

                self.assertTrue(manager.update_server_name("10001", "survival", "涓绘湇"))
                self.assertEqual(manager.get_server_by_name("10001", "涓绘湇")["address"], "mc.example.net")
                self.assertEqual(manager.get_server_by_nickname("10001", "survival")["name"], "涓绘湇")

                manager.add_player_gametime("Steve", "10001", "survival", 3600)
                manager.add_player_gametime("Alex", "10001", "mc.example.net", 120)
                top = manager.get_top_players(10001, "mc.example.net", limit=1)
                self.assertEqual(top[0]["player_id"], "Steve")
                self.assertEqual(top[0]["formatted_time"], "1\u5c0f\u65f60\u5206\u949f")

                self.assertTrue(manager.update_server_address("10001", "涓绘湇", "mc2.example.net"))
                self.assertIsNone(manager.get_server_by_address("10001", "mc.example.net"))
                self.assertEqual(manager.get_server_by_name("10001", "涓绘湇")["address"], "mc2.example.net")
            finally:
                os.chdir(cwd)

    def test_minecraft_ping_filters_anonymous_player_sample(self):
        ping_module = _load_module("minecraft_ping_for_test", "plugins/minecraft_plugin/ping.py")

        players, hidden = ping_module._extract_player_sample(
            {"sample": [{"name": "Anonymous Player"}, {"name": "Anonymous Player"}]}
        )
        self.assertEqual(players, [])
        self.assertTrue(hidden)

        players, hidden = ping_module._extract_player_sample(
            {
                "sample": [
                    {"name": "Anonymous Player"},
                    {"name": "Steve"},
                    {"name": "Steve"},
                    {"name": ""},
                ]
            }
        )
        self.assertEqual(players, ["Steve"])
        self.assertFalse(hidden)

    def test_minecraft_direct_address_offline_uses_list_card_source_paths(self):
        source = (ROOT / "plugins/minecraft_plugin/__init__.py").read_text(encoding="utf-8")
        start = source.index("r = await ping(command_args")
        end = source.index("# ── add_server", start)
        direct_address_source = source[start:end]

        self.assertIn("output = await run_image_render(_draw_ping_result, info)", direct_address_source)
        self.assertIn('if info.get("status") == "success":', source)
        self.assertIn("return draw_server_players(info)", source)
        self.assertIn('return draw_server_list([draw_server_info(info)], "Ping")', source)

    def test_minecraft_server_info_draws_motd_prefix_source_paths(self):
        source = (ROOT / "plugins/minecraft_plugin/draw.py").read_text(encoding="utf-8")
        start = source.index("def draw_server_info")
        end = source.index("def draw_server_list", start)
        draw_info_source = source[start:end]

        self.assertIn('"Motd: "', draw_info_source)
        self.assertIn("_motd_prefix_w", draw_info_source)
        self.assertIn("_motd_x + _motd_prefix_w", draw_info_source)

    def test_minecraft_broadcast_snapshot_marks_hidden_players(self):
        utils = _load_module(
            "minecraft_broadcast_utils_for_snapshot_test",
            "plugins/minecraft_plugin/broadcast_utils.py",
        )

        snapshot = utils.build_broadcast_snapshot(
            "mc.example.net",
            {
                "name": "survival-server",
                "players": {},
                "players_hidden": True,
                "online_players": 3,
            },
            now=1000,
        )

        self.assertEqual(snapshot["status"], "success")
        self.assertEqual(snapshot["data"]["players"], [])
        self.assertEqual(snapshot["data"]["online_players"], 3)
        self.assertTrue(snapshot["data"]["players_hidden"])

    def test_minecraft_broadcast_playtime_uses_shared_manager_and_address(self):
        data_source = _load_module(
            "minecraft_data_source_for_broadcast_shared_test",
            "plugins/minecraft_plugin/data_source.py",
        )
        broadcast_module = _load_minecraft_broadcast_module(
            "minecraft_broadcast_for_shared_manager_test",
        )

        with TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            os.chdir(tmp)
            try:
                manager = data_source.MinecraftDataManager()
                manager.add_group_server("10001", "SameName", "a.example.net")
                manager.add_group_server("10001", "samename", "b.example.net")

                class BM:
                    data_manager = manager

                broadcast_module._bm = BM()
                with patch.object(broadcast_module.time, "time", return_value=220):
                    messages = broadcast_module._check_player_changes_simple(
                        "samename",
                        {"Steve": 100},
                        [],
                        10001,
                        "b.example.net",
                    )

                self.assertIn("Steve", messages[0])
                self.assertEqual(
                    manager.get_server_player_gametimes("10001", "b.example.net"),
                    {"Steve": 120},
                )
                self.assertEqual(manager.get_server_player_gametimes("10001", "a.example.net"), {})
                self.assertEqual(manager.get_top_players_all_servers(10001)[0]["player_id"], "Steve")
            finally:
                broadcast_module._bm = None
                os.chdir(cwd)

    def test_minecraft_broadcast_cache_entry_initializes_visible_players(self):
        broadcast_module = _load_minecraft_broadcast_module(
            "minecraft_broadcast_for_cache_entry_test",
        )

        entry = broadcast_module._build_broadcast_cache_entry(
            "mc.example.net",
            "survival",
            {
                "status": "success",
                "data": {
                    "players": ["Steve", "Alex"],
                    "players_hidden": False,
                    "online_players": 2,
                },
            },
            1000,
        )

        self.assertEqual(entry["players"], {"Steve": 1000, "Alex": 1000})
        self.assertFalse(entry["players_hidden"])
        self.assertEqual(entry["online_players"], 2)

    def test_minecraft_group_broadcast_interval_settings(self):
        data_source = _load_module(
            "minecraft_data_source_for_interval_test",
            "plugins/minecraft_plugin/data_source.py",
        )

        with TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            os.chdir(tmp)
            try:
                manager = data_source.MinecraftDataManager()
                self.assertIn("group_settings", manager.pl_data.plugin_data)
                manager.add_group_server("10001", "survival", "mc.example.net")

                self.assertEqual(manager.get_group_broadcast_interval("10001", 300), 300)
                self.assertTrue(manager.set_group_broadcast_interval("10001", 600))
                self.assertEqual(manager.get_group_broadcast_interval("10001", 300), 600)
                self.assertIn("mc.example.net", manager.pl_data.plugin_data["group_server"]["10001"])

                self.assertTrue(manager.reset_group_broadcast_interval("10001"))
                self.assertEqual(manager.get_group_broadcast_interval("10001", 300), 300)
            finally:
                os.chdir(cwd)

    def test_minecraft_group_broadcast_due_uses_group_interval(self):
        data_source = _load_module(
            "minecraft_data_source_for_interval_due_test",
            "plugins/minecraft_plugin/data_source.py",
        )
        broadcast_module = _load_minecraft_broadcast_module(
            "minecraft_broadcast_for_interval_due_test",
        )

        with TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            os.chdir(tmp)
            try:
                manager = data_source.MinecraftDataManager()
                manager.set_group_broadcast_interval("10001", 60)

                class BM:
                    data_manager = manager

                broadcast_module._bm = BM()
                broadcast_module._last_broadcast_at.clear()

                self.assertTrue(broadcast_module._is_group_broadcast_due("10001", 1000))
                broadcast_module._mark_group_broadcasted("10001", 1000)
                self.assertFalse(broadcast_module._is_group_broadcast_due("10001", 1059))
                self.assertTrue(broadcast_module._is_group_broadcast_due("10001", 1060))

                broadcast_module._mark_group_broadcasted("10002", 1000)
                self.assertFalse(broadcast_module._is_group_broadcast_due("10002", 1299))
                self.assertTrue(broadcast_module._is_group_broadcast_due("10002", 1300))
            finally:
                broadcast_module._bm = None
                broadcast_module._last_broadcast_at.clear()
                os.chdir(cwd)

    def test_minecraft_broadcast_interval_command_source_paths(self):
        source = (ROOT / "plugins/minecraft_plugin/__init__.py").read_text(encoding="utf-8")
        self.assertIn('broadcast_interval = _cmd("broadcastinterval"', source)
        self.assertIn('if unit in {"", "m", "分", "分钟"}:', source)
        self.assertIn("return value * 60", source)
        self.assertIn('command_args.lower() in {"reset", "default", "默认", "重置"}', source)
        self.assertIn("seconds < 60 or seconds > 24 * 3600", source)
        self.assertIn("data_manager.set_group_broadcast_interval(group_id, seconds)", source)
        self.assertIn("data_manager.reset_group_broadcast_interval(group_id)", source)

    def test_minecraft_ping_group_name_prefers_event_guild_and_fallback(self):
        source = (ROOT / "plugins/minecraft_plugin/__init__.py").read_text(encoding="utf-8")
        self.assertIn("async def _get_group_name", source)
        self.assertIn("guild_name = str(getattr(guild, \"name\", \"\") or \"\").strip()", source)
        self.assertIn("full_guild = await guild_get(guild_id=str(group_id))", source)
        self.assertIn("return str(group_id)", source)
        self.assertIn("group_name = await _get_group_name(event, group_id)", source)
        self.assertIn("_draw_server_list_from_responses", source)
        self.assertIn("run_image_render(", source)

    def test_broadcast_utils_merge_player_changes_and_throttle_errors(self):
        utils = _load_module("minecraft_broadcast_utils_for_test", "plugins/minecraft_plugin/broadcast_utils.py")

        messages, deltas = utils.build_player_change_messages(
            "survival-server",
            {"Steve": 100, "Alex": 160},
            ["Alex", "Herobrine"],
            timestamp=220,
        )
        self.assertEqual(messages, [
            "[MC_Server] survival-server: Herobrine \u52a0\u5165\u4e86\u670d\u52a1\u5668",
            "[MC_Server] survival-server: Steve(2\u5206\u949f) \u79bb\u5f00\u4e86\u670d\u52a1\u5668",
        ])
        self.assertEqual(deltas, {"Steve": 120})

        messages, deltas = utils.build_player_change_messages("survival-server", None, ["Steve", "Alex"], timestamp=1)
        self.assertEqual(messages, ["[MC_Server] \u670d\u52a1\u5668 survival-server \u5df2\u542f\u52a8\uff0c\u5f53\u524d\u5728\u7ebf: Alex\u3001Steve"])
        self.assertEqual(deltas, {})

        last_sent = {}
        self.assertTrue(utils.should_send_error_digest("10001", 2, 1000, last_sent, 600))
        self.assertFalse(utils.should_send_error_digest("10001", 1, 1200, last_sent, 600))
        self.assertTrue(utils.should_send_error_digest("10001", 1, 1700, last_sent, 600))

        grouped = utils.group_errors_by_group(["\u7fa4 10001 server a: boom", "\u7fa4 10002 send failed: x"])
        self.assertEqual(set(grouped), {"10001", "10002"})

    def test_steam_to_image_data_supports_entari_image_src(self):
        from arclet.entari import Image
        import plugins.steamInfo as steam_plugin

        raw_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
            b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfeA\xe2'\xb5"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        self.assertEqual(asyncio.run(steam_plugin.to_image_data(Image.of(raw=raw_png))), raw_png)

        with TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "avatar.png"
            file_path.write_bytes(b"file-data")
            self.assertEqual(
                asyncio.run(steam_plugin.to_image_data(Image.of(path=file_path))),
                b"file-data",
            )

        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def read(self):
                return b"http-data"

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def get(self, url, proxy=None):
                self.url = url
                self.proxy = proxy
                return FakeResponse()

        with patch.object(steam_plugin.aiohttp, "ClientSession", FakeSession):
            self.assertEqual(
                asyncio.run(steam_plugin.to_image_data(Image(src="https://example.com/avatar.png"))),
                b"http-data",
            )

    def test_steam_compare_detects_start_stop_and_change(self):
        steam_data = _load_steam_data_source()
        with TemporaryDirectory() as tmp:
            store = steam_data.SteamInfoData(Path(tmp) / "steam_info.json")
            parent_id = "10001"
            store.update(parent_id, {
                "players": [
                    {"steamid": "1", "personaname": "A"},
                    {"steamid": "2", "personaname": "B", "gameextrainfo": "Old"},
                    {"steamid": "3", "personaname": "C", "gameextrainfo": "Game"},
                ]
            })

            result = store.compare(parent_id, {
                "players": [
                    {"steamid": "1", "personaname": "A", "gameextrainfo": "New"},
                    {"steamid": "2", "personaname": "B", "gameextrainfo": "Newer"},
                    {"steamid": "3", "personaname": "C"},
                ]
            })

        self.assertEqual([item["type"] for item in result], ["start", "change", "stop"])

    def test_steam_bind_data_nickname_compatibility(self):
        steam_data = _load_steam_data_source()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "bind_data.json"
            path.write_text(
                json.dumps({"10001": [{"user_id": "246", "steam_id": "765"}]}),
                encoding="utf-8",
            )

            bind_data = steam_data.BindData(path)
            user_data = bind_data.get("10001", "246")
            self.assertIn("nickname", user_data)
            self.assertIsNone(user_data["nickname"])

            user_data["nickname"] = "otae"
            by_steam = bind_data.get_by_steam_id("10001", "765")
            self.assertEqual(by_steam["nickname"], "otae")

            bind_data.add("10001", {"user_id": "135", "steam_id": "999"})
            self.assertIsNone(bind_data.get("10001", "135")["nickname"])

    def test_steam_bind_data_converts_legacy_user_map(self):
        steam_data = _load_steam_data_source()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "bind_data.json"
            path.write_text(
                json.dumps(
                    {
                        "246": {
                            "steam_id": "765",
                            "bindGroups": [
                                {"group_id": "10001", "nickname": "otae"},
                                {"group_id": "10001", "nickname": "otae"},
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )

            bind_data = steam_data.BindData(path)

            self.assertEqual(
                bind_data.content,
                {
                    "10001": [
                        {
                            "user_id": "246",
                            "steam_id": "765",
                            "nickname": "otae",
                        }
                    ]
                },
            )

    def test_steam_bind_data_remove_deletes_duplicate_user_records(self):
        steam_data = _load_steam_data_source()
        with TemporaryDirectory() as tmp:
            bind_data = steam_data.BindData(Path(tmp) / "bind_data.json")
            bind_data.add("10001", {"user_id": "246", "steam_id": "765"})
            bind_data.content["10001"].append(
                {"user_id": "246", "steam_id": "999", "nickname": None}
            )
            bind_data.add("10001", {"user_id": "135", "steam_id": "888"})

            removed = bind_data.remove("10001", "246")

        self.assertEqual(
            [(item["user_id"], item["steam_id"]) for item in removed],
            [("246", "765"), ("246", "999")],
        )
        self.assertEqual(bind_data.get_all("10001"), ["888"])

    def test_steam_bind_data_add_replaces_same_user_binding(self):
        steam_data = _load_steam_data_source()
        with TemporaryDirectory() as tmp:
            bind_data = steam_data.BindData(Path(tmp) / "bind_data.json")
            bind_data.add("10001", {"user_id": "246", "steam_id": "765"})
            bind_data.add("10001", {"user_id": "246", "steam_id": "999"})

        self.assertEqual(bind_data.content["10001"], [{"user_id": "246", "steam_id": "999", "nickname": None}])

    def test_steam_bind_data_remove_deletes_empty_parent(self):
        steam_data = _load_steam_data_source()
        with TemporaryDirectory() as tmp:
            bind_data = steam_data.BindData(Path(tmp) / "bind_data.json")
            bind_data.add("10001", {"user_id": "246", "steam_id": "765"})

            removed = bind_data.remove("10001", "246")

        self.assertEqual([item["steam_id"] for item in removed], ["765"])
        self.assertNotIn("10001", bind_data.content)

    def test_steam_info_data_prunes_unbound_players(self):
        steam_data = _load_steam_data_source()
        with TemporaryDirectory() as tmp:
            steam_info_data = steam_data.SteamInfoData(Path(tmp) / "steam_info.json")
            steam_info_data.content["10001"] = {
                "players": [
                    {"steamid": "765", "personaname": "A", "gameextrainfo": "Game"},
                    {"steamid": "999", "personaname": "B"},
                ]
            }

            changed = steam_info_data.prune_players("10001", ["999"])

        self.assertTrue(changed)
        self.assertEqual(
            steam_info_data.content["10001"]["players"],
            [{"steamid": "999", "personaname": "B"}],
        )

    def test_steam_info_data_prune_removes_empty_parent(self):
        steam_data = _load_steam_data_source()
        with TemporaryDirectory() as tmp:
            steam_info_data = steam_data.SteamInfoData(Path(tmp) / "steam_info.json")
            steam_info_data.content["10001"] = {
                "players": [{"steamid": "765", "personaname": "A"}]
            }

            changed = steam_info_data.prune_players("10001", [])

        self.assertTrue(changed)
        self.assertNotIn("10001", steam_info_data.content)

    def test_steam_format_display_name_marks_group_nickname(self):
        steam_data = _load_steam_data_source()

        self.assertEqual(steam_data.format_display_name("ABC", "AAA"), "*AAA")
        self.assertEqual(steam_data.format_display_name("ABC", "*AAA"), "*AAA")
        self.assertEqual(steam_data.format_display_name("ABC", None), "ABC")
        self.assertEqual(steam_data.format_display_name(None, None, "765"), "765")

    def test_steam_status_data_uses_marked_group_nickname(self):
        steam_data = _load_steam_data_source()
        steam_draw = _load_steam_draw_module()

        async def fake_fetch(avatar_url, proxy=None):
            return steam_draw._unknown_avatar(), False

        player = {
            "steamid": "765",
            "avatarfull": "https://example.invalid/a.png",
            "personaname": "ABC",
            "personastate": 1,
        }
        with TemporaryDirectory() as tmp:
            bind_data = steam_data.BindData(Path(tmp) / "bind_data.json")
            bind_data.add("10001", {"user_id": "246", "steam_id": "765", "nickname": "AAA"})
            user_data = bind_data.get_by_steam_id("10001", player["steamid"])
            with patch.object(steam_draw, "_fetch_avatar", fake_fetch):
                item = asyncio.run(
                    steam_draw.simplize_steam_player_data(player, avatar_dir=Path(tmp) / "cache")
                )
            item["name"] = steam_data.format_display_name(
                player["personaname"], user_data.get("nickname"), player["steamid"]
            )

        self.assertEqual(item["name"], "*AAA")

    def test_steam_parent_data_keeps_name_when_avatar_missing(self):
        steam_data = _load_steam_data_source()
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "parent_data.json"
            path.write_text(json.dumps({"10001": "Test Group"}), encoding="utf-8")

            parent_data = steam_data.ParentData(path)
            _avatar, parent_name = parent_data.get("10001")

            self.assertEqual(parent_name, "Test Group")
            self.assertFalse(parent_data.has_avatar("10001"))

    def test_steam_repair_from_project_data_backfills_new_format_only(self):
        steam_data = _load_steam_data_source()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "data" / "steam_info"
            project.mkdir(parents=True)
            (project / "data.json").write_text(
                json.dumps(
                    {
                        "246": {
                            "steam_id": "765",
                            "bindGroups": [
                                {"group_id": "10001", "nickname": "dev"}
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            (project / "parent_data.json").write_text(
                json.dumps({"10001": "Test Group"}),
                encoding="utf-8",
            )
            (project / "steam_info.json").write_text(
                json.dumps({"10001": {"players": [{"steamid": "765"}]}}),
                encoding="utf-8",
            )

            local = root / "localstore"
            bind_data = steam_data.BindData(local / "bind_data.json")
            steam_info_data = steam_data.SteamInfoData(local / "steam_info.json")
            parent_data = steam_data.ParentData(local / "parent_data.json")

            stats = steam_data.repair_from_project_data(
                project, bind_data, steam_info_data, parent_data
            )

            self.assertEqual(stats, {"bind": 1, "steam_info": 1, "parent": 1})
            self.assertEqual(
                bind_data.content,
                {
                    "10001": [
                        {
                            "user_id": "246",
                            "steam_id": "765",
                            "nickname": "dev",
                        }
                    ]
                },
            )
            self.assertEqual(parent_data.content["10001"], "Test Group")
            self.assertEqual(
                steam_info_data.content["10001"]["players"][0]["steamid"], "765"
            )

            stats = steam_data.repair_from_project_data(
                project, bind_data, steam_info_data, parent_data
            )
            self.assertEqual(stats, {"bind": 0, "steam_info": 0, "parent": 0})

    def test_steam_repair_from_project_data_does_not_restore_existing_runtime_bind(self):
        steam_data = _load_steam_data_source()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "data" / "steam_info"
            project.mkdir(parents=True)
            (project / "data.json").write_text(
                json.dumps(
                    {
                        "246": {
                            "steam_id": "765",
                            "bindGroups": [{"group_id": "10001"}],
                        }
                    }
                ),
                encoding="utf-8",
            )

            local = root / "localstore"
            bind_data = steam_data.BindData(local / "bind_data.json")
            bind_data.add("10001", {"user_id": "135", "steam_id": "999"})
            bind_data.save()
            steam_info_data = steam_data.SteamInfoData(local / "steam_info.json")
            parent_data = steam_data.ParentData(local / "parent_data.json")

            stats = steam_data.repair_from_project_data(
                project, bind_data, steam_info_data, parent_data
            )

        self.assertEqual(stats["bind"], 0)
        self.assertEqual(bind_data.get_all("10001"), ["999"])

    def test_steam_fetch_avatar_falls_back_on_network_error(self):
        steam_draw = _load_steam_draw_module()

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def get(self, *args, **kwargs):
                raise steam_draw.aiohttp.ClientConnectorError(None, OSError("boom"))

        with patch.object(steam_draw.aiohttp, "ClientSession", return_value=FakeSession()):
            avatar, fetched = asyncio.run(steam_draw._fetch_avatar("https://example.invalid/a.png", "http://proxy"))

        self.assertFalse(fetched)
        self.assertGreater(avatar.width, 0)
        self.assertGreater(avatar.height, 0)

    def test_steam_fetch_avatar_does_not_retry_without_proxy(self):
        steam_draw = _load_steam_draw_module()
        calls = []

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def get(self, *args, **kwargs):
                calls.append(kwargs.get("proxy"))
                raise steam_draw.aiohttp.ClientConnectorError(None, OSError("boom"))

        with patch.object(steam_draw.aiohttp, "ClientSession", return_value=FakeSession()):
            avatar, fetched = asyncio.run(steam_draw._fetch_avatar("https://example.invalid/a.png", "http://proxy"))

        self.assertFalse(fetched)
        self.assertEqual(calls, ["http://proxy"])
        self.assertGreater(avatar.width, 0)
        self.assertGreater(avatar.height, 0)

    def test_steam_fetch_avatar_ignores_broken_cache(self):
        steam_draw = _load_steam_draw_module()

        async def fake_fetch(avatar_url, proxy=None):
            return steam_draw._unknown_avatar(), False

        player = {
            "steamid": "765",
            "avatarfull": "https://example.invalid/a.png",
            "personaname": "A",
            "personastate": 1,
        }
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            cache.joinpath("avatar_765.png").write_text("broken", encoding="utf-8")
            with patch.object(steam_draw, "_fetch_avatar", fake_fetch):
                avatar = asyncio.run(steam_draw.fetch_avatar(player, cache))
            self.assertGreater(avatar.width, 0)
            self.assertFalse((cache / "avatar_765.png").exists())

    def test_steam_simplize_player_data_survives_avatar_failure(self):
        steam_draw = _load_steam_draw_module()

        async def fake_fetch(avatar_url, proxy=None):
            return steam_draw._unknown_avatar(), False

        player = {
            "steamid": "765",
            "avatarfull": "https://example.invalid/a.png",
            "personaname": "A",
            "personastate": 1,
        }
        with TemporaryDirectory() as tmp:
            with patch.object(steam_draw, "_fetch_avatar", fake_fetch):
                data = asyncio.run(
                    steam_draw.simplize_steam_player_data(player, avatar_dir=Path(tmp))
                )

        self.assertEqual(data["name"], "A")
        self.assertIn("avatar", data)
        self.assertGreater(data["avatar"].width, 0)

    def test_steam_game_stats_uses_blue_rank_text_without_achievement_bar(self):
        steam_draw = _load_steam_draw_module()
        avatar = steam_draw.Image.new("RGB", (72, 72), "white")
        app_icon = steam_draw.Image.new("RGB", (64, 64), "black")

        image = steam_draw.draw_game_stats(
            avatar,
            "Test Group",
            app_icon,
            "Terraria",
            105600,
            1,
            1,
            "203h6m",
            "0m",
            [
                {
                    "name": "WindowsSov8",
                    "avatar": avatar,
                    "last_text": "no record",
                    "total_text": "567h47m",
                    "recent_text": "0m",
                    "recent": 0,
                    "achievement_completed": 103,
                    "achievement_total": 137,
                }
            ],
        )

        self.assertEqual(image.size[0], steam_draw.WIDTH)
        first_row_y = 120 + 142 + 48
        name_area_pixels = [
            image.getpixel((x, y))
            for x in range(100, 240)
            for y in range(first_row_y + 11, first_row_y + 33)
        ]
        self.assertIn(steam_draw.hex_to_rgb("cdefff"), name_area_pixels)
        old_bar_pixels = [
            image.getpixel((x, first_row_y + 81))
            for x in range(100, 358, 32)
        ]
        self.assertNotIn(steam_draw.hex_to_rgb("8ebe56"), old_bar_pixels)
        self.assertNotIn(steam_draw.hex_to_rgb("333439"), old_bar_pixels)

    def test_steam_update_tracks_game_start_time(self):
        steam_data = _load_steam_data_source()
        with TemporaryDirectory() as tmp:
            store = steam_data.SteamInfoData(Path(tmp) / "steam_info.json")

            with patch.object(steam_data.time, "time", return_value=1000):
                store.update(
                    "10001",
                    {"players": [{"steamid": "1", "personaname": "A", "gameextrainfo": "Game"}]},
                )
            self.assertEqual(store.get("10001")["players"][0]["game_start_time"], 1000)

            with patch.object(steam_data.time, "time", return_value=1300):
                store.update(
                    "10001",
                    {"players": [{"steamid": "1", "personaname": "A", "gameextrainfo": "Game"}]},
                )
            self.assertEqual(store.get("10001")["players"][0]["game_start_time"], 1000)

            result = store.compare(
                "10001", {"players": [{"steamid": "1", "personaname": "A"}]}
            )
            self.assertEqual(result[0]["type"], "stop")
            self.assertEqual(result[0]["old_player"]["game_start_time"], 1000)

    def test_steam_find_app_from_cached_list(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "steam_app_list.json").write_text(
                json.dumps(
                    {
                        "applist": {
                            "apps": [
                                {"appid": 570, "name": "Dota 2"},
                                {"appid": 1245620, "name": "ELDEN RING"},
                                {"appid": 367520, "name": "Hollow Knight"},
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            async def no_store_search(query, proxy=None):
                return None

            async def no_wikidata_search(query, proxy=None):
                return None

            with patch.object(steam, "search_steam_store_app", no_store_search), patch.object(
                steam, "search_wikidata_steam_app", no_wikidata_search
            ):
                alias_path = cache / "aliases.json"
                exact = asyncio.run(
                    steam.find_steam_app("elden ring", cache, alias_path=alias_path)
                )
                contains = asyncio.run(
                    steam.find_steam_app("Knight", cache, alias_path=alias_path)
                )
                numeric = asyncio.run(steam.find_steam_app("730", cache))

        self.assertEqual(exact["appid"], 1245620)
        self.assertEqual(contains["appid"], 367520)
        self.assertEqual(numeric, {"appid": 730, "name": "730"})

    def test_steam_cached_alias_or_lookup_skips_llm_path(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            alias_path = cache / "aliases.json"
            alias_app = {"appid": 105600, "name": "Terraria"}
            lookup_app = {"appid": 367520, "name": "Hollow Knight"}

            steam.write_steam_app_alias("娉版媺", alias_app, alias_path)
            steam.write_app_lookup_cache("绌烘礊", cache, lookup_app)

            self.assertEqual(
                steam.find_cached_steam_app_alias_or_lookup("娉版媺", cache, alias_path),
                alias_app,
            )
            self.assertEqual(
                steam.find_cached_steam_app_alias_or_lookup("绌烘礊", cache, alias_path),
                lookup_app,
            )
            self.assertIsNone(
                steam.find_cached_steam_app_alias_or_lookup("new game", cache, alias_path)
            )

    def test_steam_ambiguous_cache_dedupes_and_keeps_candidates(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            steam.write_app_ambiguous_cache(
                "portal",
                cache,
                [
                    {"appid": 400, "name": "Portal", "source": "llm"},
                    {"appid": 620, "name": "Portal 2", "source": "steam_contains"},
                    {"appid": 400, "name": "Portal", "source": "bangumi"},
                ],
            )
            candidates = steam.read_app_ambiguous_cache("portal", cache)

        self.assertEqual(
            [(item["appid"], item["name"]) for item in candidates],
            [(400, "Portal"), (620, "Portal 2")],
        )
        self.assertEqual(candidates[0]["source"], "llm")

    def test_steam_ambiguous_cache_appends_user_confirmed_candidate(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            steam.write_app_ambiguous_cache(
                "portal",
                cache,
                [{"appid": 400, "name": "Portal", "source": "llm"}],
            )
            steam.append_app_ambiguous_cache(
                "portal",
                cache,
                {"appid": 620, "name": "Portal 2"},
                "user_confirmed",
            )
            candidates = steam.read_app_ambiguous_cache("portal", cache)

        self.assertEqual(
            [(item["appid"], item["name"], item["source"]) for item in candidates],
            [(620, "Portal 2", "user_confirmed"), (400, "Portal", "llm")],
        )

    def test_steam_similar_ambiguous_cache_merges_related_queries(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            steam.write_app_ambiguous_cache(
                "pubg",
                cache,
                [{"appid": 578080, "name": "PUBG: BATTLEGROUNDS", "source": "user_selected"}],
            )
            steam.write_app_ambiguous_cache(
                "\u9b54\u6cd5\u5c11\u5973\u30ce\u9b54\u5973\u88c1\u5224",
                cache,
                [{"appid": 3345610, "name": "\u9b54\u6cd5\u5c11\u5973\u30ce\u9b54\u5973\u88c1\u5224", "source": "user_selected"}],
            )

            ascii_candidates = steam.read_similar_app_ambiguous_cache("PUBG steam", cache)
            cjk_candidates = steam.read_similar_app_ambiguous_cache("\u9b54\u6cd5\u5c11\u5973", cache)

        self.assertEqual(ascii_candidates[0]["appid"], 578080)
        self.assertEqual(cjk_candidates[0]["appid"], 3345610)

    def test_steam_candidate_resolution_includes_similar_ambiguous_cache(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "steam_app_list.json").write_text(
                json.dumps({"applist": {"apps": []}}),
                encoding="utf-8",
            )
            steam.write_app_ambiguous_cache(
                "pubg",
                cache,
                [{"appid": 578080, "name": "PUBG: BATTLEGROUNDS", "source": "user_selected"}],
            )

            async def empty_names(*args, **kwargs):
                return []

            with patch.object(steam, "suggest_steam_game_names", empty_names), patch.object(
                steam, "search_bangumi_game_candidates", empty_names
            ):
                candidates = asyncio.run(
                    steam.resolve_steam_app_candidates("pubg steam", cache)
                )

        self.assertEqual(candidates[0]["appid"], 578080)

    def test_steam_candidate_resolution_returns_cache_without_slow_sources(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            alias_path = cache / "aliases.json"
            steam.write_steam_app_alias(
                "\u96c0\u9b42", {"appid": 1329410, "name": "Mahjong Soul"}, alias_path
            )

            async def slow_source(*args, **kwargs):
                await asyncio.sleep(10)
                return []

            async def slow_app(*args, **kwargs):
                await asyncio.sleep(10)
                return {"appid": 1, "name": "Slow"}

            with patch.object(steam, "suggest_steam_game_names", slow_source), patch.object(
                steam, "search_bangumi_game_candidates", slow_source
            ), patch.object(steam, "search_steam_store_app", slow_app), patch.object(
                steam, "search_wikidata_steam_app", slow_app
            ):
                started = time.perf_counter()
                candidates = asyncio.run(
                    steam.resolve_steam_app_candidates(
                        "\u96c0\u9b42", cache, alias_path=alias_path, network_budget=0.1
                    )
                )
                elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1)
        self.assertEqual(
            candidates,
            [{"appid": 1329410, "name": "Mahjong Soul", "source": "cache"}],
        )

    def test_steam_candidate_resolution_times_out_slow_network_sources(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "steam_app_list.json").write_text(
                json.dumps({"applist": {"apps": []}}),
                encoding="utf-8",
            )

            async def slow_source(*args, **kwargs):
                await asyncio.sleep(10)
                return []

            async def slow_app(*args, **kwargs):
                await asyncio.sleep(10)
                return None

            with patch.object(steam, "suggest_steam_game_names", slow_source), patch.object(
                steam, "search_bangumi_game_candidates", slow_source
            ), patch.object(steam, "search_steam_store_app", slow_app), patch.object(
                steam, "search_wikidata_steam_app", slow_app
            ):
                started = time.perf_counter()
                candidates = asyncio.run(
                    steam.resolve_steam_app_candidates(
                        "unknown game", cache, network_budget=0.1
                    )
                )
                elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 1)
        self.assertEqual(candidates, [])

    def test_steam_player_summaries_uses_https_params_and_configured_proxy(self):
        steam = _load_steam_module()
        calls = []

        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def json(self):
                return {"response": {"players": [{"steamid": "123"}]}}

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def get(self, url, **kwargs):
                calls.append((url, kwargs))
                return FakeResponse()

        with patch.object(steam.aiohttp, "ClientSession", return_value=FakeSession()):
            result = asyncio.run(
                steam.get_steam_users_info(
                    ["123"], ["test-key"], "http://127.0.0.1:7890"
                )
            )

        self.assertEqual(result["response"]["players"][0]["steamid"], "123")
        self.assertEqual(calls[0][0], steam.STEAM_PLAYER_SUMMARIES_URL)
        self.assertTrue(calls[0][0].startswith("https://"))
        self.assertEqual(
            calls[0][1]["params"], {"key": "test-key", "steamids": "123"}
        )
        self.assertEqual(calls[0][1]["proxy"], "http://127.0.0.1:7890")

    def test_steam_authenticated_web_api_urls_use_https(self):
        steam = _load_steam_module()

        self.assertTrue(steam.STEAM_PLAYER_SUMMARIES_URL.startswith("https://"))
        self.assertTrue(steam.STEAM_OWNED_GAMES_URL.startswith("https://"))
        self.assertTrue(steam.STEAM_PLAYER_ACHIEVEMENTS_URL.startswith("https://"))

    def test_steam_player_summaries_falls_back_to_direct_after_proxy_failure(self):
        steam = _load_steam_module()
        calls = []

        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def json(self):
                return {"response": {"players": [{"steamid": "123"}]}}

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def get(self, url, **kwargs):
                request_proxy = kwargs.get("proxy")
                calls.append(request_proxy)
                if request_proxy:
                    raise steam.aiohttp.ClientConnectionError("proxy unavailable")
                return FakeResponse()

        with patch.object(steam.aiohttp, "ClientSession", return_value=FakeSession()):
            result = asyncio.run(
                steam.get_steam_users_info(
                    ["123"], ["first-key", "second-key"], "http://127.0.0.1:7890"
                )
            )

        self.assertEqual(result["response"]["players"][0]["steamid"], "123")
        self.assertEqual(calls, ["http://127.0.0.1:7890", None])

    def test_steam_app_list_prefers_official_store_service_with_api_key(self):
        steam = _load_steam_module()

        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def json(self, content_type=None):
                return {
                    "response": {
                        "apps": [
                            {"appid": 3527290, "name": "PEAK"},
                            {"appid": 3506430, "name": "Peak"},
                        ]
                    }
                }

        class FakeSession:
            def __init__(self):
                self.urls = []
                self.params = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def get(self, url, **kwargs):
                self.urls.append(url)
                self.params.append(kwargs.get("params") or {})
                return FakeResponse()

        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "steam_app_list.json").write_text(
                json.dumps(
                    {
                        "applist": {
                            "apps": [{"appid": 2272630, "name": "PEAK.53"}]
                        }
                    }
                ),
                encoding="utf-8",
            )
            fake_session = FakeSession()
            with patch.object(steam.aiohttp, "ClientSession", return_value=fake_session):
                apps = asyncio.run(
                    steam.get_steam_app_list(cache, steam_api_key="test-key")
                )

            cached = json.loads((cache / "steam_app_list.json").read_text(encoding="utf-8"))

        self.assertEqual(
            [(app["appid"], app["name"]) for app in apps],
            [(3527290, "PEAK"), (3506430, "Peak")],
        )
        self.assertEqual(fake_session.urls, [steam.STEAM_STORE_APP_LIST_URL])
        self.assertIn('"include_games":true', fake_session.params[0]["input_json"])
        self.assertEqual(cached["applist"]["apps"], apps)

    def test_steam_app_list_prefer_cache_skips_official_refresh(self):
        steam = _load_steam_module()

        class FakeSession:
            def get(self, *args, **kwargs):
                raise AssertionError("network should not be called")

        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            cached_apps = [{"appid": 2272630, "name": "PEAK.53"}]
            (cache / "steam_app_list.json").write_text(
                json.dumps({"applist": {"apps": cached_apps}}),
                encoding="utf-8",
            )
            with patch.object(steam.aiohttp, "ClientSession", return_value=FakeSession()):
                apps = asyncio.run(
                    steam.get_steam_app_list(
                        cache,
                        steam_api_key="test-key",
                        prefer_cache=True,
                    )
                )

        self.assertEqual(apps, cached_apps)

    def test_steam_app_list_store_service_falls_back_to_partner_host(self):
        steam = _load_steam_module()

        class PublicFailureResponse:
            status = 403

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def json(self, content_type=None):
                return {}

        class PartnerResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def json(self, content_type=None):
                return {
                    "response": {
                        "apps": [{"appid": 3527290, "name": "PEAK"}]
                    }
                }

        class FakeSession:
            def __init__(self):
                self.urls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def get(self, url, **kwargs):
                self.urls.append(url)
                if url == steam.STEAM_STORE_APP_LIST_URL:
                    return PublicFailureResponse()
                return PartnerResponse()

        with TemporaryDirectory() as tmp:
            fake_session = FakeSession()
            with patch.object(steam.aiohttp, "ClientSession", return_value=fake_session):
                apps = asyncio.run(
                    steam.get_steam_app_list(Path(tmp), steam_api_key="test-key")
                )

        self.assertEqual(apps, [{"appid": 3527290, "name": "PEAK"}])
        self.assertEqual(fake_session.urls, list(steam.STEAM_STORE_APP_LIST_URLS))

    def test_steam_app_list_falls_back_to_direct_after_proxy_failure(self):
        steam = _load_steam_module()
        calls = []

        class StoreFailureResponse:
            status = 500

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def json(self, content_type=None):
                return {}

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def get(self, url, **kwargs):
                calls.append((url, kwargs.get("proxy")))
                return StoreFailureResponse()

        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            cached_apps = [{"appid": 2272630, "name": "PEAK.53"}]
            (cache / "steam_app_list.json").write_text(
                json.dumps({"applist": {"apps": cached_apps}}),
                encoding="utf-8",
            )
            with patch.object(steam.aiohttp, "ClientSession", return_value=FakeSession()):
                apps = asyncio.run(
                    steam.get_steam_app_list(
                        cache,
                        proxy="http://127.0.0.1:7890",
                        steam_api_key="test-key",
                    )
                )

        self.assertEqual(apps, cached_apps)
        self.assertEqual(
            calls,
            [
                (steam.STEAM_STORE_APP_LIST_URLS[0], "http://127.0.0.1:7890"),
                (steam.STEAM_STORE_APP_LIST_URLS[0], None),
                (steam.STEAM_STORE_APP_LIST_URLS[1], "http://127.0.0.1:7890"),
                (steam.STEAM_STORE_APP_LIST_URLS[1], None),
            ],
        )

    def test_steam_app_list_with_api_key_falls_back_to_fresh_cache_on_failure(self):
        steam = _load_steam_module()

        class StoreFailureResponse:
            status = 500

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def json(self, content_type=None):
                return {}

        class FakeSession:
            def __init__(self):
                self.urls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def get(self, url, **kwargs):
                self.urls.append(url)
                return StoreFailureResponse()

        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            cached_apps = [{"appid": 2272630, "name": "PEAK.53"}]
            (cache / "steam_app_list.json").write_text(
                json.dumps({"applist": {"apps": cached_apps}}),
                encoding="utf-8",
            )
            fake_session = FakeSession()
            with patch.object(steam.aiohttp, "ClientSession", return_value=fake_session):
                apps = asyncio.run(
                    steam.get_steam_app_list(cache, steam_api_key="test-key")
                )

        self.assertEqual(apps, cached_apps)
        self.assertEqual(fake_session.urls, list(steam.STEAM_STORE_APP_LIST_URLS))

    def test_steam_app_list_falls_back_when_store_service_fails(self):
        steam = _load_steam_module()

        class StoreFailureResponse:
            status = 500

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def json(self, content_type=None):
                return {}

        class LegacyResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def json(self):
                return {
                    "applist": {
                        "apps": [{"appid": 467360, "name": "Off-Peak"}]
                    }
                }

        class FakeSession:
            def __init__(self):
                self.urls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def get(self, url, **kwargs):
                self.urls.append(url)
                if url in steam.STEAM_STORE_APP_LIST_URLS:
                    return StoreFailureResponse()
                return LegacyResponse()

        with TemporaryDirectory() as tmp:
            fake_session = FakeSession()
            with patch.object(steam.aiohttp, "ClientSession", return_value=fake_session):
                apps = asyncio.run(
                    steam.get_steam_app_list(Path(tmp), steam_api_key="test-key")
                )

        self.assertEqual(apps, [{"appid": 467360, "name": "Off-Peak"}])
        self.assertEqual(fake_session.urls[0], steam.STEAM_STORE_APP_LIST_URL)
        self.assertEqual(fake_session.urls[1], steam.STEAM_STORE_APP_LIST_URLS[1])
        self.assertIn(steam.STEAM_APP_LIST_URLS[0], fake_session.urls)

    def test_steam_app_list_without_api_key_uses_legacy_sources(self):
        steam = _load_steam_module()

        class LegacyResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def json(self):
                return {
                    "applist": {
                        "apps": [{"appid": 467360, "name": "Off-Peak"}]
                    }
                }

        class FakeSession:
            def __init__(self):
                self.urls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def get(self, url, **kwargs):
                self.urls.append(url)
                return LegacyResponse()

        with TemporaryDirectory() as tmp:
            fake_session = FakeSession()
            with patch.object(steam.aiohttp, "ClientSession", return_value=fake_session):
                apps = asyncio.run(steam.get_steam_app_list(Path(tmp)))

        self.assertEqual(apps, [{"appid": 467360, "name": "Off-Peak"}])
        self.assertNotIn(steam.STEAM_STORE_APP_LIST_URL, fake_session.urls)

    def test_steam_peak_like_exact_casefold_match_stays_ambiguous(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "steam_app_list.json").write_text(
                json.dumps(
                    {
                        "applist": {
                            "apps": [
                                {"appid": 3527290, "name": "PEAK"},
                                {"appid": 3506430, "name": "Peak"},
                                {"appid": 2272630, "name": "PEAK.53"},
                                {"appid": 467360, "name": "Off-Peak"},
                                {"appid": 1248230, "name": "PeakPoise"},
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            async def no_remote(query, proxy=None):
                return None

            with patch.object(steam, "search_wikidata_steam_app", no_remote), patch.object(
                steam, "search_steam_store_app", no_remote
            ), patch.object(steam, "search_bangumi_game_candidates", no_remote):
                direct = asyncio.run(steam.find_steam_app("peak", cache))
                candidates = asyncio.run(steam.resolve_steam_app_candidates("peak", cache))

        self.assertIsNone(direct)
        self.assertEqual(
            [(item["appid"], item["name"]) for item in candidates[:5]],
            [
                (3527290, "PEAK"),
                (3506430, "Peak"),
                (2272630, "PEAK.53"),
                (467360, "Off-Peak"),
                (1248230, "PeakPoise"),
            ],
        )

    def test_steam_candidate_resolution_includes_remote_single_result(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            (cache / "steam_app_list.json").write_text(
                json.dumps({"applist": {"apps": []}}),
                encoding="utf-8",
            )

            async def empty_names(*args, **kwargs):
                return []

            async def store_result(query, proxy=None):
                return {"appid": 2272630, "name": "PEAK.53"}

            async def no_wikidata(query, proxy=None):
                return None

            with patch.object(steam, "suggest_steam_game_names", empty_names), patch.object(
                steam, "search_bangumi_game_candidates", empty_names
            ), patch.object(steam, "search_steam_store_app", store_result), patch.object(
                steam, "search_wikidata_steam_app", no_wikidata
            ):
                candidates = asyncio.run(steam.resolve_steam_app_candidates("peak", cache))

        self.assertEqual(
            [(item["appid"], item["name"], item["source"]) for item in candidates],
            [(2272630, "PEAK.53", "steam_store")],
        )

    def test_steam_unambiguous_app_ignores_poisoned_cache_for_ambiguous_name(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            steam.write_app_lookup_cache(
                "peak", cache, {"appid": 2272630, "name": "PEAK.53"}
            )

            async def app_list(cache_path, proxy=None, steam_api_key=None, **kwargs):
                return [
                    {"appid": 3527290, "name": "PEAK"},
                    {"appid": 3506430, "name": "Peak"},
                    {"appid": 467360, "name": "Off-Peak"},
                ]

            with patch.object(steam, "get_steam_app_list", app_list):
                app = asyncio.run(steam.find_unambiguous_steam_app("peak", cache))

        self.assertIsNone(app)

    def test_steam_unambiguous_app_accepts_exact_case_official_name(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)

            async def app_list(cache_path, proxy=None, steam_api_key=None, **kwargs):
                return [
                    {"appid": 3527290, "name": "PEAK"},
                    {"appid": 3506430, "name": "Peak"},
                    {"appid": 467360, "name": "Off-Peak"},
                ]

            with patch.object(steam, "get_steam_app_list", app_list):
                app = asyncio.run(steam.find_unambiguous_steam_app("PEAK", cache))

        self.assertEqual(app, {"appid": 3527290, "name": "PEAK"})

    def test_steam_find_app_can_disable_alias_and_lookup_direct_match(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            alias_path = cache / "aliases.json"
            steam.write_steam_app_alias("雀魂", {"appid": 1329410, "name": "Mahjong Soul"}, alias_path)
            steam.write_app_lookup_cache(
                "魔法少女的魔女审判",
                cache,
                {"appid": 3330000, "name": "Sakura Magical Girls"},
            )
            (cache / "steam_app_list.json").write_text(
                json.dumps({"applist": {"apps": []}}),
                encoding="utf-8",
            )

            async def no_remote(*args, **kwargs):
                return None

            async def empty_names(*args, **kwargs):
                return []

            with patch.object(steam, "search_wikidata_steam_app", no_remote), patch.object(
                steam, "search_steam_store_app", no_remote
            ), patch.object(steam, "search_bangumi_game_candidates", empty_names):
                alias_direct = asyncio.run(
                    steam.find_steam_app("雀魂", cache, alias_path=alias_path)
                )
                alias_disabled = asyncio.run(
                    steam.find_steam_app(
                        "雀魂",
                        cache,
                        alias_path=alias_path,
                        allow_cache_match=False,
                    )
                )
                lookup_disabled = asyncio.run(
                    steam.find_steam_app(
                        "魔法少女的魔女审判",
                        cache,
                        alias_path=alias_path,
                        allow_cache_match=False,
                    )
                )

        self.assertEqual(alias_direct, {"appid": 1329410, "name": "Mahjong Soul"})
        self.assertIsNone(alias_disabled)
        self.assertIsNone(lookup_disabled)

    def test_steam_candidate_resolution_includes_cache_without_direct_match(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            alias_path = cache / "aliases.json"
            steam.write_steam_app_alias("雀魂", {"appid": 1329410, "name": "Mahjong Soul"}, alias_path)
            (cache / "steam_app_list.json").write_text(
                json.dumps({"applist": {"apps": []}}),
                encoding="utf-8",
            )

            async def no_remote(*args, **kwargs):
                return None

            async def empty_names(*args, **kwargs):
                return []

            with patch.object(steam, "suggest_steam_game_names", empty_names), patch.object(
                steam, "search_bangumi_game_candidates", empty_names
            ), patch.object(steam, "search_steam_store_app", no_remote), patch.object(
                steam, "search_wikidata_steam_app", no_remote
            ):
                candidates = asyncio.run(
                    steam.resolve_steam_app_candidates("雀魂", cache, alias_path=alias_path)
                )

        self.assertEqual(candidates, [{"appid": 1329410, "name": "Mahjong Soul", "source": "cache"}])

    def test_steam_cache_crud_helpers(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            alias_path = cache / "aliases.json"
            app = {"appid": 400, "name": "Portal"}

            steam.write_steam_app_alias("传送门", app, alias_path)
            steam.write_app_lookup_cache("portal cn", cache, app)
            steam.write_app_ambiguous_cache("portal?", cache, [app])

            self.assertEqual(steam.read_steam_app_aliases(alias_path)["传送门"], app)
            self.assertIn("portal cn", steam.read_all_app_lookup_cache(cache))
            self.assertIn("portal?", steam.read_all_app_ambiguous_cache(cache))

            self.assertTrue(steam.delete_steam_app_alias("传送门", alias_path))
            self.assertTrue(steam.delete_app_lookup_cache("portal cn", cache))
            self.assertTrue(steam.delete_app_ambiguous_cache("portal?", cache))
            self.assertFalse(steam.delete_steam_app_alias("missing", alias_path))
            self.assertFalse(steam.delete_app_lookup_cache("missing", cache))
            self.assertFalse(steam.delete_app_ambiguous_cache("missing", cache))

            self.assertEqual(steam.read_steam_app_aliases(alias_path), {})
            self.assertEqual(steam.read_all_app_lookup_cache(cache), {})
            self.assertEqual(steam.read_all_app_ambiguous_cache(cache), {})

    def test_steam_find_app_can_skip_query_cache_writes_for_ambiguous_input(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            alias_path = cache / "aliases.json"

            async def no_remote(query, proxy=None):
                return None

            with patch.object(steam, "search_wikidata_steam_app", no_remote), patch.object(
                steam, "search_steam_store_app", no_remote
            ):
                app = asyncio.run(
                    steam.find_steam_app(
                        "Portal",
                        cache,
                        alias_path=alias_path,
                        write_query_cache=False,
                    )
                )

            self.assertEqual(app, {"appid": 400, "name": "Portal"})
            self.assertEqual(steam.read_steam_app_aliases(alias_path), {})
            self.assertIsNone(steam.read_app_lookup_cache("Portal", cache))

    def test_steam_suggest_game_names_normalizes_llm_json(self):
        steam = _load_steam_module()

        self.assertEqual(
            steam._normalize_suggested_game_names(
                {
                    "names": [
                        " Terraria ",
                        "Terraria",
                        {"name": "Hollow Knight"},
                        "",
                        {"title": "ELDEN RING"},
                    ]
                }
            ),
            ["Terraria", "Hollow Knight", "ELDEN RING"],
        )
        self.assertEqual(
            steam.protect_suggested_game_names(
                "\u9b54\u6cd5\u5c11\u5973\u30ce\u9b54\u5973\u88c1\u5224", ["Magical Girl Witch Trials"]
            ),
            ["\u9b54\u6cd5\u5c11\u5973\u30ce\u9b54\u5973\u88c1\u5224", "Magical Girl Witch Trials"],
        )
        self.assertTrue(
            steam.is_distant_llm_game_name(
                "\u9b54\u6cd5\u5c11\u5973\u30ce\u9b54\u5973\u88c1\u5224", "Magical Girl Witch Trials"
            )
        )

    def test_steam_suggest_game_names_from_llm_response(self):
        steam = _load_steam_module()

        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def json(self, content_type=None):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": '{"names":["Terraria","Hollow Knight"]}'
                            }
                        }
                    ]
                }

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def post(self, *args, **kwargs):
                self.kwargs = kwargs
                return FakeResponse()

        fake_session = FakeSession()
        query = "\u6cf0\u62c9"
        async def fake_evidence(query, proxy=None):
            return "store.steampowered.com: Terraria"

        with patch.object(steam.aiohttp, "ClientSession", return_value=fake_session), patch.object(
            steam, "fetch_llm_game_name_web_evidence", fake_evidence
        ):
            result = asyncio.run(
                steam.suggest_steam_game_names(
                    query,
                    {
                        "api_key": "key",
                        "base_url": "https://llm.test",
                        "model": "m",
                    },
                )
            )

        self.assertEqual(result, [query, "Terraria", "Hollow Knight"])
        self.assertIn("Authorization", fake_session.kwargs["headers"])
        payload = fake_session.kwargs["json"]["messages"][1]["content"]
        self.assertIn("store.steampowered.com: Terraria", payload)

    def test_steam_suggest_game_names_keeps_original_non_english_first(self):
        steam = _load_steam_module()

        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def json(self, content_type=None):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": '{"names":["Magical Girl Witch Trials"]}'
                            }
                        }
                    ]
                }

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def post(self, *args, **kwargs):
                return FakeResponse()

        async def fake_evidence(query, proxy=None):
            return "google: Magical Girl Witch Trials"

        with patch.object(steam.aiohttp, "ClientSession", return_value=FakeSession()), patch.object(
            steam, "fetch_llm_game_name_web_evidence", fake_evidence
        ):
            result = asyncio.run(
                steam.suggest_steam_game_names(
                    "\u9b54\u6cd5\u5c11\u5973\u30ce\u9b54\u5973\u88c1\u5224",
                    {"api_key": "key", "base_url": "https://llm.test", "model": "m"},
                )
            )

        self.assertEqual(
            result, ["\u9b54\u6cd5\u5c11\u5973\u30ce\u9b54\u5973\u88c1\u5224", "Magical Girl Witch Trials"]
        )

    def test_steam_llm_game_name_web_evidence_parses_suggestions(self):
        steam = _load_steam_module()

        class FakeResponse:
            status = 200
            headers = {"content-type": "application/json"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def text(self):
                return json.dumps(["query", ["Terraria", "Terraria steam"]])

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            def get(self, *args, **kwargs):
                return FakeResponse()

        with patch.object(steam.aiohttp, "ClientSession", return_value=FakeSession()):
            evidence = asyncio.run(steam.fetch_llm_game_name_web_evidence("terraria"))

        self.assertIn("Terraria", evidence)
        self.assertIn("store.steampowered.com", evidence)

    def test_steam_find_app_fetches_app_list_before_remote_search(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            app = {"appid": 1030300, "name": "Hollow Knight: Silksong"}
            calls = {"app_list": 0}

            async def app_list(cache_path, proxy=None, steam_api_key=None):
                calls["app_list"] += 1
                return [app]

            async def fail_remote(query, proxy=None):
                raise AssertionError("remote search should not run")

            with patch.object(steam, "get_steam_app_list", app_list), patch.object(
                steam, "search_wikidata_steam_app", fail_remote
            ), patch.object(steam, "search_steam_store_app", fail_remote):
                result = asyncio.run(
                    steam.find_steam_app(
                        "Hollow Knight: Silksong",
                        cache,
                        alias_path=cache / "aliases.json",
                    )
                )

        self.assertEqual(result, app)
        self.assertEqual(calls["app_list"], 1)

    def test_steam_find_app_uses_app_list_for_bangumi_candidate(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            app = {"appid": 1030300, "name": "Hollow Knight: Silksong"}

            async def app_list(cache_path, proxy=None, steam_api_key=None):
                return [app]

            async def no_remote(query, proxy=None):
                return None

            async def bangumi_candidates(query, proxy=None):
                return ["Hollow Knight: Silksong"]

            with patch.object(steam, "get_steam_app_list", app_list), patch.object(
                steam, "search_wikidata_steam_app", no_remote
            ), patch.object(
                steam, "search_steam_store_app", no_remote
            ), patch.object(
                steam, "search_bangumi_game_candidates", bangumi_candidates
            ):
                result = asyncio.run(
                    steam.find_steam_app(
                        "娌℃湁鏈湴鍒悕鐨勪腑鏂囧悕",
                        cache,
                        alias_path=cache / "aliases.json",
                    )
                )

        self.assertEqual(result, app)

    def test_steam_abbreviation_uses_network_popularity_signal(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            target = {"appid": 881020, "name": "Granblue Fantasy: Relink"}
            apps = [
                {"appid": 999999, "name": "Grand Battle for Relics"},
                target,
            ]

            async def app_list(cache_path, proxy=None, steam_api_key=None):
                return apps

            async def popularity(query, proxy=None):
                return "GBFR Steam Granblue Fantasy: Relink guide and community"

            async def fail_remote(query, proxy=None):
                raise AssertionError("remote search should not run after abbreviation match")

            with patch.object(steam, "get_steam_app_list", app_list), patch.object(
                steam, "fetch_steam_abbreviation_popularity_text", popularity
            ), patch.object(
                steam, "search_wikidata_steam_app", fail_remote
            ), patch.object(
                steam, "search_steam_store_app", fail_remote
            ):
                result = asyncio.run(
                    steam.find_steam_app(
                        "gbfr", cache, alias_path=cache / "aliases.json"
                    )
                )

            aliases = json.loads((cache / "aliases.json").read_text(encoding="utf-8"))

        self.assertEqual(result, target)
        self.assertEqual(aliases["gbfr"], target)

    def test_steam_abbreviation_without_signal_is_not_persisted(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            alias_path = cache / "aliases.json"

            async def app_list(cache_path, proxy=None, steam_api_key=None):
                return [{"appid": 999999, "name": "Alpha Beta Charlie Delta"}]

            async def no_popularity(query, proxy=None):
                return ""

            async def no_remote(query, proxy=None):
                return None

            async def no_bangumi(query, proxy=None):
                return []

            with patch.object(steam, "get_steam_app_list", app_list), patch.object(
                steam, "fetch_steam_abbreviation_popularity_text", no_popularity
            ), patch.object(
                steam, "search_wikidata_steam_app", no_remote
            ), patch.object(
                steam, "search_steam_store_app", no_remote
            ), patch.object(
                steam, "search_bangumi_game_candidates", no_bangumi
            ):
                result = asyncio.run(
                    steam.find_steam_app("abcd", cache, alias_path=alias_path)
                )

        self.assertIsNone(result)
        self.assertFalse(alias_path.exists())

    def test_steam_abbreviation_can_use_llm_arbitration(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            alias_path = cache / "aliases.json"
            target = {"appid": 578080, "name": "PUBG: BATTLEGROUNDS"}
            apps = [
                {"appid": 622590, "name": "PUBG: Test Server"},
                target,
            ]

            async def app_list(cache_path, proxy=None, steam_api_key=None):
                return apps

            async def no_popularity(query, proxy=None):
                return ""

            async def llm_choice(
                query, candidates, popularity_text, llm_config, proxy=None
            ):
                self.assertEqual(query, "pubg")
                self.assertEqual(llm_config["api_key"], "key")
                self.assertEqual(popularity_text, "")
                return target

            async def fail_remote(query, proxy=None):
                raise AssertionError("remote search should not run after LLM match")

            with patch.object(steam, "get_steam_app_list", app_list), patch.object(
                steam, "fetch_steam_abbreviation_popularity_text", no_popularity
            ), patch.object(
                steam, "select_steam_abbreviation_with_llm", llm_choice
            ), patch.object(
                steam, "search_wikidata_steam_app", fail_remote
            ), patch.object(
                steam, "search_steam_store_app", fail_remote
            ):
                result = asyncio.run(
                    steam.find_steam_app(
                        "pubg",
                        cache,
                        alias_path=alias_path,
                        llm_config={"api_key": "key"},
                    )
                )

            aliases = json.loads(alias_path.read_text(encoding="utf-8"))

        self.assertEqual(result, target)
        self.assertEqual(aliases["pubg"], target)

    def test_steam_abbreviation_prefers_main_title_over_noise_apps(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            target = {"appid": 578080, "name": "PUBG: BATTLEGROUNDS"}
            apps = [
                {
                    "appid": 372112,
                    "name": "Phantom Breaker: Battle Grounds - FM sound generator BGM",
                },
                {"appid": 622590, "name": "PUBG: Test Server"},
                target,
            ]

            async def app_list(cache_path, proxy=None, steam_api_key=None):
                return apps

            async def popularity(query, proxy=None):
                return "pubg mobile pubg self service pubg pubg account"

            async def fail_remote(query, proxy=None):
                raise AssertionError(
                    "remote search should not run after abbreviation match"
                )

            with patch.object(steam, "get_steam_app_list", app_list), patch.object(
                steam, "fetch_steam_abbreviation_popularity_text", popularity
            ), patch.object(
                steam, "search_wikidata_steam_app", fail_remote
            ), patch.object(
                steam, "search_steam_store_app", fail_remote
            ):
                result = asyncio.run(
                    steam.find_steam_app(
                        "pubg", cache, alias_path=cache / "aliases.json"
                    )
                )

        self.assertEqual(result, target)

    def test_steam_find_app_uses_local_alias(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            app = asyncio.run(steam.find_steam_app("\u672a\u8f6c\u53d8\u8005", Path(tmp)))
            apex = asyncio.run(steam.find_steam_app("Apex", Path(tmp)))
            senren = asyncio.run(steam.find_steam_app("\u5343\u604b\u4e07\u82b1", Path(tmp)))
            gbfr = asyncio.run(steam.find_steam_app("gbfr", Path(tmp)))
            silksong = asyncio.run(steam.find_steam_app("\u4e1d\u4e4b\u6b4c", Path(tmp)))
            helldivers2 = asyncio.run(steam.find_steam_app("\u7edd\u5730\u6f5c\u51752", Path(tmp)))
            helldivers2_alt = asyncio.run(steam.find_steam_app("\u5730\u72f1\u6f5c\u8005", Path(tmp)))
            hd2 = asyncio.run(steam.find_steam_app("hd2", Path(tmp)))
            pubg = asyncio.run(steam.find_steam_app("pubg", Path(tmp)))
            gta5 = asyncio.run(steam.find_steam_app("gta5", Path(tmp)))
            cities = asyncio.run(steam.find_steam_app("\u5929\u9645\u7ebf", Path(tmp)))
            cities_exact = asyncio.run(
                steam.find_steam_app("Cities: Skylines", Path(tmp))
            )

        self.assertEqual(app, {"appid": 304930, "name": "Unturned"})
        self.assertEqual(apex, {"appid": 1172470, "name": "Apex Legends"})
        self.assertEqual(senren, {"appid": 1144400, "name": "Senren\uff0aBanka"})
        self.assertEqual(
            gbfr, {"appid": 881020, "name": "Granblue Fantasy: Relink"}
        )
        self.assertEqual(
            silksong, {"appid": 1030300, "name": "Hollow Knight: Silksong"}
        )
        self.assertEqual(
            helldivers2, {"appid": 553850, "name": "HELLDIVERS\u2122 2"}
        )
        self.assertEqual(
            helldivers2_alt, {"appid": 394510, "name": "HELLDIVERS\u2122"}
        )
        self.assertEqual(hd2, {"appid": 553850, "name": "HELLDIVERS\u2122 2"})
        self.assertEqual(pubg, {"appid": 578080, "name": "PUBG: BATTLEGROUNDS"})
        self.assertEqual(gta5, {"appid": 271590, "name": "Grand Theft Auto V"})
        self.assertEqual(cities, {"appid": 255710, "name": "Cities: Skylines"})
        self.assertEqual(cities_exact, {"appid": 255710, "name": "Cities: Skylines"})

    def test_steam_bangumi_candidate_extracts_aliases(self):
        steam = _load_steam_module()
        candidates = []
        steam._append_infobox_value(
            candidates,
            [{"v": "Senren\uff0aBanka"}, {"v": "\u5343\u604b\u4e07\u82b1"}],
        )
        steam._append_unique(candidates, "Senren\uff0aBanka")

        self.assertEqual(candidates, ["Senren\uff0aBanka", "\u5343\u604b\u4e07\u82b1"])

    def test_steam_bangumi_candidate_ignores_tags(self):
        steam = _load_steam_module()
        candidates = []
        steam._append_bangumi_subject_candidates(
            candidates,
            {
                "name": "Hollow Knight: Silksong",
                "name_cn": "\u7a7a\u6d1e\u9a91\u58eb\uff1a\u4e1d\u4e4b\u6b4c",
                "tags": [{"name": "\u7c7b\u94f6\u6cb3\u6076\u9b54\u57ce"}],
                "infobox": [
                    {"key": "\u522b\u540d", "value": [{"v": "Silksong"}]},
                    {"key": "\u5f00\u53d1", "value": [{"v": "NPCs"}]},
                ],
            },
        )

        self.assertEqual(
            candidates,
            ["Hollow Knight: Silksong", "\u7a7a\u6d1e\u9a91\u58eb\uff1a\u4e1d\u4e4b\u6b4c", "Silksong"],
        )

    def test_steam_app_lookup_cache(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            app = {"appid": 1144400, "name": "Senren锛夿anka"}
            steam.write_app_lookup_cache("鍗冩亱涓囪姳", cache, app)

            self.assertEqual(steam.read_app_lookup_cache("鍗冩亱涓囪姳", cache), app)

    def test_steam_app_lookup_writes_resource_alias(self):
        steam = _load_steam_module()
        with TemporaryDirectory() as tmp:
            cache = Path(tmp)
            alias_path = cache / "steam_app_aliases.json"
            app = {"appid": 123456, "name": "Remote Game"}

            async def found_on_wikidata(query, proxy=None):
                return app

            async def no_store_search(query, proxy=None):
                return None

            async def no_app_list(cache_path, proxy=None, steam_api_key=None):
                return []

            with patch.object(
                steam, "search_wikidata_steam_app", found_on_wikidata
            ), patch.object(
                steam, "search_steam_store_app", no_store_search
            ), patch.object(
                steam, "get_steam_app_list", no_app_list
            ):
                result = asyncio.run(
                    steam.find_steam_app("杩滅▼娓告垙", cache, alias_path=alias_path)
                )

            self.assertEqual(result, app)
            aliases = json.loads(alias_path.read_text(encoding="utf-8"))
            self.assertEqual(aliases["杩滅▼娓告垙"], app)
            self.assertEqual(
                asyncio.run(
                    steam.find_steam_app("杩滅▼娓告垙", cache, alias_path=alias_path)
                ),
                app,
            )


if __name__ == "__main__":
    unittest.main()
