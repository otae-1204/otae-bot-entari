"""更新日志的纯文本渲染：不碰网络、不读文件、不拼 HTML。

文本是图片的降级通道——渲染或发送失败时用户仍能拿到完整内容，所以这里的
输出必须自成一体：版本号、日期区间、每条更新的类型与说明都要在文本里。
"""

from __future__ import annotations

from .models import KIND_LABELS, KIND_ORDER, Changelog, Release

#: 脚注里回显数据边界，避免用户以为日志比实际更新。
FOOTNOTE = "版本号按时间段划定；每条更新都对应仓库里的真实提交，合并提交不计入。"


def _header(release: Release, *, number: int, total: int) -> str:
    tags = f"（{'/'.join(release.tags)}）" if release.tags else ""
    return (
        f"{release.version} · {release.title}{tags}\n"
        f"{release.date_range} · {release.commit_count} 个提交 · "
        f"第 {number}/{total} 个版本"
    )


def format_release(release: Release, *, number: int, total: int) -> list[str]:
    """单个版本的详细文本。"""
    lines = [_header(release, number=number, total=total)]
    if release.summary:
        lines += ["", release.summary]
    lines.append("")
    for item in release.highlights:
        lines.append(f"[{item.label}] {item.text}")
    lines += ["", f"提交：{' '.join(release.commits)}", FOOTNOTE]
    return lines


def format_latest(changelog: Changelog) -> list[str]:
    lines = format_release(changelog.latest, number=1, total=len(changelog.releases))
    lines.append(
        f"查看全部 {len(changelog.releases)} 个版本：/更新日志 列表"
        f"；按版本号或序号查看：/更新日志 {changelog.latest.version}、/更新日志 2"
    )
    return lines


def format_index(changelog: Changelog, *, page: int = 1, page_size: int = 12) -> list[str]:
    """版本目录，分页。序号 1 为最新版本。"""
    total = len(changelog.releases)
    pages = max(1, -(-total // page_size))
    page = min(max(1, page), pages)
    start = (page - 1) * page_size
    window = changelog.releases[start : start + page_size]
    lines = [
        f"更新日志 · 共 {total} 个版本"
        f"（{changelog.first_date} ~ {changelog.last_date}）"
        f"，{changelog.highlight_count} 条更新 · 第 {page}/{pages} 页"
    ]
    for offset, release in enumerate(window, start + 1):
        tags = f" [{'/'.join(release.tags)}]" if release.tags else ""
        kinds = "、".join(release.kind_labels())
        lines.append(
            f"{offset:>2}. {release.version} {release.date_range} "
            f"{release.title}{tags}（{kinds}）"
        )
    lines.append(
        "查看详情：/更新日志 <版本号|序号>，例如 /更新日志 "
        f"{changelog.latest.version} 或 /更新日志 1；也可按关键词检索。"
    )
    if pages > 1:
        lines.append(f"翻页：/更新日志 列表 {min(page + 1, pages)}")
    return lines


def format_search(results: tuple[Release, ...], query: str, *, limit: int = 6) -> list[str]:
    if not results:
        return [f"没有找到与「{query}」相关的更新。试试 /更新日志 列表 看全部版本。"]
    lines = [f"与「{query}」相关的更新（{len(results)} 个版本）："]
    for release in results[:limit]:
        lines.append(
            f"{release.version} {release.date_range} {release.title} — {release.summary}"
        )
    if len(results) > limit:
        lines.append(f"（只显示前 {limit} 个，共 {len(results)} 个版本）")
    lines.append(f"查看详情：/更新日志 {results[0].version}")
    return lines


def format_stats(changelog: Changelog) -> list[str]:
    counted: dict[str, int] = {}
    for release in changelog.releases:
        for kind, value in release.counts().items():
            counted[kind] = counted.get(kind, 0) + value
    return [
        f"更新日志统计：{len(changelog.releases)} 个版本，"
        f"{changelog.highlight_count} 条更新，{changelog.commit_count} 个提交。",
        f"时间范围：{changelog.first_date} ~ {changelog.last_date}",
        "按类型：" + "，".join(
            f"{KIND_LABELS.get(kind, kind)} {counted[kind]}"
            for kind in KIND_ORDER
            if kind in counted
        ),
        "每条更新都对应仓库里的真实提交；"
        "scripts/generate_changelog.py --check 会校验不重不漏。",
    ]


def format_help() -> list[str]:
    return [
        "更新日志：/更新日志 —— 看最近一次版本更新",
        "/更新日志 列表 [页码] —— 全部版本目录，每页 12 个",
        "/更新日志 <版本号|序号> —— 查看指定版本，例如 /更新日志 v1.13.0、/更新日志 1",
        "/更新日志 <日期|关键词> —— 例如 /更新日志 2026-09-05、/更新日志 雷达",
        "/更新日志 统计 —— 版本、更新条数与提交数汇总",
        "/更新日志 帮助 —— 显示本帮助",
        "别名：/更新、/changelog、/版本",
        FOOTNOTE,
    ]


__all__ = [
    "FOOTNOTE",
    "format_help",
    "format_index",
    "format_latest",
    "format_release",
    "format_search",
    "format_stats",
]
