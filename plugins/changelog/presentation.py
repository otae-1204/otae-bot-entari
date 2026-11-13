"""更新日志的卡片视图：只读数据 → 已转义的结构化 HTML 片段。

这里不碰网络、不读文件、不写样式（样式在 ``assets/card.css``）。所有进入
HTML 的文本都过 ``escape``——数据虽然来自仓库内的 JSON，但渲染层不该依赖
「数据一定是干净的」这个假设。

``ChangelogPage`` 的 ``title`` / ``subtitle`` 是横幅上的原始文本，由
``rendering.page_html`` 负责转义；``body`` 是这里已经转义好的面板内容。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from otae_bot.infrastructure.changelog import KIND_LABELS, KIND_ORDER, Changelog, Release


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


#: 版本页有这么多条更新时，正文分两栏排。
TWO_COLUMN_MIN_ITEMS = 3


def _cell(label: str, value: object, unit: str = "", note: str = "") -> str:
    """顶部概览条里的一格：小标签、大数字、单位、一行注脚。"""
    unit_html = f"<i>{text(unit)}</i>" if unit else ""
    note_html = f"<em>{text(note)}</em>" if note else ""
    return f"<div><small>{text(label)}</small><b>{text(value)}{unit_html}</b>{note_html}</div>"


def _strip(*cells: str, extra: str = "") -> str:
    return f'<div class="cl-strip{extra}">{"".join(cells)}</div>'


def _heading(title: str, note: str = "") -> str:
    note_html = f"<em>{text(note)}</em>" if note else ""
    return f'<h2 class="cl-heading">{text(title)}{note_html}</h2>'


def _note(message: str, label: str = "") -> str:
    """一条说明。``message`` 由调用方负责转义（里面可能带 ``&lt;版本号&gt;``）。"""
    label_html = f"<b>{text(label)}</b>" if label else ""
    return f'<p class="cl-note">{label_html}{message}</p>'


def _kind_counts(release: Release) -> str:
    counts = release.counts()
    return "".join(
        f'<span class="kind-{text(kind)}"><i></i>{text(KIND_LABELS[kind])} {counts[kind]}</span>'
        for kind in KIND_ORDER
        if kind in counts
    )


def _items(release: Release) -> str:
    groups, number = [], 0
    for kind in KIND_ORDER:
        items = [item for item in release.highlights if item.kind == kind]
        if not items:
            continue
        rows = []
        for item in items:
            number += 1
            # Hashes stay in changelog.json for the checker; readers only need date and size.
            stamp = f"{item.date} · " if item.date else ""
            rows.append(
                f'<div class="cl-item"><span class="cl-n">{number:02}</span><p>{text(item.text)}'
                f'<small>{text(f"{stamp}{len(item.commits)} 个提交")}</small></p></div>'
            )
        groups.append(
            f'<section class="cl-group kind-{text(kind)}"><h2 class="cl-heading"><i></i>{text(KIND_LABELS[kind])}'
            f"<em>{len(items)} 条</em></h2>{''.join(rows)}</section>"
        )
    columns = " cl-two" if len(release.highlights) >= TWO_COLUMN_MIN_ITEMS else ""
    return f'<div class="cl-items{columns}">{"".join(groups)}</div>'


def release_pages(
    release: Release, *, number: int, total: int
) -> tuple[ChangelogPage, ...]:
    """单个版本的详情卡。"""
    tags = "".join(f'<span class="cl-tag">{text(tag)}</span>' for tag in release.tags)
    head = (
        f'<div class="cl-head"><h2>{text(release.title)}</h2>'
        f'<p><span class="cl-ver">{text(release.version)}</span>{tags}{text(release.date_range)}</p></div>'
    )
    body = _strip(
        head,
        _cell("更新", len(release.highlights), "条"),
        _cell("提交", release.commit_count, "个"),
        _cell("提交日", len(release.dates) or 1, "天"),
        extra=" cl-release",
    )
    if release.summary:
        body += f'<p class="cl-summary">{text(release.summary)}</p>'
    body += _items(release)
    return (
        ChangelogPage(
            "更新日志",
            f"第 {number} / {total} 个版本 · /更新日志 列表 查看全部",
            body,
            "版本",
            number,
            total,
        ),
    )


def latest_pages(changelog: Changelog) -> tuple[ChangelogPage, ...]:
    return release_pages(changelog.latest, number=1, total=len(changelog.releases))


def _row(release: Release, number: int | None = None) -> str:
    """目录与检索共用的一行。检索结果没有目录序号，不留序号列。"""
    if number is None:
        index, classes = "", "cl-row cl-row-ver"
    else:
        index = f'<span class="cl-no">{number:02}</span>'
        classes = "cl-row cl-newest" if number == 1 else "cl-row"
    meta = " · ".join((release.date_range, *release.tags))
    return (
        f'<div class="{classes}">{index}<div class="cl-main">'
        f'<div class="cl-top"><b>{text(release.version)}</b><span>{text(meta)}</span></div>'
        f'<span class="cl-title">{text(release.title)}</span>'
        f'<span class="cl-hint">{text(release.summary or "—")}</span>'
        f'<div class="cl-kinds">{_kind_counts(release)}</div></div></div>'
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
    tip = (
        f"/更新日志 &lt;版本号&gt;，例如 /更新日志 {text(changelog.latest.version)}；"
        "也可用 /更新日志 2026-09 或关键词检索。"
    )
    if pages > 1:
        tip += f"翻页：/更新日志 列表 {min(page + 1, pages)}。"
    body = (
        _strip(
            _cell("版本", total_releases, "个", f"{changelog.first_date} 至今"),
            _cell("更新", changelog.highlight_count, "条", "全部版本合计"),
            _cell("本页", page, f"/ {pages}", f"每页 {page_size} 个，序号 1 为最新"),
        )
        + '<div class="cl-rows">'
        + "".join(_row(release, offset) for offset, release in enumerate(window, start + 1))
        + "</div>"
        + _note(tip, "查看")
    )
    return (
        ChangelogPage(
            "更新日志目录",
            "全部版本，最新在前",
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
            _strip(_cell("关键词", query, note="没有命中任何版本"), extra=" cl-query")
            + _note(f"没有找到与「{text(query)}」相关的更新。")
            + _note("试试 /更新日志 列表 看全部版本，或换一个更短的关键词。")
        )
    else:
        extra = (
            _note(f"只显示前 {limit} 个，共 {len(results)} 个版本。换个更具体的关键词，或直接查版本号。")
            if len(results) > limit
            else ""
        )
        body = (
            _strip(
                _cell("关键词", query, note="版本号、标题、概括、标签与正文"),
                _cell("命中版本", len(results), "个", "按时间倒序"),
                extra=" cl-query",
            )
            + '<div class="cl-rows">'
            + "".join(_row(release) for release in results[:limit])
            + "</div>"
            + extra
        )
    return (
        ChangelogPage(
            "更新检索",
            f"关键词：{query}",
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
        f'<div class="cl-tally kind-{text(kind)}"><b>{text(KIND_LABELS.get(kind, kind))}</b>'
        f'<span class="cl-track"><i style="width:{count / busiest * 100:.1f}%"></i></span>'
        f"<span>{count} 条</span></div>"
        for kind, count in ((kind, counted[kind]) for kind in KIND_ORDER if kind in counted)
    )
    releases = tuple(reversed(changelog.releases))
    tallest = max((len(release.highlights) for release in releases), default=1)
    peak = next((release for release in releases if len(release.highlights) == tallest), None)
    columns = "".join(
        f'<i class="{"cl-peak" if release is peak else ""}" '
        f'style="height:{len(release.highlights) / tallest * 100:.0f}%"></i>'
        for release in releases
    )
    # 横轴只标首、三分点与尾，51 个版本号挤在一起读不出来。
    ticks = sorted({0, len(releases) // 3, 2 * len(releases) // 3, len(releases) - 1}) if releases else []
    axis = "".join(f"<span>{text(releases[index].version)}</span>" for index in ticks)
    count = len(changelog.releases)
    average = f"平均每版 {changelog.highlight_count / count:.1f} 条" if count else ""
    peak_note = f"从早到晚 · 最多的是 {peak.version}（{tallest} 条）" if peak else "从早到晚"
    body = (
        _strip(
            _cell("版本", count, "个", f"{changelog.first_date} ~ {changelog.last_date}"),
            _cell("更新条数", changelog.highlight_count, "条", average),
            _cell("覆盖提交", changelog.commit_count, "个", f"当前提交 {changelog.head}"),
        )
        + '<div class="cl-cols cl-stats">'
        + f'<section>{_heading("各版本更新条数", peak_note)}'
        f'<div class="cl-chart">{columns}</div><div class="cl-axis">{axis}</div></section>'
        + f'<section>{_heading("按类型", "全部版本合计")}{tallies}</section>'
        + "</div>"
    )
    return (
        ChangelogPage(
            "更新统计",
            "版本、更新条数与提交数汇总",
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
    rows = [
        f'<div class="cl-item cl-command"><span class="cl-n">{index:02}</span>'
        f"<p><code>{text(command)}</code>{text(hint)}</p></div>"
        for index, (command, hint) in enumerate(commands, 1)
    ]
    half = -(-len(rows) // 2)
    body = (
        '<p class="cl-summary">让群里的每个人都能查到我们每个版本改了什么。</p>'
        f'<div class="cl-cols"><section>{"".join(rows[:half])}</section>'
        f'<section>{"".join(rows[half:])}</section></div>'
        + _note("/更新、/changelog、/版本", "别名")
        + _note("版本号按时间段划定（仓库没有打 tag）；合并提交不计入。")
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
