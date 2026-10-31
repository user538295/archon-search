#!/usr/bin/env python3
"""Host-side proxy for claude CLI — lets Docker containers call claude on the Mac host.

Run before starting the dev container (the same token must be set in the container):
    CLAUDE_PROXY_TOKEN=<random secret> python3 scripts/claude-proxy.py

Listens on 127.0.0.1:18766. The container wrapper at /usr/local/bin/claude
POSTs {"args": [...], "stdin": "..."} here (stdin is optional: the prompt the claude_cli
provider writes to the wrapper's standard input); this script runs the real claude binary
with those args and that standard input, and streams stdout back with an X-Exit-Code header.

This server executes `claude` with caller-supplied arguments on the host, so it
authenticates every request and only answers requests addressed to it:
  - the X-Claude-Proxy-Token header must equal $CLAUDE_PROXY_TOKEN (the server
    refuses to start without one), which also blocks CSRF from a browser: a page
    cannot read the token, and the custom header forces a CORS preflight we never grant;
  - the Host header must name this machine (DNS rebinding sends the attacker's own
    hostname), and a request carrying an Origin header (i.e. any browser-initiated
    one) is refused — the container wrapper is not a browser and sends none.
"""
import hmac
import json
import os
import shutil
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = "127.0.0.1"
PORT = 18766
CLAUDE = shutil.which("claude")
TOKEN_ENV = "CLAUDE_PROXY_TOKEN"
TOKEN_HEADER = "X-Claude-Proxy-Token"
# Names a request may address this server by: loopback, and the alias Docker Desktop gives the host.
ALLOWED_HOST_NAMES = frozenset({"127.0.0.1", "localhost", "host.docker.internal"})
# Room for a very large prompt on stdin (a description or graph sample is far over the former 1 MB).
MAX_BODY_BYTES = 16 * 1024 * 1024
# A refused request's body is drained (not kept) up to this much before the refusal is sent.
REFUSED_BODY_DRAIN_BYTES = 1_000_000
DRAIN_CHUNK_BYTES = 64 * 1024
CLAUDE_TIMEOUT_S = 120


def _host_name(host_header: str) -> str:
    """Host header without its port; IPv6 literals are never allowed names, so a split is enough."""
    return host_header.rsplit(":", 1)[0] if ":" in host_header else host_header


def check_request(headers, token: str) -> HTTPStatus | None:
    """None when the request may run claude, else the status to refuse it with."""
    if "Origin" in headers:
        return HTTPStatus.FORBIDDEN
    if _host_name(headers.get("Host", "")).lower() not in ALLOWED_HOST_NAMES:
        return HTTPStatus.FORBIDDEN
    supplied = headers.get(TOKEN_HEADER, "")
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        return HTTPStatus.UNAUTHORIZED
    return None


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status: HTTPStatus, body: bytes = b"", exit_code: int | None = None) -> None:
        self.send_response(status)
        if exit_code is not None:
            self.send_header("X-Exit-Code", str(exit_code))
        self.end_headers()
        self.wfile.write(body)

    def _content_length(self) -> int | None:
        """The declared body length, or None when Content-Length is missing, malformed or over the cap."""
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        return length if 0 <= length <= MAX_BODY_BYTES else None

    def _discard_body(self, length: int | None) -> None:
        """Read and drop the start of a body the request is refused for, without keeping it in memory."""
        remaining = min(length or 0, REFUSED_BODY_DRAIN_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, DRAIN_CHUNK_BYTES))
            if not chunk:
                return
            remaining -= len(chunk)

    def do_POST(self):
        length = self._content_length()
        refused = check_request(self.headers, self.server.token)
        if refused is not None:
            # Drain what is in flight before replying: answering with an unread upload still in flight makes the
            # OS reset the connection, so the client would see a reset instead of the 401/403. Only the
            # authenticated caller's body is ever kept in memory.
            self._discard_body(length)
            self._reply(refused)
            return
        raw = self.rfile.read(length) if length is not None else None
        try:
            if raw is None:
                raise ValueError("missing or oversized body")
            request = json.loads(raw)
            args = request.get("args", [])
            stdin_text = request.get("stdin", "")
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                raise ValueError("args must be a list of strings")
            if not isinstance(stdin_text, str):
                raise TypeError("stdin must be a string")
        except (ValueError, AttributeError, TypeError):
            self._reply(HTTPStatus.BAD_REQUEST)
            return
        if not CLAUDE:
            self._reply(HTTPStatus.SERVICE_UNAVAILABLE, b"claude not found on host PATH")
            return
        try:
            r = subprocess.run(
                [CLAUDE, *args],
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CLAUDE_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc).encode())
            return
        self._reply(HTTPStatus.OK, r.stdout.encode(), exit_code=r.returncode)

    def log_message(self, fmt, *args):
        # Only log errors, not every request.
        if args and str(args[1]) not in ("200",):
            super().log_message(fmt, *args)


def make_server(host: str, port: int, token: str) -> HTTPServer:
    if not token:
        raise ValueError(f"{TOKEN_ENV} must be a non-empty shared secret")
    server = HTTPServer((host, port), Handler)
    server.token = token
    return server


if __name__ == "__main__":
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        sys.exit(f"{TOKEN_ENV} is not set: refusing to run claude for unauthenticated callers")
    print(f"claude proxy listening on {HOST}:{PORT}")
    print(f"claude binary: {CLAUDE or 'NOT FOUND — fix your PATH before starting'}")
    make_server(HOST, PORT, token).serve_forever()
