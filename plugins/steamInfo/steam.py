import re
import aiohttp
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union
from lxml import html as lxml_html
from loguru import logger

from configs.path_config import IMAGE_PATH, JSON_PATH
from .models import PlayerSummaries, PlayerData


STEAM_ID_OFFSET = 76561197960265728
APP_LIST_CACHE_SECONDS = 24 * 60 * 60
APP_LOOKUP_CACHE_SECONDS = 7 * 24 * 60 * 60
APP_AMBIGUOUS_CACHE_SECONDS = 30 * 24 * 60 * 60
STEAM_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=8)
ABBREVIATION_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=5)
STEAM_LLM_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=12)
STEAM_LLM_WEB_EVIDENCE_TIMEOUT = aiohttp.ClientTimeout(total=5)
STEAM_CANDIDATE_NETWORK_BUDGET_SECONDS = 2.5
STEAM_APP_ALIAS_FILE = Path(JSON_PATH) / "steamInfo/steam_app_aliases.json"
STEAM_APP_AMBIGUOUS_FILE_NAME = "steam_app_ambiguous_cache.json"
STEAM_APP_LIST_URLS = (
    "https://cdn.jsdelivr.net/gh/dgibbs64/SteamCMD-AppID-List@master/steamcmd_appid.json",
    "https://raw.githubusercontent.com/dgibbs64/SteamCMD-AppID-List/master/steamcmd_appid.json",
    "https://api.steampowered.com/ISteamApps/GetAppList/v2/",
    "http://api.steampowered.com/ISteamApps/GetAppList/v2/",
)
STEAM_STORE_APP_LIST_URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
STEAM_STORE_APP_LIST_URLS = (
    STEAM_STORE_APP_LIST_URL,
    "https://partner.steam-api.com/IStoreService/GetAppList/v1/",
)
STEAM_STORE_APP_LIST_PAGE_SIZE = 50000
ABBREVIATION_SUGGEST_URLS = (
    "https://api.bing.com/osjson.aspx?query={query}",
    "https://suggestqueries.google.com/complete/search?client=firefox&q={query}",
)
ABBREVIATION_MAX_CANDIDATES = 8
LLM_GAME_NAME_EVIDENCE_URLS = (
    "https://store.steampowered.com/search/suggest?term={query}&f=games&cc=us&l=english&realm=1",
    "https://suggestqueries.google.com/complete/search?client=firefox&q={query}%20steam",
    "https://api.bing.com/osjson.aspx?query={query}%20steam",
)
ABBREVIATION_NOISE_TOKENS = {
    "beta",
    "demo",
    "dlc",
    "editor",
    "ost",
    "server",
    "soundtrack",
    "test",
    "tool",
}
BANGUMI_ALIAS_INFOBOX_KEYS = {
    "中文名",
    "别名",
    "别称",
    "其它名称",
    "其他名称",
    "英文名",
    "原名",
}


def _proxy_candidates(proxy: str = None) -> List[Optional[str]]:
    return [proxy] if proxy else [None]


def _request_proxy_candidates(url: str, proxy: str = None) -> List[Optional[str]]:
    return _proxy_candidates(proxy)


def _steam_api_key_list(steam_api_key: Union[str, List[str]]) -> List[str]:
    if not steam_api_key:
        return []
    if isinstance(steam_api_key, str):
        return [key.strip() for key in steam_api_key.split(",") if key.strip()]
    return steam_api_key


def get_steam_id(steam_id_or_steam_friends_code: str) -> str:
    if not steam_id_or_steam_friends_code.isdigit():
        return None

    id_ = int(steam_id_or_steam_friends_code)

    if id_ < STEAM_ID_OFFSET:
        return str(id_ + STEAM_ID_OFFSET)

    return steam_id_or_steam_friends_code


async def get_steam_users_info(
    steam_ids: List[str], steam_api_key: Union[str, List[str]], proxy: str = None
) -> PlayerSummaries:
    if len(steam_ids) == 0:
        return {"response": {"players": []}}

    if len(steam_ids) > 100:
        result = {"response": {"players": []}}
        for i in range(0, len(steam_ids), 100):
            batch_result = await get_steam_users_info(
                steam_ids[i : i + 100], steam_api_key, proxy
            )
            result["response"]["players"].extend(batch_result["response"]["players"])
        return result

    api_keys = _steam_api_key_list(steam_api_key)

    if not api_keys:
        logger.error("Steam API key is not configured.")
        return {"response": {"players": []}}

    url = "http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
    for api_key in api_keys:
        for request_proxy in _request_proxy_candidates(url, proxy):
            try:
                async with aiohttp.ClientSession(timeout=STEAM_REQUEST_TIMEOUT) as session:
                    async with session.get(
                        f'{url}?key={api_key}&steamids={",".join(steam_ids)}',
                        proxy=request_proxy,
                    ) as resp:
                        if resp.status == 200:
                            return await resp.json()
                        logger.warning(
                            f"Steam API key failed: {resp.status}, {await resp.text()}"
                        )
            except aiohttp.ClientError as exc:
                logger.warning(f"Steam API key request failed: {exc}")

    logger.error("All Steam API keys failed to get player summaries.")
    return {"response": {"players": []}}


async def get_steam_app_list(
    cache_path: Path,
    proxy: str = None,
    steam_api_key: Union[str, List[str]] = None,
    force_refresh: bool = False,
    prefer_cache: bool = False,
) -> List[dict]:
    cache_file = cache_path / "steam_app_list.json"
    if prefer_cache and not force_refresh and cache_file.exists():
        try:
            return await _read_cached_app_list(cache_file)
        except Exception as exc:
            logger.warning(f"Failed to read Steam app list cache: {exc}")

    has_api_key = bool(_steam_api_key_list(steam_api_key))
    if has_api_key:
        apps = await fetch_steam_store_app_list(steam_api_key, proxy)
        if apps:
            _write_steam_app_list_cache(cache_file, apps)
            return apps

    if (
        not force_refresh
        and cache_file.exists()
        and time.time() - cache_file.stat().st_mtime < APP_LIST_CACHE_SECONDS
    ):
        try:
            return await _read_cached_app_list(cache_file)
        except Exception as exc:
            logger.warning(f"Failed to read Steam app list cache: {exc}")

    for url in STEAM_APP_LIST_URLS:
        for request_proxy in _request_proxy_candidates(url, proxy):
            try:
                async with aiohttp.ClientSession(timeout=STEAM_REQUEST_TIMEOUT) as session:
                    async with session.get(url, proxy=request_proxy) as resp:
                        if resp.status != 200:
                            raise ValueError(f"status {resp.status}")
                        data = await resp.json()
                        apps = data.get("applist", {}).get("apps", [])
                        _write_steam_app_list_cache(cache_file, apps)
                        return apps
            except Exception as exc:
                logger.warning(f"Failed to fetch Steam app list from {url}: {exc}")

    if cache_file.exists():
        try:
            return await _read_cached_app_list(cache_file)
        except Exception as exc:
            logger.warning(f"Failed to read stale Steam app list cache: {exc}")
    return []


