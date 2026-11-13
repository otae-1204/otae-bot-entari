"""Relative links in the live docs must point at files that exist.

Covers README.md, CLAUDE.md, docs/index.md and every Markdown file under docs/operations,
docs/development and docs/features. docs/history (frozen records) and docs/reference
(external material) are not checked. Also checks backticked ``docs/….md`` paths.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIRS = ("docs/operations", "docs/development", "docs/features")
LIVE_FILES = ("README.md", "CLAUDE.md", "docs/index.md")

FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]*`")
LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
IMG_SRC = re.compile(r"<img[^>]*\bsrc=\"([^\"]+)\"")
DOC_PATH = re.compile(r"`(docs/[^`\s]+\.md)`")


def live_docs() -> list[Path]:
    files = [ROOT / name for name in LIVE_FILES]
    for folder in LIVE_DIRS:
        files.extend(sorted((ROOT / folder).rglob("*.md")))
    return files


def broken_links(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    prose = FENCE.sub("", text)
    targets = [m.group(1) for m in LINK.finditer(INLINE_CODE.sub("", prose))]
    targets += IMG_SRC.findall(prose)
    broken = []
    for target in targets:
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
            continue  # external URL, mailto:, in-page anchor
        relative = unquote(target.split("#", 1)[0])
        if not relative:
            continue
        resolved = (ROOT / relative.lstrip("/")) if relative.startswith("/") else (path.parent / relative)
        if not resolved.exists():
            broken.append(target)
    for doc in DOC_PATH.findall(prose):
        if not (ROOT / doc).exists():
            broken.append(doc)
    return broken


class LiveDocsLinkTests(unittest.TestCase):
    def test_live_doc_set_exists(self):
        for name in LIVE_FILES:
            self.assertTrue((ROOT / name).is_file(), name)
        self.assertGreater(len(live_docs()), len(LIVE_FILES))

    def test_relative_links_resolve(self):
        for path in live_docs():
            with self.subTest(doc=str(path.relative_to(ROOT))):
                self.assertEqual(broken_links(path), [])


if __name__ == "__main__":
    unittest.main()
