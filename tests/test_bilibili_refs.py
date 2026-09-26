from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
import pytest

from tests.test_core_logic import _bili_root_package, _load_bili_new_module


UID = "12345"  # doubles as a live room number: the collision this module guards against
ROOM_OF_UID = "555"
OTHER_UID = "999"


@pytest.fixture
def bili():
    service = _load_bili_new_module("service")
    package = _bili_root_package(service)
    return SimpleNamespace(
        service=service,
        client=sys.modules[package + ".api"],
        store=sys.modules[package + ".store"],
        models=sys.modules[package + ".models"],
        refs=sys.modules[package + ".refs"],
    )


class FakeBackend:
    """Minimal Bilibili stand-in where UID and room numbers collide."""

    def __init__(self, *, rooms_of_uid=None, owners_of_room=None):
        self.rooms_of_uid = dict(rooms_of_uid or {})
        self.owners_of_room = dict(owners_of_room or {})
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        self.requests.append((path, params))
        if path == "/x/web-interface/nav":
            return _json(
                {
                    "code": 0,
                    "data": {
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png",
                        }
                    },
                }
            )
        if path == "/room/v1/Room/get_info":
            room = str(params.get("room_id") or "")
            owner = self.owners_of_room.get(room)
            if owner is None:
                return _json({"code": 1, "message": "room not found"})
            return _json(
                {
                    "code": 0,
                    "data": {
                        "uid": int(owner),
                        "room_id": int(room),
                        "live_status": 0,
                        "title": f"room {room}",
                        "uname": f"主播{owner}",
                    },
                }
            )
        if path == "/live_user/v1/Master/info":
            uid = str(params.get("uid") or "")
            return _json(
                {
                    "code": 0,
                    "data": {
                        "room_id": int(self.rooms_of_uid.get(uid, 0)),
                        "info": {"uname": f"主播{uid}", "face": ""},
                    },
                }
            )
        if path == "/x/space/wbi/acc/info":
            uid = str(params.get("mid") or "")
            return _json({"code": 0, "data": {"name": f"主播{uid}", "face": ""}})
        if path == "/x/space/wbi/arc/search":
            uid = str(params.get("mid") or "")
            return _json(
                {
                    "code": 0,
                    "data": {
                        "list": {
                            "vlist": [
                                {
                                    "bvid": "BV1xx411c7mD",
                                    "title": f"视频{uid}",
                                    "created": 100,
                                }
                            ]
                        }
                    },
                }
            )
        if path == "/x/web-interface/view":
            return _json(
                {
                    "code": 0,
                    "data": {
                        "title": "视频",
                        "desc": "",
                        "pic": "",
                        "pubdate": 100,
                        "owner": {"mid": 1, "name": "UP", "face": ""},
                    },
                }
            )
        if path == "/x/polymer/web-dynamic/v1/feed/space":
            return _json({"code": 0, "data": {"items": []}})
        return _json({"code": -404, "message": f"unmocked {path}"})


