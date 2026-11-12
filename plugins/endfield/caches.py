"""终末地插件的进程内缓存、素材代次与 /ef dev 缓存维护；缓存对象只在这里创建。"""

from __future__ import annotations

import json

from otae_bot.infrastructure.cache import AsyncTTLCache, CacheStats
from otae_bot.infrastructure.http.assets import broker as asset_broker
from otae_bot.infrastructure.http.client import (
    clear_http_cache,
    get_http_cache_diagnostics,
    get_http_cache_stats,
)
from otae_bot.infrastructure.rendering.browser import browser_queue_snapshot

from .account.challenge.i18n import clear_challenge_locale
from .account.detail.names import clear_account_detail_name_map
from .account.investment.service import clear_account_investment_catalog
from .providers.akedata import clear_i18n_process_warm
from .rendering.cards import clear_render_asset_caches
from .state import calendar_source, gacha_asset_cache, official_client, service, stage_service


# 成品图缓存（资料卡、配装、账号页、挑战页）。key 已含视图内容 digest、渲染版本
# （账号页 / 挑战页还含素材代次），内容一变 key 就变；TTL 只决定同一结果能复用多久。
# 素材代次变化或 /ef dev clear 时整体失效。个人原始数据只在 _CARD_DETAIL_CACHE（60 秒，
# key 含凭据指纹）与 _CHALLENGE_DATA_CACHE（60 秒）里短暂复用，不落盘。
RENDER_CACHE_TTL_SECONDS = 30 * 60.0
CARD_CACHE_TTL_SECONDS = RENDER_CACHE_TTL_SECONDS
CARD_CACHE_MAX_BYTES = 48 * 1024 * 1024
CARD_RENDER_VERSION = "endfield-card-v58"
CardCacheKey = tuple[str, str, str, str, str, str, str]
_CARD_CACHE: AsyncTTLCache[CardCacheKey, tuple[bytes, ...]] = AsyncTTLCache(
    ttl_seconds=CARD_CACHE_TTL_SECONDS,
    max_bytes=CARD_CACHE_MAX_BYTES,
    max_entries=64,
    # A card can render as several images, so bound the cache on total bytes, not page count.
    sizeof=lambda pages: sum(len(page) for page in pages),
)
_LOADOUT_CACHE: AsyncTTLCache[tuple[str, str], bytes] = AsyncTTLCache(
    ttl_seconds=RENDER_CACHE_TTL_SECONDS, max_bytes=24 * 1024 * 1024, max_entries=32, sizeof=len,
)
_ACCOUNT_PAGE_CACHE: AsyncTTLCache[tuple[str, ...], tuple[bytes, ...]] = AsyncTTLCache(
    ttl_seconds=RENDER_CACHE_TTL_SECONDS, max_bytes=32 * 1024 * 1024, max_entries=24,
    sizeof=lambda pages: sum(map(len, pages)),
)
_ASSET_GENERATION = 0
_CALENDAR_CACHE: AsyncTTLCache[str, bytes] = AsyncTTLCache(
    ttl_seconds=600.0,
    max_bytes=8 * 1024 * 1024,
    max_entries=4,
    sizeof=len,
)
# 挑战的原始个人 API 数据只复用 60 秒；渲染结果按成品图 TTL。
CHALLENGE_CACHE_TTL_SECONDS = 60.0
ChallengeCacheKey = tuple
_CHALLENGE_DATA_CACHE: AsyncTTLCache[tuple[str, str, str], dict] = AsyncTTLCache(
    ttl_seconds=CHALLENGE_CACHE_TTL_SECONDS,
    max_bytes=12 * 1024 * 1024,
    max_entries=16,
    sizeof=lambda payload: len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
)
_CHALLENGE_RENDER_CACHE: AsyncTTLCache[ChallengeCacheKey, tuple[bytes, ...]] = AsyncTTLCache(
    ttl_seconds=RENDER_CACHE_TTL_SECONDS,
    max_bytes=48 * 1024 * 1024,
    max_entries=64,
    sizeof=lambda pages: sum(len(page) for page in pages),
)
# 同一凭据同一角色的 card/detail 60 秒内复用（账号 / 基建 / 养成 / 探索 / 日常连查），
# 仅进程内存。key 是（凭据指纹, role_id, server_id），不含 token 原文；不挂 on_event，
# 免得 key 流进任何回调。
CARD_DETAIL_CACHE_TTL_SECONDS = 60.0
CardDetailCacheKey = tuple[str, str, str]
_CARD_DETAIL_CACHE: AsyncTTLCache[CardDetailCacheKey, dict] = AsyncTTLCache(
    ttl_seconds=CARD_DETAIL_CACHE_TTL_SECONDS,
    max_bytes=16 * 1024 * 1024,
    max_entries=64,
    sizeof=lambda detail: len(
        json.dumps(detail, ensure_ascii=False, separators=(",", ":"), default=str)
    ),
)