async def fetch_steam_store_app_list(
    steam_api_key: Union[str, List[str]] = None,
    proxy: str = None,
) -> List[dict]:
    api_keys = _steam_api_key_list(steam_api_key)
    if not api_keys:
        return []

    for api_key in api_keys:
        for url in STEAM_STORE_APP_LIST_URLS:
            for request_proxy in _request_proxy_candidates(url, proxy):
                try:
                    apps: List[dict] = []
                    last_appid = 0
                    async with aiohttp.ClientSession(timeout=STEAM_REQUEST_TIMEOUT) as session:
                        while True:
                            params = {
                                "key": api_key,
                                "input_json": json.dumps(
                                    {
                                        "include_games": True,
                                        "include_dlc": False,
                                        "last_appid": last_appid,
                                        "max_results": STEAM_STORE_APP_LIST_PAGE_SIZE,
                                    },
                                    separators=(",", ":"),
                                ),
                            }
                            async with session.get(
                                url,
                                params=params,
                                proxy=request_proxy,
                            ) as resp:
                                if resp.status != 200:
                                    raise ValueError(f"status {resp.status}")
                                data = await resp.json(content_type=None)

                            page_apps = _extract_store_app_list(data)
                            if not page_apps:
                                break
                            apps.extend(page_apps)
                            if len(page_apps) < STEAM_STORE_APP_LIST_PAGE_SIZE:
                                break
                            next_last_appid = int(page_apps[-1]["appid"])
                            if next_last_appid <= last_appid:
                                break
                            last_appid = next_last_appid

                    if apps:
                        return _dedupe_steam_apps(apps)
                except Exception as exc:
                    logger.warning(
                        f"Failed to fetch Steam Store app list from {url}: {exc}"
                    )
    return []


def _extract_store_app_list(data: dict) -> List[dict]:
    raw_apps = data.get("response", {}).get("apps", [])
    if not isinstance(raw_apps, list):
        return []
    return [
        normalized
        for normalized in (_normalize_steam_app(app) for app in raw_apps)
        if normalized is not None
    ]


def _dedupe_steam_apps(apps: List[dict]) -> List[dict]:
    result = {}
    for app in apps:
        normalized = _normalize_steam_app(app)
        if normalized is not None:
            result[normalized["appid"]] = normalized
    return list(result.values())


def _write_steam_app_list_cache(cache_file: Path, apps: List[dict]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps({"applist": {"apps": apps}}, ensure_ascii=False),
        encoding="utf-8",
    )


async def get_cached_steam_app_list(cache_path: Path) -> List[dict]:
    cache_file = cache_path / "steam_app_list.json"
    if not cache_file.exists():
        return []
    try:
        return await _read_cached_app_list(cache_file)
    except Exception as exc:
        logger.warning(f"Failed to read Steam app list cache: {exc}")
        return []


def is_steam_app_list_cache_stale(cache_path: Path) -> bool:
    cache_file = cache_path / "steam_app_list.json"
    return (
        not cache_file.exists()
        or time.time() - cache_file.stat().st_mtime >= APP_LIST_CACHE_SECONDS
    )


async def _read_cached_app_list(cache_file: Path) -> List[dict]:
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    return data.get("applist", {}).get("apps", [])


def _steam_app_alias_file(alias_path: Optional[Path] = None) -> Path:
    return Path(alias_path) if alias_path is not None else STEAM_APP_ALIAS_FILE


def _normalize_steam_app(app: dict) -> Optional[dict]:
    try:
        appid = int(app.get("appid"))
    except (TypeError, ValueError):
        return None
    return {"appid": appid, "name": str(app.get("name") or appid)}


def read_steam_app_aliases(alias_path: Optional[Path] = None) -> dict:
    alias_file = _steam_app_alias_file(alias_path)
    if not alias_file.exists():
        return {}
    try:
        raw_aliases = json.loads(alias_file.read_text(encoding="utf-8"))
        aliases = {}
        for alias, app in raw_aliases.items():
            if not isinstance(alias, str) or not isinstance(app, dict):
                continue
            normalized_app = _normalize_steam_app(app)
            if normalized_app is not None:
                aliases[alias.strip().casefold()] = normalized_app
        return aliases
    except Exception as exc:
        logger.warning(f"Failed to read Steam app aliases: {exc}")
        return {}


def find_cached_steam_app_alias_or_lookup(
    query: str, cache_path: Path, alias_path: Optional[Path] = None
) -> Optional[dict]:
    query = query.strip()
    if not query:
        return None
    cached_alias = read_steam_app_aliases(alias_path).get(query.casefold())
    if cached_alias is not None:
        return cached_alias
    return read_app_lookup_cache(query, cache_path)


async def find_unambiguous_steam_app(
    query: str,
    cache_path: Path,
    proxy: str = None,
    alias_path: Optional[Path] = None,
    steam_api_key: Union[str, List[str]] = None,
    prefer_cache: bool = False,
) -> Optional[dict]:
    query = query.strip()
    if not query:
        return None
    if query.isdigit():
        return {"appid": int(query), "name": query}

    app_list = await get_steam_app_list(
        cache_path, proxy, steam_api_key, prefer_cache=prefer_cache
    )
    exact_matches = [
        normalized
        for normalized in (
            _normalize_steam_app(app)
            for app in app_list
            if str(app.get("name", "")).strip() == query
        )
        if normalized is not None
    ]
    exact_matches = list({app["appid"]: app for app in exact_matches}.values())
    if len(exact_matches) == 1 and not has_multiple_steam_name_candidates(query, app_list):
        return exact_matches[0]

    return None


def _ambiguous_cache_file(cache_path: Path) -> Path:
    return Path(cache_path) / STEAM_APP_AMBIGUOUS_FILE_NAME


def _normalize_ambiguous_candidate(app: dict, source: str = "unknown") -> Optional[dict]:
    normalized_app = _normalize_steam_app(app)
    if normalized_app is None:
        return None
    return {
        **normalized_app,
        "source": str(app.get("source") or source or "unknown"),
        "time": float(app.get("time") or time.time()),
    }


def _dedupe_ambiguous_candidates(candidates: List[dict]) -> List[dict]:
    result: List[dict] = []
    seen = set()
    for candidate in candidates:
        normalized = _normalize_ambiguous_candidate(candidate, candidate.get("source", "unknown"))
        if normalized is None:
            continue
        appid = normalized["appid"]
        if appid in seen:
            continue
        seen.add(appid)
        normalized.pop("time", None)
        result.append(normalized)
    return result[:8]


def _ambiguous_query_key(query: str) -> str:
    return re.sub(r"[\W_]+", "", query.casefold())


