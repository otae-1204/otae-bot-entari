"""Matrix source identity, missing evidence, single-image output and real rendering."""

import asyncio
import re
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lxml import html
from PIL import Image

from plugins.radar.config import RadarConfig
from plugins.radar.errors import UpstreamUnavailable
from plugins.radar.matrix import (
    IQ_MISSING_COLOR,
    MATRIX_MAX_HEIGHT,
    MATRIX_WIDTH,
    iq_color,
    matrix_pages,
    matrix_text,
)
from plugins.radar.models import (
    SCORING_CONTINUOUS,
    EfficiencyPayload,
    EfficiencyPoint,
    HistorySeries,
    MatrixModel,
    RadarMatrix,
    RadarMeta,
    TrendPoint,
)
from plugins.radar.rendering import DEVICE_SCALE_FACTOR, render_page
from plugins.radar.service import RadarService

META = RadarMeta("deep-swe", mode="equal_latest_3")
LOW = EfficiencyPoint(
    "astra", "low", 98.3, 89, 136, average_price_usd=1.96, average_minutes=8.6
)
HIGH = replace(LOW, effort="high", iq=108.2, average_price_usd=3.14)
POINT = TrendPoint("2026-09-14T09:00:00Z", 105.4, 807)


def test_matrix_keeps_family_history_separate_from_effort_scores():
    client = SimpleNamespace(
        efficiency=AsyncMock(return_value=EfficiencyPayload(META, (LOW, HIGH))),
        history=AsyncMock(
            return_value=(
                HistorySeries("astra", "astra", None, (POINT,)),
                HistorySeries("astra@low", "astra", "low", (replace(POINT, iq=40),)),
                HistorySeries(
                    "latest:astra", "astra", None, (replace(POINT, iq=10),), latest=True
                ),
            )
        ),
    )
    snapshot = asyncio.run(
        RadarService(client, RadarConfig()).model_matrix(benchmark="deep-swe")
    )
    model = snapshot.models[0]
    assert [point.effort for point in model.tiers] == ["high", "low"]
    assert model.history[-1].iq == 105.4
    assert model.tiers[0].average_price_usd == 3.14
    assert model.tiers[1].average_price_usd == 1.96
    client.efficiency.assert_awaited_once_with("deep-swe")
    client.history.assert_awaited_once_with("deep-swe")


def test_history_outage_preserves_tiles_without_inventing_family_iq():
    client = SimpleNamespace(
        efficiency=AsyncMock(return_value=EfficiencyPayload(META, (LOW,))),
        history=AsyncMock(side_effect=UpstreamUnavailable()),
    )
    snapshot = asyncio.run(RadarService(client, RadarConfig()).model_matrix())
    document = html.fromstring(matrix_pages(snapshot)[0].body)
    assert "—IQ" in document.xpath('//div[@class="family-score"]')[0].text_content()
    assert document.xpath('//section[@class="tier"]')
    assert "暂无跨档位历史" in document.text_content()


def test_all_models_and_efforts_fit_one_long_image():
    models = tuple(
        MatrixModel(f"model-{i}", (replace(LOW, model=f"model-{i}"), HIGH), (POINT,))
        for i in range(8)
    )
    pages = matrix_pages(RadarMatrix(META, models))
    docs = [html.fromstring(page.body) for page in pages]
    assert len(pages) == 1
    assert [
        len(doc.xpath('//section[starts-with(@class,"model-panel")]')) for doc in docs
    ] == [8]
    assert sum(len(doc.xpath('//section[@class="tier"]')) for doc in docs) == 16
    assert pages[0].number == pages[0].total == 1
    assert pages[0].layout == "matrix"