def _json(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


def collision_backend(**kwargs) -> FakeBackend:
    return FakeBackend(
        rooms_of_uid={
            UID: ROOM_OF_UID,
            OTHER_UID: UID,
            **kwargs.pop("rooms_of_uid", {}),
        },
        owners_of_room={
            UID: OTHER_UID,
            ROOM_OF_UID: UID,
            **kwargs.pop("owners_of_room", {}),
        },
        **kwargs,
    )


def make_store(bili, tmp_path):
    return bili.store.BiliStore(tmp_path / "bilibili.db", tmp_path / "missing.db")


def make_service(bili, backend, store):
    client = bili.client.BiliApi(transport=httpx.MockTransport(backend))
    return bili.service.BiliService(store, client)


async def subscriptions(store, kind):
    return [
        sub
        for sub, _target in await store.subscriptions_for_subscriber(
            "group", "900", kind
        )
    ]


def run(coro):
    """Drive one coroutine on a fresh loop (this repo has no pytest-asyncio)."""
    return asyncio.run(coro)


def open_store(store):
    run(store.open())
    return store


def close_store(store):
    run(store.close())


# --- refs.parse_target_ref -------------------------------------------------


@pytest.mark.parametrize(
    "raw,by,value",
    [
        ("114514", "uid", "114514"),
        ("uid:114514", "uid", "114514"),
        ("UID:114514", "uid", "114514"),
        ("https://space.bilibili.com/114514", "uid", "114514"),
        ("https://space.bilibili.com/114514/article", "uid", "114514"),
        ("https://space.bilibili.com/114514?tab=video", "uid", "114514"),
        (" 114514 ", "uid", "114514"),
        ("room:5302860", "room", "5302860"),
        ("ROOM:5302860", "room", "5302860"),
        ("https://live.bilibili.com/5302860", "room", "5302860"),
        ("https://live.bilibili.com/blanc/5302860?broadcast_type=0", "room", "5302860"),
    ],
)
def test_parse_target_ref_resolves_supported_forms(bili, raw, by, value):
    ref = bili.refs.parse_target_ref(raw)
    assert (ref.by, ref.value) == (by, value)
    assert ref.raw == raw


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "abc",
        "uid:abc",
        "room:",
        "b23.tv/abcd",
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://space.bilibili.com/",
    ],
)
def test_parse_target_ref_rejects_unknown_forms(bili, raw):
    with pytest.raises(ValueError, match="无法识别"):
        bili.refs.parse_target_ref(raw)


def test_parse_target_ref_error_names_the_accepted_forms(bili):
    with pytest.raises(ValueError) as excinfo:
        bili.refs.parse_target_ref("nope")
    assert (
        str(excinfo.value) == '无法识别 "nope"，请使用 UID、room:直播间号 或直播间链接'
    )


# --- follow -----------------------------------------------------------------


def test_follow_all_with_plain_number_subscribes_every_kind_to_the_uid(bili, tmp_path):
    backend = collision_backend()
    with TemporaryDirectory() as tmp:
        store = make_store(bili, Path(tmp))
        service = make_service(bili, backend, store)
        try:

            async def scenario():
                await store.open()
                ok, failed = await service.follow("all", [UID], "group", "900")
                by_kind = {
                    kind: await subscriptions(store, kind)
                    for kind in ("live", "video", "dynamic")
                }
                live = await store.get_target("live", UID)
                other = await store.get_target("live", OTHER_UID)
                return ok, failed, by_kind, live, other

            ok, failed, by_kind, live, other = run(scenario())
            assert failed == []
            assert len(ok) == 3
            for kind in ("live", "video", "dynamic"):
                assert [sub.target_uid for sub in by_kind[kind]] == [UID]
            assert live.room_id == ROOM_OF_UID
            assert other is None
            assert all(f"UID {UID}" in line for line in ok)
            assert any(f"直播间 {ROOM_OF_UID}" in line for line in ok)
        finally:
            close_store(store)


def test_follow_live_with_plain_number_treats_it_as_a_uid(bili, tmp_path):
    backend = collision_backend()
    with TemporaryDirectory() as tmp:
        store = make_store(bili, Path(tmp))
        service = make_service(bili, backend, store)
        try:

            async def scenario():
                await store.open()
                ok, failed = await service.follow("live", [UID], "group", "900")
                return (
                    ok,
                    failed,
                    await subscriptions(store, "live"),
                    await store.get_target("live", OTHER_UID),
                )

            ok, failed, subs, other = run(scenario())
            assert failed == []
            assert [sub.target_uid for sub in subs] == [UID]
            assert other is None
            assert f"UID {UID}" in ok[0]
        finally:
            close_store(store)


