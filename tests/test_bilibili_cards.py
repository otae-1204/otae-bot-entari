from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageOps

from tests.test_core_logic import _bili_root_package, _load_bili_new_module

ROOT = Path(__file__).resolve().parents[1]


def _load_preview_script():
    spec = importlib.util.spec_from_file_location(
        "preview_bilibili_cards_for_test", ROOT / "scripts/preview_bilibili_cards.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def test_documentation_figure_is_reproducible_by_the_preview_script(tmp_path):
    """docs/images/bilibili-cards-sakura.png must be the script's own output.

    The figure is generated, so a hand-edited or stale copy would silently
    disagree with the card renderer. Regenerate with:
        python scripts/preview_bilibili_cards.py --write-doc-figure
    """
    preview = _load_preview_script()
    regenerated = tmp_path / "bilibili-cards-sakura.png"
    subprocess.run(
        [
            sys.executable,
            str(preview.__file__),
            "--output",
            str(tmp_path / "cards"),
            "--write-doc-figure",
            "--doc-figure",
            str(regenerated),
        ],
        check=True,
        cwd=ROOT,
        capture_output=True,
        timeout=180,
    )
    generated = Image.open(regenerated).convert("RGB")
    committed = Image.open(preview.DOC_FIGURE).convert("RGB")
    assert generated.size == committed.size, (
        "documentation figure size differs; regenerate it with "
        "scripts/preview_bilibili_cards.py --write-doc-figure"
    )
    difference = ImageChops.difference(generated, committed)
    assert difference.getbbox() is None, (
        "documentation figure differs from the preview script output; regenerate it "
        "with scripts/preview_bilibili_cards.py --write-doc-figure"
    )


@pytest.mark.parametrize(
    "kind,badge,expected",
    [("live_on", "RELABELLED", True), ("live_idle", "LIVE", False)],
)
def test_live_state_follows_card_type_not_badge_copy(renderer, kind, badge, expected):
    """Badge text is presentation only; subscription state comes from card_type."""
    api_module = _load_bili_new_module("api")
    models = sys.modules[_bili_root_package(api_module) + ".models"]
    client = api_module.BiliApi()
    misleading = models.BiliCard(kind, "同一直播间", badge=badge, published_at=1234)
    client.live_card = AsyncMock(return_value=misleading)
    latest = asyncio.run(
        client.latest_live_state(
            models.TargetInfo("live", "123", name="主播", room_id="456")
        )
    )
    assert latest.is_live is expected
    assert latest.live_last_seen_at == (1234 if expected else 0)


def test_whitespace_fields_do_not_crash(renderer):
    card = _card(renderer, title="   ", author="\n ", subtitle="\t ")
    image = Image.open(BytesIO(renderer._render_bili_card(card, None, None)))
    assert image.width == 900


@pytest.mark.parametrize(
    "status,kind", [(0, "live_idle"), (1, "live_on"), (2, "live_idle")]
)
def test_room_preview_does_not_claim_a_stream_just_ended(status, kind):
    api_module = _load_bili_new_module("api")
    models = sys.modules[_bili_root_package(api_module) + ".models"]
    client = api_module.BiliApi()
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
    # The transition rule now lives in the pure detect layer (refactor phase 4);
    # the assertions are the ones the service-level test made before.
    detect_module = _load_bili_new_module("detect")
    models = sys.modules[detect_module.__package__ + ".models"]
    target = models.TargetInfo(
        "live",
        "123",
        name="主播",
        room_id="456",
        is_live=was_live,
        last_cover="https://example.test/previous-cover.png",
    )
    observation = models.LiveObservation("123", room_id="456", is_live=is_live)
    updated, events = detect_module.detect_live(target, observation, 1234)
    assert [event.card.card_type for event in events] == [kind]
    card = events[0].card
    assert card.cover_url == target.last_cover
    assert updated.is_live is is_live
