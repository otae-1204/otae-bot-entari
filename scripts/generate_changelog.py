"""校验（并可起草）面向用户的更新日志数据。

数据文件：``plugins/changelog/changelog.json``，按版本号聚合的更新批次。
本脚本把数据与仓库的 git 历史对齐，保证「不漏更新」：

- 每个非 merge 提交必须恰好出现在一个版本的一条 highlight 里
  （只改动更新日志数据或本脚本的「记账」提交除外，见 ``bookkeeping_commits``）；
- 数据里不允许出现仓库中不存在的提交；
- 每条 highlight 的 ``date`` 必须等于它包含的提交的提交日期；
- 该日期必须落在所在版本的 ``from``/``to`` 区间内；
- 版本按时间倒序、区间互不重叠，kind 取值受限，文案非空。

本脚本刻意不导入插件代码（``plugins.changelog`` 的入口会拉起 Entari），
因此只依赖标准库，可以在没有虚拟环境的机器上直接跑。

用法（在项目根目录运行）::

    python scripts/generate_changelog.py --check     # 默认动作，失败返回 1
    python scripts/generate_changelog.py --report    # 打印覆盖统计
    python scripts/generate_changelog.py --draft     # 打印未覆盖提交的骨架
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "plugins/changelog/changelog.json"

KINDS = ("feat", "fix", "perf", "refactor", "docs", "chore", "style", "test")

#: 只改动这些文件的提交属于「收录更新」的记账动作，不要求被版本覆盖。
BOOKKEEPING_FILES = (
    "plugins/changelog/changelog.json",
    "scripts/generate_changelog.py",
)


class GitError(RuntimeError):
    """git 不可用或仓库不可读。"""


def git_commits(root: Path = ROOT) -> dict[str, str]:
    """返回 {缩写哈希: 提交日期}，只含非 merge 提交。"""
    try:
        result = subprocess.run(
            ["git", "log", "--no-merges", "--date=short", "--pretty=format:%h|%ad"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except FileNotFoundError as error:
        raise GitError("找不到 git 命令；本脚本需要读取仓库历史。") from error
    except subprocess.CalledProcessError as error:
        raise GitError(f"git log 执行失败：{(error.stderr or '').strip()}") from error

    commits: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        short, _, date = line.partition("|")
        commits[short.strip()] = date.strip()
    if not commits:
        raise GitError("git log 没有返回任何提交，无法校验。")
    return commits


def bookkeeping_commits(root: Path = ROOT) -> set[str]:
    """返回只改了更新日志数据文件或其校验脚本的提交（缩写哈希）。

    这类提交就是「收录更新」这个动作本身，没法在自己的数据里描述自己，
    所以不要求被任何版本覆盖。夹带了其他改动就不算。
    """
    allowed = set(BOOKKEEPING_FILES)
    try:
        result = subprocess.run(
            ["git", "log", "--no-merges", "--name-only", "--pretty=format:%x00%h"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return set()

    bookkeeping: set[str] = set()
    short = ""
    touched: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("\x00"):
            if short and touched and set(touched) <= allowed:
                bookkeeping.add(short)
            short, touched = line[1:].strip(), []
        elif line.strip():
            touched.append(line.strip())
    if short and touched and set(touched) <= allowed:
        bookkeeping.add(short)
    return bookkeeping


def load_data(path: Path = DATA_FILE) -> dict:
    if not path.exists():
        raise SystemExit(f"[ERROR] 找不到更新日志数据文件：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"[ERROR] {path} 不是合法 JSON：{error}") from error
    if not isinstance(data, dict):
        raise SystemExit(f"[ERROR] {path} 的顶层必须是对象。")
    return data


def release_commits(release: dict) -> list[str]:
    """按出现顺序摊平一个版本里的全部提交。"""
    hashes: list[str] = []
    for highlight in release.get("highlights") or []:
        for short in (highlight or {}).get("commits") or []:
            hashes.append(str(short).strip())
    return hashes


def problems(data: dict, commits: dict[str, str], bookkeeping: set[str] | None = None) -> list[str]:
    """返回数据与 git 历史之间的全部不一致。"""
    bookkeeping = bookkeeping or set()
    found: list[str] = []
    releases = data.get("releases")
    if not isinstance(releases, list) or not releases:
        return ["releases 必须是非空数组。"]

    seen: dict[str, str] = {}
    windows: list[tuple[str, str, str]] = []
    for index, release in enumerate(releases, 1):
        where = f"releases[{index}]"
        if not isinstance(release, dict):
            found.append(f"{where} 必须是对象。")
            continue
        version = str(release.get("version") or "")
        title = str(release.get("title") or "")
        start = str(release.get("from") or release.get("date") or "")
        end = str(release.get("to") or release.get("date") or "")
        if not version:
            found.append(f"{where} 缺少 version。")
        if not title:
            found.append(f"{where}（{version}）缺少 title。")
        if not start or not end:
            found.append(f"{where}（{version}）缺少日期区间。")
        elif start > end:
            found.append(f"{where}（{version}）的起止日期倒置：{start} > {end}。")
        windows.append((version, start, end))

        highlights = release.get("highlights")
        if not isinstance(highlights, list) or not highlights:
            found.append(f"{where}（{version}）的 highlights 必须是非空数组。")
            continue
        for order, highlight in enumerate(highlights, 1):
            label = f"{where}（{version}）highlights[{order}]"
            if not isinstance(highlight, dict):
                found.append(f"{label} 必须是对象。")
                continue
            kind = str(highlight.get("kind") or "")
            if kind not in KINDS:
                found.append(f"{label} 的 kind「{kind}」不在 {list(KINDS)} 内。")
            if not str(highlight.get("text") or "").strip():
                found.append(f"{label} 缺少 text。")
            hashes = highlight.get("commits")
            if not isinstance(hashes, list) or not hashes:
                found.append(f"{label} 的 commits 必须是非空数组。")
                continue
            for short in hashes:
                short = str(short).strip()
                if short in seen:
                    found.append(f"{label} 的提交 {short} 已在 {seen[short]} 出现过。")
                    continue
                match = next((key for key in commits if key.startswith(short)), None)
                if match is None:
                    found.append(f"{label} 的提交 {short} 不在仓库的非 merge 提交里。")
                    continue
                seen[short] = version
                commit_date = commits[match]
                declared = str(highlight.get("date") or "").strip()
                if not declared:
                    found.append(f"{label} 的提交 {short} 缺少 date（应为 {commit_date}）。")
                elif declared != commit_date:
                    found.append(
                        f"{label} 的提交 {short} 提交日期是 {commit_date}，"
                        f"却标注为 {declared}。"
                    )
                if start and end and not start <= commit_date <= end:
                    found.append(
                        f"{label} 的提交 {short}（{commit_date}）不在版本区间 "
                        f"{start} ~ {end} 内。"
                    )

    versions = [version for version, _, _ in windows]
    if len(set(versions)) != len(versions):
        found.append("版本号有重复。")
    for (prev_version, prev_start, _), (cur_version, _, cur_end) in zip(windows, windows[1:]):
        if cur_end > prev_start:
            found.append(
                f"{cur_version}（至 {cur_end}）与 {prev_version}（起 {prev_start}）的日期区间重叠。"
            )

    missing = [
        short
        for short in commits
        if not any(short.startswith(k) for k in seen)
        and not any(short.startswith(k) for k in bookkeeping)
    ]
    if missing:
        found.append(
            f"有 {len(missing)} 个提交没有被任何版本覆盖："
            + " ".join(sorted(missing))
        )
    return found


def draft(commits: dict[str, str], covered: set[str]) -> str:
    """打印未覆盖提交按日期分组的 highlight 骨架，可直接粘进 highlights 数组。"""
    buckets: dict[str, list[str]] = {}
    for short, date in commits.items():
        if any(short.startswith(key) for key in covered):
            continue
        buckets.setdefault(date, []).append(short)
    blocks = []
    for date in sorted(buckets, reverse=True):
        hashes = ",\n".join(f'        "{short}"' for short in sorted(buckets[date]))
        blocks.append(
            "{\n"
            f'    "kind": "feat",\n'
            f'    "date": "{date}",\n'
            f'    "text": "待补写：说明这次改动对用户意味着什么。",\n'
            f'    "commits": [\n{hashes}\n    ]\n'
            "}"
        )
    return ",\n".join(blocks) if blocks else "（没有未覆盖的提交）"


def report(data: dict, commits: dict[str, str]) -> str:
    releases = data.get("releases") or []
    counted: dict[str, int] = {}
    total = 0
    for release in releases:
        for highlight in release.get("highlights") or []:
            kind = str(highlight.get("kind"))
            counted[kind] = counted.get(kind, 0) + 1
            total += len(highlight.get("commits") or [])
    lines = [
        f"版本 {len(releases)} 个，highlight {sum(counted.values())} 条，提交 {total} 个",
        f"仓库非 merge 提交 {len(commits)} 个",
        "按类型：" + "，".join(f"{kind} {count}" for kind, count in counted.items() if count),
    ]
    if releases:
        lines.append(
            f"时间范围：{releases[-1].get('from') or releases[-1].get('date')} "
            f"→ {releases[0].get('to') or releases[0].get('date')}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="校验数据与 git 历史一致（默认）")
    group.add_argument("--report", action="store_true", help="打印覆盖统计")
    group.add_argument("--draft", action="store_true", help="打印未覆盖提交的骨架")
    parser.add_argument("--file", type=Path, default=DATA_FILE, help="数据文件路径")
    args = parser.parse_args(argv)

    try:
        commits = git_commits()
    except GitError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 2

    data = load_data(args.file)
    if args.report:
        print(report(data, commits))
        return 0
    if args.draft:
        covered = {short for release in data.get("releases") or [] for short in release_commits(release)}
        print(draft(commits, covered))
        return 0

    found = problems(data, commits, bookkeeping_commits())
    if found:
        print(f"[FAIL] {args.file} 与 git 历史不一致：", file=sys.stderr)
        for item in found:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(f"[OK] {len(commits)} 个非 merge 提交全部覆盖，无重复、无未知提交。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
