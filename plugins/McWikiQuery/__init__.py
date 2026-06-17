"""Minecraft Wiki 查询插件 — 搜索并截图 wiki 页面."""

import httpx
import tempfile
from io import BytesIO
from loguru import logger
from lxml import html as _html
from PIL import Image as PILImage
from urllib.parse import quote_plus

from arclet.alconna import Args
from utils.entari_native import (
    ArgVal, ChainMsg, make_image, Reply,
)
from arclet.entari import Event

from configs.config import SYSTEM_PROXY
from utils.image_utils import screenshot_web_element
from utils.temp_files import schedule_temp_file_cleanup
from utils.entari_native import cmd_with_args as _cmd

wiki = _cmd("wiki", args=Args["content", str], aliases={"查wiki"})
etree = _html.etree
WIKI_BASE_URL = "https://zh.minecraft.wiki"
WIKI_CONTENT_SELECTOR = "#content, .mw-body"
WIKI_SCREENSHOT_MAX_HEIGHT = 50000
WIKI_IMAGE_CHUNK_HEIGHT = 7000


def _wiki_search_url(keyword: str) -> str:
    query = quote_plus(keyword)
    return (
        f"{WIKI_BASE_URL}/?search={query}"
        "&title=Special%3A%E6%90%9C%E7%B4%A2&profile=default&fulltext=1"
    )


def _wiki_article_url(path: str) -> str:
    return f"{WIKI_BASE_URL}{path}?variant=zh"


def _wiki_client_kwargs(proxy_url: str | None) -> dict:
    kwargs = {"timeout": 15, "follow_redirects": True}
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return kwargs


def _extract_first_result_path(html_text: str) -> str | None:
    if not html_text.strip():
        return None

    tree = etree.HTML(html_text)
    if tree is None:
        return None

    src = tree.xpath('//a[@data-serp-pos="0"]/@href')
    return src[0] if src else None


def _image_segment_from_png(png: bytes):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png)
        f.flush()
        schedule_temp_file_cleanup(f.name)
        return make_image(path=f.name)


def _image_segments_from_png(png: bytes, max_height: int = WIKI_IMAGE_CHUNK_HEIGHT):
    image = PILImage.open(BytesIO(png))
    width, height = image.size
    if height <= max_height:
        return [_image_segment_from_png(png)]

    segments = []
    for top in range(0, height, max_height):
        bottom = min(top + max_height, height)
        part = image.crop((0, top, width, bottom))
        buf = BytesIO()
        part.save(buf, format="PNG")
        segments.append(_image_segment_from_png(buf.getvalue()))
    return segments


def _event_message_id(event: Event) -> str | None:
    for attr in ("msg_id", "message_id", "id"):
        value = getattr(event, attr, None)
        if value:
            return str(value)

    message = getattr(event, "message", None)
    value = getattr(message, "id", None)
    if value:
        return str(value)

    return None


def _reply_segments(event: Event) -> list[Reply]:
    message_id = _event_message_id(event)
    return [Reply(message_id)] if message_id else []


def _reply_message(event: Event, *segments) -> ChainMsg:
    return ChainMsg([*_reply_segments(event), *segments])


async def _finish_reply(event: Event, text: str):
    await _reply_message(event, text).finish()


@wiki.handle()
async def handle_wiki(event: Event, content: ArgVal[str]):
    keyword = content.result.strip() if content.available else ""
    if not keyword:
        await _finish_reply(event, "用法: /wiki ＜搜索内容＞")

    proxy_url = SYSTEM_PROXY.get("http") if isinstance(SYSTEM_PROXY, dict) else None

    try:
        async with httpx.AsyncClient(**_wiki_client_kwargs(proxy_url)) as client:
            resp = await client.get(_wiki_search_url(keyword))
            resp.raise_for_status()
            path = _extract_first_result_path(resp.text)

        if path is None:
            await _finish_reply(event, f'未找到与 "{keyword}" 相关的内容')

        # 使用 ChainMsg 发送回复，不再用 CQ 字符串注入
        await _reply_message(event, "图片生成中, 请稍后").send()

        web_url = _wiki_article_url(path)
        try:
            image = await screenshot_web_element(
                web_url,
                WIKI_CONTENT_SELECTOR,
                max_height=WIKI_SCREENSHOT_MAX_HEIGHT,
            )
        except Exception:
            logger.exception("McWiki screenshot failed: url={}", web_url)
            await _finish_reply(event, "Wiki 页面截图失败，请稍后再试")

        try:
            await _reply_message(event, *_image_segments_from_png(image)).finish()
        except Exception:
            await _finish_reply(event, "消息可能被风控或出现其他问题, 请尝试重新查询")

    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code if e.response else "unknown"
        logger.exception("McWiki search HTTP error: status={}, keyword={}", status_code, keyword)
        await _finish_reply(event, f"Wiki 搜索请求失败: HTTP {status_code}")
    except httpx.TimeoutException:
        logger.exception("McWiki search timeout: keyword={}", keyword)
        await _finish_reply(event, "Wiki 搜索请求超时，请稍后再试")
    except httpx.RequestError:
        logger.exception("McWiki search request failed: keyword={}", keyword)
        await _finish_reply(event, "Wiki 搜索网络请求失败，请检查代理或稍后再试")
    except IndexError:
        logger.exception("McWiki search result parse failed: keyword={}", keyword)
        await _finish_reply(event, "Wiki 搜索结果解析失败，请稍后再试")
