"""把更新日志的各类卡片渲染成 PNG，供维护时目检，不发到群里。

用法：

    python scripts/preview_changelog.py                  # 渲染全部卡片类型
    python scripts/preview_changelog.py --only stats     # 只渲染一类
    python scripts/preview_changelog.py --version v1.13.0

渲染与线上同一条路径（``plugins.changelog.rendering.render_page``），
输出到 ``output/``，文件名形如 ``cl-<类型>.png``。本脚本不发任何消息、
不连任何网络，只读 ``plugins/changelog/changelog.json``。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plugins.changelog import presentation as views
from plugins.changelog.models import load_changelog
from plugins.changelog.presentation import ChangelogPage
from plugins.changelog.rendering import render_page

#: 类型名 → (取页函数, 说明)
BUILDERS = {
    "latest": lambda data, args: views.latest_pages(data),
    "index": lambda data, args: views.index_pages(data, page=args.page),
    "stats": lambda data, args: views.stats_pages(data),
    "help": lambda data, args: views.help_pages(),
    "search": lambda data, args: views.search_pages(
        data.search(args.keyword), args.keyword
    ),
    "version": lambda data, args: views.release_pages(
        _pick(data, args.version), number=1, total=len(data.releases)
    ),
}


def _pick(data, version: str):
    """按版本号（支持前缀）取一个版本；空值取最新，取不到就报错列出可选值。"""
    if not version:
        return data.latest
    wanted = version.lstrip("v")
    for release in data.releases:
        if release.version.lstrip("v").startswith(wanted):
            return release
    raise SystemExit(
        f"没有版本号匹配 {version!r}；可选：" + "、".join(r.version for r in data.releases)
    )


async def _render(name: str, pages: tuple[ChangelogPage, ...], out_dir: Path) -> None:
    for page in pages:
        png = await render_page(page)
        suffix = "" if page.total == 1 else f"-{page.number:02}"
        path = out_dir / f"cl-{name}{suffix}.png"
        path.write_bytes(png)
        print(f"{path.name}  {len(png)} 字节  {page.title}（{page.number}/{page.total}）")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        action="append",
        choices=sorted(BUILDERS),
        help="只渲染指定类型，可重复；默认全部",
    )
    parser.add_argument("--version", default="", help="配合 --only version 使用，默认取最新版本")
    parser.add_argument("--keyword", default="雷达", help="配合 --only search 使用")
    parser.add_argument("--page", type=int, default=1, help="配合 --only index 使用")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output")
    args = parser.parse_args()

    names = args.only or sorted(BUILDERS)

    data = load_changelog()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        await _render(name, BUILDERS[name](data, args), args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
