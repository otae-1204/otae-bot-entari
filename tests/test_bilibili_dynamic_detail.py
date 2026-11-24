"""Feed shells are filled from the detail endpoint by the poller, not the feed.

The raw `dynamic_items` stays untouched so the subscription baseline always sees
every item; enrichment runs on exactly the window the poller is about to detect,
and any transient miss defers the whole window instead of advancing the cursor
past a shell that was never retried.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from loguru import logger

from plugins.bilibilibot.official import ENDFIELD_OFFICIAL_UID
from tests.support.bilibili import asyncio_test, open_store
from tests.support.loaders import _load_bili_new_module


@pytest.fixture(scope="module")
def bili():
    return SimpleNamespace(api=_load_bili_new_module("api"))


@pytest.fixture
def poller_env():
    """A fresh poller package so its store/models match the poller module."""
    poller = _load_bili_new_module("poller")
    package = poller.__package__
    api_pkg = sys.modules[package + ".api"]
    return SimpleNamespace(
        poller=poller,
        store=sys.modules[package + ".store"],
        models=sys.modules[package + ".models"],
        # The poller's own api package: exceptions raised here are the exact
        # classes the poller catches (the module-scope `bili` fixture loads a
        # second synthetic copy with distinct class objects).
        space=api_pkg.space,
    )


def _empty_draw(item_id: str, pub_ts: int = 100) -> dict:
    return {
        "id_str": item_id,
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"pub_ts": pub_ts, "name": "UP"},
            "module_dynamic": {
                "desc": None,
                "major": {"type": "MAJOR_TYPE_DRAW", "draw": {"items": []}},
            },
        },
    }


def _opus_detail(item_id: str, text: str, pub_ts: int = 100) -> dict:
    return {
        "id_str": item_id,
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"pub_ts": pub_ts, "name": "UP"},
            "module_dynamic": {
                "desc": None,
                "major": {
                    "type": "MAJOR_TYPE_OPUS",
                    "opus": {"summary": {"text": text}, "pics": []},
                },
            },
        },
    }


def _official_detail(item_id: str, text: str = "官号正文", pub_ts: int = 100) -> dict:
    item = _opus_detail(item_id, text, pub_ts=pub_ts)
    item["modules"]["module_author"]["mid"] = ENDFIELD_OFFICIAL_UID
    item["modules"]["module_dynamic"]["desc"] = {"text": text}
    return item


def _text_item(item_id: str, text: str, pub_ts: int = 100) -> dict:
    return {
        "id_str": item_id,
        "type": "DYNAMIC_TYPE_WORD",
        "modules": {
            "module_author": {"pub_ts": pub_ts, "name": "UP"},
            "module_dynamic": {"desc": {"text": text}},
        },
    }


def _video_dynamic(item_id: str, pub_ts: int = 100) -> dict:
    return {
        "id_str": item_id,
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"pub_ts": pub_ts, "name": "UP"},
            "module_dynamic": {
                "desc": None,
                "major": {
                    "type": "MAJOR_TYPE_ARCHIVE",
                    "archive": {
                        "title": "视频动态",
                        "cover": "https://example.com/c.jpg",
                        "jump_url": "https://www.bilibili.com/video/BV1xx411c7mD",
                    },
                },
            },
        },
    }


def _live_rcmd(item_id: str, pub_ts: int = 100, *, type_only: bool = False) -> dict:
    major = {} if type_only else {"type": "MAJOR_TYPE_LIVE_RCMD"}
    return {
        "id_str": item_id,
        "type": "DYNAMIC_TYPE_LIVE_RCMD",
        "modules": {
            "module_author": {"pub_ts": pub_ts, "name": "UP"},
            "module_dynamic": {"desc": None, "major": major},
        },
    }


def _typed_shell(
    item_id: str,
    *,
    item_type: str = "DYNAMIC_TYPE_DRAW",
    major_type: str | None = None,
    additional_type: str | None = None,
    pub_ts: int = 100,
) -> dict:
    """An empty item of a specific dynamic type, used for skip-list tests."""
    return {
        "id_str": item_id,
        "type": item_type,
        "modules": {
            "module_author": {"pub_ts": pub_ts, "name": "UP"},
            "module_dynamic": {
                "desc": None,
                "major": {"type": major_type} if major_type else None,
                "additional": {"type": additional_type} if additional_type else None,
            },
        },
    }


def _forward(item_id: str, orig, pub_ts: int = 100) -> dict:
    return {
        "id_str": item_id,
        "type": "DYNAMIC_TYPE_FORWARD",
        "orig": orig,
        "modules": {
            "module_author": {"pub_ts": pub_ts, "name": "UP"},
            "module_dynamic": {"desc": {"text": "转发语"}, "major": None},
        },
    }


class _FakeApi:
    """Canned feed/detail payloads behind the real space helpers."""

    def __init__(
        self,
        space,
        feed: list[dict],
        details: dict[str, dict],
        transient=(),
        *,
        slow_seconds: float = 0.0,
        clock=None,
    ):
        self.space = space
        self.feed = feed
        self.details = details
        self.transient = set(transient)
        # A fresh detail lookup spends ``slow_seconds`` of the fake budget; a
        # cached one is instant (BiliApi keeps successes 300 s, misses 1800 s).
        self.slow_seconds = slow_seconds
        self.clock = clock
        self.requested_once: set[str] = set()
        self.cached_details: set[str] = set()
        self.cached_misses: set[str] = set()
        self.detail_calls: list[str] = []
        self.enrich_calls: list[tuple[int, int, int]] = []

    async def _within(self, label, deadline, factory):
        return await factory()

    async def _get_json_with_risk_retry(self, url, label, params=None, sign_params=None):
        if url == self.space.DYNAMIC_URL:
            return {"data": {"items": self.feed}}
        if url == self.space.DYNAMIC_DETAIL_URL:
            item_id = str((params or {}).get("id") or "")
            self.detail_calls.append(item_id)
            if item_id in self.transient:
                # Same class as a transport timeout: retryable, not a miss.
                raise self.space.BiliAPIError(f"Bilibili request timeout: {item_id}")
            if item_id not in self.requested_once:
                if self.clock is not None:
                    self.clock.advance(self.slow_seconds)
                self.requested_once.add(item_id)
            return {"data": {"item": self.details.get(item_id, {})}}
        raise AssertionError(url)

    async def dynamic_items(self, uid, *, deadline=None):
        return await self.space.dynamic_items(self, uid, deadline=deadline)

    async def dynamic_detail(self, item_id: str, *, deadline=None):
        if item_id in self.cached_misses:
            raise self.space.DynamicDetailUnavailable(f"动态 {item_id} 不可见")
        if item_id in self.cached_details:
            return self.details[item_id]
        try:
            detail = await self.space.dynamic_detail(self, item_id, deadline=deadline)
        except self.space.DynamicDetailUnavailable:
            self.cached_misses.add(item_id)
            raise
        self.cached_details.add(item_id)
        return detail

    def has_cached_dynamic_detail(self, item_id: str) -> bool:
        return item_id in self.cached_details or item_id in self.cached_misses

    async def _enrich_video_card(self, card, *, deadline=None):
        return await self.space.enrich_video_card(self, card, deadline=deadline)

    async def enrich_empty_dynamic_items(
        self,
        items,
        *,
        deadline=None,
        after_ts=0,
        limit=None,
        release_ids=None,
        skip_ids=None,
    ):
        if limit is None:
            limit = self.space.DYNAMIC_DETAIL_LIMIT
        self.enrich_calls.append((after_ts, limit, len(items)))
        return await self.space.enrich_empty_dynamic_items(
            self,
            items,
            deadline=deadline,
            after_ts=after_ts,
            limit=limit,
            release_ids=release_ids,
            skip_ids=skip_ids,
        )


def _fake(bili, feed, details, transient=(), *, slow_seconds=0.0, clock=None):
    return _FakeApi(
        bili.api.space,
        feed,
        details,
        transient,
        slow_seconds=slow_seconds,
        clock=clock,
    )


def _fake_in(space, feed, details, transient=(), *, slow_seconds=0.0, clock=None):
    """Fake api bound to one loaded space module (same exception classes)."""
    return _FakeApi(
        space, feed, details, transient, slow_seconds=slow_seconds, clock=clock
    )


class _FakeClock:
    """A `loop.time()` stand-in that only slow detail lookups advance."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _AsyncioProxy:
    """Replace `space.asyncio` with a fake loop clock; everything else is real."""

    def __init__(self, loop):
        self._loop = loop

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def get_running_loop(self):
        return self._loop


