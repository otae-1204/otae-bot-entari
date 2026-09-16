"""更新日志的只读数据模型与加载校验。

数据来自同目录的 ``changelog.json``：按版本号聚合的更新批次，每条更新都带上
它对应的提交与提交日期。这里**不出网、不读环境变量、不注册任何事件**，只做
解析与校验（框架 §2.1：业务模型与纯转换函数不注册事件、不实例化机器人）。

版本号由维护者按时间段划定（仓库没有打 tag），因此数据里既有版本号也有日期
区间。覆盖保证由 ``scripts/generate_changelog.py --check`` 在仓库侧强制：
每个非 merge 提交恰好出现在一个版本的一条更新里。本模块负责另一半——数据
本身自洽，坏数据要显式报错而不是静默降级成空列表。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_FILE = Path(__file__).parent / "changelog.json"

#: 更新类型 → 中文标签。顺序即卡片里的展示顺序。
KIND_LABELS: dict[str, str] = {
    "feat": "新功能",
    "fix": "修复",
    "perf": "性能",
    "refactor": "重构",
    "docs": "文档",
    "chore": "维护",
    "style": "样式",
    "test": "测试",
}
KIND_ORDER: tuple[str, ...] = tuple(KIND_LABELS)


class ChangelogDataError(RuntimeError):
    """更新日志数据缺失或不可解析。"""


@dataclass(frozen=True)
class Highlight:
    """一条面向用户的更新说明，覆盖一到多个提交。"""

    kind: str
    text: str
    commits: tuple[str, ...]
    date: str = ""

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)


@dataclass(frozen=True)
class Release:
    """一个版本（一个更新批次）。"""

    version: str
    date: str
    start_date: str
    end_date: str
    title: str
    summary: str
    tags: tuple[str, ...]
    highlights: tuple[Highlight, ...]

    @property
    def commits(self) -> tuple[str, ...]:
        return tuple(short for item in self.highlights for short in item.commits)

    @property
    def commit_count(self) -> int:
        return len(self.commits)

    @property
    def dates(self) -> tuple[str, ...]:
        """本版本涉及的提交日期（去重、倒序）。"""
        return tuple(sorted({item.date for item in self.highlights if item.date}, reverse=True))

    @property
    def date_range(self) -> str:
        """``2026-09-10 ~ 2026-09-12``；单日版本只显示一天。"""
        if not self.start_date or self.start_date == self.end_date:
            return self.end_date or self.date
        return f"{self.start_date} ~ {self.end_date}"

    def kinds(self) -> tuple[str, ...]:
        present = {item.kind for item in self.highlights}
        return tuple(kind for kind in KIND_ORDER if kind in present)

    def kind_labels(self) -> tuple[str, ...]:
        """本版本出现的类型，取中文标签（``kinds()`` 返回的是原始代码）。"""
        return tuple(KIND_LABELS[kind] for kind in self.kinds())

    def counts(self) -> dict[str, int]:
        counted: dict[str, int] = {}
        for item in self.highlights:
            counted[item.kind] = counted.get(item.kind, 0) + 1
        return counted


@dataclass(frozen=True)
class Changelog:
    """全部版本，按时间倒序（最新的在前）。"""

    head: str
    first_date: str
    last_date: str
    commit_count: int
    releases: tuple[Release, ...]

    @property
    def latest(self) -> Release:
        return self.releases[0]

    @property
    def highlight_count(self) -> int:
        return sum(len(release.highlights) for release in self.releases)

    def by_number(self, number: int) -> Release | None:
        """1 表示最新版本。"""
        if 1 <= number <= len(self.releases):
            return self.releases[number - 1]
        return None

    def by_version(self, version: str) -> Release | None:
        """``v1.2.0``、``1.2.0``、``1.2`` 都能定位到版本。"""
        wanted = version.strip().casefold().removeprefix("v")
        for release in self.releases:
            current = release.version.casefold().removeprefix("v")
            if current == wanted or current.startswith(wanted + "."):
                return release
        return None

    def search(self, query: str) -> tuple[Release, ...]:
        """按版本号、日期前缀或关键词查找版本。空查询返回全部。"""
        text = (query or "").strip()
        if not text:
            return self.releases
        if text.isdigit():
            found = self.by_number(int(text))
            return (found,) if found else ()
        found = self.by_version(text)
        if found is not None and text[:1].casefold() in {"v", *"0123456789"}:
            return (found,)
        if _looks_like_date(text):
            return tuple(item for item in self.releases if _release_in_date(item, text))
        needle = text.casefold()
        matched = []
        for release in self.releases:
            haystack = " ".join(
                (release.version, release.date, release.date_range, release.title,
                 release.summary, *release.tags,
                 *(f"{item.label} {item.text} {item.date}" for item in release.highlights))
            ).casefold()
            if needle in haystack:
                matched.append(release)
        return tuple(matched)


def _release_in_date(release: Release, prefix: str) -> bool:
    """版本区间内任一提交日期（或版本结束日）匹配日期前缀。"""
    if release.date.startswith(prefix) or release.start_date.startswith(prefix):
        return True
    return any(item.date.startswith(prefix) for item in release.highlights)


def _looks_like_date(text: str) -> bool:
    parts = text.split("-")
    return 2 <= len(parts) <= 3 and all(part.isdigit() for part in parts)


def _parse_highlight(label: str, item: object) -> Highlight:
    if not isinstance(item, dict):
        raise ChangelogDataError(f"{label} 必须是对象。")
    kind = str(item.get("kind") or "").strip()
    if kind not in KIND_LABELS:
        raise ChangelogDataError(f"{label} 的 kind「{kind}」不在 {list(KIND_LABELS)} 内。")
    text = str(item.get("text") or "").strip()
    if not text:
        raise ChangelogDataError(f"{label} 缺少 text。")
    raw_commits = item.get("commits")
    if not isinstance(raw_commits, list) or not raw_commits:
        raise ChangelogDataError(f"{label} 的 commits 必须是非空数组。")
    commits = tuple(str(short).strip() for short in raw_commits)
    if any(not short for short in commits):
        raise ChangelogDataError(f"{label} 的 commits 里有空值。")
    return Highlight(kind, text, commits, str(item.get("date") or "").strip())


def _parse_release(index: int, raw: object) -> Release:
    where = f"releases[{index}]"
    if not isinstance(raw, dict):
        raise ChangelogDataError(f"{where} 必须是对象。")
    version = str(raw.get("version") or "").strip()
    date = str(raw.get("date") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not version or not date or not title:
        raise ChangelogDataError(f"{where} 缺少 version、date 或 title。")
    raw_highlights = raw.get("highlights")
    if not isinstance(raw_highlights, list) or not raw_highlights:
        raise ChangelogDataError(f"{where}（{version}）的 highlights 必须是非空数组。")
    highlights = tuple(
        _parse_highlight(f"{where}（{version}）highlights[{order}]", item)
        for order, item in enumerate(raw_highlights, 1)
    )
    tags = raw.get("tags") or []
    if not isinstance(tags, list) or any(not str(tag).strip() for tag in tags):
        raise ChangelogDataError(f"{where}（{version}）的 tags 必须是字符串数组。")
    start_date = str(raw.get("from") or date).strip()
    end_date = str(raw.get("to") or date).strip()
    if start_date > end_date:
        raise ChangelogDataError(f"{where}（{version}）的起止日期倒置。")
    return Release(
        version=version,
        date=date,
        start_date=start_date,
        end_date=end_date,
        title=title,
        summary=str(raw.get("summary") or "").strip(),
        tags=tuple(str(tag).strip() for tag in tags),
        highlights=highlights,
    )


def parse_changelog(payload: object) -> Changelog:
    """把已解析的 JSON 转成模型，并在结构不自洽时抛错。"""
    if not isinstance(payload, dict):
        raise ChangelogDataError("更新日志的顶层必须是对象。")
    raw_releases = payload.get("releases")
    if not isinstance(raw_releases, list) or not raw_releases:
        raise ChangelogDataError("更新日志的 releases 必须是非空数组。")
    releases = tuple(
        _parse_release(index, raw) for index, raw in enumerate(raw_releases, 1)
    )
    versions = [release.version for release in releases]
    if len(set(versions)) != len(versions):
        raise ChangelogDataError("更新日志里有重复的版本号。")
    end_dates = [release.end_date for release in releases]
    if end_dates != sorted(end_dates, reverse=True):
        raise ChangelogDataError("更新日志必须按时间倒序排列（最新版本在前）。")
    for previous, current in zip(releases, releases[1:]):
        if current.end_date > previous.start_date:
            raise ChangelogDataError(
                f"{current.version} 与 {previous.version} 的日期区间重叠。"
            )
    meta = payload.get("generated_from") or {}
    if not isinstance(meta, dict):
        raise ChangelogDataError("generated_from 必须是对象。")
    return Changelog(
        head=str(payload.get("head") or "").strip(),
        first_date=str(meta.get("first_date") or releases[-1].start_date),
        last_date=str(meta.get("last_date") or releases[0].end_date),
        commit_count=int(meta.get("commit_count") or sum(r.commit_count for r in releases)),
        releases=releases,
    )


@lru_cache(maxsize=1)
def load_changelog(path: Path | None = None) -> Changelog:
    """读取并缓存更新日志数据（默认 ``plugins/changelog/changelog.json``）。"""
    target = Path(path) if path else DATA_FILE
    if not target.exists():
        raise ChangelogDataError(f"找不到更新日志数据文件：{target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ChangelogDataError(f"更新日志不是合法 JSON：{error}") from error
    return parse_changelog(payload)


__all__ = [
    "DATA_FILE",
    "KIND_LABELS",
    "KIND_ORDER",
    "Changelog",
    "ChangelogDataError",
    "Highlight",
    "Release",
    "load_changelog",
    "parse_changelog",
]
