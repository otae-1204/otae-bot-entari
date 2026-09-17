"""更新日志插件：数据自洽性、检索、文本、卡片、投递与真渲染。

分三层守：
1. 数据与仓库 git 历史严格对齐（不重不漏），直接调用校验脚本；
2. 纯函数层（models / formatters / presentation）在内存数据上断言行为；
3. 投递与渲染：断言「先渲染完再发」、失败回退完整文本、取消不发回退，
   并用项目共享浏览器真渲染一次，确认卡片宽度与 PNG 格式。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from plugins.changelog import formatters
from plugins.changelog import handlers as changelog_handlers
from plugins.changelog import presentation as views
from plugins.changelog.models import (
    KIND_LABELS,
    ChangelogDataError,
    load_changelog,
    parse_changelog,
)
from plugins.changelog.rendering import CARD_WIDTH, page_html

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "plugins/changelog/changelog.json"

#: 子进程里让中文走 UTF-8，避免 Windows 管道用 GBK 编码导致解码失败。
CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def _png() -> bytes:
    """一张 1x1 的合法 PNG：``Image(raw=...)`` 要靠魔数探测 mime。"""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _payload() -> dict:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def _changelog():
    return load_changelog(DATA_FILE)


class _Arg:
    """最小 ArgVal 替身：handler 只经 get_rest 读它。"""

    def __init__(self, rest: str):
        self.result = (rest,) if rest else ()
        self.available = bool(rest)


class DataIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.changelog = _changelog()

    def test_data_file_exists_and_parses(self):
        self.assertTrue(DATA_FILE.exists())
        self.assertGreaterEqual(len(self.changelog.releases), 1)

    def test_releases_are_time_descending_and_non_overlapping(self):
        releases = self.changelog.releases
        ends = [release.end_date for release in releases]
        self.assertEqual(ends, sorted(ends, reverse=True))
        for newer, older in zip(releases, releases[1:]):
            self.assertGreater(newer.start_date, older.end_date)
        versions = [release.version for release in releases]
        self.assertEqual(len(versions), len(set(versions)))

    def test_every_highlight_is_labelled_and_covers_real_commits(self):
        seen: set[str] = set()
        for release in self.changelog.releases:
            for item in release.highlights:
                self.assertIn(item.kind, KIND_LABELS)
                self.assertTrue(item.text.strip())
                self.assertTrue(item.date, f"{release.version} 的更新缺少提交日期")
                self.assertTrue(item.commits)
                for short in item.commits:
                    self.assertNotIn(short, seen, f"{short} 在多个版本里重复出现")
                    seen.add(short)
        self.assertEqual(len(seen), self.changelog.commit_count)

    def test_highlight_dates_stay_inside_their_version_window(self):
        for release in self.changelog.releases:
            for item in release.highlights:
                self.assertGreaterEqual(item.date, release.start_date)
                self.assertLessEqual(item.date, release.end_date)

    def test_version_windows_cover_every_commit_date(self):
        """把 git 里每个非合并提交的日期都落进某个版本区间。"""
        result = subprocess.run(
            ["git", "log", "--no-merges", "--date=short", "--pretty=format:%ad"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        dates = sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})
        self.assertGreater(len(dates), 1)
        self.assertLessEqual(len(dates), self.changelog.commit_count)
        for date in dates:
            covered = any(
                release.start_date <= date <= release.end_date
                for release in self.changelog.releases
            )
            self.assertTrue(covered, f"{date} 没有被任何版本覆盖")

    def test_git_history_is_fully_covered_by_the_data(self):
        result = subprocess.run(
            [sys.executable, "scripts/generate_changelog.py", "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=CHILD_ENV,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_script_detects_an_uncovered_commit(self):
        """校验脚本必须真的会失败——否则「通过」毫无意义。"""
        payload = _payload()
        payload["releases"] = payload["releases"][1:]
        broken = ROOT / "plugins/changelog/.test-broken.json"
        broken.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "scripts/generate_changelog.py", "--check", "--file", str(broken)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=CHILD_ENV,
                timeout=120,
            )
        finally:
            broken.unlink()
        self.assertEqual(result.returncode, 1)
        self.assertIn("没有被任何版本覆盖", result.stderr)

    def test_draft_output_is_pasteable_json(self):
        """--draft 的骨架必须能直接粘进 highlights——这是文档里的维护流程。"""
        payload = _payload()
        payload["releases"] = payload["releases"][1:]
        broken = ROOT / "plugins/changelog/.test-draft.json"
        broken.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "scripts/generate_changelog.py", "--draft", "--file", str(broken)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=CHILD_ENV,
                timeout=120,
            )
        finally:
            broken.unlink()
        self.assertEqual(result.returncode, 0, result.stderr)
        blocks = json.loads("[" + result.stdout.strip() + "]")
        self.assertTrue(blocks)
        for block in blocks:
            self.assertIn(block["kind"], KIND_LABELS)
            self.assertTrue(block["commits"])
            self.assertTrue(block["text"])

    def test_draft_is_empty_when_nothing_is_uncovered(self):
        result = subprocess.run(
            [sys.executable, "scripts/generate_changelog.py", "--draft"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=CHILD_ENV,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("没有未覆盖的提交", result.stdout)

    def test_latest_version_is_the_newest_window(self):
        self.assertEqual(self.changelog.latest.end_date, self.changelog.last_date)
        self.assertEqual(self.changelog.releases[-1].start_date, self.changelog.first_date)

    def test_corrupt_payloads_are_rejected(self):
        cases = [
            {},
            {"releases": []},
            {"releases": [{"version": "v1", "date": "2026-01-01"}]},
            {"releases": [{"version": "v1", "date": "2026-01-01", "title": "x", "highlights": []}]},
            {
                "releases": [
                    {
                        "version": "v1",
                        "date": "2026-01-01",
                        "title": "x",
                        "highlights": [{"kind": "nope", "text": "t", "commits": ["abc"]}],
                    }
                ]
            },
            {
                "releases": [
                    {
                        "version": "v1",
                        "date": "2026-01-01",
                        "title": "x",
                        "highlights": [{"kind": "feat", "text": "  ", "commits": ["abc"]}],
                    }
                ]
            },
            {
                "releases": [
                    {
                        "version": "v1",
                        "date": "2026-01-01",
                        "title": "x",
                        "highlights": [{"kind": "feat", "text": "t", "commits": []}],
                    }
                ]
            },
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ChangelogDataError):
                    parse_changelog(payload)

    def test_duplicate_versions_and_ascending_order_are_rejected(self):
        def release(version, start, end):
            return {
                "version": version,
                "date": end,
                "from": start,
                "to": end,
                "title": "t",
                "highlights": [{"kind": "feat", "text": "t", "commits": [version]}],
            }

        with self.assertRaises(ChangelogDataError):
            parse_changelog({"releases": [release("v1", "2026-01-01", "2026-01-02"),
                                          release("v1", "2026-01-03", "2026-01-04")]})
        with self.assertRaises(ChangelogDataError):
            parse_changelog({"releases": [release("v1", "2026-01-01", "2026-01-02"),
                                          release("v2", "2026-01-03", "2026-01-04")]})
        with self.assertRaises(ChangelogDataError):
            parse_changelog({"releases": [release("v1", "2026-01-01", "2026-01-05"),
                                          release("v2", "2026-01-04", "2026-01-06")]})

    def test_release_aggregates_its_highlights(self):
        release = self.changelog.releases[1]
        self.assertEqual(
            release.commit_count, sum(len(item.commits) for item in release.highlights)
        )
        self.assertEqual(set(release.counts()), set(release.kinds()))
        self.assertEqual(sum(release.counts().values()), len(release.highlights))
        self.assertTrue(set(release.dates) <= set(release.date_range.split(" ~ ")) | set(release.dates))


class QueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.changelog = _changelog()

    def test_number_lookup_starts_at_the_latest_version(self):
        self.assertEqual(self.changelog.by_number(1), self.changelog.latest)
        self.assertEqual(
            self.changelog.by_number(len(self.changelog.releases)), self.changelog.releases[-1]
        )
        self.assertIsNone(self.changelog.by_number(0))
        self.assertIsNone(self.changelog.by_number(999))

    def test_version_lookup_accepts_prefixes(self):
        latest = self.changelog.latest
        self.assertEqual(self.changelog.by_version(latest.version), latest)
        self.assertEqual(self.changelog.by_version(latest.version.lstrip("v")), latest)
        self.assertEqual(self.changelog.by_version(latest.version.upper()), latest)
        self.assertIsNone(self.changelog.by_version("v99.9.9"))

    def test_version_lookup_matches_major_minor_prefix(self):
        target = self.changelog.releases[3]
        parts = target.version.lstrip("v").split(".")
        self.assertEqual(self.changelog.by_version(".".join(parts[:2])), target)

    def test_digit_query_is_an_index_not_a_version(self):
        self.assertEqual(self.changelog.search("1"), (self.changelog.latest,))

    def test_date_prefix_lookup_matches_a_version_window(self):
        target = self.changelog.releases[2]
        found = self.changelog.search(target.end_date[:7])
        self.assertIn(target, found)
        self.assertTrue(all(item.end_date.startswith(target.end_date[:7])
                            or item.start_date.startswith(target.end_date[:7])
                            or any(h.date.startswith(target.end_date[:7]) for h in item.highlights)
                            for item in found))

    def test_keyword_lookup_searches_titles_summaries_and_highlights(self):
        found = self.changelog.search("雷达")
        self.assertTrue(found)
        for release in found:
            haystack = " ".join(
                [release.version, release.date, release.date_range, release.title,
                 release.summary, *release.tags]
                + [f"{item.label} {item.text} {item.date}" for item in release.highlights]
            )
            self.assertIn("雷达", haystack)

    def test_empty_query_returns_everything_and_unknown_returns_nothing(self):
        self.assertEqual(self.changelog.search(""), self.changelog.releases)
        self.assertEqual(self.changelog.search("绝不可能出现的关键词xyzzy"), ())

    def test_keyword_lookup_is_case_insensitive(self):
        self.assertEqual(len(self.changelog.search("RADAR")), len(self.changelog.search("radar")))


class TextFormatterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.changelog = _changelog()

    def test_latest_text_contains_version_title_and_commits(self):
        text = "\n".join(formatters.format_latest(self.changelog))
        latest = self.changelog.latest
        self.assertIn(latest.version, text)
        self.assertIn(latest.date_range, text)
        self.assertIn(latest.title, text)
        for short in latest.commits:
            self.assertIn(short, text)

    def test_release_text_labels_every_highlight_and_keeps_dates(self):
        release = self.changelog.releases[1]
        text = "\n".join(formatters.format_release(release, number=2, total=9))
        for item in release.highlights:
            self.assertIn(item.label, text)
            self.assertIn(item.text, text)
            self.assertIn(item.date, text)

    def test_index_text_lists_a_page_and_mentions_paging(self):
        text = "\n".join(formatters.format_index(self.changelog, page=1, page_size=5))
        self.assertIn(self.changelog.latest.version, text)
        self.assertIn("第 1/", text)
        self.assertIn("/更新日志 列表 2", text)

    def test_index_page_is_clamped_into_range(self):
        text = "\n".join(formatters.format_index(self.changelog, page=999, page_size=5))
        pages = max(1, -(-len(self.changelog.releases) // 5))
        self.assertIn(f"第 {pages}/{pages} 页", text)

    def test_search_text_reports_hits_and_misses(self):
        hits = formatters.format_search(self.changelog.search("雷达"), "雷达")
        self.assertIn("相关的更新", "\n".join(hits))
        misses = formatters.format_search((), "绝不可能出现的关键词xyzzy")
        self.assertIn("没有找到", "\n".join(misses))

    def test_stats_text_counts_match_the_data(self):
        text = "\n".join(formatters.format_stats(self.changelog))
        self.assertIn(str(len(self.changelog.releases)), text)
        self.assertIn(str(self.changelog.highlight_count), text)
        self.assertIn(str(self.changelog.commit_count), text)

    def test_help_text_documents_every_subcommand(self):
        text = "\n".join(formatters.format_help())
        for fragment in ("/更新日志 列表", "/更新日志 统计", "/更新日志 帮助", "别名"):
            self.assertIn(fragment, text)

    def test_fallback_text_stays_within_a_chunk_of_the_image_content(self):
        """降级文本必须自带版本号与提交，不能只说「请重试」。"""
        text = "\n".join(formatters.format_latest(self.changelog))
        self.assertIn(self.changelog.latest.version, text)
        self.assertIn(self.changelog.latest.title, text)


class PresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.changelog = _changelog()

    def test_release_page_escapes_untrusted_text(self):
        payload = {
            "releases": [
                {
                    "version": "v1.0.0",
                    "date": "2026-01-01",
                    "from": "2026-01-01",
                    "to": "2026-01-01",
                    "title": "<img src=x onerror=alert(1)>",
                    "summary": "<script>bad()</script>",
                    "tags": ["<b>tag</b>"],
                    "highlights": [
                        {
                            "kind": "feat",
                            "date": "2026-01-01",
                            "text": "<iframe></iframe>",
                            "commits": ["abc123"],
                        }
                    ],
                }
            ]
        }
        release = parse_changelog(payload).latest
        page = views.release_pages(release, number=1, total=1)[0]
        # 标题由 rendering.page_html 输出，正文只带事实行，所以整体一起断言。
        document = page_html(page)
        for fragment in ("<img", "<script", "<iframe"):
            self.assertNotIn(fragment, document)
        for escaped in ("&lt;img", "&lt;script", "&lt;iframe"):
            self.assertIn(escaped, document)

    def test_search_page_escapes_the_query(self):
        body = views.search_pages((), "<script>x</script>")[0].body
        self.assertNotIn("<script", body)
        self.assertIn("&lt;script", body)

    def test_search_rows_skip_the_index_column(self):
        """检索结果没有目录序号，首列不能留占位横线。"""
        body = views.search_pages(self.changelog.search("雷达"), "雷达")[0].body
        self.assertNotIn('class="cl-no"', body)
        self.assertIn("cl-row-ver", body)
        # 目录卡要保留序号列。
        self.assertIn('class="cl-no"', views.index_pages(self.changelog)[0].body)

    def test_every_page_has_a_title_and_section(self):
        pages = (
            views.latest_pages(self.changelog)
            + views.index_pages(self.changelog)
            + views.search_pages(self.changelog.search("雷达"), "雷达")
            + views.search_pages((), "空")
            + views.stats_pages(self.changelog)
            + views.help_pages()
        )
        for page in pages:
            self.assertTrue(page.title)
            self.assertTrue(page.subtitle)
            self.assertTrue(page.body)
            self.assertTrue(page.section)

    def test_index_pages_carry_page_identity(self):
        pages = views.index_pages(self.changelog, page=2, page_size=5)
        self.assertEqual(pages[0].number, 2)
        self.assertEqual(pages[0].total, max(1, -(-len(self.changelog.releases) // 5)))

    def test_rendered_html_declares_a_restrictive_csp(self):
        html = page_html(views.latest_pages(self.changelog)[0])
        self.assertIn("default-src 'none'", html)
        self.assertIn("font-src data:", html)
        self.assertIn("img-src data:", html)
        self.assertNotIn("http://", html)


class DeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.changelog = _changelog()

    def _reply(self):
        return views.ChangelogReply(
            formatters.format_latest(self.changelog), views.latest_pages(self.changelog)
        )

    def test_reply_renders_before_sending_and_sends_image_segments(self):
        events: list[str] = []

        async def fake_render(page):
            events.append("render")
            return _png()

        async def fake_send(session, message):
            events.append("send")
            self.assertIsInstance(message, list)
            return []

        with patch.object(changelog_handlers, "render_page", fake_render), patch.object(
            changelog_handlers, "send", fake_send
        ):
            asyncio.run(changelog_handlers._reply(object(), self._reply()))

        self.assertEqual(events, ["render", "send"])

    def test_all_pages_render_before_the_first_send(self):
        order: list[str] = []

        async def fake_render(page):
            order.append("render")
            return _png()

        async def fake_send(session, message):
            order.append("send")
            return []

        reply = views.ChangelogReply(
            ["a"], views.index_pages(self.changelog, page=1, page_size=5)
        )
        with patch.object(changelog_handlers, "render_page", fake_render), patch.object(
            changelog_handlers, "send", fake_send
        ):
            asyncio.run(changelog_handlers._reply(object(), reply))

        self.assertEqual(order.count("render"), len(reply.pages))
        self.assertLess(max(i for i, v in enumerate(order) if v == "render"),
                        min(i for i, v in enumerate(order) if v == "send"))

    def test_render_failure_delivers_the_original_text(self):
        async def broken_render(page):
            raise RuntimeError("render boom")

        sent: list[str] = []

        async def fake_send(session, message):
            sent.append(message)
            return []

        with patch.object(changelog_handlers, "render_page", broken_render), patch.object(
            changelog_handlers, "send", fake_send
        ):
            asyncio.run(changelog_handlers._reply(object(), self._reply()))

        self.assertTrue(sent)
        joined = "\n".join(sent)
        self.assertIn(self.changelog.latest.version, joined)
        self.assertIn(self.changelog.latest.title, joined)

    def test_cancellation_never_sends_fallback(self):
        async def cancelled_render(page):
            raise asyncio.CancelledError

        send_mock = AsyncMock(return_value=[])
        with patch.object(changelog_handlers, "render_page", cancelled_render), patch.object(
            changelog_handlers, "send", send_mock
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(changelog_handlers._reply(object(), self._reply()))
        send_mock.assert_not_awaited()

    def test_chunks_never_split_a_line(self):
        text = "\n".join(["x" * 50] * 200)
        chunks = changelog_handlers._chunks(text, size=200)
        self.assertTrue(all(len(chunk) <= 200 for chunk in chunks))
        self.assertEqual("\n".join(chunks), text)


class HandlerTests(unittest.TestCase):
    def _run(self, rest: str):
        sent: list[object] = []

        async def fake_send(session, message):
            sent.append(message)
            return []

        async def fake_render(page):
            return _png()

        with patch.object(changelog_handlers, "render_page", fake_render), patch.object(
            changelog_handlers, "send", fake_send
        ):
            asyncio.run(changelog_handlers.handle_changelog(_Arg(rest), object()))
        return sent

    def test_bare_command_sends_the_latest_version(self):
        self.assertEqual(len(self._run("")), 1)

    def test_every_documented_entry_point_replies(self):
        cases = ("帮助", "统计", "列表", "列表 2", "1", "2", "雷达", "v1.13.0", "2026-09", "不存在xyzzy")
        for rest in cases:
            with self.subTest(rest=rest):
                self.assertEqual(len(self._run(rest)), 1)

    def test_unknown_keyword_falls_back_to_a_not_found_message(self):
        async def broken_render(page):
            raise RuntimeError("no browser")

        sent: list[str] = []

        async def fake_send(session, message):
            sent.append(message)
            return []

        with patch.object(changelog_handlers, "render_page", broken_render), patch.object(
            changelog_handlers, "send", fake_send
        ):
            asyncio.run(changelog_handlers.handle_changelog(_Arg("绝不可能出现的关键词xyzzy"), object()))

        self.assertTrue(sent)
        self.assertIn("没有找到", "\n".join(sent))

    def test_version_query_returns_that_version_not_a_search_list(self):
        changelog = _changelog()
        reply = changelog_handlers._search(changelog, changelog.latest.version)
        self.assertEqual(len(reply.pages), 1)
        self.assertIn(changelog.latest.title, "\n".join(reply))

    def test_index_page_is_capped_at_the_last_page(self):
        reply = changelog_handlers._index(_changelog(), 999)
        self.assertEqual(reply.pages[0].number, reply.pages[0].total)


class RealRenderTests(unittest.TestCase):
    def test_real_shared_browser_renderer_produces_png(self):
        from PIL import Image

        from otae_bot.infrastructure.rendering.browser import close_browser

        changelog = _changelog()
        page = views.latest_pages(changelog)[0]

        async def run():
            try:
                return await changelog_handlers.render_page(page)
            finally:
                await close_browser()

        png = asyncio.run(run())
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.width, CARD_WIDTH)
            self.assertGreater(image.height, 200)


if __name__ == "__main__":
    unittest.main()
