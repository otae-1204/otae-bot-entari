from __future__ import annotations

import asyncio
import sys
import time

import httpx
import pytest

from tests.test_core_logic import (
    _bili_root_package,
    _load_bili_new_module,
    _load_module,
)


def _load_in_package(package: str, name: str):
    """Load one more module inside the synthetic package the loader created."""
    key = f"{package}.{name}"
    if key in sys.modules:
        return sys.modules[key]
    return _load_module(key, f"plugins/bilibilibot/{name}.py")


@pytest.fixture(scope="module")
def api():
    api_module = _load_bili_new_module("api")
    session = sys.modules[api_module.__name__ + ".session"]
    return session


@pytest.fixture(scope="module")
def bili():
    from types import SimpleNamespace

    api_module = _load_bili_new_module("api")
    package = _bili_root_package(api_module)
    return SimpleNamespace(
        client=api_module,
        poller=_load_in_package(package, "poller"),
        store=sys.modules[package + ".store"],
        models=sys.modules[package + ".models"],
    )


NAV = {
    "code": 0,
    "data": {
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
        }
    },
}


def test_concurrent_risk_control_hits_share_one_refresh(api):
    """Ten requests hit -352 together; exactly one refresh must happen."""
    refreshes = 0
    signed_after_refresh = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refreshes
        if request.url.host == "www.bilibili.com":
            return httpx.Response(
                200, text='<meta name="spm_prefix" content="333.999">'
            )
        if request.url.path.endswith("/ExClimbWuzhi"):
            return httpx.Response(200, json={"code": 0})
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json=NAV)
        if request.url.path == "/probe":
            if "buvid3=fresh" not in request.headers.get("cookie", ""):
                return httpx.Response(200, json={"code": -352, "message": "risk"})
            signed_after_refresh.append(dict(request.url.params))
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(404, json={"code": -404})

    session = api.BiliSession(transport=httpx.MockTransport(handler), min_interval=0)

    original = session.refresh_risk_cookies

    async def counting_refresh():
        nonlocal refreshes
        refreshes += 1
        await original()
        session.cookies.set("buvid3", "fresh", domain=".bilibili.com")

    session.refresh_risk_cookies = counting_refresh

    async def run():
        try:
            return await asyncio.gather(
                *(
                    session.get_json(
                        "https://api.bilibili.com/probe", label=f"probe{i}"
                    )
                    for i in range(10)
                )
            )
        finally:
            await session.aclose()

    results = asyncio.run(run())
    assert len(results) == 10
    assert refreshes == 1
    assert len(signed_after_refresh) == 10


def test_risk_retry_reports_risk_control_when_it_persists(api):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.bilibili.com":
            return httpx.Response(200, text="")
        if request.url.path.endswith("/ExClimbWuzhi"):
            return httpx.Response(200, json={"code": 0})
        if request.url.path == "/probe":
            return httpx.Response(200, json={"code": -352, "message": "risk"})
        return httpx.Response(404)

    session = api.BiliSession(transport=httpx.MockTransport(handler), min_interval=0)

    async def run():
        try:
            await session.get_json("https://api.bilibili.com/probe", label="probe")
        finally:
            await session.aclose()

    with pytest.raises(api.BiliRiskControlError) as excinfo:
        asyncio.run(run())
    assert "BILI_SESSDATA/BILI_BUVID3" in str(excinfo.value)


def test_concurrent_wbi_rotation_happens_once(api):
    rotations = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal rotations
        if request.url.path == "/x/web-interface/nav":
            rotations += 1
            return httpx.Response(200, json=NAV)
        return httpx.Response(200, json={"code": 0})

    session = api.BiliSession(transport=httpx.MockTransport(handler), min_interval=0)

    async def run():
        try:
            await asyncio.gather(*(session.ensure_wbi_keys() for _ in range(10)))
        finally:
            await session.aclose()

    asyncio.run(run())
    assert rotations == 1


