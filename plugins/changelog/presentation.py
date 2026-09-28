"""更新日志的卡片视图：只读数据 → 已转义的结构化 HTML 片段。

这里不碰网络、不读文件、不写样式（样式在 ``assets/card.css``）。所有进入
HTML 的文本都过 ``escape``——数据虽然来自仓库内的 JSON，但渲染层不该依赖
「数据一定是干净的」这个假设。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from .models import KIND_LABELS, KIND_ORDER, Changelog, Release


@dataclass(frozen=True)
class ChangelogPage:
    """一张待渲染的卡片。``body`` 是已转义的 HTML 片段。"""

    title: str
    subtitle: str
    body: str
    section: str
    number: int = 1
    total: int = 1


class ChangelogReply(list[str]):
    """兼容纯文本的回复：文本用于降级，``pages`` 用于出图。"""

    def __init__(self, lines: list[str], pages: Sequence[ChangelogPage]):
        super().__init__(lines)
        self.pages = tuple(pages)


def text(value: object) -> str:
    return escape(str(value)) if value is not None else "—"


def _chips(release: Release) -> str:
    tags = "".join(f'<span class="cl-chip">{text(tag)}</span>' for tag in release.tags)
    return f'<div class="cl-meta"><span class="cl-chip accent">{text(release.version)}</span>{tags}</div>'


def _fact(label: str, value: object, unit: str) -> str:
    return f"<div><small>{text(label)}</small><b>{text(value)}<i>{text(unit)}</i></b></div>"


def _items(release: Release) -> str:
    groups, number = [], 0
    for kind in KIND_ORDER:
        items = [item for item in release.highlights if item.kind == kind]
        if not items:
            continue
        rows = []
        for item in items:
            number += 1
            stamp = f"{item.date} · " if item.date else ""
            rows.append(
                f'<div class="cl-item"><span class="cl-n">{number:02}</span><p>{text(item.text)}'
                f'<span class="cl-commits">{text(stamp + " ".join(item.commits))}</span></p></div>'
            )
        groups.append(
            f'<section class="cl-group kind-{text(kind)}"><h2 class="cl-gh"><i></i>{text(KIND_LABELS[kind])}'
            f"<em>{len(items)} 条</em></h2>{''.join(rows)}</section>"
        )
    return "".join(groups)


def release_pages(
    release: Release, *, number: int, total: int
) -> tuple[ChangelogPage, ...]:
    """单个版本的详情卡。"""
    body = _chips(release)
    if release.summary:
        body += f'<p class="cl-summary">{text(release.summary)}</p>'
    body += (
        '<div class="cl-facts">'
        + _fact("更新", len(release.highlights), "条")
        + _fact("提交", release.commit_count, "个")
        + _fact("提交日", len(release.dates) or 1, "天")
        + "</div>"
        + _items(release)
    )
    return (
        ChangelogPage(
            release.title,
            release.date_range,
            body,
            "版本",
            number,
            total,
        ),
    )


def latest_pages(changelog: Changelog) -> tuple[ChangelogPage, ...]:
    return release_pages(changelog.latest, number=1, total=len(changelog.releases))


def _row(release: Release, number: int | None = None) -> str:
    detail = " · ".join(
        part for part in ("/".join(release.tags), "、".join(release.kind_labels())) if part
    )
    index = f'<span class="cl-no">{number:02}</span>' if number is not None else ""
    return (
        f'<div class="cl-row{"" if number is not None else " cl-row-ver"}">{index}<div class="cl-main">'
        f'<div class="cl-top"><span class="cl-ver">{text(release.version)}</span>'
        f'<span class="cl-date">{text(release.date_range)}</span></div>'
        f'<b class="cl-title">{text(release.title)}</b>'
        f'<span class="cl-hint">{text(release.summary or "—")}</span>'
        + (f'<span class="cl-kinds">{text(detail)}</span>' if detail else "")
        + "</div></div>"
    )


def index_pages(
    changelog: Changelog, *, page: int = 1, page_size: int = 12
) -> tuple[ChangelogPage, ...]:
    """版本目录卡，按页切分。"""
    total_releases = len(changelog.releases)
    pages = max(1, -(-total_releases // page_size))
    page = min(max(1, page), pages)
    start = (page - 1) * page_size
    window = changelog.releases[start : start + page_size]
    body = (
        '<div class="cl-meta">'
        f'<span class="cl-chip accent">共 {total_releases} 个版本</span>'
        f'<span class="cl-chip">{changelog.highlight_count} 条更新</span>'
        f'<span class="cl-chip">第 {page}/{pages} 页</span></div>'
        + '<div class="cl-list">'
        + "".join(_row(release, offset) for offset, release in enumerate(window, start + 1))
        + "</div>"
        + '<div class="cl-note">序号 1 为最新版本。查看详情：/更新日志 &lt;版本号&gt;，'
        "例如 /更新日志 " + text(changelog.latest.version) + "；也可用 /更新日志 2026-09 "
        "或关键词检索。"
        + (f"翻页：/更新日志 列表 {min(page + 1, pages)}。" if pages > 1 else "")
        + "</div>"
    )
    return (
        ChangelogPage(
            "更新日志目录",
            f"{text(changelog.first_date)} 至今的全部版本",
            body,
            "目录",
            page,
            pages,
        ),
    )


def search_pages(
    results: tuple[Release, ...], query: str, *, limit: int = 6
) -> tuple[ChangelogPage, ...]:
    if not results:
        body = (
            '<div class="cl-note">'
            f"没有找到与「{text(query)}」相关的更新。试试 /更新日志 列表 看全部版本。</div>"
        )
    else:
        extra = (
            f'<div class="cl-note">只显示前 {limit} 个，共 {len(results)} 个版本。</div>'
            if len(results) > limit
            else ""
        )
        body = (
            f'<div class="cl-meta"><span class="cl-chip accent">关键词 {text(query)}</span>'
            f'<span class="cl-chip">{len(results)} 个版本</span></div>'
            + '<div class="cl-list">'
            + "".join(_row(release) for release in results[:limit])
            + "</div>"
            + extra
        )
    return (
        ChangelogPage(
            "更新检索",
            f"关键词：{text(query)}",
            body,
            "检索",
        ),
    )


def stats_pages(changelog: Changelog) -> tuple[ChangelogPage, ...]:
    counted: dict[str, int] = {}
    for release in changelog.releases:
        for kind, value in release.counts().items():
            counted[kind] = counted.get(kind, 0) + value
    busiest = max(counted.values(), default=1)
    tallies = "".join(
        f'<div class="cl-tally kind-{text(kind)}">'
        f'<span class="cl-tally-name">{text(KIND_LABELS.get(kind, kind))}</span>'
        '<span class="cl-tally-track">'
        f'<span class="cl-tally-bar" style="width:{count / busiest * 100:.1f}%"></span></span>'
        f'<span class="cl-tally-count">{count} 条</span></div>'
        for kind, count in ((kind, counted[kind]) for kind in KIND_ORDER if kind in counted)
    )
    releases = tuple(reversed(changelog.releases))
    tallest = max((len(release.highlights) for release in releases), default=1)
    columns = "".join(
        f'<div class="cl-col"><b>{len(release.highlights)}</b>'
        f'<i style="height:{len(release.highlights) / tallest * 150:.0f}px"></i>'
        f"<span>{text(release.version.removeprefix('v').removesuffix('.0'))}</span></div>"
        for release in releases
    )
    body = (
        '<div class="cl-facts">'
        + _fact("版本", len(changelog.releases), "个")
        + _fact("更新条数", changelog.highlight_count, "条")
        + _fact("覆盖提交", changelog.commit_count, "个")
        + "</div>"
        f'<div class="cl-meta"><span class="cl-chip">当前提交 {text(changelog.head)}</span></div>'
        '<h2 class="cl-sec">各版本更新条数<em>从早到晚</em></h2>'
        f'<div class="cl-columns">{columns}</div>'
        '<h2 class="cl-sec">按类型<em>全部版本合计</em></h2>' + tallies
    )
    return (
        ChangelogPage(
            "更新统计",
            f"{text(changelog.first_date)} ~ {text(changelog.last_date)}",
            body,
            "统计",
        ),
    )


def help_pages() -> tuple[ChangelogPage, ...]:
    commands = (
        ("/更新日志", "看最近一次版本更新（默认命令）"),
        ("/更新日志 列表 [页码]", "全部版本目录，每页 12 个"),
        ("/更新日志 <版本号>", "例如 /更新日志 v1.13.0"),
        ("/更新日志 <序号>", "按序号查看，1 为最新版本"),
        ("/更新日志 <日期>", "例如 /更新日志 2026-09-05，也支持 /更新日志 2026-09"),
        ("/更新日志 <关键词>", "在版本号、标题、概括、标签与正文里检索"),
        ("/更新日志 统计", "版本、更新条数与提交数汇总"),
        ("/更新日志 帮助", "显示本帮助"),
    )
    rows = "".join(
        f'<div class="cl-command"><span class="cl-n">{index:02}</span><code>{text(command)}</code><span>{text(hint)}</span></div>'
        for index, (command, hint) in enumerate(commands, 1)
    )
    body = (
        '<p class="cl-summary">让群里的每个人都能查到我们每个版本改了什么。</p>'
        + rows
        + '<div class="cl-note">别名：/更新、/changelog、/版本。'
        "版本号按时间段划定（仓库没有打 tag）；合并提交不计入。</div>"
    )
    return (
        ChangelogPage(
            "更新日志用法",
            "命令一览",
            body,
            "帮助",
        ),
    )


__all__ = [
    "ChangelogPage",
    "ChangelogReply",
    "help_pages",
    "index_pages",
    "latest_pages",
    "release_pages",
    "search_pages",
    "stats_pages",
    "text",
]