def _stub_transport() -> httpx.MockTransport:
    # Never reached: every test replaces the request step below it.
    return httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0}))


# --- content / eligibility --------------------------------------------------


def test_content_check_distinguishes_empty_draw_from_opus_text(bili):
    assert not bili.api.space._dynamic_has_content(_empty_draw("1"))
    assert bili.api.space._dynamic_has_content(_opus_detail("1", "正文"))
    assert bili.api.space._dynamic_has_content(_text_item("1", "正文"))


def test_content_check_accepts_ugc_when_mapping_renders_it(bili):
    item = {
        "id_str": "1",
        "type": "DYNAMIC_TYPE_DRAW",
        "modules": {
            "module_author": {"pub_ts": 1},
            "module_dynamic": {
                "desc": None,
                "major": None,
                "additional": {
                    "type": "ADDITIONAL_TYPE_UGC",
                    "ugc": {"title": "视频标题", "cover": "https://example.com/c.jpg"},
                },
            },
        },
    }
    assert bili.api.space._dynamic_has_content(item)


def test_ugc_additional_does_not_mask_an_empty_draw(bili):
    item = _empty_draw("1")
    item["modules"]["module_dynamic"]["additional"] = {
        "type": "ADDITIONAL_TYPE_UGC",
        "ugc": {"title": "视频标题", "cover": "https://example.com/c.jpg"},
    }
    assert not bili.api.space._dynamic_has_content(item)


def test_live_rcmd_shells_do_not_need_detail(bili):
    assert not bili.api.space._dynamic_needs_detail(_live_rcmd("1"))
    # The type alone is enough; some payloads omit the major block.
    assert not bili.api.space._dynamic_needs_detail(_live_rcmd("2", type_only=True))


def test_deleted_or_empty_forward_shells_do_not_need_detail(bili):
    assert not bili.api.space._dynamic_needs_detail(_forward("1", None))
    assert not bili.api.space._dynamic_needs_detail(_forward("2", {}))
    assert not bili.api.space._dynamic_needs_detail(_forward("3", _empty_draw("orig")))


def test_plain_empty_draw_and_word_still_need_detail(bili):
    assert bili.api.space._dynamic_needs_detail(_empty_draw("1"))
    assert bili.api.space._dynamic_needs_detail(
        _typed_shell("2", item_type="DYNAMIC_TYPE_WORD")
    )


@asyncio_test
async def test_unrenderable_types_make_zero_detail_calls(bili):
    # Detail cannot fill these, so they must not consume the request budget.
    feed = [
        _typed_shell("live-dynamic", item_type="DYNAMIC_TYPE_LIVE"),
        _typed_shell("live-major", major_type="MAJOR_TYPE_LIVE"),
        _typed_shell("live-additional", additional_type="ADDITIONAL_TYPE_LIVE"),
        _typed_shell("common", major_type="MAJOR_TYPE_COMMON"),
        _typed_shell("music", major_type="MAJOR_TYPE_MUSIC"),
        _typed_shell("pgc", major_type="MAJOR_TYPE_PGC"),
    ]
    api = _fake(bili, feed, {})

    await bili.api.space.enrich_empty_dynamic_items(api, feed)

    assert api.detail_calls == []


# --- the raw feed stays raw -------------------------------------------------


@asyncio_test
async def test_dynamic_items_returns_the_raw_page_without_detail_calls(bili):
    feed = [_empty_draw("newest", pub_ts=200), _empty_draw("old", pub_ts=100)]
    api = _fake(bili, feed, {"newest": _opus_detail("newest", "新正文")})

    items = await bili.api.space.dynamic_items(api, "341368")

    assert items == feed
    assert api.detail_calls == []
    assert not bili.api.space._dynamic_has_content(items[0])


@asyncio_test
async def test_latest_dynamic_baseline_makes_no_detail_calls_and_keeps_the_shell(bili):
    feed = [
        _empty_draw("newest", pub_ts=200),
        _text_item("old", "旧动态", pub_ts=100),
    ]
    # The detail would fail if it were asked: a baseline lookup must not ask.
    api = _fake(
        bili,
        feed,
        {"newest": _opus_detail("newest", "新正文")},
        transient={"newest"},
    )

    card = await bili.api.space.latest_dynamic(api, "341368")

    assert api.detail_calls == []
    assert card.item_id == "newest"
    assert card.published_at == 200


