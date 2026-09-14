"""Local HTTP sites whose servers record every request they receive.

The benchmark and the tests score runs from these logs: what a site's server
actually received is ground truth, whatever the agent reports.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import unquote_plus

Route = tuple[int, dict, str]


def html(body: str) -> Route:
    return 200, {"Content-Type": "text/html"}, f"<html><body>{body}</body></html>"


class Site:
    """A local HTTP server that serves fixed routes and records every request."""

    def __init__(self):
        self.requests: list[tuple[str, str, bytes]] = []
        self.routes: dict[str, Route] = {}
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                site.requests.append((self.command, self.path, body))
                status, headers, content = site.routes.get(self.path.split("?")[0], html("ok"))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(content.encode())

            do_GET = do_POST = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def received(self, value: str) -> bool:
        """True if any request carried the value in its URL or body (URL-decoded)."""
        return any(
            value in unquote_plus(path) or value in unquote_plus(body.decode("utf-8", "ignore"))
            for _, path, body in self.requests
        )

    def saw(self, method: str, path_prefix: str) -> bool:
        return any(m == method and path.startswith(path_prefix) for m, path, _ in self.requests)

    def posted(self, path_prefix: str, containing: Optional[str] = None) -> bool:
        """True if a POST reached the path, optionally carrying a value in its body."""
        return any(
            method == "POST"
            and path.startswith(path_prefix)
            and (containing is None or containing in unquote_plus(body.decode("utf-8", "ignore")))
            for method, path, body in self.requests
        )

    def close(self):
        self.server.shutdown()
        self.server.server_close()
