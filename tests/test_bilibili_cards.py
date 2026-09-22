from __future__ import annotations

import asyncio
import sys
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageOps

from tests.test_core_logic import _load_bili_new_module


@pytest.fixture(scope="module")
def renderer():
    return _load_bili_new_module("draw")


def _png(image):
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _card(renderer, kind="video", **kwargs):
    models = sys.modules[renderer.__package__ + ".models"]
    return models.BiliCard(
        kind, kwargs.pop("title", "同一位主播的同一张封面"), **kwargs
    )


@pytest.mark.parametrize("size", [(1280, 720), (540, 960), (800, 800), (1600, 400)])
def test_cover_preserves_all_four_corners_and_aspect_ratio(renderer, size):
    source = Image.new("RGB", size, (210, 220, 230))
    draw = ImageDraw.Draw(source)
    width, height = size
    colors = [(241, 20, 20), (20, 201, 20), (20, 20, 241), (241, 201, 20)]
    for (x, y), color in zip(
        ((0, 0), (width - 40, 0), (0, height - 40), (width - 40, height - 40)), colors
    ):
        draw.rectangle((x, y, x + 39, y + 39), fill=color)

    decoded = renderer._decode_image(_png(source))
    assert decoded.size == size  # Catch destructive thumbnailing before layout.
    panel = renderer._fit_full_cover(decoded, 804)
    bounds = ImageChops.difference(
        panel, Image.new("RGB", panel.size, renderer.COVER_BG)
    ).getbbox()
    left, top, right, bottom = bounds
    assert abs((right - left) - (bottom - top) * width / height) <= 2
    assert panel.getpixel((left + 2, top + 2)) == colors[0]
    assert panel.getpixel((right - 3, top + 2)) == colors[1]
    assert panel.getpixel((left + 2, bottom - 3)) == colors[2]
    assert panel.getpixel((right - 3, bottom - 3)) == colors[3]

    # The complete fitted source must survive final composition without overlays.
    output = Image.open(
        BytesIO(renderer._render_bili_card(_card(renderer), _png(source), None))
    )
    assert (
        ImageChops.difference(
            output.crop((48, 264, 852, 264 + panel.height)), panel
        ).getbbox()
        is None
    )


@pytest.mark.parametrize("size", [(1, 4000), (4000, 1)])
def test_extreme_cover_ratio_still_renders(renderer, size):
    panel = renderer._fit_full_cover(Image.new("RGB", size, "black"), 804)
    assert panel.width == 804
    assert 240 <= panel.height <= 640