@asyncio_test
async def test_dynamic_latest_video_reads_the_raw_feed_only(bili):
    feed = [
        _video_dynamic("video", pub_ts=300),
        _empty_draw("newer-shell", pub_ts=400),
    ]
    api = _fake(bili, feed, {"newer-shell": _opus_detail("newer-shell", "新正文")})

    card = await bili.api.space.dynamic_latest_video(api, "341368")

    assert api.detail_calls == []
    assert card.item_id == "BV1xx411c7mD"


@asyncio_test
async def test_official_feed_is_raw_and_never_calls_generic_detail(bili):
    feed = [_official_detail("1", "官号正文")]
    api = _fake(bili, feed, {"1": _opus_detail("1", "普通动态详情")})

    items = await bili.api.space.dynamic_items(api, ENDFIELD_OFFICIAL_UID)

    assert [item["id_str"] for item in items] == ["1"]
    assert api.detail_calls == []


# --- enrichment of the poller's selected window -----------------------------


@asyncio_test
async def test_empty_feed_item_is_replaced_with_detail_item(bili):
    feed = [_empty_draw("1266116596080836657")]
    details = {
        "1266116596080836657": _opus_detail(
            "1266116596080836657", "下一首应该是一个完全的手癖放出"
        )
    }
    api = _fake(bili, feed, details)

    items = await bili.api.space.enrich_empty_dynamic_items(api, feed)

    assert api.detail_calls == ["1266116596080836657"]
    assert bili.api.space._dynamic_has_content(items[0])


@asyncio_test
async def test_non_empty_feed_item_does_not_trigger_a_detail_request(bili):
    feed = [_text_item("1", "正文")]
    api = _fake(bili, feed, {})

    items = await bili.api.space.enrich_empty_dynamic_items(api, feed)

    assert api.detail_calls == []
    assert items[0]["id_str"] == "1"


@asyncio_test
async def test_live_rcmd_and_forward_shells_are_not_enriched(bili):
    feed = [
        _live_rcmd("live-1"),
        _live_rcmd("live-2", type_only=True),
        _forward("fwd-1", None),
        _forward("fwd-2", {}),
        _forward("fwd-3", _empty_draw("orig")),
        _text_item("text", "正文"),
    ]
    api = _fake(bili, feed, {})

    items = await bili.api.space.enrich_empty_dynamic_items(api, feed)

    assert api.detail_calls == []
    assert [item["id_str"] for item in items] == [
        "live-1",
        "live-2",
        "fwd-1",
        "fwd-2",
        "fwd-3",
        "text",
    ]


@asyncio_test
async def test_non_dict_items_are_skipped(bili):
    feed = [None, "junk", _empty_draw("1")]
    api = _fake(bili, feed, {"1": _opus_detail("1", "正文")})

    await bili.api.space.enrich_empty_dynamic_items(api, feed)

    assert api.detail_calls == ["1"]


@asyncio_test
async def test_items_without_an_id_are_not_queried(bili):
    shell = _empty_draw("")
    api = _fake(bili, [shell], {})

    await bili.api.space.enrich_empty_dynamic_items(api, [shell])

    assert api.detail_calls == []


@asyncio_test
async def test_only_items_newer_than_the_poll_baseline_are_enriched(bili):
    feed = [_empty_draw("new", pub_ts=200), _empty_draw("old", pub_ts=100)]
    details = {"new": _opus_detail("new", "新正文"), "old": _opus_detail("old", "旧正文")}
    api = _fake(bili, feed, details)

    items = await bili.api.space.enrich_empty_dynamic_items(api, feed, after_ts=100)

    assert api.detail_calls == ["new"]
    assert bili.api.space._dynamic_has_content(items[0])
    assert not bili.api.space._dynamic_has_content(items[1])


@asyncio_test
async def test_ineligible_items_do_not_consume_the_limit(bili):
    # Ineligible shells in front must not count against the five-request budget.
    feed = [_live_rcmd("live"), _forward("fwd", None)]
    feed += [_empty_draw(f"d{index}") for index in range(5)]
    details = {f"d{index}": _opus_detail(f"d{index}", "正文") for index in range(5)}
    api = _fake(bili, feed, details)

    await bili.api.space.enrich_empty_dynamic_items(api, feed, limit=5)

    assert api.detail_calls == [f"d{index}" for index in range(5)]


@asyncio_test
async def test_more_eligible_shells_than_the_limit_defer_the_round(bili):
    feed = [_empty_draw(str(index)) for index in range(6)]
    details = {str(index): _opus_detail(str(index), "正文") for index in range(6)}
    api = _fake(bili, feed, details)

    with pytest.raises(bili.api.DynamicEnrichDeferred) as excinfo:
        await bili.api.space.enrich_empty_dynamic_items(api, feed, limit=5)

    assert isinstance(excinfo.value, bili.api.BiliAPIError)
    # Nothing is fetched or replaced: a partially filled window must not leak.
    # No lookup ran, so there is no progress either.
    assert excinfo.value.progress == 0
    assert api.detail_calls == []
    assert all(not bili.api.space._dynamic_has_content(item) for item in feed)


@asyncio_test
async def test_only_the_passed_window_is_enriched(bili):
    feed = [_empty_draw(str(index)) for index in range(6)]
    details = {str(index): _opus_detail(str(index), "正文") for index in range(6)}
    api = _fake(bili, feed, details)
    selected = feed[:5]

    await bili.api.space.enrich_empty_dynamic_items(api, selected, limit=5)

    assert api.detail_calls == [str(index) for index in range(5)]
    assert all(bili.api.space._dynamic_has_content(item) for item in selected)
    # The item outside the window was never considered, not even deferred.
    assert not bili.api.space._dynamic_has_content(feed[5])


@asyncio_test
async def test_invisible_detail_keeps_the_feed_item(bili):
    feed = [_empty_draw("1")]
    api = _fake(bili, feed, {})

    items = await bili.api.space.enrich_empty_dynamic_items(api, feed)

    assert items[0] is feed[0]
    assert not bili.api.space._dynamic_has_content(items[0])


@asyncio_test
async def test_transient_detail_failure_defers_the_whole_window(bili):
    feed = [_empty_draw("1"), _empty_draw("2")]
    details = {"2": _opus_detail("2", "正文")}
    api = _fake(bili, feed, details, transient={"1"})

    with pytest.raises(bili.api.DynamicEnrichDeferred) as excinfo:
        await bili.api.space.enrich_empty_dynamic_items(api, feed)

    # The failure stops the round at the first shell instead of half-filling it.
    assert api.detail_calls == ["1"]
    # The first lookup failed, so this round made no progress.
    assert excinfo.value.progress == 0


