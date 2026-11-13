"""AKEData（终末地数据库 https://akedata.wiki ）奖章数据抓取。

AKEData 是基于游戏客户端 TableCfg 的数据站，CDN（data.akedata.wiki）稳定，作为
蚀刻章/奖章的权威主源，替代易超时的 FZ Wiki。

数据结构（2026-07-28 实测）：
- ``manifest.json`` → ``latest``（如 ``1.4.4@8764515-7``）→ ``versions[].tableCfgPath``
  （如 ``public/1.4.4/8764515-7/TableCfg``）。
- ``<tableCfgPath>/AchievementTable.json``：140 枚，按 ``achv_*`` id 索引；含
  ``canBeUpgraded``/``canBePlated``/``initLevel``/``levelInfos``/``order``/``groupId`` 等
  （名字/描述只有 text-id，``text`` 为空）。
- ``<tableCfgPath>/I18nTextTable_CN.json``：~13.8 万条，text-id → 中文文本。
- ``<tableCfgPath>/AchievementTypeTable.json``：8 个分类，含 ``categoryName``(text-id)、
  ``categoryPriority``、``achievementGroupData[]``(groupId + groupName text-id)。
- 图标：``<dataBase>/public/images/.../sprites/medaliconbig/<achvId>_lv<NN>.png``。

achv_id 与森空岛 ``achievementData.id`` 经 md5 一一对应（见 ``docs/reference/skland_medal_id_mapping.md``）。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from otae_bot.infrastructure.http.client import clear_http_cache, fetch_json

AKEDATA_DATA_BASE = "https://data.akedata.wiki"
AKEDATA_ICON_BASE = (
    f"{AKEDATA_DATA_BASE}/public/images/assets/beyond/dynamicassets/"
    "gameplay/ui/sprites/medaliconbig"
)
# 档案库组图标（PrtsFirstLv.icon，如 prts_002_settlement_sns）；站点 v3-archive.js 的
# assetUrl('prts/icon', name) 同模板，2026-09-10 实测 447/447 组均有图。
AKEDATA_PRTS_ICON_BASE = (
    f"{AKEDATA_DATA_BASE}/public/images/assets/beyond/dynamicassets/"
    "gameplay/ui/sprites/prts/icon"
)
AKEDATA_HEADERS = {
    "User-Agent": "otae-bot-entari/1.0 (+https://github.com/otae-1204/otae-bot-entari)",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://cf.akedata.top/",
}
AKEDATA_TIMEOUT_SECONDS = 30.0
# I18nTextTable_CN 约 18MB，超过 fetch_json 默认 10MB 上限。
_I18N_MAX_BYTES = 64 * 1024 * 1024
_I18N_SUFFIX = "/I18nTextTable_CN.json"
# 当前版本 I18n 在内存常驻槽里留多久：表路径按版本寻址，同一路径内容不变；
# 按需刷新时显式重新校验，manifest 换版本时整组清掉。
I18N_TTL_SECONDS = 24 * 3600.0
# manifest：60 秒内直接用；60–600 秒先返回旧值、后台刷新；更久没刷新过才等网络。
MANIFEST_FRESH_SECONDS = 60.0
MANIFEST_STALE_LIMIT_SECONDS = 600.0
# 刷新失败后继续用旧值，隔这么久再在后台试一次；告警最多这么久一条。
MANIFEST_RETRY_SECONDS = 30.0
MANIFEST_WARNING_INTERVAL_SECONDS = 300.0
# Path of the I18n table successfully loaded into memory in this process.
# None until that read returns; cleared when /ef dev drops the akedata cache.
_i18n_loaded_path: str | None = None
# Table path -> refresh token it was last revalidated for. One refresh batch
# reads the shared I18n twice (medals, archives) but asks the server once.
_revalidated: dict[str, str] = {}


def normalize_table_path(path: str) -> str:
    """Cache identity of a table path, without any query string."""
    return "/" + str(path or "").split("?", 1)[0].lstrip("/")


def i18n_loaded_path() -> str | None:
    return _i18n_loaded_path


def i18n_loaded_request_path() -> str | None:
    """Kept for callers of the old refresh-token paths; table reads no longer carry one."""
    return _i18n_loaded_path


def i18n_process_warm() -> bool:
    return _i18n_loaded_path is not None


def _forget_i18n_loaded() -> None:
    global _i18n_loaded_path
    _i18n_loaded_path = None
    _revalidated.clear()


def clear_i18n_process_warm() -> None:
    _forget_i18n_loaded()
    reset_manifest_state()


def remember_i18n_loaded(path: str) -> None:
    global _i18n_loaded_path
    normalized = normalize_table_path(path)
    if normalized.endswith(_I18N_SUFFIX):
        _i18n_loaded_path = normalized


def i18n_path_from_manifest(manifest: dict[str, Any]) -> str | None:
    """Current version's I18n path, or None when the manifest has no table path."""
    latest = manifest.get("latest")
    entry = next(
        (
            item
            for item in manifest.get("versions") or []
            if isinstance(item, dict) and item.get("id") == latest
        ),
        None,
    )
    table_cfg = str((entry or {}).get("tableCfgPath") or "").strip("/")
    if not latest or not table_cfg:
        return None
    return f"/{table_cfg}{_I18N_SUFFIX}"


