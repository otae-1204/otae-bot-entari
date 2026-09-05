"""Read-only preview server boundaries; no bot imports or account fixtures."""

import importlib.util
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

spec = importlib.util.spec_from_file_location(
    "preview_server", Path(__file__).resolve().parents[1] / "tools/serve.py"
)
server_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server_module)


class PreviewServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = server_module.make_server(0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_preview_is_served_without_network_api_access(self):
        with urlopen(self.url + "/", timeout=2) as response:
            self.assertIn(b"src/app.js", response.read())
            self.assertIn(
                "connect-src 'none'", response.headers["Content-Security-Policy"]
            )
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_existing_game_assets_are_available(self):
        with urlopen(self.url + "/shared/ui/potential_0.png", timeout=2) as response:
            self.assertEqual(response.read(8), b"\x89PNG\r\n\x1a\n")

    def test_repository_data_and_traversal_are_not_served(self):
        for path in (
            "/data/endfield/account_store.json",
            "/.env",
            "/%2e%2e/%2e%2e/bot.py",
            "/shared/ui/%2e%2e/%2e%2e/__init__.py",
            "/shared/ui/",
        ):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(self.url + path, timeout=2)
                self.assertIn(caught.exception.code, (403, 404))


if __name__ == "__main__":
    unittest.main()