def test_follow_live_room_prefix_uses_the_room_owner(bili, tmp_path):
    backend = collision_backend()
    with TemporaryDirectory() as tmp:
        store = make_store(bili, Path(tmp))
        service = make_service(bili, backend, store)
        try:

            async def scenario():
                await store.open()
                ok, failed = await service.follow(
                    "live", [f"room:{UID}"], "group", "900"
                )
                return (
                    ok,
                    failed,
                    await subscriptions(store, "live"),
                    await store.get_target("live", OTHER_UID),
                )

            ok, failed, subs, owner = run(scenario())
            assert failed == []
            assert [sub.target_uid for sub in subs] == [OTHER_UID]
            assert owner.room_id == UID
            assert (
                ok[0]
                == f"直播 主播{OTHER_UID}（UID {OTHER_UID}，直播间 {UID}，按直播间号解析）已订阅"
            )
        finally:
            close_store(store)


def test_follow_all_room_prefix_subscribes_every_kind_to_the_room_owner(bili, tmp_path):
    backend = collision_backend()
    with TemporaryDirectory() as tmp:
        store = make_store(bili, Path(tmp))
        service = make_service(bili, backend, store)
        try:

            async def scenario():
                await store.open()
                ok, failed = await service.follow(
                    "all", [f"room:{UID}"], "group", "900"
                )
                by_kind = {
                    kind: await subscriptions(store, kind)
                    for kind in ("live", "video", "dynamic")
                }
                return ok, failed, by_kind

            ok, failed, by_kind = run(scenario())
            assert failed == []
            assert len(ok) == 3
            for kind in ("live", "video", "dynamic"):
                assert [sub.target_uid for sub in by_kind[kind]] == [OTHER_UID]
        finally:
            close_store(store)


def test_follow_video_rejects_a_room_reference(bili, tmp_path):
    backend = collision_backend()
    with TemporaryDirectory() as tmp:
        store = make_store(bili, Path(tmp))
        service = make_service(bili, backend, store)
        try:

            async def scenario():
                await store.open()
                ok, failed = await service.follow(
                    "video", [f"room:{UID}"], "group", "900"
                )
                return ok, failed, await subscriptions(store, "video")

            ok, failed, subs = run(scenario())
            assert ok == []
            assert failed == [f"直播间号只能用于 live 或 all: room:{UID}"]
            assert subs == []
        finally:
            close_store(store)


def test_follow_all_without_a_live_room_keeps_video_and_dynamic(bili, tmp_path):
    backend = FakeBackend(rooms_of_uid={}, owners_of_room={})
    with TemporaryDirectory() as tmp:
        store = make_store(bili, Path(tmp))
        service = make_service(bili, backend, store)
        try:

            async def scenario():
                await store.open()
                ok, failed = await service.follow("all", [UID], "group", "900")
                return (
                    ok,
                    failed,
                    {
                        kind: await subscriptions(store, kind)
                        for kind in ("live", "video", "dynamic")
                    },
                )

            ok, failed, by_kind = run(scenario())
            assert len(ok) == 2
            assert len(failed) == 1
            assert "没有直播间" in failed[0]
            assert by_kind["live"] == []
            assert [sub.target_uid for sub in by_kind["video"]] == [UID]
            assert [sub.target_uid for sub in by_kind["dynamic"]] == [UID]
        finally:
            close_store(store)


def test_follow_refuses_a_resolution_that_returns_a_different_uid(bili, tmp_path):
    class WrongClient:
        async def resolve_video_target(self, uid):
            return bili.models.TargetInfo("video", OTHER_UID, name="别人")

    with TemporaryDirectory() as tmp:
        store = make_store(bili, Path(tmp))
        service = bili.service.BiliService(store, WrongClient())
        try:

            async def scenario():
                await store.open()
                ok, failed = await service.follow("video", [UID], "group", "900")
                return (
                    ok,
                    failed,
                    await subscriptions(store, "video"),
                    await store.get_target("video", OTHER_UID),
                )

            ok, failed, subs, other = run(scenario())
            assert ok == []
            assert len(failed) == 1
            assert subs == []
            assert other is None
        finally:
            close_store(store)