async def _get(
    path: str,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    ttl_seconds: float | None = None,
    cache_token: str = "",
) -> Any:
    """``cache_token`` (one per on-demand refresh) revalidates the table once per token."""
    if ttl_seconds is None and path.endswith(_I18N_SUFFIX):
        ttl_seconds = I18N_TTL_SECONDS
    revalidate = bool(cache_token) and _revalidated.get(path) != cache_token
    payload = await fetch_json(
        f"{AKEDATA_DATA_BASE}{path}",
        namespace="akedata",
        headers=AKEDATA_HEADERS,
        timeout_seconds=AKEDATA_TIMEOUT_SECONDS,
        max_bytes=max_bytes,
        read_only=True,
        **({"ttl_seconds": ttl_seconds} if ttl_seconds is not None else {}),
        **({"revalidate": True} if revalidate else {}),
    )
    if revalidate:
        _revalidated[path] = cache_token
    remember_i18n_loaded(path)
    return payload


@dataclass
class _ManifestState:
    value: dict[str, Any] | None = None
    fetched_at: float = 0.0  # time.monotonic() of the last successful fetch
    retry_at: float = 0.0
    warned_at: float = float("-inf")
    task: asyncio.Task | None = None


_manifest = _ManifestState()


def reset_manifest_state() -> None:
    """Forget the cached manifest; a refresh still running publishes nowhere."""
    global _manifest
    _manifest = _ManifestState()


async def fetch_akedata_manifest() -> dict[str, Any]:
    """``manifest.json`` → ``{latest, versions:[{id, tableCfgPath, ...}], sharedRevision}``。

    Stale-while-revalidate: a manifest younger than ``MANIFEST_FRESH_SECONDS``
    is returned as is; one younger than ``MANIFEST_STALE_LIMIT_SECONDS`` is
    returned while a single background task refreshes it; otherwise callers
    wait for that refresh. A failed refresh keeps serving the last manifest.
    """
    state = _manifest
    now = time.monotonic()
    if state.value is not None:
        age = now - state.fetched_at
        if age < MANIFEST_FRESH_SECONDS:
            return state.value
        if age < MANIFEST_STALE_LIMIT_SECONDS:
            if now >= state.retry_at:
                _manifest_refresh(state)
            return state.value
    return await asyncio.shield(_manifest_refresh(state))


def _manifest_refresh(state: _ManifestState) -> asyncio.Task:
    """The one refresh task for ``state`` on this loop, started if none is running."""
    loop = asyncio.get_running_loop()
    task = state.task
    if task is None or task.done() or task.get_loop() is not loop:
        task = loop.create_task(_load_manifest(state))
        # Background refreshes may have no awaiter; retrieve their outcome.
        task.add_done_callback(lambda done: done.cancelled() or done.exception())
        state.task = task
    return task


