"""Patch LLBot's llbot.js so a group send whose self-echo never arrives still succeeds.

Why: in headless protocol mode (``llbot.js --protocol=linux``) LLBot 8.3.2 sends a
group message (PbSendMsg), then waits up to 10 s for QQ to push that message back
("self echo") only to learn its exact msgSeq. The push often never comes, and LLBot
then answers ``retcode 1200 waitForSelfEcho timeout`` although the message was
delivered — callers that retry post it again (seen 2026-11-14: one live card three
times). LLBot already builds a fallback result from the PbSendMsg response; this
patch makes the echo timeout use it and shortens the wait to 3 s.

Usage (on the host running LLBot; stop LLBot first, start it again afterwards):

    python scripts/ops/patch_llbot_echo.py C:\\Bot\\LLBot-bot-2\\bin\\llbot\\llbot.js

Idempotent. Keeps ``llbot.js.bak-echo`` next to the file. Refuses to touch a file in
which the two patched statements are not found exactly once (another LLBot version):
re-check the source before adapting the patterns. LLBot updates overwrite llbot.js,
so run this again after every update.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

MARKER = "otae patch"
REPLACEMENTS = (
    ("this.waitForSelfEcho(peer, random, 1e4)", "this.waitForSelfEcho(peer, random, 3e3)"),
    (
        "const echoed = echoP ? await echoP : void 0;",
        "const echoed = echoP ? await echoP.catch(() => void 0) : void 0;"
        " /* otae patch: echo timeout after a successful PbSendMsg falls back */",
    ),
)


def patch(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    if MARKER in source:
        return "already patched"
    for old, _new in REPLACEMENTS:
        count = source.count(old)
        if count != 1:
            raise SystemExit(f"{path}: expected one {old!r}, found {count}; LLBot version changed?")
    shutil.copy2(path, path.with_name(path.name + ".bak-echo"))
    patched = source
    for old, new in REPLACEMENTS:
        patched = patched.replace(old, new)
    # Same line count, so the bundled source map still points at the right lines.
    assert patched.count("\n") == source.count("\n")
    path.write_bytes(patched.encode("utf-8"))
    return "patched"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(patch(Path(sys.argv[1])))


if __name__ == "__main__":
    main()
