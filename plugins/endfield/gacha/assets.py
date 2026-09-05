from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable

from PIL import Image
from loguru import logger

from otae_bot.infrastructure.http.client import fetch_many_resilient

from ..account.store import GachaRecord
from ..providers.assets import item_icon_urls, operator_icon_urls, operator_portrait_urls, unique_urls
from ..catalog.service import EndfieldService


GACHA_CACHE_DIR = Path("data") / "endfield" / "image_cache"
CATALOG_CACHE_PATH = GACHA_CACHE_DIR / "catalog.json"
CATALOG_TTL_SECONDS = 24 * 60 * 60
IMAGE_NAMESPACE = "endfield-gacha-images"
POOL_ARTICLE_PREFIX = "卡池/"
KEEPSAKE_ARTICLE_PREFIX = "物品/干员信物/"
IMAGE_FETCH_TIMEOUT_SECONDS = 20.0
IMAGE_FETCH_ATTEMPTS = 3
IMAGE_FETCH_MAX_BYTES = 24 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class GachaItemMetadata:
    item_id: str
    name: str
    rarity: int
    item_type: str
    weapon_type: str = ""
    icon_url: str = ""
    icon_path: str = ""


@dataclass(frozen=True, slots=True)
class GachaPoolRule:
    pool_id: str
    up_item_ids: tuple[str, ...] = ()
    hard_guarantee: int = 0
    pool_name: str = ""
    pool_kind: str = ""


@dataclass(frozen=True, slots=True)
class GachaPoolBanner:
    item_id: str
    name: str
    item_type: str
    image_path: str = ""


