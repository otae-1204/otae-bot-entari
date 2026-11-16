"""JsonStore: async saves snapshot on the loop, write off it, and never go backwards."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from otae_bot.infrastructure.storage.json_store import JsonStore


class JsonStoreAsyncSaveTests(unittest.TestCase):
    def test_set_async_writes_in_a_worker_thread(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            store = JsonStore(path)
            writers: list[str] = []
            write = store._write

            def recording_write(version, text):
                writers.append(threading.current_thread().name)
                write(version, text)

            async def main():
                with patch.object(store, "_write", recording_write):
                    await store.set_async("a", 1)
                    store.setdefault("b", {})["c"] = 2  # memory only until the next save
                    self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1})
                    await store.save_async()

            asyncio.run(main())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1, "b": {"c": 2}})
            self.assertEqual(len(writers), 2)
            self.assertNotIn(threading.main_thread().name, writers)
            self.assertEqual(JsonStore(path).get("b"), {"c": 2})

    def test_a_stale_snapshot_never_overwrites_a_newer_one(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            store = JsonStore(path)
            store.set("n", 2)  # version 2 on disk (version 1 was the initial file)
            store._write(1, json.dumps({"n": 1}))  # a slow writer arriving late
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"n": 2})


if __name__ == "__main__":
    unittest.main()