def test_long_overview_splits_into_domestic_and_international_pages():
    # Domestic and international vendors, enough models to exceed one page.
    names = (
        "gpt-6-astra",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "claude-sonnet-5",
        "claude-opus-5",
        "gemini-3.7-flash",
        "gemini-3.8-flash",
        "grok-4.6",
        "deepseek-v4-flash",
        "dsh-deepseek-v4-flash",
        "glm-5.3",
        "glm-5.3-flash",
        "k3",
        "hy4-preview",
    )
    models = tuple(
        MatrixModel(name, (replace(HIGH, model=name),), (POINT,)) for name in names
    )
    pages = matrix_pages(RadarMatrix(META, models))
    assert len(pages) == 2
    assert [page.number for page in pages] == [1, 2]
    assert all(page.total == 2 for page in pages)
    assert [page.section for page in pages] == ["国外模型", "国内模型"]
    docs = [html.fromstring(page.body) for page in pages]
    assert [doc.xpath("//section/@data-region") for doc in docs] == [
        ["international"],
        ["domestic"],
    ]
    # Every model survives the split exactly once.
    rendered = [
        name for doc in docs for name in doc.xpath('//section[@data-model]/@data-model')
    ]
    assert sorted(rendered) == sorted(names)
    assert "本页 01/02" in docs[0].text_content()
    assert "本页 02/02" in docs[1].text_content()


def test_region_larger_than_budget_splits_by_vendor_without_cutting_a_group():
    names = tuple(f"gpt-{i}" for i in range(10)) + tuple(f"claude-{i}" for i in range(6))
    models = tuple(MatrixModel(name, (LOW,), ()) for name in names)
    pages = matrix_pages(RadarMatrix(META, models))
    assert len(pages) > 1
    docs = [html.fromstring(page.body) for page in pages]
    assert all(
        doc.xpath("//section/@data-region") == ["international"] for doc in docs
    )
    # A vendor group is never broken across pages.
    for doc in docs:
        for vendor in doc.xpath('//section[@data-vendor]'):
            models_in_group = len(vendor.xpath(".//section[@data-model]"))
            assert models_in_group in {10, 6}
    rendered = [
        name for doc in docs for name in doc.xpath('//section[@data-model]/@data-model')
    ]
    assert sorted(rendered) == sorted(names)


def test_region_and_vendor_groups_reunite_shuffled_models_and_harness_variants():
    names = (
        "glm-5.3",
        "claude-sonnet-5",
        "gpt-5.6-sol",
        "dsh-deepseek-v4-flash",
        "k3",
        "grok-4.6",
        "gpt-6-astra",
        "hy4-preview",
        "gemini-3.8-flash",
        "deepseek-v4-flash",
        "glm-5.3-flash",
        "unfamiliar-model",
    )
    models = tuple(
        MatrixModel(name, (replace(HIGH, model=name),), (POINT,)) for name in names
    )
    snapshot = RadarMatrix(META, models)
    pages = matrix_pages(snapshot)
    assert len(pages) == 1
    document = html.fromstring(pages[0].body)
    regions = document.xpath('//section[@class="region-section"]')
    assert [region.get("data-region") for region in regions] == [
        "international",
        "domestic",
        "unclassified",
    ]
    grouped = [
        [
            (vendor.get("data-vendor"), vendor.xpath(".//section/@data-model"))
            for vendor in region.xpath(".//section[@data-vendor]")
        ]
        for region in regions
    ]
    assert grouped == [
        [
            ("openai", ["gpt-5.6-sol", "gpt-6-astra"]),
            ("anthropic", ["claude-sonnet-5"]),
            ("google", ["gemini-3.8-flash"]),
            ("xai", ["grok-4.6"]),
        ],
        [
            ("deepseek", ["dsh-deepseek-v4-flash", "deepseek-v4-flash"]),
            ("zhipu", ["glm-5.3", "glm-5.3-flash"]),
            ("moonshot", ["k3"]),
            ("tencent", ["hy4-preview"]),
        ],
        [("unknown", ["unfamiliar-model"])],
    ]
    text_names = [
        line.split(" · ")[0]
        for line in matrix_text(snapshot)
        if " · 跨档位 IQ " in line
    ]
    assert text_names == document.xpath("//section/@data-model")
    assert sorted(text_names) == sorted(names)
    assert [model.model for model in snapshot.models] == list(names)
    assert len(document.xpath('//section[@class="tier"]')) == len(names)