class EndfieldGachaAssetCache:
    def __init__(
        self,
        service: EndfieldService,
        *,
        cache_dir: str | Path = GACHA_CACHE_DIR,
        catalog_ttl_seconds: int = CATALOG_TTL_SECONDS,
    ):
        self.service = service
        self.cache_dir = Path(cache_dir)
        self.catalog_path = self.cache_dir / "catalog.json"
        self.catalog_ttl_seconds = int(catalog_ttl_seconds)
        self._catalog_lock = asyncio.Lock()
        self._generation = 0
        self._invalidated_at = 0.0
        self._catalog_memory: tuple[int, dict[str, GachaItemMetadata]] | None = None
        self._catalog_source = "AKEData (fallback permitted)"
        self._catalog_revision = ""
        self._catalog_schema = 0

    def clear_caches(self) -> int:
        """Invalidate disposable catalogs/images without touching account records."""
        self._generation += 1
        self._invalidated_at = time.time()
        self._catalog_memory = None
        self._catalog_lock = asyncio.Lock()
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            marker = self.cache_dir / "invalidated-at"
            marker.write_text(str(self._invalidated_at), encoding="ascii")
        except OSError:
            pass  # Read-only/full cache disks must not prevent memory clearing.
        return 1

    def _fresh_file(self, path: Path) -> bool:
        try:
            modified = path.stat().st_mtime
            marker = self.cache_dir / "invalidated-at"
            invalidated = float(marker.read_text(encoding="ascii")) if marker.exists() else 0
            invalidated = max(invalidated, self._invalidated_at)
            return modified > invalidated and time.time() - modified < self.catalog_ttl_seconds
        except (OSError, ValueError):
            return False

    async def prepare(
        self,
        records: Iterable[GachaRecord],
        *,
        download_all: bool = False,
    ) -> dict[str, GachaItemMetadata]:
        record_list = list(records)
        requested_ids = {record.item_id for record in record_list if record.item_id}
        catalog = await self._load_catalog(required_ids=requested_ids)
        catalog = _merge_records_into_catalog(catalog, record_list)
        selected = {item_id: catalog[item_id] for item_id in requested_ids if item_id in catalog}
        image_items = [
            item
            for item in selected.values()
            if download_all or item.rarity >= 6
        ]
        cached_paths = await self._cache_images(image_items)
        return {
            item_id: replace(item, icon_path=cached_paths.get(item_id, self._existing_icon_path(item_id)))
            for item_id, item in selected.items()
        }

    async def prepare_names(self, names: Iterable[str]) -> dict[str, GachaItemMetadata]:
        requested = {_normalized_name(name) for name in names if str(name or "").strip()}
        if not requested:
            return {}
        catalog = await self._load_catalog()
        selected = {
            _normalized_name(item.name): item
            for item in catalog.values()
            if _normalized_name(item.name) in requested
        }
        if requested - set(selected):
            catalog = await self._load_catalog(force=True)
            selected = {
                _normalized_name(item.name): item
                for item in catalog.values()
                if _normalized_name(item.name) in requested
            }
        cached_paths = await self._cache_images(
            [item for item in selected.values() if item.rarity >= 6]
        )
        return {
            name: replace(item, icon_path=cached_paths.get(item.item_id, self._existing_icon_path(item.item_id)))
            for name, item in selected.items()
        }

    async def prepare_keepsakes(
        self,
        pool_rules: dict[str, GachaPoolRule],
    ) -> dict[str, GachaItemMetadata]:
        catalog = await self._load_catalog(required_ids=(
            item_id
            for rule in pool_rules.values()
            for item_id in rule.up_item_ids
        ))
        operator_ids = tuple(dict.fromkeys(
            item_id
            for rule in pool_rules.values()
            for item_id in rule.up_item_ids
            if item_id.startswith("chr_")
            or (item_id in catalog and catalog[item_id].item_type == "角色")
        ))
        result: dict[str, GachaItemMetadata] = {}
        requests: list[tuple[str, str, GachaItemMetadata]] = []
        try:
            ake_items = await self.service.get_ake_items(f"item_charpotentialup_{key}" for key in operator_ids)
        except Exception as exc:
            logger.warning("[endfield] AKE keepsake metadata unavailable; FZ fallback ({})", type(exc).__name__)
            ake_items = {}
        for operator_id in operator_ids:
            operator = catalog.get(operator_id)
            keepsake_id = f"item_charpotentialup_{operator_id}"
            fallback = GachaItemMetadata(
                keepsake_id,
                f"{operator.name}的信物" if operator else "干员信物",
                6,
                "信物",
                icon_path=self._existing_icon_path(keepsake_id),
            )
            result[operator_id] = fallback
            native = ake_items.get(keepsake_id)
            if native and native.get("name") and native.get("iconId"):
                result[operator_id] = replace(fallback, name=native["name"], rarity=int(native.get("rarity") or 6),
                    icon_url=(item_icon_urls(native["iconId"]) or ("",))[0])
            elif not fallback.icon_path and operator:
                requests.append((operator_id, f"{KEEPSAKE_ARTICLE_PREFIX}{fallback.name}", fallback))
        payloads = await asyncio.gather(
            *(self.service.client.fz_article_by_title(title) for _, title, _ in requests),
            return_exceptions=True,
        )
        for (operator_id, _title, fallback), payload in zip(requests, payloads):
            if isinstance(payload, dict):
                result[operator_id] = extract_keepsake_metadata(payload, fallback)
        cached_paths = await self._cache_images([
            item for item in result.values() if not item.icon_path
        ])
        prepared = {
            operator_id: replace(
                item,
                icon_path=cached_paths.get(item.item_id, item.icon_path or self._existing_icon_path(item.item_id)),
            )
            for operator_id, item in result.items()
        }
        return {
            operator_id: replace(
                item,
                icon_path=self._normalize_keepsake_icon(item.item_id, item.icon_path),
            )
            if item.icon_path else item
            for operator_id, item in prepared.items()
        }

    async def prepare_pool_banners(
        self,
        pool_rules: dict[str, GachaPoolRule],
    ) -> dict[str, tuple[GachaPoolBanner, ...]]:
        requested_ids = tuple(dict.fromkeys(
            item_id
            for rule in pool_rules.values()
            for item_id in rule.up_item_ids
            if item_id
        ))
        catalog = await self._load_catalog(required_ids=requested_ids)
        cache_items: list[GachaItemMetadata] = []
        cache_ids: dict[str, str] = {}
        banner_candidates: dict[str, tuple[str, ...]] = {}
        resolved_items: dict[str, GachaItemMetadata] = {}
        for item_id in requested_ids:
            item = catalog.get(item_id) or GachaItemMetadata(
                item_id,
                item_id,
                6,
                "角色" if item_id.startswith("chr_") else "武器",
            )
            resolved_items[item_id] = item
            if item.item_type == "角色" or item_id.startswith("chr_"):
                cache_id = f"banner_{item_id}"
                cache_items.append(replace(item, item_id=cache_id, item_type="角色"))
                banner_candidates[cache_id] = operator_portrait_urls(item_id)
            else:
                cache_id = item_id
                cache_items.append(item)
                banner_candidates[cache_id] = item_icon_urls(item_id, item.icon_url)
            cache_ids[item_id] = cache_id
        cached_paths = await self._cache_images(cache_items, candidates=banner_candidates)
        banners_by_item: dict[str, GachaPoolBanner] = {}
        for item_id in requested_ids:
            item = resolved_items[item_id]
            cache_id = cache_ids[item_id]
            image_path = (
                cached_paths.get(cache_id)
                or self._existing_icon_path(cache_id)
                or self._existing_icon_path(item_id)
            )
            if (item.item_type == "角色" or item_id.startswith("chr_")) and image_path:
                image_path = self._crop_character_banner(item_id, image_path)
            banners_by_item[item_id] = GachaPoolBanner(
                item_id,
                item.name,
                item.item_type,
                image_path,
            )
        return {
            pool_id: tuple(
                banners_by_item[item_id]
                for item_id in rule.up_item_ids
                if item_id in banners_by_item
            )
            for pool_id, rule in pool_rules.items()
        }

    async def prepare_pool_rules(self, records: Iterable[GachaRecord]) -> dict[str, GachaPoolRule]:
        records = list(records)
        try:
            native = await self.service.get_ake_pool_metadata()
        except Exception as exc:
            logger.warning("[endfield] AKE pool metadata unavailable; FZ fallback ({})", type(exc).__name__)
            native = {}
        # Client pool tables do not contain server-side hard-guarantee counts.
        # Keep that explicitly scoped fallback; never infer counts from names.
        rules = await self._prepare_pool_rules_fz(records)
        for key, row in native.items():
            previous = rules.get(key)
            rules[key] = GachaPoolRule(key, tuple(row["up_item_ids"]),
                previous.hard_guarantee if previous else 0, row["pool_name"], row["pool_kind"])
        if native:
            logger.info("[endfield] gacha pool metadata source=AKEData guarantee source=FZ-or-unknown")
        return rules

    async def _prepare_pool_rules_fz(self, records: Iterable[GachaRecord]) -> dict[str, GachaPoolRule]:
        current_by_type: dict[str, GachaRecord] = {}
        for item in records:
            if "standard" in item.pool_type.casefold():
                continue
            current = current_by_type.get(item.item_type)
            if current is None or (item.gacha_ts, item.seq_id) > (current.gacha_ts, current.seq_id):
                current_by_type[item.item_type] = item
        fallback_titles = tuple(
            f"{POOL_ARTICLE_PREFIX}{item.pool_name}"
            for item in current_by_type.values()
            if item.pool_name
        )
        try:
            summaries = await self.service.client.fz_article_summaries(POOL_ARTICLE_PREFIX)
        except Exception:
            summaries = {}
        directory_titles = tuple(
            str(item.get("title") or "").strip()
            for item in summaries.get("articles") or []
            if isinstance(item, dict)
            and str(item.get("title") or "").strip().startswith(POOL_ARTICLE_PREFIX)
        )
        titles = tuple(dict.fromkeys((*directory_titles, *fallback_titles)))
        if not titles:
            return {}
        payloads = await asyncio.gather(
            *(self.service.client.fz_article_by_title(title) for title in titles),
            return_exceptions=True,
        )
        rules: dict[str, GachaPoolRule] = {}
        for payload in payloads:
            if isinstance(payload, dict):
                rules.update(extract_gacha_pool_rules(payload))
        return rules

    async def _load_catalog(
        self,
        *,
        required_ids: Iterable[str] = (),
        force: bool = False,
    ) -> dict[str, GachaItemMetadata]:
        wanted = {item_id for item_id in required_ids if item_id}
        latest_revision = ""
        if hasattr(self.service, "get_public_data_revision"):
            try:
                latest_revision = await self.service.get_public_data_revision()
            except Exception:
                pass  # AKE outage must not invalidate a usable local catalog.
        generation = self._generation
        async with self._catalog_lock:
            cached = self._read_catalog()
            missing = [item_id for item_id in wanted if item_id not in cached]
            current_version = not latest_revision or latest_revision == self._catalog_revision
            if cached and self._catalog_is_fresh() and current_version and not missing and not force:
                return cached
            try:
                if hasattr(self.service, "get_gacha_catalog_views"):
                    operators, weapons = await self.service.get_gacha_catalog_views()
                else:
                    operators, weapons = await asyncio.gather(
                        self.service.get_operator_catalog_view(), self.service.get_weapon_catalog_view())
                self._catalog_source = " + ".join(dict.fromkeys(getattr(view, "source_name", "") or "legacy" for view in (operators, weapons)))
                items: dict[str, GachaItemMetadata] = {}
                for element in operators.elements:
                    for profession in element.professions:
                        for item in profession.items:
                            if item.operator_id:
                                items[item.operator_id] = GachaItemMetadata(
                                    item_id=item.operator_id,
                                    name=item.name,
                                    rarity=int(item.rarity),
                                    item_type="角色",
                                    icon_url=item.icon_url,
                                )
                for group in weapons.groups:
                    for item in group.items:
                        if item.weapon_id:
                            items[item.weapon_id] = GachaItemMetadata(
                                item_id=item.weapon_id,
                                name=item.name,
                                rarity=int(item.rarity),
                                item_type="武器",
                                weapon_type=item.weapon_type,
                                icon_url=item.icon_url,
                            )
                if items and generation == self._generation:
                    self._catalog_revision = latest_revision
                    self._catalog_schema = 2
                    self._write_catalog(items)
                    return items
            except Exception:
                if cached:
                    return cached
            return cached

    def _catalog_is_fresh(self) -> bool:
        return self._catalog_schema == 2 and self._fresh_file(self.catalog_path)

    def _read_catalog(self) -> dict[str, GachaItemMetadata]:
        try:
            revision = self.catalog_path.stat().st_mtime_ns
            if self._catalog_memory is not None and self._catalog_memory[0] == revision:
                return dict(self._catalog_memory[1])
            raw = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return {}
        self._catalog_schema = 2 if raw.get("schema") == 2 else 0
        self._catalog_revision = str(raw.get("revision") or "")
        self._catalog_source = str(raw.get("source") or "legacy")
        result: dict[str, GachaItemMetadata] = {}
        for item in items:
            if not isinstance(item, dict) or not item.get("item_id"):
                continue
            try:
                metadata = GachaItemMetadata(
                    item_id=str(item["item_id"]),
                    name=str(item.get("name") or ""),
                    rarity=int(item.get("rarity") or 0),
                    item_type=str(item.get("item_type") or ""),
                    weapon_type=str(item.get("weapon_type") or ""),
                    icon_url=str(item.get("icon_url") or ""),
                )
            except (TypeError, ValueError):
                continue
            result[metadata.item_id] = metadata
        self._catalog_memory = revision, result
        return dict(result)

    def _write_catalog(self, items: dict[str, GachaItemMetadata]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 2,
            "revision": self._catalog_revision,
            "updated_at": int(time.time()),
            "source": self._catalog_source,
            "items": [asdict(items[key]) for key in sorted(items)],
        }
        temporary = self.catalog_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(self.catalog_path)

    async def _cache_images(
        self,
        items: list[GachaItemMetadata],
        *,
        candidates: dict[str, tuple[str, ...]] | None = None,
    ) -> dict[str, str]:
        generation = self._generation
        result = {
            item.item_id: path
            for item in items
            if (path := self._existing_icon_path(item.item_id))
        }
        missing = [item for item in items if item.item_id not in result]
        if not missing:
            return result
        pending: dict[str, tuple[GachaItemMetadata, tuple[str, ...]]] = {}
        first_batch: list[str] = []
        for item in missing:
            urls = unique_urls(*(candidates[item.item_id] if candidates and item.item_id in candidates else _gacha_icon_urls(item)))
            if not urls:
                continue
            pending[item.item_id] = (item, urls)
            first_batch.append(urls[0])
        resources = await self._fetch_image_urls(first_batch)
        if generation != self._generation:
            return result
        retry_urls: list[str] = []
        retry_ids: list[str] = []
        for item_id, (item, urls) in pending.items():
            path = self._store_resource(item_id, resources.get(urls[0]), urls[0])
            if path:
                result[item_id] = path
                continue
            rest = urls[1:]
            if rest:
                retry_urls.extend(rest)
                pending[item_id] = (item, rest)
                retry_ids.append(item_id)
        if retry_urls:
            extra = await self._fetch_image_urls(retry_urls)
            if generation != self._generation:
                return result
            resources.update(extra)
            for item_id in retry_ids:
                _item, urls = pending[item_id]
                for url in urls:
                    path = self._store_resource(item_id, resources.get(url), url)
                    if path:
                        result[item_id] = path
                        break
        return result

    async def _fetch_image_urls(self, urls: Iterable[str]) -> dict[str, object]:
        fetched = await fetch_many_resilient(
            urls,
            namespace=IMAGE_NAMESPACE,
            timeout_seconds=IMAGE_FETCH_TIMEOUT_SECONDS,
            attempts=IMAGE_FETCH_ATTEMPTS,
            max_bytes=IMAGE_FETCH_MAX_BYTES,
            log_prefix="[endfield-gacha]",
        )
        if isinstance(fetched, tuple):
            resources, _failures = fetched
        else:
            resources = fetched
        return dict(resources)

    def _store_resource(self, item_id: str, resource, url: str) -> str:
        if resource is None:
            return ""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        suffix = _image_suffix(getattr(resource, "content_type", ""), url)
        path = self.cache_dir / f"{_safe_item_id(item_id)}{suffix}"
        try:
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(resource.content)
            temporary.replace(path)
        except OSError:
            return ""
        return str(path.resolve())

    def _existing_icon_path(self, item_id: str) -> str:
        stem = _safe_item_id(item_id)
        for suffix in (".png", ".webp", ".jpg", ".jpeg"):
            path = self.cache_dir / f"{stem}{suffix}"
            if path.is_file() and path.stat().st_size > 0 and self._fresh_file(path):
                return str(path.resolve())
        return ""

    def _normalize_keepsake_icon(self, item_id: str, source_path: str) -> str:
        source = Path(source_path)
        target = self.cache_dir / f"keepsake_v1_{_safe_item_id(item_id)}.png"
        try:
            if (target.is_file() and target.stat().st_size > 0 and self._fresh_file(target)
                    and target.stat().st_mtime_ns >= source.stat().st_mtime_ns):
                return str(target.resolve())
            image = Image.open(source).convert("RGBA")
            bounds = image.getchannel("A").getbbox()
            if bounds is None:
                return str(source.resolve())
            cropped = image.crop(bounds)
            padding = max(4, round(max(cropped.size) * 0.06))
            side = max(cropped.size) + padding * 2
            normalized = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            normalized.alpha_composite(
                cropped,
                ((side - cropped.width) // 2, (side - cropped.height) // 2),
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".png.tmp")
            normalized.save(temporary, format="PNG", optimize=True)
            temporary.replace(target)
            return str(target.resolve())
        except (OSError, ValueError):
            return str(source.resolve()) if source.is_file() else source_path

    def _crop_character_banner(self, item_id: str, source_path: str) -> str:
        source = Path(source_path)
        target = self.cache_dir / f"banner_bust_v1_{_safe_item_id(item_id)}.webp"
        try:
            if (target.is_file() and target.stat().st_size > 0 and self._fresh_file(target)
                    and target.stat().st_mtime_ns >= source.stat().st_mtime_ns):
                return str(target.resolve())
            with Image.open(source) as opened:
                image = opened.convert("RGBA")
            crop_width = max(1, round(image.width * 0.50))
            crop_height = max(1, round(crop_width / 1.48))
            left = max(0, (image.width - crop_width) // 2)
            top = max(0, min(image.height - crop_height, round(image.height * 0.19)))
            cropped = image.crop((left, top, left + crop_width, top + crop_height))
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".webp.tmp")
            cropped.save(temporary, format="WEBP", quality=90, method=6)
            temporary.replace(target)
            return str(target.resolve())
        except (OSError, ValueError):
            return str(source.resolve()) if source.is_file() else source_path


def apply_gacha_metadata(
    records: Iterable[GachaRecord],
    metadata: dict[str, GachaItemMetadata],
) -> list[GachaRecord]:
    result: list[GachaRecord] = []
    for record in records:
        item = metadata.get(record.item_id)
        if item is None:
            result.append(record)
            continue
        result.append(
            replace(
                record,
                item_name=item.name or record.item_name,
                rarity=item.rarity or record.rarity,
                item_type=item.item_type or record.item_type,
                weapon_type=item.weapon_type or record.weapon_type,
            )
        )
    return result


def _gacha_icon_urls(item: GachaItemMetadata) -> tuple[str, ...]:
    if item.item_type == "角色" or str(item.item_id).startswith("chr_"):
        return operator_icon_urls(item.item_id, item.icon_url)
    return item_icon_urls(item.item_id, item.icon_url)


def _merge_records_into_catalog(
    catalog: dict[str, GachaItemMetadata],
    records: Iterable[GachaRecord],
) -> dict[str, GachaItemMetadata]:
    result = dict(catalog)
    for record in records:
        if not record.item_id or record.item_id in result:
            continue
        result[record.item_id] = GachaItemMetadata(
            record.item_id,
            record.item_name,
            record.rarity or 0,
            record.item_type or ("角色" if record.item_id.startswith("chr_") else "武器"),
            weapon_type=record.weapon_type or "",
        )
    return result


def _normalized_name(value: str) -> str:
    return "".join(str(value or "").split()).casefold()


def extract_keepsake_metadata(
    payload: object,
    fallback: GachaItemMetadata,
) -> GachaItemMetadata:
    for item in _walk_dicts(payload):
        if str(item.get("typeCode") or "").casefold() != "charpotentialup":
            continue
        item_id = str(item.get("id") or item.get("iconId") or fallback.item_id).strip()
        name = str(item.get("name") or fallback.name).strip()
        icon_url = str(item.get("iconUrl") or "").strip()
        try:
            rarity = max(0, int(item.get("rarity") or fallback.rarity))
        except (TypeError, ValueError):
            rarity = fallback.rarity
        return replace(
            fallback,
            item_id=item_id or fallback.item_id,
            name=name or fallback.name,
            rarity=rarity,
            icon_url=icon_url,
        )
    return fallback


def extract_gacha_pool_rules(payload: object) -> dict[str, GachaPoolRule]:
    result: dict[str, GachaPoolRule] = {}
    for item in _walk_dicts(payload):
        pool_id = str(item.get("poolId") or "").strip()
        if not pool_id or ("upItemIds" not in item and "upItems" not in item):
            continue
        existing = result.get(pool_id)
        up_item_ids = _pool_up_item_ids(item.get("upItemIds"))
        up_items = item.get("upItems")
        if isinstance(up_items, dict):
            up_items = [up_items]
        if isinstance(up_items, list):
            up_item_ids = tuple(
                str(up_item.get("id") or up_item.get("itemId") or "")
                for up_item in up_items if isinstance(up_item, dict)
            )
        try:
            hard_guarantee = max(0, int(item.get("hardGuarantee") or 0))
        except (TypeError, ValueError):
            hard_guarantee = 0
        pool_name = str(item.get("poolName") or "").strip()
        pool_kind = str(item.get("poolKind") or "").strip().casefold()
        if not pool_kind:
            normalized_pool_id = pool_id.casefold()
            pool_kind = "weapon" if normalized_pool_id.startswith(("weapon", "wepon")) else "char"
        result[pool_id] = GachaPoolRule(
            pool_id,
            up_item_ids or (existing.up_item_ids if existing else ()),
            hard_guarantee or (existing.hard_guarantee if existing else 0),
            pool_name or (existing.pool_name if existing else ""),
            pool_kind or (existing.pool_kind if existing else ""),
        )
    return result


def _walk_dicts(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _pool_up_item_ids(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values = re.split(r"[\s,，]+", value.strip())
    elif isinstance(value, list):
        values = [
            str(item.get("id") or item.get("itemId") or "") if isinstance(item, dict) else str(item)
            for item in value
        ]
    else:
        values = []
    return tuple(dict.fromkeys(item for item in values if item))


def _safe_item_id(item_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(item_id or "")).strip("._")
    return value or "unknown"


def _image_suffix(content_type: str, url: str) -> str:
    normalized = str(content_type or "").split(";", 1)[0].lower()
    if normalized == "image/jpeg":
        return ".jpg"
    if normalized == "image/webp":
        return ".webp"
    if normalized == "image/png":
        return ".png"
    guessed = mimetypes.guess_extension(normalized) if normalized else None
    if guessed in {".png", ".jpg", ".jpeg", ".webp"}:
        return guessed
    path = str(url or "").split("?", 1)[0].split("@", 1)[0].lower()
    return next((suffix for suffix in (".png", ".webp", ".jpg", ".jpeg") if path.endswith(suffix)), ".png")