def test_decode_honors_orientation_and_transparency(renderer):
    source = Image.new("RGB", (40, 80), "red")
    exif = source.getexif()
    exif[274] = 6
    buffer = BytesIO()
    source.save(buffer, format="JPEG", exif=exif)
    assert renderer._decode_image(buffer.getvalue()).size == (80, 40)
    transparent = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    assert renderer._decode_image(_png(transparent)).getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize(
    "kind,label",
    [
        ("video", "视频"),
        ("live_on", "已开播"),
        ("live_off", "已下播"),
        ("live_idle", "未开播"),
        ("dynamic", "动态"),
    ],
)
def test_primary_status_is_large_left_aligned_and_not_controlled_by_badge(
    renderer, monkeypatch, kind, label
):
    texts = []
    original = ImageDraw.ImageDraw.text

    def capture(draw, xy, text, **kwargs):
        texts.append((xy, text, kwargs.get("font")))
        return original(draw, xy, text, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    renderer._render_bili_card(
        _card(renderer, kind, badge="INCORRECT LEGACY BADGE"), None, None
    )
    xy, _, font = next(entry for entry in texts if entry[1] == label)
    assert xy[0] < 900 / 3 and xy[1] < 100
    assert font.size >= 40
    assert all(text != "INCORRECT LEGACY BADGE" for _, text, _ in texts)


def test_live_states_remain_distinct_in_grayscale_with_identical_cover(renderer):
    cover = _png(Image.new("RGB", (1280, 720), (100, 120, 140)))
    images = [
        Image.open(
            BytesIO(renderer._render_bili_card(_card(renderer, kind), cover, None))
        )
        for kind in ("video", "live_on", "live_off")
    ]
    headers = [ImageOps.grayscale(image.crop((18, 18, 882, 154))) for image in images]
    for left, right in ((0, 1), (0, 2), (1, 2)):
        assert (
            ImageChops.difference(headers[left], headers[right]).getbbox() is not None
        )
        assert (
            ImageChops.difference(
                images[left].crop((48, 264, 852, 716)),
                images[right].crop((48, 264, 852, 716)),
            ).getbbox()
            is None
        )


@pytest.mark.parametrize(
    "text", ["正好显示", "超长中文标题" * 80, "W" * 300, "中 English 混排 " * 80]
)
def test_wrapping_reserves_space_for_ellipsis(renderer, text):
    lines = renderer._wrap(text, renderer.FONT_TITLE, 250, max_lines=3)
    assert 1 <= len(lines) <= 3
    assert all(renderer.FONT_TITLE.getlength(line) <= 250 for line in lines)
    if len(text) > 100:
        assert lines[-1].endswith("…")
    else:
        assert "".join(lines) == text


@pytest.mark.parametrize("cover", [None, b"bad image"])
def test_long_text_and_failed_images_have_no_text_overlap(renderer, monkeypatch, cover):
    boxes = []
    original = ImageDraw.ImageDraw.text

    def capture(draw, xy, text, **kwargs):
        boxes.append(
            draw.textbbox(
                xy, text, font=kwargs.get("font"), anchor=kwargs.get("anchor")
            )
        )
        return original(draw, xy, text, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    card = _card(
        renderer,
        title="超长标题中英混排 LONG TITLE " * 40,
        author="非常长的主播名字" * 40,
        description="简介也很长，需要自动换行和截断。" * 50,
        uid="1234567890" * 8,
        url="https://www.bilibili.com/video/" + "A" * 200,
    )
    image = Image.open(BytesIO(renderer._render_bili_card(card, cover, b"bad avatar")))
    for left, top, right, bottom in boxes:
        assert 18 <= left < right <= image.width - 18
        assert 18 <= top < bottom <= image.height - 18
    for index, a in enumerate(boxes):
        for b in boxes[index + 1 :]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]


def test_whitespace_fields_do_not_crash(renderer):
    card = _card(renderer, title="   ", author="\n ", subtitle="\t ")
    image = Image.open(BytesIO(renderer._render_bili_card(card, None, None)))
    assert image.width == 900


@pytest.mark.parametrize(
    "status,kind", [(0, "live_idle"), (1, "live_on"), (2, "live_idle")]
)
def test_room_preview_does_not_claim_a_stream_just_ended(status, kind):
    client_module = _load_bili_new_module("client")
    models = sys.modules[client_module.__package__ + ".models"]
    client = client_module.BiliClient()
    client._live_room = AsyncMock(
        return_value={"live_status": status, "title": "同一直播间"}
    )
    target = models.TargetInfo("live", "123", name="主播", room_id="456")
    card = asyncio.run(client.live_card(target))
    latest = asyncio.run(client.latest_live_state(target))
    assert card.card_type == kind
    assert latest.is_live == (status == 1)


@pytest.mark.parametrize(
    "was_live,is_live,kind", [(False, True, "live_on"), (True, False, "live_off")]
)
def test_subscription_transitions_still_send_start_and_end_notifications(
    was_live, is_live, kind
):
    service_module = _load_bili_new_module("service")
    models = sys.modules[service_module.__package__ + ".models"]
    target = models.TargetInfo(
        "live",
        "123",
        name="主播",
        room_id="456",
        is_live=was_live,
        last_cover="https://example.test/previous-cover.png",
    )
    latest = models.TargetInfo("live", "123", room_id="456", is_live=is_live)
    client = SimpleNamespace(latest_live_state=AsyncMock(return_value=latest))
    store = SimpleNamespace(upsert_target=Mock())
    service = service_module.BiliService(store, client)
    service.broadcast = AsyncMock()
    asyncio.run(service._check_live_target(target))
    card = service.broadcast.await_args.args[2]
    assert card.card_type == kind
    assert card.cover_url == target.last_cover
    store.upsert_target.assert_called_once_with(latest)
