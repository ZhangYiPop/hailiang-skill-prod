#!/usr/bin/env python3
"""Serve a built SPA without Vite, including client-side route fallback."""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class SpaHandler(SimpleHTTPRequestHandler):
    def send_head(self):  # noqa: D401
        """Return index.html for route paths that are not static files."""
        requested = Path(self.translate_path(self.path.split("?", 1)[0]))
        if not requested.exists() and "." not in Path(self.path).name:
            self.path = "/index.html"
        return super().send_head()

    def log_message(self, fmt: str, *args) -> None:
        print(f"frontend {self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if not (args.directory / "index.html").is_file():
        raise SystemExit(f"frontend build is missing index.html: {args.directory}")
    server = ThreadingHTTPServer((args.host, args.port), partial(SpaHandler, directory=str(args.directory)))
    print(f"frontend listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