def test_future_family_version_can_group_without_guessing_unknown_vendors():
    names = ("dsh-deepseek-future", "gpt-future", "my-claude-wrapper", "unknown-model")
    snapshot = RadarMatrix(META, tuple(MatrixModel(name, (LOW,)) for name in names))
    document = html.fromstring(matrix_pages(snapshot)[0].body)
    assert document.xpath(
        '//section[@data-vendor="deepseek"]//section/@data-model'
    ) == ["dsh-deepseek-future"]
    assert document.xpath('//section[@data-vendor="openai"]//section/@data-model') == [
        "gpt-future"
    ]
    assert document.xpath(
        '//section[@data-region="unclassified"]//section/@data-model'
    ) == ["my-claude-wrapper", "unknown-model"]


def test_continuous_metrics_and_zero_cost_are_not_missing_or_pass_counts():
    meta = replace(
        META, benchmark_id="pompeii-adjacency", scoring_mode=SCORING_CONTINUOUS
    )
    point = replace(
        LOW, passed=49.79, total=55, average_price_usd=0, average_minutes=None
    )
    page = matrix_pages(RadarMatrix(meta, (MatrixModel("astra", (point,)),)))[0]
    document = html.fromstring(page.body)
    body = document.text_content()
    assert "F1 0.905" in body and "通过" not in body
    assert "~$0.00" in body
    assert "—分钟" in body and "~0.0分钟" not in body


def test_stale_history_stays_visible_and_labels_are_escaped():
    model = MatrixModel(
        '<img src="https://example.invalid/">', (LOW,), (POINT,), history_stale=True
    )
    snapshot = RadarMatrix(META, (model,))
    document = html.fromstring(matrix_pages(snapshot)[0].body)
    icons = document.xpath("//img")
    assert len(icons) == 1
    assert icons[0].get("data-model-icon") == "generic"
    assert icons[0].get("src") is None
    assert "历史缓存可能过期" in document.text_content()
    assert "历史数据可能过期" in "\n".join(matrix_text(snapshot))


def test_empty_matrix_is_explicit():
    page = matrix_pages(RadarMatrix(META))[0]
    assert "该频道暂无模型档位数据" in page.body
    assert page.number == page.total == 1


def test_history_axes_show_adaptive_iq_and_utc_time_with_proportional_coordinates():
    history = (
        TrendPoint("2026-09-14T08:00:00+08:00", 0, 40),
        TrendPoint("2026-09-14T01:00:00Z", 100, 50),
        TrendPoint("2026-09-14T04:00:00Z", 150, 60),
    )
    snapshot = RadarMatrix(META, (MatrixModel("astra", (LOW,), history),))
    document = html.fromstring(matrix_pages(snapshot)[0].body)
    # The window spans the data (0–150) so it lands on the fixed full domain.
    assert document.xpath('//span[@class="spark-y-tick"]/text()') == [
        "0",
        "50",
        "100",
        "150",
    ]
    ticks = document.xpath('//span[@class="spark-x-tick"]/text()')
    assert ticks == ["00:00", "01:00", "02:00", "03:00", "04:00"]
    dots = document.xpath('//span[@class="spark-point"]')
    positions = [
        dict(re.findall(r"(left|top):([\d.]+)%", dot.get("style"))) for dot in dots
    ]
    # One hour is a quarter of the four-hour window, not half the sample count.
    assert [float(position["left"]) for position in positions] == [0, 25, 100]
    assert float(positions[0]["top"]) == 100
    assert float(positions[1]["top"]) == pytest.approx(100 / 3, abs=0.0001)
    assert float(positions[2]["top"]) == 0
    assert "09-14 00:00 — 09-14 04:00 UTC" in document.text_content()
    assert "纵轴 0–150" in document.text_content()
    assert "UTC" in document.xpath('//div[@class="history-label"]')[0].text_content()
    assert not document.xpath('//div[@class="model-warning"]')