def _normalize_cache_scope(value: str) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"all", "全部"}:
        return "all"
    if normalized in {"operator", "op", "干员"}:
        return "operator"
    if normalized in {"weapon", "wp", "武器"}:
        return "weapon"
    if normalized in {"equipment", "equip", "eq", "装备"}:
        return "equipment"
    if normalized in {"stage", "stages", "关卡", "副本"}:
        return "stage"
    if normalized in {"icon", "icons", "图标", "素材"}:
        return "icon"
    return None


async def _clear_endfield_caches(scope: str) -> int:
    global _ASSET_GENERATION
    # 个人 card/detail 体积小，任何清缓存入口都顺带清掉。
    removed = await _CARD_DETAIL_CACHE.clear()
    if scope in {"all", "icon", "operator", "weapon", "equipment", "stage"}:
        _ASSET_GENERATION += 1
        removed += await _ACCOUNT_PAGE_CACHE.clear()
    if scope == "all":
        removed += clear_render_asset_caches()
        removed += gacha_asset_cache.clear_caches()
        removed += await _CARD_CACHE.clear()
        removed += await _LOADOUT_CACHE.clear()
        removed += await _CALENDAR_CACHE.clear()
        removed += await _CHALLENGE_DATA_CACHE.clear()
        removed += await _CHALLENGE_RENDER_CACHE.clear()
        removed += await service.clear_query_caches()
        removed += await clear_http_cache("endfield-")
        removed += await clear_http_cache("akedata")
        clear_i18n_process_warm()
        removed += stage_service.clear_caches()
        removed += calendar_source.clear_caches()
        removed += clear_account_detail_name_map()
        removed += clear_account_investment_catalog()
        removed += clear_challenge_locale()
        _forget_asset_prefetch()
    elif scope == "icon":
        removed += clear_render_asset_caches()
        removed += gacha_asset_cache.clear_caches()
        removed += await _CARD_CACHE.clear()
        removed += await _LOADOUT_CACHE.clear()
        removed += await _CALENDAR_CACHE.clear()
        removed += await _CHALLENGE_RENDER_CACHE.clear()
        removed += await clear_http_cache("endfield-assets")
        removed += await clear_http_cache("endfield-account")
        removed += await clear_http_cache("endfield-gacha")
        removed += await clear_http_cache("endfield-official-calendar")
        _forget_asset_prefetch()
    elif scope in {"operator", "weapon", "equipment", "stage"}:
        removed += await _LOADOUT_CACHE.clear()
        removed += await service.clear_query_caches()
        cache_kinds = (
            {scope, "equipment_catalog", "equipment_attribute"}
            if scope == "equipment" else {scope, f"{scope}_catalog"}
        )
        removed += await _CARD_CACHE.clear(lambda key: key[1] in cache_kinds)
        removed += await clear_http_cache("endfield-api")
        removed += await clear_http_cache("akedata")
        clear_i18n_process_warm()
        removed += clear_account_detail_name_map()
        removed += clear_account_investment_catalog()
        if scope == "stage":
            removed += stage_service.clear_caches()
    return removed


def _forget_asset_prefetch() -> None:
    """After clearing images: drop the restart hot set and queued prefetches (refills keep going)."""
    asset_broker.hotset.clear()
    asset_broker.cancel_background()


