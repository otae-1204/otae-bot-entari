from __future__ import annotations

import time
from typing import Any

import httpx

from utils.http_client import fetch_json


API_CACHE_NAMESPACE = "endfield-api"
AKEDATA_MAX_TABLE_BYTES = 24 * 1024 * 1024
AKEDATA_MAX_RESOURCE_BYTES = 6 * 1024 * 1024
AKEDATA_MAX_ASSET_INDEX_BYTES = 8 * 1024 * 1024


class WarfarinAPIError(Exception):
    pass


class WarfarinClient:
    BASE_URL = "https://api.warfarin.wiki/v1"
    FZ_BASE_URL = "https://api.fz.wiki/api/v1"
    AKEDATA_BASE_URL = "https://data.akedata.wiki"

    def __init__(self, *, timeout: float = 12.0):
        self.timeout = timeout
        self.headers = {
            "User-Agent": "otae-bot-entari/1.0 (+https://github.com/otae-1204/otae-bot-entari)",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://warfarin.wiki/cn",
        }

    async def search(self, query: str, *, lang: str = "cn") -> dict[str, Any]:
        return await self._get_json(f"{self.BASE_URL}/{lang}/search", params={"q": query})

    async def operator_detail(self, slug: str, *, lang: str = "cn") -> dict[str, Any]:
        return await self._get_json(f"{self.BASE_URL}/{lang}/operators/{slug}")

    async def operators(self, *, lang: str = "cn") -> dict[str, Any]:
        return await self._get_json(f"{self.BASE_URL}/{lang}/operators")

    async def weapons(self, *, lang: str = "cn") -> dict[str, Any]:
        return await self._get_json(f"{self.BASE_URL}/{lang}/weapons")

    async def weapon_detail(self, slug: str, *, lang: str = "cn") -> dict[str, Any]:
        return await self._get_json(f"{self.BASE_URL}/{lang}/weapons/{slug}")

    async def fz_article_by_title(self, title: str, *, ns: int = 0, with_revision: bool = True) -> dict[str, Any]:
        return await self._get_json(
            f"{self.FZ_BASE_URL}/articles/by-title",
            params={"ns": ns, "title": title, "withRevision": 1 if with_revision else 0},
        )

    async def fz_article_summaries(self, prefix: str, *, ns: int = 0) -> dict[str, Any]:
        return await self._get_json(f"{self.FZ_BASE_URL}/articles/summaries", params={"ns": ns, "prefix": prefix})

    async def fz_articles(self, *, category: str = "", ns: int = 0) -> dict[str, Any]:
        params: dict[str, Any] = {"ns": ns, "all": 1}
        if category:
            params["category"] = category
        return await self._get_json(f"{self.FZ_BASE_URL}/articles", params=params)

    async def fz_search(self, query: str, *, limit: int = 8) -> dict[str, Any]:
        return await self._get_json(f"{self.FZ_BASE_URL}/search", params={"q": query, "limit": limit})

    async def fz_game_richtext(self) -> dict[str, Any]:
        return await self._get_json(f"{self.FZ_BASE_URL}/game-richtext")

    async def akedata_manifest(self) -> dict[str, Any]:
        return await self._get_json(
            f"{self.AKEDATA_BASE_URL}/manifest.json",
            params={"t": str(int(time.time() // 60))},
            ttl_seconds=60.0,
        )

    async def akedata_asset_index(self) -> dict[str, Any]:
        data = await self._get_json(
            f"{self.AKEDATA_BASE_URL}/asset-sync-index.json",
            max_bytes=AKEDATA_MAX_ASSET_INDEX_BYTES,
        )
        datasets = data.get("datasets")
        json_files = datasets.get("json", {}).get("files") if isinstance(datasets, dict) else None
        if data.get("schemaVersion") != 2 or not isinstance(json_files, dict):
            raise WarfarinAPIError("AkeData 资产索引结构异常")
        return data

    async def akedata_table(self, table_cfg_path: str, table_name: str) -> dict[str, Any]:
        path = str(table_cfg_path or "").strip("/")
        name = str(table_name or "").strip()
        if not path or not name or not name.replace("_", "").isalnum():
            raise WarfarinAPIError("AkeData 表路径无效")
        return await self._get_json(
            f"{self.AKEDATA_BASE_URL}/{path}/{name}.json",
            max_bytes=AKEDATA_MAX_TABLE_BYTES,
        )

    async def akedata_public_json(self, resource_path: str) -> dict[str, Any] | list[Any]:
        path = str(resource_path or "").strip().lstrip("/")
        parts = path.split("/")
        if (
            not path.startswith("public/Json/")
            or not path.endswith(".json")
            or any(not part or part in {".", ".."} for part in parts)
        ):
            raise WarfarinAPIError("AkeData 公共资源路径无效")
        data = await self._get_json_value(
            f"{self.AKEDATA_BASE_URL}/{path}",
            max_bytes=AKEDATA_MAX_RESOURCE_BYTES,
        )
        if not isinstance(data, (dict, list)):
            raise WarfarinAPIError("AkeData 返回结构异常")
        return data

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        max_bytes: int = 10 * 1024 * 1024,
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        data = await self._get_json_value(
            url,
            params=params,
            max_bytes=max_bytes,
            ttl_seconds=ttl_seconds,
        )
        if not isinstance(data, dict):
            if url.startswith(self.FZ_BASE_URL):
                source = "FZ Wiki"
            elif url.startswith(self.AKEDATA_BASE_URL):
                source = "AkeData"
            else:
                source = "Warfarin Wiki"
            raise WarfarinAPIError(f"{source} 返回结构异常")
        return data

    async def _get_json_value(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        max_bytes: int = 10 * 1024 * 1024,
        ttl_seconds: float | None = None,
    ) -> Any:
        if url.startswith(self.FZ_BASE_URL):
            source = "FZ Wiki"
        elif url.startswith(self.AKEDATA_BASE_URL):
            source = "AkeData"
        else:
            source = "Warfarin Wiki"
        fetch_kwargs: dict[str, Any] = {
            "namespace": API_CACHE_NAMESPACE,
            "params": params,
            "headers": self._headers_for(url),
            "timeout_seconds": self.timeout,
            "max_bytes": max_bytes,
        }
        if ttl_seconds is not None:
            fetch_kwargs["ttl_seconds"] = ttl_seconds
        try:
            data = await fetch_json(url, **fetch_kwargs)
        except httpx.TimeoutException as exc:
            raise WarfarinAPIError(f"{source} 请求超时") from exc
        except httpx.HTTPStatusError as exc:
            raise WarfarinAPIError(f"{source} HTTP {exc.response.status_code}") from exc
        except ValueError as exc:
            raise WarfarinAPIError(f"{source} 返回了无法解析的 JSON") from exc
        except httpx.HTTPError as exc:
            raise WarfarinAPIError(f"{source} 请求失败: {exc}") from exc
        return data

    def _headers_for(self, url: str) -> dict[str, str]:
        headers = dict(self.headers)
        if url.startswith(self.AKEDATA_BASE_URL):
            headers["Referer"] = "https://cf.akedata.top/"
            headers["Cache-Control"] = "no-cache"
            headers["Pragma"] = "no-cache"
        return headers