async def _load_manifest(state: _ManifestState) -> dict[str, Any]:
    try:
        # ``?t=`` busts CDN caches per minute; the state above is the memory cache.
        manifest = await _get(f"/manifest.json?t={int(time.time() // 60)}", ttl_seconds=0)
        if not isinstance(manifest, dict) or not manifest.get("latest"):
            raise RuntimeError("AKEData manifest 结构异常")
    except Exception as exc:
        if state.value is None:
            raise
        now = time.monotonic()
        # Keep the last good manifest inside the stale window, so callers do not
        # wait on a failing network; try again in the background later.
        state.fetched_at = now - MANIFEST_FRESH_SECONDS
        state.retry_at = now + MANIFEST_RETRY_SECONDS
        if now - state.warned_at >= MANIFEST_WARNING_INTERVAL_SECONDS:
            state.warned_at = now
            logger.warning(
                f"[akedata] manifest refresh failed; keeping latest={state.value.get('latest')} "
                f"({type(exc).__name__}: {exc})"
            )
        return state.value
    previous = state.value
    state.value = manifest
    state.fetched_at = time.monotonic()
    state.retry_at = 0.0
    if previous is not None and _manifest_revision(previous) != _manifest_revision(manifest):
        logger.info(
            f"[akedata] manifest changed latest={previous.get('latest')}->{manifest.get('latest')} "
            f"sharedRevision={previous.get('sharedRevision')}->{manifest.get('sharedRevision')}; "
            "dropping cached tables"
        )
        if _manifest is state:  # not reset meanwhile
            _forget_i18n_loaded()
            await clear_http_cache("akedata", include_disk=False)
    return manifest


def _manifest_revision(manifest: dict[str, Any]) -> tuple[Any, Any]:
    return manifest.get("latest"), manifest.get("sharedRevision")


def latest_version_entry(manifest: dict[str, Any]) -> dict[str, Any]:
    """Resolve the exact build; the display's major.minor label is not a cache key."""
    latest = manifest.get("latest")
    entry = next(
        (v for v in manifest.get("versions") or [] if isinstance(v, dict) and v.get("id") == latest),
        None,
    )
    if not latest or not entry or not entry.get("tableCfgPath"):
        raise RuntimeError(f"AKEData manifest 缺少版本 {latest} 的 tableCfgPath")
    return entry


def _table_path(table_cfg: str, name: str) -> str:
    # Always the version path: an on-demand refresh revalidates it (``_get(cache_token=)``)
    # instead of reading a second, token-suffixed copy.
    return f"/{table_cfg.strip('/')}/{name}.json"