def test_session_rate_limits_bilibili_hosts_but_not_rsshub(api):
    stamps: list[tuple[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        stamps.append((request.url.host, time.monotonic()))
        return httpx.Response(200, json={"code": 0})

    session = api.BiliSession(transport=httpx.MockTransport(handler), min_interval=0.05)

    async def run():
        try:
            await session.fetch_json("https://api.bilibili.com/a")
            await session.fetch_json("https://api.bilibili.com/b")
            await session.fetch_json("https://rss.example/a")
            await session.fetch_json("https://rss.example/b")
        finally:
            await session.aclose()

    asyncio.run(run())
    api_gap = stamps[1][1] - stamps[0][1]
    rss_gap = stamps[3][1] - stamps[2][1]
    assert api_gap >= 0.045
    assert rss_gap < 0.02


def test_session_reuses_one_http_client_per_loop(api):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0})

    session = api.BiliSession(transport=httpx.MockTransport(handler), min_interval=0)

    async def run():
        try:
            await session.fetch_json("https://api.bilibili.com/a")
            first = session._http_client
            await session.fetch_json("https://api.bilibili.com/b")
            return first is session._http_client
        finally:
            await session.aclose()

    assert asyncio.run(run())
    assert asyncio.run(run())


def test_fallback_chain_stops_at_the_deadline(bili):
    """A slow fallback chain must not outlive the target's budget."""
    client_module = bili.client
    client = client_module.BiliApi()
    client.img_key = "a" * 32
    client.sub_key = "b" * 32
    client.session.img_key = "a" * 32
    client.session.sub_key = "b" * 32
    client.session._wbi_updated_at = 9999999999
    client.session._risk_cookie_updated_at = 9999999999
    client.session.cookies.set("buvid3", "x", domain=".bilibili.com")

    async def slow_primary(*args, **kwargs):
        raise client_module.BiliAPIError("primary down")

    async def slow_rss(*args, **kwargs):
        await asyncio.sleep(5)
        raise client_module.BiliAPIError("rss slow")

    async def slow_dynamic(*args, **kwargs):
        await asyncio.sleep(5)
        raise client_module.BiliAPIError("dynamic slow")

    client._get_json_with_risk_retry = slow_primary
    client._rsshub_latest_video = slow_rss
    client._dynamic_latest_video = slow_dynamic

    async def run():
        deadline = asyncio.get_running_loop().time() + 0.15
        started = asyncio.get_running_loop().time()
        with pytest.raises(client_module.BiliAPIError) as excinfo:
            await client.latest_video("135116630", deadline=deadline)
        return asyncio.get_running_loop().time() - started, str(excinfo.value)

    elapsed, message = asyncio.run(run())
    assert elapsed < 1.0
    assert "video sources failed" in message
    assert "primary=" in message and "video_rss=" in message and "dynamic=" in message


def test_poller_bounds_each_target_with_the_configured_timeout(bili, tmp_path):
    """A hanging target must be cut off by the poller's per-target budget."""
    models = bili.models
    poller_module = bili.poller
    seen: list[float] = []

    class Client:
        async def latest_video(self, uid, *, deadline=None):
            seen.append(deadline)
            await asyncio.sleep(10)
            return models.BiliCard("video", "x")

    async def run():
        store = bili.store.BiliStore(tmp_path / "bilibili.db", tmp_path / "missing.db")
        await store.open()
        try:
            await store.upsert_target(models.TargetInfo("video", "1", name="UP"))
            await store.add_subscription("video", "1", "group", "900")
            poller = poller_module.Poller(
                Client(), store, target_timeout=0.2, intervals={"video": 0}
            )
            started = asyncio.get_running_loop().time()
            await poller.tick_video()
            return (
                asyncio.get_running_loop().time() - started,
                await store.outbox_count(),
                await store.get_target("video", "1"),
            )
        finally:
            await store.close()

    elapsed, outbox, target = asyncio.run(run())
    assert elapsed < 2.0
    # The deadline handed to the API is the loop clock plus the configured budget.
    assert seen and seen[0] > 0
    # The hanging call was cancelled, so no event and no state change were committed.
    assert outbox == 0
    assert target.latest_id == ""
