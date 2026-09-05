"""Serve only the preview and explicitly shared game assets, never bot data."""

from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

PREVIEW = Path(__file__).resolve().parents[1]
REPO = PREVIEW.parents[1]
MOUNTS = {
    "/shared/ui/": REPO / "plugins/endfield/assets/ui",
    "/shared/fonts/": REPO / "plugins/endfield/assets/fonts",
    "/shared/endfield/": REPO / "assets/image/endfield",
}


class PreviewHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        clean = unquote(urlsplit(path).path)
        base = PREVIEW
        relative = clean.lstrip("/")
        for prefix, mounted in MOUNTS.items():
            if clean.startswith(prefix):
                base, relative = mounted, clean[len(prefix) :]
                break
        candidate = (base / relative).resolve()
        if not candidate.is_relative_to(base.resolve()):
            return str(PREVIEW / "__not_found__")
        return str(candidate)

    def list_directory(self, path):
        self.send_error(403, "Directory listing disabled")

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        super().end_headers()

    def log_message(self, format, *args):
        if str(args[1]) != "200":
            super().log_message(format, *args)


def make_server(port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), PreviewHandler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = make_server(args.port)
    print(f"Preview: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
