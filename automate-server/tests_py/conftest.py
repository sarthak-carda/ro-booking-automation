"""Shared pytest fixtures/helpers for the ported Node test suite.

Ports tests/helpers/staticServer.js: a minimal static file server for the
offline example-vendor fixture (no live network, no real vendor), used by
test_pipeline.py and test_self_heal.py exactly like the Node tests use it.
"""
import http.server
import functools
import threading
import urllib.parse
from pathlib import Path


class _FixtureRequestHandler(http.server.BaseHTTPRequestHandler):
    """Mirrors staticServer.js: serve a file under root_dir by URL path, 404 on
    anything else (missing file, traversal, etc.) — no directory listing."""

    CONTENT_TYPES = {".html": "text/html", ".js": "application/javascript"}

    def __init__(self, *args, root_dir: str, **kwargs):
        self.root_dir = root_dir
        super().__init__(*args, **kwargs)

    def _handle(self):
        parsed = urllib.parse.urlparse(self.path)
        rel_path = urllib.parse.unquote(parsed.path).lstrip("/")
        file_path = Path(self.root_dir) / rel_path
        try:
            # Mirrors path.join(rootDir, ...) + readFile: any failure -> 404.
            resolved = file_path.resolve()
            if not str(resolved).startswith(str(Path(self.root_dir).resolve())):
                raise FileNotFoundError
            body = resolved.read_bytes()
        except OSError:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return
        ext = file_path.suffix
        content_type = self.CONTENT_TYPES.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib naming
        self._handle()

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # silence request logging


class StaticServer:
    def __init__(self, root_dir: str, port: int):
        handler = functools.partial(_FixtureRequestHandler, root_dir=root_dir)
        self._httpd = http.server.HTTPServer(("127.0.0.1", port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def start_static_server(root_dir: str, port: int) -> StaticServer:
    """Mirrors startStaticServer(rootDir, port) from tests/helpers/staticServer.js."""
    return StaticServer(root_dir, port)