async def _cache_status_lines() -> list[str]:
    api_stats = await get_http_cache_stats("endfield-api")
    asset_stats = await get_http_cache_stats("endfield-assets")
    table_stats = await get_http_cache_stats("akedata")
    card_stats = await _CARD_CACHE.stats()
    loadout_stats = await _LOADOUT_CACHE.stats()
    account_stats = await _ACCOUNT_PAGE_CACHE.stats()
    detail_stats = await _CARD_DETAIL_CACHE.stats()
    diagnostics = await get_http_cache_diagnostics()
    return [
        _format_cache_stats("API", api_stats),
        _format_cache_stats("AKEData", table_stats),
        _format_cache_stats("远程素材", asset_stats),
        _format_cache_stats("成品卡片", card_stats),
        _format_cache_stats("配装卡片", loadout_stats),
        _format_cache_stats("账号卡片", account_stats),
        _format_cache_stats("账号数据", detail_stats),
        _format_memory_pools(diagnostics),
        *_format_asset_queue(diagnostics.get("broker")),
        _format_community_queue(official_client.community_context_metrics()),
        _format_browser_queue(browser_queue_snapshot()),
        f"缓存策略: 成品图 TTL {int(RENDER_CACHE_TTL_SECONDS)}s / 下载并发 8",
    ]


def _format_browser_queue(snapshot: dict) -> str:
    """Render slots in use, renders waiting by priority, slot count and pooled browser contexts."""
    waiting = snapshot["waiting"]
    return (
        f"浏览器: 在途 {snapshot['inflight']} / 排队 交互 {waiting['interactive']} 后台 {waiting['background']} / "
        f"并发 {snapshot['concurrency']} / context {snapshot['contexts']}"
    )


def _format_community_queue(metrics: dict) -> str:
    """Community credential exchanges and their queueing: counters and durations only."""
    def seconds(key: str) -> str:
        return f"{metrics.get(key, 0) / 1000:.1f}s"

    return (
        f"社区换签: 命中 {metrics.get('cache_hits', 0)} / 换签 {metrics.get('exchange_attempts', 0)}"
        f"（失败 {metrics.get('exchange_failed', 0)}，熔断拒绝 {metrics.get('circuit_rejections', 0)}）/ "
        f"等待锁 {metrics.get('lock_waits', 0)} 次 共 {seconds('lock_wait_ms')} / "
        f"等待间隔 {metrics.get('interval_waits', 0)} 次 共 {seconds('interval_wait_ms')} / "
        f"换签耗时 {seconds('exchange_ms')}"
    )


def _format_asset_queue(snapshot: dict | None) -> list[str]:
    """In-flight and queued asset downloads by priority, and today's prefetch traffic."""
    if not snapshot:
        return []
    waiting = snapshot["waiting"]
    prefetch = snapshot["prefetch"]
    mib = 1024 * 1024
    return [
        f"素材队列: 在途 {snapshot['inflight']} / 排队 交互 {waiting['interactive']} "
        f"续传 {waiting['refill']} 后台 {waiting['background']} / 续传中 {snapshot['refill']} / "
        f"今日预取 {prefetch['bytes'] / mib:.1f} MiB / 剩余 {prefetch['remaining_bytes'] / mib:.1f} MiB"
    ]


def _format_cache_stats(label: str, stats: CacheStats) -> str:
    return (
        f"{label}: {stats.entries} 项 / {stats.bytes / 1024 / 1024:.1f} MiB / "
        f"直接命中 {stats.direct_hits} / 未命中 {stats.misses} / 等待合并 {stats.coalesced} / "
        f"过期 {stats.expirations} / 容量淘汰 {stats.capacity_evictions} / 超限 {stats.oversized}"
    )


def _format_memory_pools(diagnostics: dict) -> str:
    """Pool fill for small images and the I18n slot; large images are served from disk."""
    pools = diagnostics["pools"]
    counters = [
        values for namespace, values in diagnostics["namespaces"].items()
        if namespace.startswith("endfield-")
    ]
    large_hits = int(sum(values.get("large_disk_hits", 0) for values in counters))
    bypassed = int(sum(values.get("bypassed", 0) for values in counters))
    mib = 1024 * 1024
    return (
        f"素材池: 小图 {pools['assets']['bytes'] / mib:.1f}/{pools['assets']['budget_bytes'] / mib:.0f} MiB · "
        f"I18n 常驻 {pools['i18n']['bytes'] / mib:.1f} MiB · "
        f"大图直通命中 {large_hits} · 直通跳过 {bypassed}"
    )