def test_follow_reports_unparsable_input_without_subscribing(bili, tmp_path):
    backend = collision_backend()
    with TemporaryDirectory() as tmp:
        store = make_store(bili, Path(tmp))
        service = make_service(bili, backend, store)
        try:

            async def scenario():
                await store.open()
                ok, failed = await service.follow("all", ["nope"], "group", "900")
                return ok, failed, await subscriptions(store, "live")

            ok, failed, subs = run(scenario())
            assert ok == []
            assert failed == ['无法识别 "nope"，请使用 UID、room:直播间号 或直播间链接']
            assert subs == []
        finally:
            close_store(store)


# --- unfollow / refresh -----------------------------------------------------


def test_unfollow_live_room_reference_removes_the_room_owner(bili, tmp_path):
    backend = collision_backend()
    with TemporaryDirectory() as tmp:
        store = make_store(bili, Path(tmp))
        service = make_service(bili, backend, store)
        try:

            async def scenario():
                await store.open()
                await store.upsert_target(
                    bili.models.TargetInfo("live", OTHER_UID, name="主播", room_id=UID)
                )
                await store.add_subscription("live", OTHER_UID, "group", "900")
                ok, failed = await service.unfollow(
                    "live", [f"room:{UID}"], "group", "900"
                )
                return ok, failed, await subscriptions(store, "live")

            ok, failed, subs = run(scenario())
            assert failed == []
            assert subs == []
            assert f"UID {OTHER_UID}" in ok[0]
        finally:
            close_store(store)


def test_unfollow_live_with_plain_number_removes_the_uid(bili, tmp_path):
    backend = collision_backend()
    with TemporaryDirectory() as tmp:
        store = make_store(bili, Path(tmp))
        service = make_service(bili, backend, store)
        try:

            async def scenario():
                await store.open()
                await store.upsert_target(
                    bili.models.TargetInfo(
                        "live", UID, name="主播", room_id=ROOM_OF_UID
                    )
                )
                await store.add_subscription("live", UID, "group", "900")
                ok, failed = await service.unfollow("live", [UID], "group", "900")
                return ok, failed, await subscriptions(store, "live")

            ok, failed, subs = run(scenario())
            assert failed == []
            assert subs == []
        finally:
            close_store(store)


def test_refresh_live_with_plain_number_never_touches_the_room_owner(bili, tmp_path):
    backend = collision_backend()
    with TemporaryDirectory() as tmp:
        store = make_store(bili, Path(tmp))
        service = make_service(bili, backend, store)
        try:

            async def scenario():
                await store.open()
                ok, failed = await service.refresh("live", [UID])
                return (
                    ok,
                    failed,
                    await store.get_target("live", UID),
                    await store.get_target("live", OTHER_UID),
                )

            ok, failed, uid_target, other = run(scenario())
            assert failed == []
            assert uid_target.room_id == ROOM_OF_UID
            assert other is None
        finally:
            close_store(store)


# --- link preview -----------------------------------------------------------


def test_live_link_preview_uses_the_room_owner(bili):
    backend = collision_backend()
    client = bili.client.BiliApi(transport=httpx.MockTransport(backend))
    parsed = asyncio.run(client.parse_link(f"https://live.bilibili.com/{UID}"))
    assert (parsed.kind, parsed.value) == ("live", UID)
    card = asyncio.run(client.card_for_link(parsed))
    assert card.uid == OTHER_UID
    assert card.room_id == UID


def test_live_link_preview_never_resolves_the_room_number_as_a_uid(bili):
    backend = collision_backend()
    client = bili.client.BiliApi(transport=httpx.MockTransport(backend))
    asyncio.run(client.card_for_link(bili.client.ParsedLink("live", UID, "")))
    paths = [path for path, _ in backend.requests]
    assert "/live_user/v1/Master/info" in paths
    assert backend.requests[0] == ("/room/v1/Room/get_info", {"room_id": UID})