def test_narrow_history_zooms_the_axis_and_labels_the_real_range():
    # A 2-IQ band would be invisible on a fixed 0–150 axis.
    history = (
        replace(POINT, timestamp="2026-09-14T00:00:00Z", iq=90.0),
        replace(POINT, timestamp="2026-09-14T04:00:00Z", iq=92.0),
    )
    snapshot = RadarMatrix(META, (MatrixModel("astra", (LOW,), history),))
    document = html.fromstring(matrix_pages(snapshot)[0].body)
    ticks = [float(value) for value in document.xpath('//span[@class="spark-y-tick"]/text()')]
    assert ticks == [90.0, 91.0, 92.0]
    assert (max(ticks) - min(ticks)) < 150
    # The real window is disclosed, never implied.
    assert "纵轴 89.5–92.5" in document.text_content()
    # A 2-IQ band now fills most of the plot instead of collapsing onto a
    # single line: on a fixed 0–150 axis these two points differ by ~1.3%.
    tops = [
        float(dict(re.findall(r"(left|top):([\d.]+)%", dot.get("style")))["top"])
        for dot in document.xpath('//span[@class="spark-point"]')
    ]
    assert tops[0] == pytest.approx(83.3333, abs=0.001)
    assert tops[1] == pytest.approx(16.6667, abs=0.001)
    assert abs(tops[0] - tops[1]) > 50
    # Padding keeps the extremes off the frame rather than clipped to it.
    assert 0 < min(tops) and max(tops) < 100


def test_multiday_history_uses_utc_calendar_ticks_and_keeps_exact_range():
    history = (
        replace(POINT, timestamp="2026-09-07T10:00:34Z"),
        replace(POINT, timestamp="2026-09-14T09:00:54Z"),
    )
    snapshot = RadarMatrix(META, (MatrixModel("astra", (LOW,), history),))
    document = html.fromstring(matrix_pages(snapshot)[0].body)
    ticks = document.xpath('//span[@class="spark-x-tick"]')
    assert 2 <= len(ticks) <= 5
    assert all(tick.get("title").endswith("00:00 UTC") for tick in ticks)
    assert all(re.fullmatch(r"\d\d-\d\d", tick.text) for tick in ticks)
    assert "09-07 10:00 — 09-14 09:00 UTC" in document.text_content()


def test_single_history_point_is_a_dot_with_an_explicit_warning():
    snapshot = RadarMatrix(META, (MatrixModel("astra", (LOW,), (POINT,)),))
    document = html.fromstring(matrix_pages(snapshot)[0].body)
    path = document.xpath('//path[@class="spark-path"]/@d')[0]
    assert path.startswith("M") and "L" not in path
    assert len(document.xpath('//span[@class="spark-last"]')) == 1
    assert len(document.xpath('//span[@data-isolated="true"]')) == 1
    assert len(document.xpath('//span[@class="spark-x-tick"]')) == 1
    assert "仅 1 个有效历史点，暂不能判断趋势" in document.text_content()
    assert "仅 1 个有效历史点" in "\n".join(matrix_text(snapshot))


def test_invalid_history_breaks_the_line_and_cannot_become_the_headline():
    history = (
        POINT,
        replace(POINT, timestamp="2026-09-14T10:00:00Z", iq=float("nan")),
        replace(POINT, timestamp="2026-09-14T11:00:00Z", iq=80),
        replace(POINT, timestamp="bad timestamp", iq=120),
    )
    snapshot = RadarMatrix(META, (MatrixModel("astra", (LOW,), history),))
    document = html.fromstring(matrix_pages(snapshot)[0].body)
    paths = document.xpath('//path[@class="spark-path"]/@d')
    assert len(paths) == 2 and all(
        path.startswith("M") and "L" not in path for path in paths
    )
    assert len(document.xpath('//span[@class="spark-point"]')) == 2
    assert len(document.xpath('//span[@data-isolated="true"]')) == 2
    assert not document.xpath('//span[@class="spark-last"]')
    assert "—IQ" in document.xpath('//div[@class="family-score"]')[0].text_content()
    assert "最新历史点无效" in document.text_content()
    assert "2 条无效历史记录已略过" in document.text_content()
    assert "跨档位 IQ —" in "\n".join(matrix_text(snapshot))


