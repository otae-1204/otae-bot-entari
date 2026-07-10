from __future__ import annotations

from typing import Any

import httpx

from utils.http_client import fetch_json


API_CACHE_NAMESPACE = "endfield-api"


class WarfarinAPIError(Exception):
    pass


class WarfarinClient:
    BASE_URL = "https://api.warfarin.wiki/v1"
    FZ_BASE_URL = "https://api.fz.wiki/api/v1"

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

    async def fz_search(self, query: str, *, limit: int = 8) -> dict[str, Any]:
        return await self._get_json(f"{self.FZ_BASE_URL}/search", params={"q": query, "limit": limit})

    async def fz_game_richtext(self) -> dict[str, Any]:
        return await self._get_json(f"{self.FZ_BASE_URL}/game-richtext")

    async def _get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        source = "FZ Wiki" if url.startswith(self.FZ_BASE_URL) else "Warfarin Wiki"
        try:
            data = await fetch_json(
                url,
                namespace=API_CACHE_NAMESPACE,
                params=params,
                headers=self.headers,
                timeout_seconds=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise WarfarinAPIError(f"{source} 请求超时") from exc
        except httpx.HTTPStatusError as exc:
            raise WarfarinAPIError(f"{source} HTTP {exc.response.status_code}") from exc
        except ValueError as exc:
            raise WarfarinAPIError(f"{source} 返回了无法解析的 JSON") from exc
        except httpx.HTTPError as exc:
            raise WarfarinAPIError(f"{source} 请求失败: {exc}") from exc
        if not isinstance(data, dict):
            raise WarfarinAPIError(f"{source} 返回结构异常")
        return data
