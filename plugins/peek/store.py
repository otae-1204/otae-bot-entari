"""peek 的运行时状态（白名单与各群窥视目标），由命令写入。

原先放在 configs/peek/config.json；它不是配置，所以挪到 data/peek/state.json
（旧文件由 scripts/convert_config.py 迁移）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STATE_FILE = Path("data") / "peek" / "state.json"


class PeekState:
    def __init__(self, path: Path = STATE_FILE) -> None:
        self.path = path
        self.data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=4, ensure_ascii=False), encoding="utf-8")
