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
    why: str
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


def _meta(release: Release) -> str:
    tags = "".join(f'<span class="cl-tag">{text(tag)}</span>' for tag in release.tags)
    return (
        '<div class="cl-meta">'
        f"<span>{text(release.date_range)}</span>{tags}"
        f"<span>{len(release.highlights)} 条更新</span>"
        f"<span>{release.commit_count} 个提交</span>"
        "</div>"
    )


def _items(release: Release) -> str:
    rows = []
    for item in release.highlights:
        stamp = f"{item.date} · " if item.date else ""
        rows.append(
            f'<div class="cl-item"><span class="cl-kind {text(item.kind)}">'
            f"{text(item.label)}</span><p>{text(item.text)}"
            f'<span class="cl-commits">{text(stamp + " ".join(item.commits))}</span></p></div>'
        )
    return "".join(rows)


def release_pages(
    release: Release, *, number: int, total: int, why: str | None = None
) -> tuple[ChangelogPage, ...]:
    """单个版本的详情卡。"""
    body = _meta(release)
    if release.summary:
        body += (
            '<div class="cl-summary"><span class="cl-eyebrow">一句话概括</span>'
            f"{text(release.summary)}</div>"
        )
    body += _items(release)
    return (
        ChangelogPage(
            release.title,
            f"{release.version} · {release.date_range} / RELEASE NOTES",
            body,
            why or "版本号按时间段划定（仓库没有打 tag）；每条更新都对应仓库里的真实提交，"
            "合并提交不计入，所以这里的提交数总和等于非合并提交数。",
            "版本详情",
            number,
            total,
        ),
    )


def latest_pages(changelog: Changelog) -> tuple[ChangelogPage, ...]:
    return release_pages(changelog.latest, number=1, total=len(changelog.releases))


def index_pages(
    changelog: Changelog, *, page: int = 1, page_size: int = 12
) -> tuple[ChangelogPage, ...]:
    """版本目录卡，按页切分。"""
    total_releases = len(changelog.releases)
    pages = max(1, -(-total_releases // page_size))
    page = min(max(1, page), pages)
    start = (page - 1) * page_size
    window = changelog.releases[start : start + page_size]

    rows = []
    for offset, release in enumerate(window, start + 1):
        detail = " · ".join(
            part for part in ("/".join(release.tags), "、".join(release.kind_labels())) if part
        )
        rows.append(
            f'<div class="cl-row"><span class="cl-no">{offset:02}</span>'
            f'<span class="cl-ver">{text(release.version)}</span>'
            f'<span class="cl-title">{text(release.title)}'
            f'<span class="cl-hint">{text(release.summary or "—")}'
            f"（{text(release.date_range)}"
            f"{' · ' + text(detail) if detail else ''}）</span></span></div>"
        )
    body = (
        '<div class="cl-meta">'
        f"<span>共 {total_releases} 个版本</span>"
        f"<span>{text(changelog.first_date)} ~ {text(changelog.last_date)}</span>"
        f"<span>{changelog.highlight_count} 条更新</span>"
        f"<span>第 {page}/{pages} 页</span></div>"
        + "".join(rows)
        + '<div class="cl-note">序号 1 为最新版本。查看详情：/更新日志 &lt;版本号&gt;，'
        "例如 /更新日志 " + text(changelog.latest.version) + "；也可用 /更新日志 2026-09 "
        "或关键词检索。"
        + (f"翻页：/更新日志 列表 {min(page + 1, pages)}。" if pages > 1 else "")
        + "</div>"
    )
    return (
        ChangelogPage(
            "更新日志目录",
            f"{text(changelog.first_date)} 至今 / ALL VERSIONS",
            body,
            "先看目录再挑版本：目录只给版本号、日期与一句话概括，细节留在详情卡里。",
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
        rows = []
        for release in results[:limit]:
            rows.append(
                f'<div class="cl-row cl-row-ver">'
                f'<span class="cl-ver">{text(release.version)}</span>'
                f'<span class="cl-title">{text(release.title)}'
                f'<span class="cl-hint">{text(release.summary or "—")}'
                f"（{text(release.date_range)}）</span></span></div>"
            )
        extra = (
            f'<div class="cl-note">只显示前 {limit} 个，共 {len(results)} 个版本。</div>'
            if len(results) > limit
            else ""
        )
        body = (
            f'<div class="cl-meta"><span>关键词 {text(query)}</span>'
            f"<span>{len(results)} 个版本</span></div>" + "".join(rows) + extra
        )
    return (
        ChangelogPage(
            "更新检索",
            f"关键词：{text(query)} / SEARCH",
            body,
            "关键词会在版本号、日期、标题、概括、标签与每条更新正文里匹配。",
            "检索",
        ),
    )


def stats_pages(changelog: Changelog) -> tuple[ChangelogPage, ...]:
    counted: dict[str, int] = {}
    for release in changelog.releases:
        for kind, value in release.counts().items():
            counted[kind] = counted.get(kind, 0) + value
    busiest = max(counted.values(), default=1)
    rows = "".join(
        '<div class="cl-tally">'
        f'<span class="cl-tally-name">{text(KIND_LABELS.get(kind, kind))}</span>'
        '<span class="cl-tally-track">'
        f'<span class="cl-tally-bar" style="width:{count / busiest * 100:.1f}%"></span></span>'
        f'<span class="cl-tally-count">{count} 条</span></div>'
        for kind, count in ((kind, counted[kind]) for kind in KIND_ORDER if kind in counted)
    )
    body = (
        '<div class="cl-stats">'
        f"<div><span>版本</span><strong>{len(changelog.releases)}</strong></div>"
        f"<div><span>更新条数</span><strong>{changelog.highlight_count}</strong></div>"
        f"<div><span>覆盖提交</span><strong>{changelog.commit_count}</strong></div>"
        "</div>"
        f'<div class="cl-meta"><span>{text(changelog.first_date)} ~ {text(changelog.last_date)}</span>'
        f"<span>当前提交 {text(changelog.head)}</span></div>" + rows
        + '<div class="cl-note">scripts/generate_changelog.py --check 会校验'
        "「每个非合并提交恰好出现一次」，所以这里的提交数不会多也不会少。</div>"
    )
    return (
        ChangelogPage(
            "更新统计",
            f"{text(changelog.first_date)} ~ {text(changelog.last_date)} / STATS",
            body,
            "统计用来回答「我们一共做了多少事」，不改变任何一条更新的口径。",
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
        f'<div class="cl-command"><code>{text(command)}</code><span>{text(hint)}</span></div>'
        for command, hint in commands
    )
    body = (
        '<div class="cl-summary"><span class="cl-eyebrow">更新日志</span>'
        "让群里的每个人都能查到我们每个版本改了什么。</div>" + rows
        + '<div class="cl-note">别名：/更新、/changelog、/版本。'
        "版本号按时间段划定（仓库没有打 tag）；合并提交不计入。</div>"
    )
    return (
        ChangelogPage(
            "更新日志用法",
            "命令一览 / COMMANDS",
            body,
            "不确定看哪个版本时，先 /更新日志 看最近一次，或 /更新日志 列表 从目录里挑。",
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
