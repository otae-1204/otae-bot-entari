"""更新日志的命令面。

职责边界与 ``plugins/radar/handlers.py`` 一致——这里只做四件事：

1. 解析参数（子命令 → 数据查询）；
2. 从 ``models.load_changelog()`` 取数据（本地 JSON，无网络）；
3. 调 ``formatters`` / ``presentation``；
4. 发送图片卡片，渲染或图片发送失败时回退到分段文本。

**不做**：不在 import 期读 ``.env``、不发网络请求、不注册定时任务
（``tests/test_architecture.py`` 会在无 ``.env`` 的临时沙箱里 import 本模块）。
"""

from __future__ import annotations

import asyncio

from arclet.entari import Session
from loguru import logger

from otae_bot.adapters.entari import ArgVal, Image, cmd as _cmd, get_rest, send

from . import formatters, presentation as views
from .models import Changelog, ChangelogDataError, Release, load_changelog
from .rendering import render_page

#: 单段回复上限（与 radar / hyw 一致）。
CHUNK_SIZE = 2000

#: 目录每页版本数。
PAGE_SIZE = 12

#: 子命令别名 → 内部键。
_SUBCOMMANDS: dict[str, str] = {
    "列表": "list",
    "目录": "list",
    "list": "list",
    "全部": "list",
    "统计": "stats",
    "stats": "stats",
    "帮助": "help",
    "help": "help",
    "?": "help",
}

#: 允许的命令别名。
ROOT_ALIASES = {"更新", "changelog", "版本"}

changelog_cmd = _cmd("更新日志", aliases=ROOT_ALIASES, priority=5, block=True)


def _chunks(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """按行切段，不把一行劈成两半。"""
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    buffer = ""
    for line in text.split("\n"):
        if buffer and len(buffer) + len(line) + 1 > size:
            chunks.append(buffer)
            buffer = line
        else:
            buffer = f"{buffer}\n{line}" if buffer else line
    if buffer:
        chunks.append(buffer)
    return chunks


async def _reply(session: Session, reply: list[str]) -> None:
    """先出图；渲染或发送失败时把完整文本发出去。"""
    if isinstance(reply, views.ChangelogReply) and reply.pages:
        try:
            # 先把所有页渲染完再发，避免渲染失败留下半张图。
            images = [await render_page(page) for page in reply.pages]
            for png in images:
                await send(session, [Image(raw=png)])
            return
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 图片边界必须保住文本
            logger.warning(
                "[changelog] card delivery fell back to text: {}", type(error).__name__
            )
    for chunk in _chunks("\n".join(reply)):
        await send(session, chunk)


def _release_number(changelog: Changelog, release: Release) -> int:
    return changelog.releases.index(release) + 1


def _detail(changelog: Changelog, release: Release) -> views.ChangelogReply:
    number = _release_number(changelog, release)
    lines = formatters.format_release(release, number=number, total=len(changelog.releases))
    pages = views.release_pages(release, number=number, total=len(changelog.releases))
    return views.ChangelogReply(lines, pages)


def _index(changelog: Changelog, page: int) -> views.ChangelogReply:
    lines = formatters.format_index(changelog, page=page, page_size=PAGE_SIZE)
    pages = views.index_pages(changelog, page=page, page_size=PAGE_SIZE)
    return views.ChangelogReply(lines, pages)


def _search(changelog: Changelog, query: str) -> views.ChangelogReply:
    results = changelog.search(query)
    # 唯一命中且不是序号时，直接给详情比给列表更有用。
    if len(results) == 1 and not query.strip().isdigit():
        return _detail(changelog, results[0])
    lines = formatters.format_search(results, query)
    pages = views.search_pages(results, query)
    return views.ChangelogReply(lines, pages)


@changelog_cmd.handle()
async def handle_changelog(rest: ArgVal, session: Session) -> None:
    """``/更新日志`` 总入口：默认给最新版本。"""
    try:
        changelog = load_changelog()
    except ChangelogDataError as error:
        logger.warning("[changelog] data unavailable: {}", error)
        await send(session, "更新日志数据暂时不可用，请稍后再试。")
        return

    tokens = [token for token in (get_rest(rest) or "").split() if token]
    if not tokens:
        lines = formatters.format_latest(changelog)
        await _reply(session, views.ChangelogReply(lines, views.latest_pages(changelog)))
        return

    head, *args = tokens
    key = _SUBCOMMANDS.get(head.casefold())
    if key == "help":
        await _reply(session, views.ChangelogReply(formatters.format_help(), views.help_pages()))
        return
    if key == "stats":
        await _reply(
            session,
            views.ChangelogReply(formatters.format_stats(changelog), views.stats_pages(changelog)),
        )
        return
    if key == "list":
        page = 1
        if args and args[0].isdigit():
            page = max(1, int(args[0]))
        await _reply(session, _index(changelog, page))
        return

    await _reply(session, _search(changelog, " ".join(tokens)))


__all__ = [
    "CHUNK_SIZE",
    "PAGE_SIZE",
    "ROOT_ALIASES",
    "changelog_cmd",
    "handle_changelog",
]
