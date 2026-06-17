"""向后兼容的 Plugin_Data 包装器 — 底层使用 JsonStore.

新运行时数据写入 data/<plugin_name>/data.json；若旧的
assets/json/<plugin_name>.json 存在且新文件不存在，会自动迁移一次。
"""

from __future__ import annotations

from pathlib import Path

from configs.path_config import JSON_PATH
from utils.json_store import JsonStore


class Plugin_Data:
    """键值对 JSON 持久化（兼容旧接口，内部使用 JsonStore）."""

    data = {}  # 保持类属性，兼容旧代码

    def __init__(self, plugin_name: str):
        self.plugin_name = plugin_name
        self.plugin_data_path = Path("data") / plugin_name / "data.json"
        self._legacy_path = Path(JSON_PATH) / f"{plugin_name}.json"
        self._migrate_legacy_data()
        self._store = JsonStore(self.plugin_data_path)
        self.plugin_data = self._store.all()

    def _migrate_legacy_data(self) -> None:
        if self.plugin_data_path.exists() or not self._legacy_path.exists():
            return
        self.plugin_data_path.parent.mkdir(parents=True, exist_ok=True)
        self.plugin_data_path.write_text(
            self._legacy_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    def load_plugin_data(self) -> dict:
        return self._store.all()

    def save_plugin_data(self):
        self._store._data = dict(self.plugin_data)
        self._store._save()