@asyncio_test
async def test_transient_failure_after_a_success_reports_progress(bili):
    feed = [_empty_draw("1"), _empty_draw("2"), _empty_draw("3")]
    details = {"1": _opus_detail("1", "正文")}
    api = _fake(bili, feed, details, transient={"2"})

    with pytest.raises(bili.api.DynamicEnrichDeferred) as excinfo:
        await bili.api.space.enrich_empty_dynamic_items(api, feed)

    # The filled shell counts as progress so the poller retries at once.
    assert excinfo.value.progress == 1
    assert api.detail_calls == ["1", "2"]
    assert bili.api.space._dynamic_has_content(feed[0])
    assert not bili.api.space._dynamic_has_content(feed[1])


@asyncio_test
async def test_release_ids_only_free_the_shells_that_failed_repeatedly(bili):
    feed = [_empty_draw("old"), _empty_draw("new")]
    api = _fake(bili, feed, {}, transient={"old", "new"})

    with pytest.raises(bili.api.DynamicEnrichDeferred) as excinfo:
        await bili.api.space.enrich_empty_dynamic_items(api, feed, release_ids={"old"})

    # The previously stuck shell is released and counts as progress, but the
    # fresh one still gets its own retry rounds instead of losing its content
    # on the first failure.
    assert api.detail_calls == ["old", "new"]
    assert excinfo.value.item_id == "new"
    assert excinfo.value.progress == 1
    assert excinfo.value.resolved_ids == {"old"}
    assert excinfo.value.released_ids == {"old"}


@asyncio_test
async def test_already_released_shells_are_not_queried_again(bili):
    feed = [_empty_draw("old")]
    api = _fake(bili, feed, {}, transient={"old"})

    items = await bili.api.space.enrich_empty_dynamic_items(
        api, feed, release_ids={"old"}, skip_ids={"old"}
    )

    assert api.detail_calls == []
    assert items[0] is feed[0]


@asyncio_test
async def test_invisible_detail_counts_as_progress_for_the_budget(bili, monkeypatch):
    feed = [_empty_draw("1"), _empty_draw("2"), _empty_draw("3")]
    clock = _FakeClock()
    api = _fake(
        bili, feed, {"3": _opus_detail("3", "正文")}, slow_seconds=5.0, clock=clock
    )
    monkeypatch.setattr(bili.api.space, "asyncio", _AsyncioProxy(clock))

    with pytest.raises(bili.api.DynamicEnrichDeferred) as excinfo:
        await bili.api.space.enrich_empty_dynamic_items(api, feed)

    # "1" and "2" are confirmed invisible (their lookups are cached misses),
    # "3" would need a third slow lookup: both resolved shells count.
    assert api.detail_calls == ["1", "2"]
    assert excinfo.value.progress == 2
    assert not bili.api.space._dynamic_has_content(feed[2])


@asyncio_test
async def test_budget_exhaustion_defers_before_any_request(bili, monkeypatch):
    feed = [_empty_draw("1")]
    api = _fake(bili, feed, {"1": _opus_detail("1", "正文")})
    monkeypatch.setattr(bili.api.space, "DYNAMIC_ENRICH_BUDGET_SECONDS", 0.0)

    with pytest.raises(bili.api.DynamicEnrichDeferred) as excinfo:
        await bili.api.space.enrich_empty_dynamic_items(api, feed)

    assert excinfo.value.progress == 0
    assert api.detail_calls == []
    assert not bili.api.space._dynamic_has_content(feed[0])


@asyncio_test
async def test_budget_exhaustion_reports_the_items_this_round_resolved(
    bili, monkeypatch
):
    feed = [_empty_draw(str(index)) for index in range(5)]
    details = {
        str(index): _opus_detail(str(index), "正文") for index in range(5)
    }
    clock = _FakeClock()
    # 4 s per fresh lookup inside the 10 s budget: three filled, the rest waits.
    api = _fake(bili, feed, details, slow_seconds=4.0, clock=clock)
    monkeypatch.setattr(bili.api.space, "asyncio", _AsyncioProxy(clock))

    with pytest.raises(bili.api.DynamicEnrichDeferred) as excinfo:
        await bili.api.space.enrich_empty_dynamic_items(api, feed)

    assert api.detail_calls == ["0", "1", "2"]
    assert excinfo.value.progress == 3
    # The filled shells stay filled, so the retry only asks for the rest.
    assert [
        bili.api.space._dynamic_has_content(item) for item in feed
    ] == [True, True, True, False, False]


# --- poller integration -----------------------------------------------------


@asyncio_test
async def test_mid_page_transient_shell_soft_defers_and_retries_next_tick(
    bili, poller_env, tmp_path
):
    store = await open_store(poller_env, tmp_path)
    try:
        await store.upsert_target(
            poller_env.models.TargetInfo(
                "dynamic", "1", latest_id="baseline", latest_ts=100
            )
        )
        await store.add_subscription("dynamic", "1", "group", "900")
        feed = [
            _empty_draw("mid", pub_ts=400),
            _text_item("newest", "最新正文", pub_ts=500),
        ]
        api = _fake_in(
            poller_env.space,
            feed,
            {"mid": _opus_detail("mid", "补全正文", pub_ts=400)},
            transient={"mid"},
        )
        poller = poller_env.poller.Poller(
            api, store, intervals={"dynamic": 0}, clock=lambda: 1000
        )

        await poller.tick_dynamic()

        # Soft deferral: no outbox, no seen mark and the target stays untouched.
        assert await store.outbox_count() == 0
        assert await store.seen_ids("dynamic", "1", ["mid", "newest"]) == set()
        state = await store.get_target("dynamic", "1")
        assert (state.latest_id, state.latest_ts) == ("baseline", 100)
        assert poller._failures.get(("dynamic", "1"), 0) == 0
        assert poller._next_due[("dynamic", "1")] == 1000

        # The next tick retries the same window and pushes the filled shell.
        api.transient.clear()
        await poller.tick_dynamic()

        assert await store.outbox_count() == 2
        assert await store.seen_ids("dynamic", "1", ["mid", "newest"]) == {
            "mid",
            "newest",
        }
        state = await store.get_target("dynamic", "1")
        assert state.latest_ts == 500
    finally:
        await store.close()