@pytest.mark.parametrize(
    ("samples", "iq", "warning"),
    [
        (None, 90, "样本量缺失"),
        (0, None, "暂无有效样本"),
        (1, 0, "样本偏少"),
        (29, 90, "样本偏少"),
        (30, 90, None),
        (100, None, "IQ 缺失"),
    ],
)
def test_sample_warnings_distinguish_missing_zero_small_and_available(
    samples, iq, warning
):
    tier = replace(LOW, total=samples, iq=iq, passed=0)
    history = (POINT, replace(POINT, timestamp="2026-09-14T10:00:00Z"))
    snapshot = RadarMatrix(META, (MatrixModel("astra", (tier,), history),))
    document = html.fromstring(matrix_pages(snapshot)[0].body)
    tile = document.xpath('//section[@class="tier"]')[0]
    assert tile.get("data-evidence") == ("limited" if warning else "available")
    if warning:
        assert warning in tile.text_content()
        assert "1 个档位数据不足" in document.text_content()
        assert warning in "\n".join(matrix_text(snapshot))
    else:
        assert not document.xpath('//div[@class="model-warning"]')
    if iq == 0:
        assert tile.xpath('.//div[@class="tier-score"]/b/text()') == ["0"]
    assert "前端提醒线，非上游统计判定" in "\n".join(matrix_text(snapshot))


def test_history_sample_warning_is_based_on_latest_point_not_history_sum():
    history = (POINT, replace(POINT, timestamp="2026-09-14T10:00:00Z", samples=2))
    snapshot = RadarMatrix(META, (MatrixModel("astra", (LOW,), history),))
    document = html.fromstring(matrix_pages(snapshot)[0].body)
    assert "历史末点样本偏少（n=2）" in document.text_content()
    assert not document.xpath('//div[@class="tier-warning"]')


def test_iq_color_uses_score_across_models_and_efforts_without_recomputing_average():
    # Keep the aggregate at 105.4 even when effort scores would average differently.
    tiers = (
        replace(LOW, iq=105.4),
        replace(HIGH, iq=105.4),
        replace(HIGH, effort="max", iq=20),
    )
    snapshot = RadarMatrix(META, (MatrixModel("astra", tiers, (POINT,)),))
    document = html.fromstring(matrix_pages(snapshot)[0].body)
    scores = document.xpath('//div[@class="tier-score"]/b')
    headline = document.xpath('//div[@class="family-score"]/strong')[0]
    assert headline.get("data-iq") == "105.4"
    assert headline.get("style") == scores[0].get("style") == scores[1].get("style")
    assert scores[2].get("style") != scores[0].get("style")


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -1, 151])
def test_missing_or_invalid_iq_is_neutral_not_a_low_score(value):
    assert iq_color(value) == IQ_MISSING_COLOR
    assert iq_color(0) != IQ_MISSING_COLOR


def test_iq_palette_runs_red_to_green_and_is_continuous():
    def channels(value):
        color = iq_color(value)
        return tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))

    low, high = channels(0), channels(150)
    assert low[0] > low[1] and high[1] > high[0]
    # No categorical jumps at an anchor or integer display-rounding boundary.
    for boundary in (*range(15, 150, 15), 98.5):
        assert (
            max(
                abs(a - b)
                for a, b in zip(channels(boundary - 0.001), channels(boundary + 0.001))
            )
            <= 1
        )
    assert channels(90.1) != channels(90.4)


def test_shared_renderer_keeps_each_page_in_one_png_at_2x():
    from otae_bot.infrastructure.rendering.browser import close_browser

    async def run():
        try:
            models = tuple(
                MatrixModel(name, (HIGH, LOW), (POINT,))
                for name in ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.5")
                + tuple(f"model-{i}" for i in range(8))
            )
            pages = matrix_pages(RadarMatrix(META, models))
            assert len(pages) == 1
            png = await render_page(pages[0])
            with Image.open(BytesIO(png)) as image:
                assert image.width == MATRIX_WIDTH * DEVICE_SCALE_FACTOR
                assert 2000 < image.height <= MATRIX_MAX_HEIGHT * DEVICE_SCALE_FACTOR
        finally:
            await close_browser()

    asyncio.run(run())