async def fetch_akedata_medal_tables(
    *, manifest: dict[str, Any] | None = None, cache_token: str = "",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    """返回 ``(AchievementTable, AchievementTypeTable, I18nTextTable_CN, version_id)``。"""
    if manifest is None:
        manifest = await fetch_akedata_manifest()
    latest = manifest["latest"]
    entry = latest_version_entry(manifest)
    table_cfg = str(entry["tableCfgPath"]).lstrip("/")
    achievement, type_table, i18n = await asyncio.gather(
        _get(_table_path(table_cfg, "AchievementTable"), cache_token=cache_token),
        _get(_table_path(table_cfg, "AchievementTypeTable"), cache_token=cache_token),
        _get(
            _table_path(table_cfg, "I18nTextTable_CN"),
            max_bytes=_I18N_MAX_BYTES,
            cache_token=cache_token,
        ),
    )
    return achievement, type_table, i18n, latest


def game_version_label(version_id: str) -> str:
    """manifest 版本 id → 游戏大版本标签 major.minor。

    前两段跟着游戏大版本走（``1.4.4@8764515-7`` → ``1.4``）。同 major.minor 的多个
    revision 视为同一游戏版本，不当作「上一版本」。
    """
    head = str(version_id).split("@", 1)[0]
    parts = head.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else head


def pick_previous_game_version(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """从 manifest 挑「上一游戏版本」条目：与 latest 的 major.minor 不同的第一个。

    ``versions[]`` 最新在前，但同一游戏版本可能连着多个 revision（仅资源修订不同），
    故按 major.minor 跳过 latest 自身及同版本 revision，取第一个不同的条目。
    manifest 无更早游戏版本时返回 None。
    """
    latest_id = manifest.get("latest")
    versions = manifest.get("versions") or []
    latest_label = game_version_label(latest_id) if latest_id else ""
    if not latest_label:
        return None
    for entry in versions:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        if entry.get("id") == latest_id:
            continue
        if game_version_label(str(entry["id"])) != latest_label:
            return entry
    return None


# 历史版本 AchievementTable 内容不变，长 TTL 享缓存（按秒计 = 7 天）。
HISTORICAL_TABLE_TTL_SECONDS = 7 * 24 * 3600


async def fetch_akedata_achievement_table(
    table_cfg: str, *, ttl_seconds: float | None = HISTORICAL_TABLE_TTL_SECONDS,
    cache_token: str = "",
) -> dict[str, Any]:
    """抓指定版本的 ``AchievementTable.json``（按 achv_ id 索引，~175KB）。

    默认长 TTL——历史版本内容恒定；latest 版本调用方可传 None 走默认 600s。
    """
    table = await _get(
        _table_path(str(table_cfg), "AchievementTable"),
        ttl_seconds=ttl_seconds,
        cache_token=cache_token,
    )
    return table if isinstance(table, dict) else {}


async def fetch_akedata_archive_tables(
    *, manifest: dict[str, Any] | None = None, cache_token: str = "",
) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], str
]:
    """返回 ``(PrtsPage, PrtsCategory, PrtsFirstLv, PrtsAllItem, I18nTextTable_CN, version_id)``。

    档案库（情报档案库）表层级：PrtsPage（三大页签 document/multi_media/text）→
    PrtsCategory（6 子分类）→ PrtsFirstLv（档案组，itemIds[]）→ PrtsAllItem（条目，``nar_*``
    id）。条目 ``type`` 与页签 ``pageType`` 一一对应（2026-09-10 实测 1.5.3：495 条全部可归属，
    无混合类型组）。AKEData 站上「任务文本/地图文本」由 DialogTextTable/LevelDescTable 合成的
    虚拟分类不在 PrtsAllItem 内，本功能不抓这两张表，天然排除。
    """
    if manifest is None:
        manifest = await fetch_akedata_manifest()
    latest = manifest["latest"]
    entry = latest_version_entry(manifest)
    table_cfg = str(entry["tableCfgPath"]).lstrip("/")
    page, category, first_lv, all_item, i18n = await asyncio.gather(
        _get(_table_path(table_cfg, "PrtsPage"), cache_token=cache_token),
        _get(_table_path(table_cfg, "PrtsCategory"), cache_token=cache_token),
        _get(_table_path(table_cfg, "PrtsFirstLv"), cache_token=cache_token),
        _get(_table_path(table_cfg, "PrtsAllItem"), cache_token=cache_token),
        _get(
            _table_path(table_cfg, "I18nTextTable_CN"),
            max_bytes=_I18N_MAX_BYTES,
            cache_token=cache_token,
        ),
    )
    return page, category, first_lv, all_item, i18n, latest


async def fetch_akedata_prts_all_item(
    table_cfg: str, *, ttl_seconds: float | None = HISTORICAL_TABLE_TTL_SECONDS,
    cache_token: str = "",
) -> dict[str, Any]:
    """抓指定版本的 ``PrtsAllItem.json``（按 nar_ id 索引，~165KB）。

    历史版本内容恒定，默认长 TTL；latest 版本调用方可传 None 走默认 600s。
    """
    table = await _get(
        _table_path(str(table_cfg), "PrtsAllItem"),
        ttl_seconds=ttl_seconds,
        cache_token=cache_token,
    )
    return table if isinstance(table, dict) else {}