@asyncio_test
async def test_detail_pub_ts_cannot_backdate_a_filled_shell(
    bili, poller_env, tmp_path
):
    store = await open_store(poller_env, tmp_path)
    try:
        await store.upsert_target(
            poller_env.models.TargetInfo("dynamic", "1", latest_ts=50)
        )
        await store.add_subscription("dynamic", "1", "group", "900")
        feed = [
            _empty_draw("missing-ts", pub_ts=400),
            _empty_draw("backdated", pub_ts=300),
            _empty_draw("newer-detail", pub_ts=200),
        ]
        details = {
            # Missing and older timestamps inherit the feed's; a genuinely
            # newer detail timestamp wins.
            "missing-ts": _opus_detail("missing-ts", "正文", pub_ts=0),
            "backdated": _opus_detail("backdated", "正文", pub_ts=100),
            "newer-detail": _opus_detail("newer-detail", "正文", pub_ts=500),
        }
        api = _fake_in(poller_env.space, feed, details)
        poller = poller_env.poller.Poller(
            api, store, intervals={"dynamic": 0}, clock=lambda: 1000
        )

        await poller.tick_dynamic()

        # Detection cannot silently skip a filled shell for a bad timestamp.
        rows = await store.outbox_rows()
        assert sorted(
            json.loads(row.card_json)["published_at"] for row in rows
        ) == [300, 400, 500]
        assert await store.seen_ids(
            "dynamic", "1", ["missing-ts", "backdated", "newer-detail"]
        ) == {"missing-ts", "backdated", "newer-detail"}
        state = await store.get_target("dynamic", "1")
        assert state.latest_ts == 500
    finally:
        await store.close()


@asyncio_test
async def test_whole_page_with_six_shells_converges_instead_of_stalling(
    bili, poller_env, tmp_path, monkeypatch
):
    store = await open_store(poller_env, tmp_path)
    try:
        await store.upsert_target(poller_env.models.TargetInfo("dynamic", "1"))
        await store.add_subscription("dynamic", "1", "group", "900")
        feed = [
            _empty_draw(f"shell{index}", pub_ts=200 + index) for index in range(6)
        ]
        feed += [
            _text_item(f"text{index}", f"正文{index}", pub_ts=300 + index)
            for index in range(4)
        ]
        details = {
            f"shell{index}": _opus_detail(
                f"shell{index}", "补全正文", pub_ts=200 + index
            )
            for index in range(6)
        }
        api = _fake_in(poller_env.space, feed, details)
        # Force the whole-page window regardless of the lottery-filter config.
        monkeypatch.setattr(poller_env.poller, "applies_to", lambda uid: True)
        poller = poller_env.poller.Poller(
            api, store, intervals={"dynamic": 0}, clock=lambda: 1000
        )

        await poller.tick_dynamic()

        # The request cap covers the whole selected window, so all six shells
        # are filled in one round instead of deferring forever.
        assert api.enrich_calls == [(0, 10, 10)]
        assert sorted(api.detail_calls) == [f"shell{index}" for index in range(6)]
        assert await store.outbox_count() == 10
        assert await store.seen_ids(
            "dynamic", "1", [item["id_str"] for item in feed]
        ) == {item["id_str"] for item in feed}
        state = await store.get_target("dynamic", "1")
        assert state.latest_ts == 303
        assert poller._failures.get(("dynamic", "1"), 0) == 0

        # The cursor moved past the whole window, so the next round has no
        # shells to retry and no duplicate pushes.
        api.detail_calls.clear()
        await poller.tick_dynamic()
        assert api.detail_calls == []
        assert await store.outbox_count() == 10
    finally:
        await store.close()


@asyncio_test
async def test_three_consecutive_deferrals_back_off_then_release_shells(
    bili, poller_env, tmp_path
):
    store = await open_store(poller_env, tmp_path)
    messages: list[str] = []
    sink_id = logger.add(
        lambda message: messages.append(message.record["message"]),
        level="DEBUG",
        format="{message}",
    )
    try:
        await store.upsert_target(
            poller_env.models.TargetInfo("dynamic", "1", latest_ts=100)
        )
        await store.add_subscription("dynamic", "1", "group", "900")
        feed = [
            _empty_draw("mid", pub_ts=400),
            _text_item("newest", "最新正文", pub_ts=500),
        ]
        api = _fake_in(
            poller_env.space,
            feed,
            {"mid": _opus_detail("mid", "补全正文", pub_ts=400)},
            transient={"mid"},
        )
        clock = [1000]
        poller = poller_env.poller.Poller(
            api, store, intervals={"dynamic": 60}, clock=lambda: clock[0]
        )
        # Force the first round due without depending on the hash stagger.
        poller._next_due[("dynamic", "1")] = 0

        await poller.tick_dynamic()
        await poller.tick_dynamic()

        # The first deferrals stay free: no backoff, no warning.
        assert poller._failures.get(("dynamic", "1"), 0) == 0
        assert poller._next_due[("dynamic", "1")] == 0
        assert poller._deferrals[("dynamic", "1")] == 2
        assert not any("failure backoff" in line for line in messages)

        # The third crosses over: one warning plus the failure schedule.
        await poller.tick_dynamic()
        assert poller._failures[("dynamic", "1")] == 1
        assert poller._next_due[("dynamic", "1")] == 1120
        assert len([line for line in messages if "failure backoff" in line]) == 1

        # Until the backoff expires the target is not due: no extra request.
        calls_before = len(api.detail_calls)
        await poller.tick_dynamic()
        assert len(api.detail_calls) == calls_before

        # After the backoff, a window that still makes no progress is released:
        # the unreadable shell gets a placeholder push so the newer posts behind
        # it are not blocked forever.
        clock[0] = 1120
        await poller.tick_dynamic()
        assert poller._failures[("dynamic", "1")] == 0
        assert ("dynamic", "1") not in poller._deferrals
        assert await store.outbox_count() == 2
        assert await store.seen_ids("dynamic", "1", ["mid", "newest"]) == {
            "mid",
            "newest",
        }
        state = await store.get_target("dynamic", "1")
        assert state.latest_ts == 500
        assert len([line for line in messages if "failure backoff" in line]) == 1
        assert any("releasing its shell" in line for line in messages)

        # The released placeholder is pushed exactly once.
        clock[0] = 1180
        api.detail_calls.clear()
        await poller.tick_dynamic()
        assert await store.outbox_count() == 2
        assert api.detail_calls == []
    finally:
        logger.remove(sink_id)
        await store.close()


