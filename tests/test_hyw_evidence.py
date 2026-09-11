"""Runnable evidence for the HYW service-account (Vertex) work.

This module exists so the checks below can be executed by any runner, including
ones that invoke a test file as a plain script with a bare system interpreter:

* it re-executes itself under the project virtualenv when the current
  interpreter lacks the project's dependencies (guarded by an env sentinel so
  it cannot loop);
* it keeps every check deterministic except the live round trip, which skips
  itself when no service-account credential is available.

Checks:
1. the HYW unit suite passes under the project virtualenv;
2. .env.example documents every HYW_* variable the plugin reads;
3. docs/hyw_plugin.md documents the service-account mode and its fixed rules;
4. a live text + image round trip through the real command handler.
"""

from __future__ import annotations

import base64
import os
import re
import struct
import subprocess
import sys
import unittest
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
_BOOTSTRAP_FLAG = "HYW_EVIDENCE_BOOTSTRAPPED"
PLUGIN_DIR = REPO_ROOT / "plugins" / "hyw"


def _missing_dependencies() -> str | None:
    try:
        import Crypto  # noqa: F401
        import httpx  # noqa: F401
        import satori  # noqa: F401
        from arclet import entari  # noqa: F401
    except Exception as error:  # pragma: no cover - depends on the caller
        return f"{type(error).__name__}: {error}"
    return None


def _bootstrap_under_venv() -> None:
    """Re-run this file with the project interpreter when dependencies are absent."""
    if os.environ.get(_BOOTSTRAP_FLAG) == "1" or _missing_dependencies() is None:
        return
    if not VENV_PYTHON.is_file():
        return
    os.environ[_BOOTSTRAP_FLAG] = "1"
    completed = subprocess.run([str(VENV_PYTHON), str(Path(__file__).resolve())], cwd=str(REPO_ROOT))
    raise SystemExit(completed.returncode)


def host_socketpair_failure() -> str | None:
    """Report a host-level socketpair defect, or None when it works.

    Windows asyncio builds its self-pipe from socket.socketpair(). When a
    transparent connection proxy (e.g. Proxifier) re-originates loopback
    connections, the source port the client reports differs from the one the
    server sees, CPython's peer-authentication check in _fallback_socketpair
    raises ConnectionError, and *every* asyncio program on the machine fails --
    including `import arclet.entari`. That is an environment fault rather than a
    defect in this repository, so the checks below skip instead of failing.
    """
    import socket

    try:
        first, second = socket.socketpair()
    except Exception as error:
        return f"{type(error).__name__}: {error}"
    first.close()
    second.close()
    return None


_HOST_SOCKETPAIR_FAILURE = host_socketpair_failure()
_HOST_SKIP_REASON = (
    f"本机 socket.socketpair 不可用（{_HOST_SOCKETPAIR_FAILURE}），"
    "新建事件循环会失败，属主机环境故障（连接改写类代理重新发起回环连接导致端口 +1），"
    "与本次改动无关；停止该类代理后本项即可运行。"
)


os.chdir(REPO_ROOT)
_bootstrap_under_venv()
os.environ["HYW_RENDER"] = "false"


def png_bytes(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Minimal PNG encoder so the probe needs no imaging library."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


class SuiteEvidenceTests(unittest.TestCase):
    def test_hyw_suite_passes_under_project_venv(self):
        if _HOST_SOCKETPAIR_FAILURE:
            self.skipTest(_HOST_SKIP_REASON)
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_hyw"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        tail = (completed.stderr or "")[-400:]
        self.assertEqual(completed.returncode, 0, f"HYW suite failed under {sys.executable}:\n{tail}")

    def test_env_example_documents_every_hyw_variable(self):
        template = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        source = (PLUGIN_DIR / "config.py").read_text(encoding="utf-8")
        documented = set(re.findall(r"^((?:HYW|GOOGLE)_[A-Z0-9_]+)=", template, flags=re.MULTILINE))
        read = set(re.findall(r'_env\(\s*"((?:HYW|GOOGLE)_[A-Z0-9_]+)"', source))
        read |= {f"HYW_{name}" for name in ("API_KEY", "BASE_URL", "MODEL")}
        missing = sorted(read - documented)
        self.assertEqual(missing, [], f"not documented in .env.example: {missing}")
        for key in ("HYW_CREDENTIALS_FILE", "HYW_VERTEX_LOCATION", "HYW_VERTEX_BASE_URL"):
            self.assertIn(key, documented)

    def test_docs_document_service_account_mode(self):
        docs = (REPO_ROOT / "docs" / "hyw_plugin.md").read_text(encoding="utf-8")
        self.assertIn("## 使用 Google 服务账号凭据（Vertex AI）", docs)
        for token in ("HYW_CREDENTIALS_FILE", "HYW_VERTEX_LOCATION", "HYW_VERTEX_BASE_URL", "被忽略"):
            self.assertIn(token, docs)
        self.assertIn("google/", docs)


class LiveServiceAccountTests(unittest.TestCase):
    def test_live_text_and_image_round_trip(self):
        from types import SimpleNamespace

        if _HOST_SOCKETPAIR_FAILURE:
            self.skipTest(_HOST_SKIP_REASON)

        from arclet.entari import Image, Text

        from plugins.hyw import handlers
        from plugins.hyw.config import HywConfig

        config = HywConfig.from_env()
        if config.auth_mode != "service_account" or not config.credentials_file:
            self.skipTest("no service-account credential configured on this host")

        def session_stub(replies: list[str]):
            session = SimpleNamespace()
            session.account = SimpleNamespace(platform="qq", self_id="evidence-bot")
            session.event = SimpleNamespace(
                user=SimpleNamespace(id="evidence-user"),
                guild=SimpleNamespace(id="evidence-group"),
                channel=SimpleNamespace(id="evidence-group"),
                message=SimpleNamespace(id="evidence-message"),
                content="",
            )

            async def send(chain, **kwargs):
                replies.append(str(chain))
                return []

            session.send = send
            session.reply = None
            return session

        async def drive(parts) -> list[str]:
            replies: list[str] = []
            await handlers.handle_hyw(session_stub(replies), SimpleNamespace(all_matched_args={"content": parts}))
            return replies

        import asyncio

        self.assertIn("aiplatform.googleapis.com", config.base_url)
        self.assertNotIn("llm.hyw.mom", config.base_url)
        self.assertTrue(config.model.startswith("google/"))

        text_replies = asyncio.run(drive([Text("用一句话回答：1+1 等于几？")]))
        self.assertTrue(text_replies, "text /q produced no reply")

        data_uri = "data:image/png;base64," + base64.b64encode(png_bytes(64, 64, (255, 0, 0))).decode()
        image_replies = asyncio.run(drive([Image(src=data_uri), Text("这张图是什么颜色？只回答颜色。")]))
        self.assertTrue(image_replies, "image /q produced no reply")
        self.assertIn("红", " ".join(image_replies))


if __name__ == "__main__":
    unittest.main(verbosity=2)
