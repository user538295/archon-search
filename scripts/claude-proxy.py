#!/usr/bin/env python3
"""Host-side proxy for claude CLI — lets Docker containers call claude on the Mac host.

Run before starting the dev container:
    python3 scripts/claude-proxy.py

Listens on 127.0.0.1:18766. The container wrapper at /usr/local/bin/claude
POSTs {"args": [...]} here; this script runs the real claude binary and
streams stdout back with an X-Exit-Code header.
"""
import json
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 18766
CLAUDE = shutil.which("claude")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not CLAUDE:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"claude not found on host PATH")
            return
        try:
            r = subprocess.run(
                [CLAUDE] + body.get("args", []),
                capture_output=True, text=True, timeout=120,
            )
        except Exception as exc:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(exc).encode())
            return
        self.send_response(200)
        self.send_header("X-Exit-Code", str(r.returncode))
        self.end_headers()
        self.wfile.write(r.stdout.encode())

    def log_message(self, fmt, *args):
        # Only log errors, not every request.
        if args and str(args[1]) not in ("200",):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    print(f"claude proxy listening on 127.0.0.1:{PORT}")
    print(f"claude binary: {CLAUDE or 'NOT FOUND — fix your PATH before starting'}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