@asyncio_test
async def test_releasing_a_stuck_shell_counts_as_progress_and_is_not_retried(
    bili, poller_env, tmp_path
):
    store = await open_store(poller_env, tmp_path)
    try:
        await store.upsert_target(
            poller_env.models.TargetInfo("dynamic", "1", latest_ts=100)
        )
        await store.add_subscription("dynamic", "1", "group", "900")
        feed = [
            _empty_draw("old", pub_ts=400),
            _empty_draw("new", pub_ts=500),
        ]
        api = _fake_in(
            poller_env.space,
            feed,
            {},
            transient={"old", "new"},
        )
        poller = poller_env.poller.Poller(
            api, store, intervals={"dynamic": 60}, clock=lambda: 1000
        )
        key = ("dynamic", "1")
        poller._next_due[key] = 0
        poller._detail_failures[key] = {"old": 3}
        poller._deferrals[key] = 3

        await poller.tick_dynamic()

        # Releasing "old" is real progress, so the target does not keep the
        # failure backoff; only the still-fresh "new" shell defers the round.
        assert poller._released_windows[key] == {"old"}
        assert poller._detail_failures[key] == {"new": 1}
        assert key not in poller._deferrals
        assert poller._failures.get(key, 0) == 0
        assert await store.outbox_count() == 0

        # The released shell is not queried or warned about again.
        api.detail_calls.clear()
        await poller.tick_dynamic()
        assert api.detail_calls == ["new"]
        assert poller._detail_failures[key] == {"new": 2}
        assert poller._deferrals[key] == 1
    finally:
        await store.close()


@asyncio_test
async def test_cached_detail_hits_do_not_count_as_new_progress(
    bili, poller_env, tmp_path
):
    """A cached success must not hide a permanently failing sibling forever.

    Regression: ``progress`` used to be incremented for cache hits too, so a
    window with one cached item and one persistently failing item reset the
    no-progress counter every round and never reached backoff.
    """
    store = await open_store(poller_env, tmp_path)
    try:
        await store.upsert_target(
            poller_env.models.TargetInfo("dynamic", "1", latest_ts=100)
        )
        await store.add_subscription("dynamic", "1", "group", "900")
        feed = [
            _empty_draw("cached", pub_ts=400),
            _empty_draw("failing", pub_ts=500),
            _text_item("newest", "最新正文", pub_ts=600),
        ]
        api = _fake_in(
            poller_env.space,
            feed,
            {
                "cached": _opus_detail("cached", "已缓存正文", pub_ts=400),
                "failing": _opus_detail("failing", "补不出来", pub_ts=500),
            },
            transient={"failing"},
        )
        # An earlier round already resolved this item; this round's lookup is
        # a cache hit and must not be reported as progress.
        api.cached_details.add("cached")
        poller = poller_env.poller.Poller(
            api, store, intervals={"dynamic": 60}, clock=lambda: 1000
        )
        poller._next_due[("dynamic", "1")] = 0

        await poller.tick_dynamic()
        await poller.tick_dynamic()
        await poller.tick_dynamic()

        assert api.detail_calls == ["failing"] * 3
        assert poller._deferrals[("dynamic", "1")] == 3
        assert poller._failures[("dynamic", "1")] == 1
        assert await store.outbox_count() == 0
        assert await store.seen_ids("dynamic", "1", ["cached", "failing"]) == set()
    finally:
        await store.close()


@asyncio_test
async def test_cache_eviction_does_not_turn_a_refetch_into_progress(
    bili, poller_env, tmp_path
):
    """A re-fetched shell already resolved in this window is not progress.

    The 300 s cache holds only 64 entries, so a busy poll can evict a success
    and force the next round to fetch it again. The poller keeps the window's
    resolved ids, so that re-fetch cannot reset the no-progress counter.
    """
    store = await open_store(poller_env, tmp_path)
    try:
        await store.upsert_target(
            poller_env.models.TargetInfo("dynamic", "1", latest_ts=100)
        )
        await store.add_subscription("dynamic", "1", "group", "900")
        feed = [
            _empty_draw("cached", pub_ts=400),
            _empty_draw("failing", pub_ts=500),
        ]
        api = _fake_in(
            poller_env.space,
            feed,
            {
                "cached": _opus_detail("cached", "已缓存正文", pub_ts=400),
                "failing": _opus_detail("failing", "补不出来", pub_ts=500),
            },
            transient={"failing"},
        )
        poller = poller_env.poller.Poller(
            api, store, intervals={"dynamic": 60}, clock=lambda: 1000
        )
        poller._next_due[("dynamic", "1")] = 0

        # Round 1 resolves "cached" for the first time: that is real progress.
        await poller.tick_dynamic()
        assert poller._resolved_windows[("dynamic", "1")] == {"cached"}
        assert ("dynamic", "1") not in poller._deferrals

        # Evict the success cache between rounds. The refetched "cached" item is
        # already in the window's resolved-id set, so these rounds still make no
        # new progress.
        for _ in range(2):
            api.cached_details.clear()
            await poller.tick_dynamic()

        assert poller._deferrals[("dynamic", "1")] == 2
        assert poller._detail_failures[("dynamic", "1")]["failing"] == 3
        assert await store.outbox_count() == 0

        # The item that failed three times is released on the next attempt,
        # rather than keeping the window stuck forever.
        api.cached_details.clear()
        await poller.tick_dynamic()
        assert await store.outbox_count() == 2
        assert ("dynamic", "1") not in poller._detail_failures
    finally:
        await store.close()


@asyncio_test
async def test_progressing_deferral_keeps_the_normal_cadence(
    bili, poller_env, tmp_path
):
    store = await open_store(poller_env, tmp_path)
    try:
        await store.upsert_target(
            poller_env.models.TargetInfo("dynamic", "1", latest_ts=100)
        )
        await store.add_subscription("dynamic", "1", "group", "900")
        feed = [
            _empty_draw("mid", pub_ts=400),
            _text_item("newest", "最新正文", pub_ts=500),
        ]
        api = _fake_in(
            poller_env.space,
            feed,
            {"mid": _opus_detail("mid", "补全正文", pub_ts=400)},
        )
        real_enrich = api.enrich_empty_dynamic_items
        # ``None`` means "let the real enrichment run"; otherwise this is the
        # simulated deferral's newly-resolved id set.
        state = {"resolved_ids": {"mid"}}

        async def enrich(items, **kwargs):
            if state["resolved_ids"] is not None:
                raise poller_env.space.DynamicEnrichDeferred(
                    "动态详情回查预算用完，留待下一轮",
                    progress=len(state["resolved_ids"]),
                    resolved_ids=state["resolved_ids"],
                )
            return await real_enrich(items, **kwargs)

        api.enrich_empty_dynamic_items = enrich
        poller = poller_env.poller.Poller(
            api, store, intervals={"dynamic": 60}, clock=lambda: 1000
        )
        poller._next_due[("dynamic", "1")] = 0

        await poller.tick_dynamic()
        state["resolved_ids"] = {"mid", "newer"}
        await poller.tick_dynamic()

        # Slow but progressing: the retry stays at the normal cadence and the
        # consecutive-deferral counter is not incremented.
        assert ("dynamic", "1") not in poller._deferrals
        assert poller._failures.get(("dynamic", "1"), 0) == 0
        assert poller._next_due[("dynamic", "1")] == 0
        assert await store.outbox_count() == 0

        # A no-progress round starts counting from zero again, not from three.
        state["resolved_ids"] = set()
        await poller.tick_dynamic()
        assert poller._deferrals[("dynamic", "1")] == 1
        assert poller._failures.get(("dynamic", "1"), 0) == 0
        assert poller._next_due[("dynamic", "1")] == 0

        # The window finally fits: one normal success clears everything.
        state["resolved_ids"] = None
        await poller.tick_dynamic()
        assert ("dynamic", "1") not in poller._deferrals
        assert poller._failures.get(("dynamic", "1"), 0) == 0
        assert await store.outbox_count() == 2
    finally:
        await store.close()


