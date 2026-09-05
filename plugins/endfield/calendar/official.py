from __future__ import annotations

import html
import re
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import urljoin, urlparse

from otae_bot.infrastructure.http.client import HttpResource, fetch_bytes, fetch_many


OFFICIAL_HOME_URL = "https://endfield.hypergryph.com/"
OFFICIAL_CDN_HOST = "web.hycdn.cn"
OFFICIAL_MEDIA_PATH = "/endfield/official-v4/_next/static/media/"
HTTP_NAMESPACE = "endfield-official-calendar"


class OfficialCalendarDiscoveryError(RuntimeError):
    """Raised when the current official calendar assets cannot be discovered safely."""


@dataclass(frozen=True, slots=True)
class OfficialCalendarAsset:
    key: str
    url: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class OfficialVersionCalendar:
    revision: str
    title: OfficialCalendarAsset
    timeline: OfficialCalendarAsset
    content: OfficialCalendarAsset

    @property
    def assets(self) -> tuple[OfficialCalendarAsset, ...]:
        return (self.title, self.timeline, self.content)


class OfficialVersionCalendarSource:
    """Discover the zh-CN calendar images embedded in the official site's current build."""

    async def current(self) -> OfficialVersionCalendar:
        homepage = await fetch_bytes(
            OFFICIAL_HOME_URL,
            namespace=HTTP_NAMESPACE,
            timeout_seconds=12.0,
            ttl_seconds=1800.0,
            max_bytes=2 * 1024 * 1024,
        )
        layout_url = discover_layout_chunk(homepage.content.decode("utf-8", errors="replace"))
        layout = await fetch_bytes(
            layout_url,
            namespace=HTTP_NAMESPACE,
            timeout_seconds=15.0,
            ttl_seconds=1800.0,
            max_bytes=4 * 1024 * 1024,
        )
        calendar = discover_zh_cn_calendar(layout.content.decode("utf-8", errors="replace"))
        resources = await fetch_many(
            (asset.url for asset in calendar.assets),
            namespace=HTTP_NAMESPACE,
            timeout_seconds=15.0,
            ttl_seconds=6 * 3600.0,
            max_bytes=4 * 1024 * 1024,
        )
        for asset in calendar.assets:
            validate_calendar_resource(asset, resources.get(asset.url))
        return calendar


def discover_layout_chunk(homepage_html: str) -> str:
    script_sources = re.findall(r'<script[^>]+src="([^"]+)"', homepage_html, flags=re.IGNORECASE)
    layout_sources = [
        html.unescape(source)
        for source in script_sources
        if "/chunks/app/" in source and "/layout-" in source and source.endswith(".js")
    ]
    if not layout_sources:
        raise OfficialCalendarDiscoveryError("官网首页未暴露 layout 构建文件")
    preferred = next(
        (source for source in layout_sources if "%5Blang%5D" in source or "[lang]" in source),
        layout_sources[-1],
    )
    return urljoin(OFFICIAL_HOME_URL, preferred)


def discover_zh_cn_calendar(layout_script: str) -> OfficialVersionCalendar:
    mapping_pattern = re.compile(
        r'"calendar\.title":n\((\d+)\)\.A\.src,'
        r'"calendar\.timeline":n\((\d+)\)\.A\.src,'
        r'"calendar\.content":n\((\d+)\)\.A\.src'
    )
    candidates = list(mapping_pattern.finditer(layout_script))
    if not candidates:
        raise OfficialCalendarDiscoveryError("官网构建文件缺少版本日历资源映射")

    resolved: tuple[OfficialCalendarAsset, OfficialCalendarAsset, OfficialCalendarAsset] | None = None
    for candidate in candidates:
        prefix = layout_script[max(0, candidate.start() - 1800):candidate.start()]
        nearby_ids = reversed(re.findall(r'n\((\d+)\)\.A\.src', prefix))
        locale_asset = next(
            (
                asset
                for module_id in nearby_ids
                if (asset := _extract_module_asset(layout_script, module_id, "locale")) is not None
                and _is_zh_cn_marker(asset.url)
            ),
            None,
        )
        if locale_asset is None:
            continue
        assets = tuple(
            _extract_required_module_asset(layout_script, module_id, key)
            for module_id, key in zip(
                candidate.groups(),
                ("calendar.title", "calendar.timeline", "calendar.content"),
                strict=True,
            )
        )
        resolved = assets
        break

    if resolved is None:
        raise OfficialCalendarDiscoveryError("官网构建文件中未找到简体中文日历资源")
    title, timeline, content = resolved
    revision = sha256(
        "\n".join(asset.url for asset in resolved).encode("utf-8")
    ).hexdigest()[:16]
    return OfficialVersionCalendar(revision, title, timeline, content)


def _extract_required_module_asset(
    script: str,
    module_id: str,
    key: str,
) -> OfficialCalendarAsset:
    asset = _extract_module_asset(script, module_id, key)
    if asset is None:
        raise OfficialCalendarDiscoveryError(f"官网日历模块 {module_id} 缺少素材信息")
    _validate_official_asset_url(asset.url)
    return asset


def _extract_module_asset(
    script: str,
    module_id: str,
    key: str,
) -> OfficialCalendarAsset | None:
    marker = re.search(rf"(?:^|[}},])\s*({re.escape(module_id)}):", script)
    if marker is None:
        return None
    position = marker.start(1)
    module_source = script[position:position + 2400]
    match = re.search(
        r'src:"([^"]+)",height:(\d+),width:(\d+)',
        module_source,
    )
    if match is None:
        return None
    return OfficialCalendarAsset(
        key=key,
        url=match.group(1),
        width=int(match.group(3)),
        height=int(match.group(2)),
    )


def _is_zh_cn_marker(url: str) -> bool:
    filename = urlparse(url).path.rsplit("/", 1)[-1].casefold()
    return filename.startswith(("zh-cn.", "zh_cn.", "zh-cn-"))


def _validate_official_asset_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != OFFICIAL_CDN_HOST:
        raise OfficialCalendarDiscoveryError(f"官网日历素材域名异常：{url}")
    if OFFICIAL_MEDIA_PATH not in parsed.path:
        raise OfficialCalendarDiscoveryError(f"官网日历素材路径异常：{url}")


def validate_calendar_resource(
    asset: OfficialCalendarAsset,
    resource: HttpResource | None,
) -> None:
    if resource is None:
        raise OfficialCalendarDiscoveryError(f"官网日历素材下载失败：{asset.key}")
    if not resource.content_type.startswith("image/"):
        raise OfficialCalendarDiscoveryError(
            f"官网日历素材类型异常：{asset.key}={resource.content_type}"
        )
    if len(resource.content) < 8_000:
        raise OfficialCalendarDiscoveryError(f"官网日历素材体积异常：{asset.key}")
    if asset.width < 1000 or asset.height < 40:
        raise OfficialCalendarDiscoveryError(
            f"官网日历素材尺寸异常：{asset.key}={asset.width}x{asset.height}"
        )
    if asset.key == "calendar.content" and asset.height < 1000:
        raise OfficialCalendarDiscoveryError("官网日历正文高度异常")
