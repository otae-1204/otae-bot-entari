"""Data semantics and delivery checks for radar's HTML card frontend."""

import asyncio
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lxml import html
from PIL import Image as PILImage
from satori import Image as ImageSegment

from plugins.radar import handlers
from plugins.radar.models import (
    SCORING_CONTINUOUS,
    Comparison,
    DegradationAlert,
    EfficiencyPoint,
    ModelProfile,
    ModelRow,
    RadarMeta,
    TrendPoint,
)
from plugins.radar.presentation import (
    RadarReply,
    alert_pages,
    comparison_pages,
    help_pages,
    profile_pages,
    ranking_pages,
    trend_chart,
    value_pages,
)
from plugins.radar.rendering import CARD_MAX_HEIGHT, CARD_WIDTH, page_html, render_page

META = RadarMeta(
    "deep-swe",
    scoring_mode="binary-majority",
    score_label="Pass rate",
    mode="equal_latest_3",
    rolling_window=3,
    source_updated_at="2026-09-14T09:00:00Z",
)
ROW = ModelRow("model-a", "low", 135, 92, 92, 110, 74, 0.682, 109.19, False)


def document(page):
    # The offline document embeds a full CJK font, exceeding libxml's default
    # single-text-node size limit. Remove only the trusted style node for DOM assertions.
    source = page_html(page)
    start, end = source.index("<style>"), source.index("</style>") + len("</style>")
    return html.fromstring(source[:start] + source[end:])


def visible(page):
    return document(page).text_content()


def png_sample():
    output = BytesIO()
    PILImage.new("RGB", (1, 1)).save(output, format="PNG")
    return output.getvalue()


def test_mixed_iq_origins_are_labelled_without_changing_scores():
    derived = replace(ROW, model="model-b", iq=102.3, iq_derived=True)
    doc = document(ranking_pages((ROW, derived), META)[0])
    rows = doc.xpath("//tbody/tr")
    assert "109.2" in rows[0].text_content() and "综合 IQ" in rows[0].text_content()
    assert "102.3" in rows[1].text_content() and "频道换算 IQ" in rows[1].text_content()
    # The visible score is the raw 0.682, never reverse-derived from IQ.
    assert all(
        "68.2%" in row.text_content() and "135" in row.text_content() for row in rows
    )
    assert all(
        "68.20%" in node.get("style") for node in doc.xpath('//div[@class="bar"]/i')
    )


def test_pagination_preserves_every_model_and_page_identity():
    rows = tuple(replace(ROW, model=f"model-{i}") for i in range(17))
    pages = ranking_pages(rows, META)
    names = []
    for i, page in enumerate(pages, 1):
        doc = document(page)
        names.extend(doc.xpath('//tbody//span[@class="model"]/text()'))
        assert page.number == i and page.total == 3
        assert (
            "deep-swe" in doc.text_content() and "equal_latest_3" in doc.text_content()
        )
    assert names == [row.model for row in rows]


def test_continuous_score_missing_cost_and_zero_are_distinct():
    meta = replace(
        META, benchmark_id="pompeii-adjacency", scoring_mode=SCORING_CONTINUOUS
    )
    point = EfficiencyPoint("model-a", "max", 100, 49.79, 55, average_minutes=0)
    body = visible(value_pages((point,), meta)[0])
    assert "Macro-F1 0.905" in body
    assert "通过率" not in body and "49.79题" not in body
    assert "~0.0 分" in body and "~$0.00" not in body and "—" in body


def test_profile_keeps_supplement_effort_and_point_timestamp():
    upper = replace(ROW, effort="ultra")
    point = EfficiencyPoint(
        "model-a", "low", 99, 80, 100, source_updated_at="2026-09-13T00:00:00Z"
    )
    profile = ModelProfile("model-a", (ROW, upper), upper, efficiency=point, meta=META)
    doc = document(profile_pages(profile)[0])
    section = doc.xpath('//div[@class="section-heading"][h2="成本与等待时间"]')[0]
    assert "low" in section.text_content() and "ultra" not in section.text_content()
    assert "2026-09-13 00:00 UTC" in doc.text_content()


def test_comparison_does_not_attach_other_effort_cost_or_mixed_iq_delta():
    upper = replace(ROW, effort="ultra", iq_derived=True)
    eff = EfficiencyPoint("model-a", "low", 99, 80, 100, average_price_usd=99.99)
    left = ModelProfile("model-a", (ROW, upper), upper, efficiency=eff, meta=META)
    right = ModelProfile("model-b", (ROW,), ROW, meta=replace(META, stale=True))
    body = visible(comparison_pages(Comparison(left, right, iq_delta=15, meta=META))[0])
    assert "$99.99" not in body
    assert "暂不计算 IQ 差" in body
    assert "数据可能过期" in body