@asyncio_test
async def test_slow_budget_window_with_ten_shells_converges_without_backoff(
    bili, poller_env, tmp_path, monkeypatch
):
    store = await open_store(poller_env, tmp_path)
    try:
        await store.upsert_target(poller_env.models.TargetInfo("dynamic", "1"))
        await store.add_subscription("dynamic", "1", "group", "900")
        feed = [
            _empty_draw(f"shell{index}", pub_ts=200 + index) for index in range(10)
        ]
        details = {
            f"shell{index}": _opus_detail(
                f"shell{index}", "补全正文", pub_ts=200 + index
            )
            for index in range(10)
        }
        clock = _FakeClock()
        # 4 s per fresh detail lookup: the 10 s budget only fits three per round,
        # the slow-window shape that used to walk into the 120/240/480 s backoff.
        api = _fake_in(poller_env.space, feed, details, slow_seconds=4.0, clock=clock)
        monkeypatch.setattr(poller_env.space, "asyncio", _AsyncioProxy(clock))
        monkeypatch.setattr(poller_env.poller, "applies_to", lambda uid: True)
        poller = poller_env.poller.Poller(
            api, store, intervals={"dynamic": 0}, clock=lambda: 1000
        )

        rounds = 0
        for _ in range(6):
            rounds += 1
            await poller.tick_dynamic()
            if await store.outbox_count() == 10:
                break

        # The window needed several slow rounds, and every one of them stayed
        # on the normal cadence because it reported progress.
        assert rounds > 1
        assert poller._failures.get(("dynamic", "1"), 0) == 0
        assert ("dynamic", "1") not in poller._deferrals
        assert await store.outbox_count() == 10
        assert await store.seen_ids(
            "dynamic", "1", [item["id_str"] for item in feed]
        ) == {item["id_str"] for item in feed}
        state = await store.get_target("dynamic", "1")
        assert state.latest_ts == 209
    finally:
        await store.close()


@asyncio_test
async def test_budget_exhaustion_soft_defers_the_whole_tick(
    bili, poller_env, tmp_path, monkeypatch
):
    store = await open_store(poller_env, tmp_path)
    try:
        await store.upsert_target(
            poller_env.models.TargetInfo("dynamic", "1", latest_ts=100)
        )
        await store.add_subscription("dynamic", "1", "group", "900")
        feed = [_empty_draw("1", pub_ts=200)]
        api = _fake_in(
            poller_env.space,
            feed,
            {"1": _opus_detail("1", "正文", pub_ts=200)},
        )
        monkeypatch.setattr(
            poller_env.space, "DYNAMIC_ENRICH_BUDGET_SECONDS", 0.0
        )
        poller = poller_env.poller.Poller(
            api, store, intervals={"dynamic": 0}, clock=lambda: 1000
        )

        await poller.tick_dynamic()

        assert await store.outbox_count() == 0
        assert await store.seen_ids("dynamic", "1", ["1"]) == set()
        state = await store.get_target("dynamic", "1")
        assert (state.latest_id, state.latest_ts) == ("", 100)
        assert api.detail_calls == []
        assert poller._failures == {}
    finally:
        await store.close()


@asyncio_test
async def test_invisible_detail_keeps_the_shell_and_pushes_a_placeholder(
    bili, poller_env, tmp_path
):
    store = await open_store(poller_env, tmp_path)
    try:
        await store.upsert_target(
            poller_env.models.TargetInfo("dynamic", "1", latest_ts=100)
        )
        await store.add_subscription("dynamic", "1", "group", "900")
        feed = [_empty_draw("1", pub_ts=200)]
        api = _fake_in(poller_env.space, feed, {})
        poller = poller_env.poller.Poller(
            api, store, intervals={"dynamic": 0}, clock=lambda: 1000
        )

        await poller.tick_dynamic()

        assert api.detail_calls == ["1"]
        rows = await store.outbox_rows()
        assert len(rows) == 1
        assert json.loads(rows[0].card_json)["title"] == "发布了新动态"
        assert await store.seen_ids("dynamic", "1", ["1"]) == {"1"}
    finally:
        await store.close()


@asyncio_test
async def test_official_target_never_runs_generic_enrichment(
    bili, poller_env, tmp_path
):
    store = await open_store(poller_env, tmp_path)
    try:
        await store.upsert_target(poller_env.models.TargetInfo("dynamic", ENDFIELD_OFFICIAL_UID))
        await store.add_subscription(
            "dynamic", ENDFIELD_OFFICIAL_UID, "group", "900"
        )
        feed = [_official_detail("1", "官号正文")]
        api = _fake_in(
            poller_env.space, feed, {"1": _opus_detail("1", "普通动态详情")}
        )
        poller = poller_env.poller.Poller(
            api, store, intervals={"dynamic": 0}, clock=lambda: 1000
        )

        await poller.tick_dynamic()

        assert api.enrich_calls == []
        assert api.detail_calls == []
        assert await store.outbox_count() == 1
    finally:
        await store.close()


# --- API-level caches -------------------------------------------------------