def _cjk_chars(text: str) -> set[str]:
    return {
        char
        for char in text
        if re.match(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", char)
    }


def _is_similar_ambiguous_query(query: str, cached_query: str) -> bool:
    left = _ambiguous_query_key(query)
    right = _ambiguous_query_key(cached_query)
    if not left or not right:
        return False
    if left == right or left in right or right in left:
        return True

    left_cjk = _cjk_chars(query)
    right_cjk = _cjk_chars(cached_query)
    if not left_cjk or not right_cjk:
        return False
    overlap = len(left_cjk & right_cjk)
    return overlap / max(len(left_cjk), len(right_cjk)) >= 0.6


def read_app_ambiguous_cache(query: str, cache_path: Path) -> List[dict]:
    cache_file = _ambiguous_cache_file(cache_path)
    query = query.strip().casefold()
    if not query or not cache_file.exists():
        return []
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        record = data.get(query)
        if not isinstance(record, dict):
            return []
        if time.time() - float(record.get("time", 0)) > APP_AMBIGUOUS_CACHE_SECONDS:
            return []
        return _dedupe_ambiguous_candidates(record.get("candidates", []))
    except Exception as exc:
        logger.warning(f"Failed to read Steam ambiguous app cache: {exc}")
        return []


def read_similar_app_ambiguous_cache(query: str, cache_path: Path) -> List[dict]:
    cache_file = _ambiguous_cache_file(cache_path)
    query = query.strip()
    if not query or not cache_file.exists():
        return []
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        candidates: List[dict] = []
        for cached_query, record in data.items():
            if not isinstance(record, dict):
                continue
            if time.time() - float(record.get("time", 0)) > APP_AMBIGUOUS_CACHE_SECONDS:
                continue
            if _is_similar_ambiguous_query(query, cached_query):
                candidates.extend(record.get("candidates", []))
        return _dedupe_ambiguous_candidates(candidates)
    except Exception as exc:
        logger.warning(f"Failed to read similar Steam ambiguous app cache: {exc}")
        return []


def write_app_ambiguous_cache(query: str, cache_path: Path, candidates: List[dict]) -> None:
    cache_file = _ambiguous_cache_file(cache_path)
    query_key = query.strip().casefold()
    normalized_candidates = _dedupe_ambiguous_candidates(candidates)
    if not query_key or not normalized_candidates:
        return
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if cache_file.exists():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
        data[query_key] = {"time": time.time(), "candidates": normalized_candidates}
        cache_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning(f"Failed to write Steam ambiguous app cache: {exc}")


def read_all_app_ambiguous_cache(cache_path: Path) -> dict:
    cache_file = _ambiguous_cache_file(cache_path)
    if not cache_file.exists():
        return {}
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning(f"Failed to read Steam ambiguous app cache: {exc}")
        return {}


def delete_app_ambiguous_cache(query: str, cache_path: Path) -> bool:
    cache_file = _ambiguous_cache_file(cache_path)
    query_key = query.strip().casefold()
    if not query_key or not cache_file.exists():
        return False
    try:
        data = read_all_app_ambiguous_cache(cache_path)
        existed = query_key in data
        data.pop(query_key, None)
        cache_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return existed
    except Exception as exc:
        logger.warning(f"Failed to delete Steam ambiguous app cache: {exc}")
        return False


def append_app_ambiguous_cache(
    query: str, cache_path: Path, app: dict, source: str = "user_confirmed"
) -> None:
    existing = read_app_ambiguous_cache(query, cache_path)
    candidate = _normalize_ambiguous_candidate(app, source)
    if candidate is None:
        return
    write_app_ambiguous_cache(query, cache_path, [candidate, *existing])


def write_steam_app_alias(
    alias: str, app: dict, alias_path: Optional[Path] = None
) -> None:
    alias = alias.strip().casefold()
    normalized_app = _normalize_steam_app(app)
    if not alias or normalized_app is None:
        return

    alias_file = _steam_app_alias_file(alias_path)
    try:
        alias_file.parent.mkdir(parents=True, exist_ok=True)
        aliases = read_steam_app_aliases(alias_file)
        if aliases.get(alias) == normalized_app:
            return
        aliases[alias] = normalized_app
        alias_file.write_text(
            json.dumps(aliases, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning(f"Failed to write Steam app alias: {exc}")


def delete_steam_app_alias(alias: str, alias_path: Optional[Path] = None) -> bool:
    alias = alias.strip().casefold()
    alias_file = _steam_app_alias_file(alias_path)
    if not alias or not alias_file.exists():
        return False
    try:
        aliases = read_steam_app_aliases(alias_file)
        existed = alias in aliases
        aliases.pop(alias, None)
        alias_file.write_text(
            json.dumps(aliases, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return existed
    except Exception as exc:
        logger.warning(f"Failed to delete Steam app alias: {exc}")
        return False


async def find_steam_app(
    query: str,
    cache_path: Path,
    proxy: str = None,
    alias_path: Optional[Path] = None,
    llm_config: Optional[dict] = None,
    write_query_cache: bool = True,
    steam_api_key: Union[str, List[str]] = None,
    allow_cache_match: bool = True,
) -> Optional[dict]:
    query = query.strip()
    if not query:
        return None
    if query.isdigit():
        return {"appid": int(query), "name": query}

    normalized_query = query.casefold()
    steam_app_aliases = read_steam_app_aliases(alias_path)
    if allow_cache_match and normalized_query in steam_app_aliases:
        return steam_app_aliases[normalized_query]

    app_list_loaded = False
    app_list_apps: List[dict] = []

    async def get_app_list_once() -> List[dict]:
        nonlocal app_list_loaded, app_list_apps
        if not app_list_loaded:
            app_list_apps = await get_steam_app_list(cache_path, proxy, steam_api_key)
            app_list_loaded = True
        return app_list_apps

    async def find_app_list_match(
        candidate: str, allow_contains: bool = True
    ) -> Optional[dict]:
        cached_app = await find_cached_steam_app(candidate, cache_path, allow_contains)
        if cached_app is not None:
            return cached_app
        return find_steam_app_in_list(
            candidate, await get_app_list_once(), allow_contains
        )

    cached_lookup = read_app_lookup_cache(query, cache_path) if allow_cache_match else None
    if cached_lookup is not None:
        if write_query_cache:
            write_steam_app_alias(query, cached_lookup, alias_path)
        return cached_lookup

    app_list_match = await find_app_list_match(query, allow_contains=False)
    if app_list_match is not None:
        if has_multiple_steam_name_candidates(query, await get_app_list_once()):
            return None
        if write_query_cache:
            write_steam_app_alias(query, app_list_match, alias_path)
        return app_list_match

    abbreviation_match = await find_steam_app_by_abbreviation(
        query, await get_app_list_once(), proxy, llm_config
    )
    if abbreviation_match is not None:
        if write_query_cache:
            write_app_lookup_cache(query, cache_path, abbreviation_match)
            write_steam_app_alias(query, abbreviation_match, alias_path)
        return abbreviation_match

    app_list_match = await find_app_list_match(query)
    if app_list_match is not None:
        if write_query_cache and _is_exact_app_name(query, app_list_match):
            write_steam_app_alias(query, app_list_match, alias_path)
        return app_list_match

    wikidata_app = await search_wikidata_steam_app(query, proxy)
    if wikidata_app is not None:
        if write_query_cache:
            write_app_lookup_cache(query, cache_path, wikidata_app)
            write_steam_app_alias(query, wikidata_app, alias_path)
        return wikidata_app

    store_search = await search_steam_store_app(query, proxy)
    if store_search is not None:
        if write_query_cache:
            write_app_lookup_cache(query, cache_path, store_search)
            write_steam_app_alias(query, store_search, alias_path)
        return store_search

    bangumi_candidates = await search_bangumi_game_candidates(query, proxy)
    for candidate in bangumi_candidates:
        normalized_candidate = candidate.casefold()
        if allow_cache_match and normalized_candidate in steam_app_aliases:
            app = steam_app_aliases[normalized_candidate]
            if write_query_cache:
                write_app_lookup_cache(query, cache_path, app)
                write_steam_app_alias(query, app, alias_path)
            return app

        app_list_match = await find_app_list_match(candidate, allow_contains=False)
        if app_list_match is not None:
            if write_query_cache:
                write_app_lookup_cache(query, cache_path, app_list_match)
                write_steam_app_alias(query, app_list_match, alias_path)
            if write_query_cache:
                write_steam_app_alias(candidate, app_list_match, alias_path)
            return app_list_match

        abbreviation_match = await find_steam_app_by_abbreviation(
            candidate, await get_app_list_once(), proxy, llm_config
        )
        if abbreviation_match is not None:
            if write_query_cache:
                write_app_lookup_cache(query, cache_path, abbreviation_match)
                write_steam_app_alias(query, abbreviation_match, alias_path)
            if write_query_cache:
                write_steam_app_alias(candidate, abbreviation_match, alias_path)
            return abbreviation_match

        app_list_match = await find_app_list_match(candidate)
        if app_list_match is not None:
            if write_query_cache:
                write_app_lookup_cache(query, cache_path, app_list_match)
                write_steam_app_alias(query, app_list_match, alias_path)
            if write_query_cache and _is_exact_app_name(candidate, app_list_match):
                write_steam_app_alias(candidate, app_list_match, alias_path)
            return app_list_match

    for candidate in bangumi_candidates[:3]:
        wikidata_app = await search_wikidata_steam_app(candidate, proxy)
        if wikidata_app is not None:
            if write_query_cache:
                write_app_lookup_cache(query, cache_path, wikidata_app)
                write_steam_app_alias(query, wikidata_app, alias_path)
            if write_query_cache:
                write_steam_app_alias(candidate, wikidata_app, alias_path)
            return wikidata_app

        store_search = await search_steam_store_app(candidate, proxy)
        if store_search is not None:
            if write_query_cache:
                write_app_lookup_cache(query, cache_path, store_search)
                write_steam_app_alias(query, store_search, alias_path)
            if write_query_cache:
                write_steam_app_alias(candidate, store_search, alias_path)
            return store_search

    return None


def read_app_lookup_cache(query: str, cache_path: Path) -> Optional[dict]:
    cache_file = cache_path / "steam_app_lookup_cache.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        record = data.get(query.casefold())
        if not record:
            return None
        if time.time() - record.get("time", 0) > APP_LOOKUP_CACHE_SECONDS:
            return None
        return record.get("app")
    except Exception as exc:
        logger.warning(f"Failed to read Steam app lookup cache: {exc}")
        return None


def write_app_lookup_cache(query: str, cache_path: Path, app: dict) -> None:
    cache_file = cache_path / "steam_app_lookup_cache.json"
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if cache_file.exists():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
        data[query.casefold()] = {"time": time.time(), "app": app}
        cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"Failed to write Steam app lookup cache: {exc}")


def read_all_app_lookup_cache(cache_path: Path) -> dict:
    cache_file = cache_path / "steam_app_lookup_cache.json"
    if not cache_file.exists():
        return {}
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning(f"Failed to read Steam app lookup cache: {exc}")
        return {}


def delete_app_lookup_cache(query: str, cache_path: Path) -> bool:
    cache_file = cache_path / "steam_app_lookup_cache.json"
    query_key = query.strip().casefold()
    if not query_key or not cache_file.exists():
        return False
    try:
        data = read_all_app_lookup_cache(cache_path)
        existed = query_key in data
        data.pop(query_key, None)
        cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return existed
    except Exception as exc:
        logger.warning(f"Failed to delete Steam app lookup cache: {exc}")
        return False


async def find_cached_steam_app(
    query: str, cache_path: Path, allow_contains: bool = True
) -> Optional[dict]:
    apps = await get_cached_steam_app_list(cache_path)
    return find_steam_app_in_list(query, apps, allow_contains)


def find_steam_app_in_list(
    query: str, apps: List[dict], allow_contains: bool = True
) -> Optional[dict]:
    normalized_query = query.casefold()
    exact_matches = [
        app
        for app in apps
        if str(app.get("name", "")).casefold() == normalized_query
    ]
    if exact_matches:
        return exact_matches[0]

    if not allow_contains:
        return None

    contains_matches = [
        app
        for app in apps
        if normalized_query in str(app.get("name", "")).casefold()
    ]
    if contains_matches:
        contains_matches.sort(key=lambda app: len(str(app.get("name", ""))))
        return contains_matches[0]
    return None


def find_steam_app_contains_candidates(query: str, apps: List[dict]) -> List[dict]:
    normalized_query = query.strip().casefold()
    if not normalized_query:
        return []
    matches = []
    for index, app in enumerate(apps):
        name = str(app.get("name", "")).strip()
        normalized_name = name.casefold()
        if normalized_query not in normalized_name:
            continue
        exact_rank = 0 if normalized_name == normalized_query else 1
        matches.append((exact_rank, len(name), index, app))
    matches.sort(key=lambda item: (item[0], item[1], item[2]))
    return [
        normalized
        for normalized in (_normalize_steam_app(app) for _, _, _, app in matches)
        if normalized is not None
    ][:8]


def has_multiple_steam_name_candidates(query: str, apps: List[dict]) -> bool:
    normalized_query = query.strip().casefold()
    if not normalized_query:
        return False
    exact_case_matches = [
        app for app in apps if str(app.get("name", "")).strip() == query.strip()
    ]
    if exact_case_matches:
        return False
    matches = [
        app
        for app in apps
        if normalized_query in str(app.get("name", "")).casefold()
    ]
    normalized_matches = _dedupe_ambiguous_candidates(matches)
    return len(normalized_matches) > 1


def _is_exact_app_name(query: str, app: dict) -> bool:
    return str(app.get("name", "")).casefold() == query.casefold()


def _is_probable_game_abbreviation(query: str) -> bool:
    return bool(re.fullmatch(r"[a-zA-Z][a-zA-Z0-9]{1,4}", query.strip()))


def _compact_ascii(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _is_subsequence(short: str, text: str) -> bool:
    position = 0
    for char in short:
        position = text.find(char, position)
        if position < 0:
            return False
        position += 1
    return True


def _steam_abbreviation_score(query: str, app: dict) -> int:
    abbreviation = _compact_ascii(query)
    name = str(app.get("name", ""))
    tokens = re.findall(r"[a-z]+|[0-9]+", name.casefold())
    if not abbreviation or not tokens:
        return 0

    roman_digits = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6"}
    initials = "".join(token if token.isdigit() else token[0] for token in tokens)
    numeric_initials = "".join(
        roman_digits.get(token, token if token.isdigit() else token[0])
        for token in tokens
    )
    if abbreviation == tokens[0]:
        return 180
    if abbreviation in tokens:
        return 100
    if abbreviation in {initials, numeric_initials}:
        return 120

    compact_name = _compact_ascii(name)
    if (
        abbreviation[0] == compact_name[0]
        and _is_subsequence(abbreviation, compact_name)
    ):
        return 40 + sum(char in initials for char in abbreviation)
    return 0


def find_steam_abbreviation_candidates(query: str, apps: List[dict]) -> List[dict]:
    if not _is_probable_game_abbreviation(query):
        return []
    ranked = []
    for app in apps:
        score = _steam_abbreviation_score(query, app)
        if score > 0:
            ranked.append((score, app))
    ranked.sort(key=lambda item: (-item[0], len(str(item[1].get("name", "")))))
    return [app for _, app in ranked[:ABBREVIATION_MAX_CANDIDATES]]


async def fetch_steam_abbreviation_popularity_text(
    query: str, proxy: str = None
) -> str:
    texts = []
    for request_proxy in _proxy_candidates(proxy):
        for template in ABBREVIATION_SUGGEST_URLS:
            try:
                async with aiohttp.ClientSession(
                    timeout=ABBREVIATION_REQUEST_TIMEOUT
                ) as session:
                    async with session.get(
                        template.format(query=query),
                        proxy=request_proxy,
                    ) as resp:
                        if resp.status == 200:
                            texts.append(await resp.text())
            except Exception as exc:
                logger.debug(
                    f"Failed to fetch Steam abbreviation popularity {query}: {exc}"
                )
        if texts:
            break
    return "\n".join(texts)


def _steam_popularity_score(app: dict, popularity_text: str) -> int:
    text = popularity_text.casefold()
    name = str(app.get("name", "")).casefold()
    if name and name in text:
        return 200
    significant_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", name)
        if len(token) >= 4
    }
    return sum(25 for token in significant_tokens if token in text)


def _steam_abbreviation_noise_penalty(app: dict) -> int:
    tokens = set(re.findall(r"[a-z0-9]+", str(app.get("name", "")).casefold()))
    return 80 if tokens & ABBREVIATION_NOISE_TOKENS else 0


def _llm_config_value(llm_config: Optional[dict], key: str, default: str = "") -> str:
    if not llm_config:
        return default
    value = llm_config.get(key, default)
    return str(value or default)


def _extract_json_object(text: str) -> Optional[dict]:
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def _extract_json_value(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for pattern in (r"\{.*\}", r"\[.*\]"):
        match = re.search(pattern, text, re.S)
        if not match:
            continue
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
    return None


def _normalize_suggested_game_names(value) -> List[str]:
    if isinstance(value, dict):
        for key in ("names", "candidates", "games", "game_names"):
            if key in value:
                value = value[key]
                break
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []

    result: List[str] = []
    seen = set()
    for item in value:
        if isinstance(item, dict):
            item = item.get("name") or item.get("game") or item.get("title")
        if not isinstance(item, str):
            continue
        name = re.sub(r"\s+", " ", item).strip()
        if not name or len(name) > 80:
            continue
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            result.append(name)
        if len(result) >= 5:
            break
    return result


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", text))


def _has_shared_non_ascii_token(left: str, right: str) -> bool:
    left_chars = {
        char.casefold()
        for char in left
        if not char.isspace() and not char.isascii()
    }
    right_chars = {
        char.casefold()
        for char in right
        if not char.isspace() and not char.isascii()
    }
    return bool(left_chars and right_chars and left_chars.intersection(right_chars))


def is_distant_llm_game_name(query: str, candidate: str) -> bool:
    query = query.strip()
    candidate = candidate.strip()
    if not query or not candidate:
        return False
    if query.casefold() == candidate.casefold():
        return False
    if _contains_cjk(query) and candidate.isascii():
        return True
    if _contains_cjk(query) and not _has_shared_non_ascii_token(query, candidate):
        return True
    return False


def protect_suggested_game_names(query: str, candidates: List[str]) -> List[str]:
    query = query.strip()
    if not query or query.isdigit():
        return candidates

    result = [query]
    seen = {query.casefold()}
    for candidate in candidates:
        candidate = str(candidate or "").strip()
        if not candidate:
            continue
        key = candidate.casefold()
        if key not in seen:
            seen.add(key)
            result.append(candidate)
        if len(result) >= 5:
            break
    return result


async def resolve_steam_app_candidates(
    query: str,
    cache_path: Path,
    proxy: str = None,
    alias_path: Optional[Path] = None,
    llm_config: Optional[dict] = None,
    steam_api_key: Union[str, List[str]] = None,
    network_budget: float = STEAM_CANDIDATE_NETWORK_BUDGET_SECONDS,
) -> List[dict]:
    query = query.strip()
    if not query or query.isdigit():
        return []

    candidates: List[dict] = read_similar_app_ambiguous_cache(query, cache_path)
    cached_app = find_cached_steam_app_alias_or_lookup(query, cache_path, alias_path)
    if cached_app is not None:
        candidates.append({**cached_app, "source": "cache"})

    local_candidates = _dedupe_ambiguous_candidates(candidates)
    if local_candidates:
        return local_candidates

    app_list = await get_cached_steam_app_list(cache_path)
    for app in find_steam_app_contains_candidates(query, app_list):
        candidates.append({**app, "source": "steam_contains"})
    for app in find_steam_abbreviation_candidates(query, app_list):
        normalized = _normalize_steam_app(app)
        if normalized is not None:
            candidates.append({**normalized, "source": "steam_abbreviation"})

    local_candidates = _dedupe_ambiguous_candidates(candidates)
    if local_candidates:
        return local_candidates

    async def safe_call(coro, default):
        try:
            return await coro
        except Exception as exc:
            logger.warning(
                f"Steam candidate network source failed for {query}: {type(exc).__name__}: {exc}"
            )
            return default

    async def collect_network_candidates() -> List[dict]:
        candidate_names, bangumi_names, store_app, wikidata_app = await asyncio.gather(
            safe_call(suggest_steam_game_names(query, llm_config, proxy), []),
            safe_call(search_bangumi_game_candidates(query, proxy), []),
            safe_call(search_steam_store_app(query, proxy), None),
            safe_call(search_wikidata_steam_app(query, proxy), None),
        )

        network_candidates: List[dict] = []
        for app, source in ((store_app, "steam_store"), (wikidata_app, "wikidata")):
            if app is not None:
                network_candidates.append({**app, "source": source})

        name_tasks = []
        name_sources = []
        for name in [*candidate_names, *bangumi_names]:
            if not name or name.casefold() == query.casefold():
                continue
            source = "llm" if name in candidate_names else "bangumi"
            app = find_steam_app_in_list(name, app_list, allow_contains=False)
            if app is not None:
                network_candidates.append({**app, "source": source})
                continue
            name_tasks.append(
                asyncio.create_task(search_steam_store_app(name, proxy))
            )
            name_sources.append((name, source))

        if name_tasks:
            results = await asyncio.gather(*name_tasks, return_exceptions=True)
            wikidata_tasks = []
            wikidata_sources = []
            for result, (name, source) in zip(results, name_sources):
                if isinstance(result, dict):
                    network_candidates.append({**result, "source": source})
                else:
                    wikidata_tasks.append(
                        asyncio.create_task(search_wikidata_steam_app(name, proxy))
                    )
                    wikidata_sources.append(source)
            if wikidata_tasks:
                wikidata_results = await asyncio.gather(
                    *wikidata_tasks, return_exceptions=True
                )
                for result, source in zip(wikidata_results, wikidata_sources):
                    if isinstance(result, dict):
                        network_candidates.append({**result, "source": source})

        return network_candidates

    try:
        candidates.extend(
            await asyncio.wait_for(collect_network_candidates(), timeout=network_budget)
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"Steam candidate network sources timed out for {query} after {network_budget:.1f}s"
        )

    return _dedupe_ambiguous_candidates(candidates)


def _strip_html_text(text: str) -> str:
    if not text:
        return ""
    try:
        doc = lxml_html.fromstring(text)
        text = " ".join(part.strip() for part in doc.xpath("//text()") if part.strip())
    except Exception:
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


async def fetch_llm_game_name_web_evidence(query: str, proxy: str = None) -> str:
    query = query.strip()
    if not query:
        return ""

    evidence: List[str] = []
    seen = set()
    for url_template in LLM_GAME_NAME_EVIDENCE_URLS:
        url = url_template.format(query=aiohttp.helpers.quote(query, safe=""))
        for request_proxy in _proxy_candidates(proxy):
            try:
                async with aiohttp.ClientSession(timeout=STEAM_LLM_WEB_EVIDENCE_TIMEOUT) as session:
                    async with session.get(url, proxy=request_proxy) as resp:
                        if resp.status != 200:
                            continue
                        content_type = resp.headers.get("content-type", "")
                        raw_text = await resp.text()
                break
            except Exception as exc:
                logger.debug(
                    f"Failed to fetch Steam game name evidence {query} from {url}: {exc}"
                )
                continue

        else:
            continue

        snippets: List[str] = []
        if "json" in content_type:
            try:
                value = json.loads(raw_text)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, list):
                        snippets.extend(str(child) for child in item)
                    elif isinstance(item, str):
                        snippets.append(item)
            elif isinstance(value, dict):
                snippets.extend(str(item) for item in value.values())
        else:
            snippets.append(_strip_html_text(raw_text))

        source = url.split("/")[2]
        for snippet in snippets:
            snippet = re.sub(r"\s+", " ", snippet).strip()
            if not snippet or len(snippet) > 500:
                continue
            key = snippet.casefold()
            if key in seen:
                continue
            seen.add(key)
            evidence.append(f"{source}: {snippet}")
            if len(evidence) >= 8:
                return "\n".join(evidence)

    return "\n".join(evidence)


async def suggest_steam_game_names(
    query: str,
    llm_config: Optional[dict],
    proxy: str = None,
) -> List[str]:
    api_key = _llm_config_value(llm_config, "api_key")
    if not api_key or not query.strip():
        return []

    base_url = _llm_config_value(llm_config, "base_url", "https://api.deepseek.com")
    model = _llm_config_value(llm_config, "model", "deepseek-v4-flash")
    web_evidence = await fetch_llm_game_name_web_evidence(query, proxy)
    prompt = {
        "task": "Suggest possible real Steam game title aliases for a user query.",
        "rules": [
            "Return JSON only.",
            "Return 0 to 5 likely real Steam game names or known aliases.",
            "Do not translate, paraphrase, romanize, or invent English titles.",
            "For Chinese, Japanese, or Korean titles, keep the original query as the first candidate.",
            "Use web evidence to confirm aliases, but only add an English candidate if the evidence names a real official Steam title or common alias.",
            "Do not include appids, explanations, platforms, editions, DLC, demos, or tools unless the query clearly asks for them.",
            "If uncertain, return the original query only or an empty list.",
        ],
        "query": query,
        "web_evidence": web_evidence[:3000],
        "response_schema": {"names": ["Steam game name"]},
    }
    body = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You suggest conservative Steam title aliases. "
                    "You are not a translator. Preserve non-English titles unless "
                    "you know a real official Steam title or common alias. "
                    "You must answer with compact JSON only."
                ),
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession(timeout=STEAM_LLM_REQUEST_TIMEOUT) as session:
            async with session.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json=body,
                headers=headers,
                proxy=proxy,
            ) as resp:
                if resp.status != 200:
                    raise ValueError(f"status {resp.status}: {await resp.text()}")
                data = await resp.json(content_type=None)
    except Exception as exc:
        logger.warning(
            f"Failed to suggest Steam game names for {query}: {type(exc).__name__}: {exc}"
        )
        return []

    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    candidates = _normalize_suggested_game_names(_extract_json_value(content))
    return protect_suggested_game_names(query, candidates)


async def select_steam_abbreviation_with_llm(
    query: str,
    candidates: List[dict],
    popularity_text: str,
    llm_config: Optional[dict],
    proxy: str = None,
) -> Optional[dict]:
    api_key = _llm_config_value(llm_config, "api_key")
    if not api_key or not candidates:
        return None

    base_url = _llm_config_value(llm_config, "base_url", "https://api.deepseek.com")
    model = _llm_config_value(llm_config, "model", "deepseek-v4-flash")
    candidate_payload = [
        {"appid": int(app["appid"]), "name": str(app.get("name", app["appid"]))}
        for app in candidates
        if _normalize_steam_app(app) is not None
    ]
    if not candidate_payload:
        return None

    prompt = {
        "task": "Choose which Steam game an abbreviation most likely refers to.",
        "rules": [
            "Return JSON only.",
            "Choose only one appid from candidates, or null if uncertain.",
            "Prefer the primary playable game over test servers, demos, DLC, soundtracks, tools, or mods.",
            "Use the web popularity/suggestion text as evidence, but do not invent appids.",
        ],
        "abbreviation": query,
        "web_popularity_text": popularity_text[:2000],
        "candidates": candidate_payload,
        "response_schema": {"appid": "number or null"},
    }
    body = {
        "model": model,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You disambiguate Steam game abbreviations. "
                    "You must answer with a compact JSON object."
                ),
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession(timeout=STEAM_LLM_REQUEST_TIMEOUT) as session:
            async with session.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json=body,
                headers=headers,
                proxy=proxy,
            ) as resp:
                if resp.status != 200:
                    raise ValueError(f"status {resp.status}: {await resp.text()}")
                data = await resp.json(content_type=None)
    except Exception as exc:
        logger.warning(
            f"Failed to select Steam abbreviation {query} with LLM: {type(exc).__name__}: {exc}"
        )
        return None

    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    result = _extract_json_object(content)
    if result is None:
        return None

    selected_appid = result.get("appid")
    if selected_appid is None:
        return None
    try:
        selected_appid = int(selected_appid)
    except (TypeError, ValueError):
        return None

    for app in candidates:
        try:
            if int(app.get("appid")) == selected_appid:
                return _normalize_steam_app(app)
        except (TypeError, ValueError):
            continue
    return None


async def find_steam_app_by_abbreviation(
    query: str,
    apps: List[dict],
    proxy: str = None,
    llm_config: Optional[dict] = None,
) -> Optional[dict]:
    candidates = find_steam_abbreviation_candidates(query, apps)
    if not candidates:
        return None
    popularity_text = await fetch_steam_abbreviation_popularity_text(query, proxy)
    llm_choice = await select_steam_abbreviation_with_llm(
        query, candidates, popularity_text, llm_config, proxy
    )
    if llm_choice is not None:
        return llm_choice

    if not popularity_text:
        return None
    ranked = []
    for app in candidates:
        popularity_score = _steam_popularity_score(app, popularity_text)
        if popularity_score <= 0:
            continue
        total_score = (
            popularity_score * 10
            + _steam_abbreviation_score(query, app)
            - _steam_abbreviation_noise_penalty(app)
        )
        ranked.append((total_score, app))

    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


async def search_wikidata_steam_app(
    query: str, proxy: str = None
) -> Optional[dict]:
    search_params = {
        "action": "wbsearchentities",
        "search": query,
        "language": "zh",
        "uselang": "zh",
        "format": "json",
        "limit": 5,
    }

    for request_proxy in _proxy_candidates(proxy):
        try:
            async with aiohttp.ClientSession(timeout=STEAM_REQUEST_TIMEOUT) as session:
                async with session.get(
                    "https://www.wikidata.org/w/api.php",
                    params=search_params,
                    proxy=request_proxy,
                ) as resp:
                    if resp.status != 200:
                        raise ValueError(f"status {resp.status}")
                    data = await resp.json(content_type=None)

                entity_ids = [
                    item["id"]
                    for item in data.get("search", [])
                    if str(item.get("id", "")).startswith("Q")
                ]
                for entity_id in entity_ids:
                    async with session.get(
                        f"https://www.wikidata.org/wiki/Special:EntityData/{entity_id}.json",
                        proxy=request_proxy,
                    ) as resp:
                        if resp.status != 200:
                            continue
                        entity_data = await resp.json(content_type=None)
                    entity = entity_data.get("entities", {}).get(entity_id, {})
                    claims = entity.get("claims", {}).get("P1733", [])
                    if not claims:
                        continue
                    steam_id = _wikidata_claim_value(claims[0])
                    if not steam_id or not str(steam_id).isdigit():
                        continue
                    labels = entity.get("labels", {})
                    name = (
                        labels.get("zh", {}).get("value")
                        or labels.get("en", {}).get("value")
                        or labels.get("ja", {}).get("value")
                        or query
                    )
                    return {"appid": int(steam_id), "name": name}
        except Exception as exc:
            logger.warning(
                f"Failed to search Wikidata Steam app {query}: {type(exc).__name__}: {exc}"
            )
    return None


def _wikidata_claim_value(claim: dict):
    return (
        claim.get("mainsnak", {})
        .get("datavalue", {})
        .get("value")
    )


async def search_bangumi_game_candidates(
    query: str, proxy: str = None
) -> List[str]:
    body = {"keyword": query, "filter": {"type": [4]}}
    headers = {
        "User-Agent": "qqbot-steamInfo/1.0",
        "Content-Type": "application/json",
    }
    candidates: List[str] = []

    for request_proxy in _proxy_candidates(proxy):
        try:
            async with aiohttp.ClientSession(timeout=STEAM_REQUEST_TIMEOUT) as session:
                async with session.post(
                    "https://api.bgm.tv/v0/search/subjects?limit=5",
                    json=body,
                    headers=headers,
                    proxy=request_proxy,
                ) as resp:
                    if resp.status != 200:
                        raise ValueError(f"status {resp.status}")
                    data = await resp.json(content_type=None)
                    for subject in data.get("data", []):
                        _append_bangumi_subject_candidates(candidates, subject)
                    return candidates
        except Exception as exc:
            logger.warning(
                f"Failed to search Bangumi game {query}: {type(exc).__name__}: {exc}"
            )
    return candidates


def _append_bangumi_subject_candidates(candidates: List[str], subject: dict) -> None:
    _append_unique(candidates, subject.get("name"))
    _append_unique(candidates, subject.get("name_cn"))
    for item in subject.get("infobox", []):
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if key in BANGUMI_ALIAS_INFOBOX_KEYS:
            _append_infobox_value(candidates, item.get("value"))


def _append_infobox_value(candidates: List[str], value) -> None:
    if isinstance(value, str):
        _append_unique(candidates, value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                _append_unique(candidates, item.get("v"))
            else:
                _append_infobox_value(candidates, item)


def _append_unique(candidates: List[str], value) -> None:
    if not value:
        return
    value = str(value).strip()
    if value and value not in candidates:
        candidates.append(value)


async def search_steam_store_app(query: str, proxy: str = None) -> Optional[dict]:
    url = "https://store.steampowered.com/api/storesearch/"
    params = {
        "term": query,
        "cc": "cn",
        "l": "schinese",
    }
    for request_proxy in _request_proxy_candidates(url, proxy):
        try:
            async with aiohttp.ClientSession(timeout=STEAM_REQUEST_TIMEOUT) as session:
                async with session.get(
                    url,
                    params=params,
                    proxy=request_proxy,
                ) as resp:
                    if resp.status != 200:
                        raise ValueError(f"status {resp.status}")
                    data = await resp.json(content_type=None)
                    items = data.get("items", [])
                    if not items:
                        return None
                    item = items[0]
                    return {"appid": int(item["id"]), "name": item.get("name", query)}
        except Exception as exc:
            logger.warning(
                f"Failed to search Steam store app {query}: {type(exc).__name__}: {exc}"
            )
    return None


async def get_owned_game(
    steam_id: str,
    app_id: int,
    steam_api_key: Union[str, List[str]],
    proxy: str = None,
) -> Optional[dict]:
    api_keys = _steam_api_key_list(steam_api_key)
    if not api_keys:
        logger.error("Steam API key is not configured.")
        return None

    params_base = {
        "steamid": steam_id,
        "format": "json",
        "include_appinfo": 1,
        "include_played_free_games": 1,
        "appids_filter[0]": app_id,
    }

    url = "http://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
    for api_key in api_keys:
        for request_proxy in _request_proxy_candidates(url, proxy):
            try:
                params = {**params_base, "key": api_key}
                async with aiohttp.ClientSession(timeout=STEAM_REQUEST_TIMEOUT) as session:
                    async with session.get(
                        url,
                        params=params,
                        proxy=request_proxy,
                    ) as resp:
                        if resp.status != 200:
                            logger.warning(
                                f"Steam owned games request failed: {resp.status}, {await resp.text()}"
                            )
                            continue
                        data = await resp.json()
                        games = data.get("response", {}).get("games", [])
                        return games[0] if games else None
            except Exception as exc:
                logger.warning(f"Steam owned games request failed: {exc}")
    return None


async def _fetch(
    url: str, default: bytes, cache_file: Optional[Path] = None, proxy: str = None
) -> bytes:
    if cache_file is not None and cache_file.exists():
        return cache_file.read_bytes()

    for request_proxy in _request_proxy_candidates(url, proxy):
        try:
            async with aiohttp.ClientSession(timeout=STEAM_REQUEST_TIMEOUT) as session:
                async with session.get(url, proxy=request_proxy) as resp:
                    if resp.status != 200:
                        raise ValueError(f"status {resp.status}")
                    content = await resp.read()
                    if cache_file is not None:
                        cache_file.parent.mkdir(parents=True, exist_ok=True)
                        cache_file.write_bytes(content)
                    return content
        except Exception as exc:
            logger.warning(f"Failed to fetch Steam asset {url}: {exc}")
    return default


async def fetch_app_icon(
    app_id: int,
    icon_hash: str,
    cache_path: Path,
    proxy: str = None,
) -> bytes:
    default_icon = (Path(IMAGE_PATH) / "steamInfo/default_header_image.jpg").read_bytes()
    if not icon_hash:
        return default_icon

    return await _fetch(
        f"http://media.steampowered.com/steamcommunity/public/images/apps/{app_id}/{icon_hash}.jpg",
        default_icon,
        cache_file=cache_path / f"app_icon_{app_id}_{icon_hash}.jpg",
        proxy=proxy,
    )


async def get_player_achievement_summary(
    steam_id: str,
    app_id: int,
    steam_api_key: Union[str, List[str]],
    proxy: str = None,
) -> Optional[dict]:
    api_keys = _steam_api_key_list(steam_api_key)
    if not api_keys:
        logger.error("Steam API key is not configured.")
        return None

    params_base = {
        "steamid": steam_id,
        "appid": app_id,
        "format": "json",
    }

    url = "http://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v0001/"
    for api_key in api_keys:
        for request_proxy in _request_proxy_candidates(url, proxy):
            try:
                params = {**params_base, "key": api_key}
                async with aiohttp.ClientSession(
                    timeout=STEAM_REQUEST_TIMEOUT
                ) as session:
                    async with session.get(
                        url,
                        params=params,
                        proxy=request_proxy,
                    ) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json(content_type=None)
                        achievements = data.get("playerstats", {}).get("achievements")
                        if not achievements:
                            return None
                        completed = sum(
                            1
                            for achievement in achievements
                            if achievement.get("achieved")
                        )
                        return {"completed": completed, "total": len(achievements)}
            except Exception as exc:
                logger.warning(f"Steam achievements request failed: {exc}")
    return None


def _text_or_empty(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return " ".join(node.split())
    return " ".join(node.text_content().split())


def _match_first(patterns: List[str], text: str) -> Optional[str]:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return next((item for item in match.groups() if item is not None), "")
    return None


async def get_user_data(
    steam_id: int, cache_path: Path, proxy: str = None
) -> PlayerData:
    res_path = Path(IMAGE_PATH) / "steamInfo"
    default_background = (res_path / "bg_dots.png").read_bytes()
    default_avatar = (res_path / "unknown_avatar.jpg").read_bytes()
    default_achievement_image = (res_path / "default_achievement_image.png").read_bytes()
    default_header_image = (res_path / "default_header_image.jpg").read_bytes()

    result = {
        "steamid": str(steam_id),
        "description": "No information given.",
        "background": default_background,
        "avatar": default_avatar,
        "player_name": "Unknown",
        "recent_2_week_play_time": None,
        "game_data": [],
    }

    local_time = datetime.now(timezone.utc).astimezone()
    utc_offset_seconds = int(local_time.utcoffset().total_seconds())

    html = None
    profile_url = f"https://steamcommunity.com/profiles/{steam_id}"
    for request_proxy in _request_proxy_candidates(profile_url, proxy):
        try:
            async with aiohttp.ClientSession(
                timeout=STEAM_REQUEST_TIMEOUT,
                headers={
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.6"
                },
                cookies={"timezoneOffset": f"{utc_offset_seconds},0"},
            ) as session:
                async with session.get(
                    profile_url,
                    proxy=request_proxy,
                    allow_redirects=True,
                ) as resp:
                    if resp.status != 200:
                        raise ValueError(f"status {resp.status}")
                    html = await resp.text()
                    break
        except Exception as exc:
            logger.warning(f"Failed to get Steam profile {steam_id}: {exc}")

    if html is None:
        return result

    doc = lxml_html.fromstring(html)

    title_text = _text_or_empty(doc.xpath("string(//title)"))
    if "::" in title_text:
        result["player_name"] = title_text.split("::", 1)[1].strip()

    summary = doc.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' profile_summary ')]")
    if summary:
        result["description"] = summary[0].text_content().strip()

    result["description"] = re.sub(r"<.*?>", "", result["description"]).strip()

    background_url = _match_first([r"background-image:\s*url\(\s*'([^']+)'\s*\)"], html)
    if background_url:
        result["background"] = await _fetch(
            background_url,
            default_background,
            proxy=proxy,
        )

    avatar_links = doc.xpath("//link[@rel='image_src']/@href")
    if avatar_links:
        avatar_url = avatar_links[0]
        avatar_name = avatar_url.split("/")[-1].split("_")[0]
        result["avatar"] = await _fetch(
            avatar_url,
            default_avatar,
            cache_file=cache_path / f"avatar_{avatar_name}.jpg",
            proxy=proxy,
        )

    recent_play_time = doc.xpath(
        "//div[contains(concat(' ', normalize-space(@class), ' '), ' recentgame_recentplaytime ')]"
    )
    if recent_play_time:
        result["recent_2_week_play_time"] = _text_or_empty(recent_play_time[0])

    for game in doc.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' recent_game ')]"):
        name_node = game.xpath(".//div[contains(concat(' ', normalize-space(@class), ' '), ' game_name ')]")
        image_urls = game.xpath(".//img[contains(concat(' ', normalize-space(@class), ' '), ' game_capsule ')]/@src")
        if not name_node or not image_urls:
            continue

        game_image_url = image_urls[0]
        game_image_parts = game_image_url.split("/")
        details = game.xpath(".//div[contains(concat(' ', normalize-space(@class), ' '), ' game_info_details ')]")
        details_text = _text_or_empty(details[0] if details else None)

        play_time = _match_first(
            [
                r"总时数\s*([\d.,]+)\s*小时",
                r"Total Hours\s*([\d.,]+)\s*hrs",
                r"([\d.,]+)\s*hours on record",
            ],
            details_text,
        )
        last_played = _match_first(
            [
                r"最后运行日期：\s*(.+?日)",
                r"Last Played:\s*(.+?)(?:Total Hours|$)",
            ],
            details_text,
        )

        achievements = []
        achievement_nodes = game.xpath(
            ".//div[contains(concat(' ', normalize-space(@class), ' '), ' game_info_achievement ')]"
        )
        for achievement in achievement_nodes:
            if "plus_more" in achievement.get("class", ""):
                continue
            achievement_urls = achievement.xpath(".//img/@src")
            if not achievement_urls:
                continue
            achievement_url = achievement_urls[0]
            achievement_parts = achievement_url.split("/")
            achievements.append(
                {
                    "name": achievement.get("data-tooltip-text", ""),
                    "image": await _fetch(
                        achievement_url,
                        default_achievement_image,
                        cache_file=cache_path
                        / f"achievement_{achievement_parts[-2]}_{achievement_parts[-1]}",
                        proxy=proxy,
                    ),
                }
            )

        completed = None
        total = None
        achievement_summary = game.xpath(
            ".//span[contains(concat(' ', normalize-space(@class), ' '), ' game_info_achievement_summary ')]"
        )
        if achievement_summary:
            remain = achievement_summary[0].xpath(
                ".//span[contains(concat(' ', normalize-space(@class), ' '), ' ellipsis ')]"
            )
            remain_text = _text_or_empty(remain[0] if remain else None)
            if "/" in remain_text:
                completed_text, total_text = remain_text.split("/", 1)
                try:
                    completed = int(completed_text.strip())
                    total = int(total_text.strip())
                except ValueError:
                    completed = None
                    total = None

        result["game_data"].append(
            {
                "game_name": _text_or_empty(name_node[0]),
                "play_time": play_time or "",
                "last_played": (
                    f"最后运行日期: {last_played}" if last_played else "当前正在游戏"
                ),
                "game_image": await _fetch(
                    game_image_url,
                    default_header_image,
                    cache_file=cache_path / f"header_{game_image_parts[-2]}.jpg",
                    proxy=proxy,
                ),
                "achievements": achievements,
                "completed_achievement_number": completed,
                "total_achievement_number": total,
            }
        )

    return result