def test_stale_empty_and_upstream_zero_alert_are_honest():
    body = visible(ranking_pages((), replace(META, stale=True))[0])
    assert "暂无数据" in body and "数据可能过期" in body
    body = visible(alert_pages((), META)[0])
    assert "当前无降智预警" in body and "不保证所有模型表现稳定" in body
    alert = DegradationAlert("model-a", "high", current_iq=0)
    body = visible(alert_pages((alert,), META)[0])
    assert "当前 IQ0.0" in body and "24h 均值—" in body


def test_chart_uses_elapsed_time_and_breaks_at_missing_points():
    chart = html.fromstring(
        trend_chart(
            (
                TrendPoint("2026-09-14T00:00:00Z", 0, 0),
                TrendPoint("2026-09-14T01:00:00Z", 50, 20),
                TrendPoint("bad timestamp", 80, 30),
                TrendPoint("2026-09-14T04:00:00Z", 100, 40),
            )
        )
    )
    dots = chart.xpath("//circle")
    assert [float(dot.get("cx")) for dot in dots] == [52, 260, 884]
    assert chart.xpath("//path")[0].get("d").count("M") == 2
    assert "n=0" in dots[0].text_content()
    singleton = html.fromstring(
        trend_chart((TrendPoint("2026-09-14T00:00:00Z", 0, 0),))
    )
    assert singleton.xpath("//circle")[0].get("cx") == "468.00"


def test_untrusted_labels_cannot_inject_html_or_resources():
    bad = '<img src="https://example.invalid/tracker" onerror="alert(1)">'
    doc = document(
        ranking_pages((replace(ROW, model=bad),), replace(META, note=bad))[0]
    )
    assert not doc.xpath("//img|//script|//iframe")
    assert bad in doc.text_content()
    assert (
        "default-src 'none'"
        in doc.xpath('//meta[@http-equiv="Content-Security-Policy"]/@content')[0]
    )


def test_image_reply_renders_before_sending_and_uses_image_segments(monkeypatch):
    events = []

    async def render(page):
        events.append("render")
        return png_sample()

    async def send(session, message):
        events.append("send")
        assert isinstance(message, list) and isinstance(message[0], ImageSegment)

    monkeypatch.setattr(handlers, "render_page", render)
    monkeypatch.setattr(handlers, "send", send)
    pages = help_pages() * 2
    asyncio.run(handlers._reply(object(), RadarReply(["fallback"], pages)))
    assert events == ["render", "render", "send", "send"]


@pytest.mark.parametrize("failure_stage", ["render", "send"])
def test_image_failure_delivers_original_text(monkeypatch, failure_stage):
    render = AsyncMock(return_value=png_sample())
    send = AsyncMock()
    if failure_stage == "render":
        render.side_effect = RuntimeError("failed")
    else:
        send.side_effect = [RuntimeError("image rejected"), None]
    monkeypatch.setattr(handlers, "render_page", render)
    monkeypatch.setattr(handlers, "send", send)
    asyncio.run(
        handlers._reply(object(), RadarReply(["完整文本", "保留口径"], help_pages()))
    )
    assert send.await_args.args[1] == "完整文本\n保留口径"


def test_cancellation_never_sends_fallback(monkeypatch):
    monkeypatch.setattr(
        handlers, "render_page", AsyncMock(side_effect=asyncio.CancelledError)
    )
    send = AsyncMock()
    monkeypatch.setattr(handlers, "send", send)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(handlers._reply(object(), RadarReply(["fallback"], help_pages())))
    send.assert_not_awaited()


def test_concurrency_gate_covers_render_and_delivery(monkeypatch):
    monkeypatch.setattr(handlers, "_active", {})
    monkeypatch.setattr(handlers, "_config", SimpleNamespace(concurrency=1))
    monkeypatch.setattr(handlers, "send", AsyncMock())

    async def reply(*args):
        assert handlers._active["radar"] == 1

    monkeypatch.setattr(handlers, "_reply", reply)

    async def query():
        return ["ready"]

    asyncio.run(handlers._run(object(), query(), timeout=1))
    assert handlers._active["radar"] == 0
    handlers._active["radar"] = 1
    rejected = query()
    asyncio.run(handlers._run(object(), rejected, timeout=1))
    assert rejected.cr_frame is None


def test_real_shared_browser_renderer_produces_png():
    from otae_bot.infrastructure.rendering.browser import close_browser

    async def run():
        try:
            png = await render_page(ranking_pages((ROW,), META)[0])
            with PILImage.open(BytesIO(png)) as image:
                assert image.format == "PNG"
                assert image.width == CARD_WIDTH
                assert 500 < image.height <= CARD_MAX_HEIGHT
        finally:
            await close_browser()

    asyncio.run(run())