@asyncio_test
async def test_bili_api_wrapper_fills_shells_and_caches_invisible_ones(bili):
    api = bili.api.BiliApi(transport=_stub_transport(), min_interval=0)
    calls: list[str] = []

    async def detail(url, label, *, params=None, sign_params=None):
        item_id = str((params or {})["id"])
        calls.append(item_id)
        if item_id == "2":
            return {"data": {"item": {}}}
        return {"data": {"item": _opus_detail(item_id, "正文")}}

    api._get_json_with_risk_retry = detail
    feed = [_empty_draw("1"), _empty_draw("2")]

    await api.enrich_empty_dynamic_items(feed, limit=2)

    assert bili.api.space._dynamic_has_content(feed[0])
    assert not bili.api.space._dynamic_has_content(feed[1])
    assert api._dynamic_detail_misses.contains("2")
    assert calls == ["1", "2"]
    await api.aclose()


@asyncio_test
async def test_miss_cache_hit_raises_dynamic_detail_unavailable(bili):
    api = bili.api.BiliApi(transport=_stub_transport(), min_interval=0)
    calls: list[str] = []

    async def invisible(url, label, *, params=None, sign_params=None):
        calls.append(str((params or {})["id"]))
        return {"data": {"item": {}}}

    api._get_json_with_risk_retry = invisible
    with pytest.raises(bili.api.DynamicDetailUnavailable):
        await api.dynamic_detail("1")
    assert api._dynamic_detail_misses.contains("1")

    # Second call is answered from the miss cache without another request.
    with pytest.raises(bili.api.DynamicDetailUnavailable):
        await api.dynamic_detail("1")
    assert calls == ["1"]
    await api.aclose()


@asyncio_test
async def test_transient_failure_is_not_cached_as_a_miss(bili):
    api = bili.api.BiliApi(transport=_stub_transport(), min_interval=0)
    calls: list[str] = []

    async def flaky(url, label, *, params=None, sign_params=None):
        calls.append(str((params or {})["id"]))
        if len(calls) == 1:
            raise bili.api.BiliAPIError("Bilibili request timeout")
        return {"data": {"item": _opus_detail("1", "正文")}}

    api._get_json_with_risk_retry = flaky
    with pytest.raises(bili.api.BiliAPIError) as failure:
        await api.dynamic_detail("1")
    assert not isinstance(failure.value, bili.api.DynamicDetailUnavailable)
    assert not api._dynamic_detail_misses.contains("1")

    detail = await api.dynamic_detail("1")

    assert detail["id_str"] == "1"
    assert calls == ["1", "1"]
    await api.aclose()


@pytest.mark.parametrize("code", [-404, 4101131])
@asyncio_test
async def test_business_unavailable_code_is_cached_as_a_miss(bili, code):
    api = bili.api.BiliApi(transport=_stub_transport(), min_interval=0)
    calls: list[str] = []

    async def detail_json(url, *, params=None):
        calls.append(str((params or {})["id"]))
        return {"code": code, "message": "请求数据发生错误"}

    api._get_json = detail_json
    feed = [_empty_draw("1", pub_ts=200)]

    await api.enrich_empty_dynamic_items(feed)

    # Deleted/hidden is a permanent answer: release the shell, remember the
    # miss, and never defer the whole window over it.
    assert not bili.api.space._dynamic_has_content(feed[0])
    assert api._dynamic_detail_misses.contains("1")
    assert calls == ["1"]
    with pytest.raises(bili.api.DynamicDetailUnavailable):
        await api.dynamic_detail("1")
    assert calls == ["1"]
    await api.aclose()


@pytest.mark.parametrize("code", [500, 4101105])
@asyncio_test
async def test_retryable_business_code_stays_a_transient_failure(bili, code):
    api = bili.api.BiliApi(transport=_stub_transport(), min_interval=0)

    async def detail_json(url, *, params=None):
        return {"code": code, "message": "加载错误，请稍后再试"}

    api._get_json = detail_json
    feed = [_empty_draw("1", pub_ts=200)]

    with pytest.raises(bili.api.DynamicEnrichDeferred):
        await api.enrich_empty_dynamic_items(feed)

    assert not api._dynamic_detail_misses.contains("1")
    with pytest.raises(bili.api.BiliAPIError) as failure:
        await api.dynamic_detail("1")
    assert not isinstance(failure.value, bili.api.DynamicDetailUnavailable)
    await api.aclose()


@asyncio_test
async def test_pub_ts_carry_does_not_mutate_the_cached_detail(bili):
    api = bili.api.BiliApi(transport=_stub_transport(), min_interval=0)
    cached = _opus_detail("1", "正文", pub_ts=50)
    api._generic_dynamic_details.put("1", cached)
    feed = [_empty_draw("1", pub_ts=200)]

    await api.enrich_empty_dynamic_items(feed, after_ts=100)

    assert cached["modules"]["module_author"]["pub_ts"] == 50
    assert feed[0]["modules"]["module_author"]["pub_ts"] == 200
    await api.aclose()


@asyncio_test
async def test_generic_and_official_detail_caches_are_separate(bili):
    api = bili.api.BiliApi(transport=_stub_transport(), min_interval=0)

    async def detail(url, label, *, params=None, sign_params=None):
        item_id = str((params or {})["id"])
        return {"data": {"item": _opus_detail(item_id, "正文")}}

    api._get_json_with_risk_retry = detail
    await api.dynamic_detail("1")
    assert api._generic_dynamic_details.contains("1")
    assert not api._dynamic_details.contains("1")

    async def official(url, label, *, params=None, sign_params=None):
        return {"data": {"item": _official_detail("2")}}

    api._get_json_with_risk_retry = official
    await api.official_dynamic_detail("2")
    assert api._dynamic_details.contains("2")
    assert not api._generic_dynamic_details.contains("2")
    await api.aclose()


def test_detail_cache_limits(bili):
    api = bili.api.BiliApi(min_interval=0)
    generic = api._generic_dynamic_details
    misses = api._dynamic_detail_misses

    assert generic is not api._dynamic_details
    assert generic.ttl_seconds == 300
    assert generic.max_entries == 64
    assert generic.max_bytes == 4 * 1024 * 1024
    assert misses.ttl_seconds == 1800
    assert misses.max_entries == 256
    assert misses.max_bytes == 256 * 1024


@asyncio_test
async def test_aclose_closes_every_detail_cache(bili):
    api = bili.api.BiliApi(min_interval=0)
    api._dynamic_details.put("official", {"id_str": "official"})
    api._generic_dynamic_details.put("generic", {"id_str": "generic"})
    api._dynamic_detail_misses.put("miss", True)

    await api.aclose()

    assert not api._dynamic_details.contains("official")
    assert not api._generic_dynamic_details.contains("generic")
    assert not api._dynamic_detail_misses.contains("miss")
