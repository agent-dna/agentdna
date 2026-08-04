"""Origination login (SDK side) — run by the developer's app at run start.

Calls the Identity Binding Service to start an OIDC login for the human,
opens the browser, catches the redirect on a local loopback, and hands the code
back to complete the login. Returns the `run_id`.

No secret or token touches this process, and the DID is not signed here —
possession is proven later on the CoCA chain. Self-contained on `requests` +
stdlib.
"""

from __future__ import annotations

import os
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urljoin, urlparse

import requests

_TIMEOUT = 30
_LOGIN_PATH = "/agent-admin/v1/run/auth/login"
_COMPLETE_PATH = "/agent-admin/v1/run/auth/complete"
_DONE_HTML = (
    b"<!doctype html><meta charset=utf-8>"
    b"<body style='font-family:sans-serif'><h2>Sign-in complete.</h2>"
    b"<p>You can close this tab and return to your app.</p>"
)


def originate(*, auth_server_url: str | None = None, timeout: float = 300) -> tuple[str, str]:
    """Drive a full origination login and return ``(run_id, email)``. Fail-closed.

    The email is the authenticated human's; the SDK builds the actor under it
    (the signing key is stored by email). Endpoints are admin-configured;
    `auth_server_url` defaults to the ``AGENTDNA_AUTH_SERVER_URL`` env var.
    """
    base = auth_server_url or os.environ.get("AGENTDNA_AUTH_SERVER_URL")
    if not base:
        raise RuntimeError("AGENTDNA_AUTH_SERVER_URL is not set")

    started = _post(base, _LOGIN_PATH, {})
    authorize_url = started["authorize_url"]

    webbrowser.open(authorize_url) or print(f"Open this URL to sign in:\n{authorize_url}")
    state, code = _wait_for_callback(_redirect_uri_of(authorize_url), timeout)

    finished = _post(base, _COMPLETE_PATH, {"state": state, "code": code})
    return finished["run_id"], finished["email"]


def _redirect_uri_of(authorize_url: str) -> str:
    """The loopback the server told the IdP to redirect to — we listen exactly there."""
    return parse_qs(urlparse(authorize_url).query)["redirect_uri"][0]


def _post(base: str, path: str, body: dict) -> dict:
    """POST to the IBS and unwrap its response; raise on failure."""
    r = requests.post(urljoin(base, path), json=body, timeout=_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if not data.get("status"):
        raise RuntimeError(f"{path}: {data.get('message')}")
    return data.get("data") or {}


def _wait_for_callback(redirect_uri: str, timeout: float) -> tuple[str, str]:
    """Block on the loopback until the IdP redirect arrives; return (state, code).

    Single-shot listener bound to the redirect's host/port; ignores stray paths
    and surfaces an IdP `error` at the front door. CSRF (`state`) is validated
    server-side by the IBS, which minted it.
    """
    target = urlparse(redirect_uri)

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib naming)
            got = urlparse(self.path)
            if got.path != (target.path or "/"):
                self.send_response(404)
                self.end_headers()
                return
            self.server.result = {k: v[0] for k, v in parse_qs(got.query).items()}
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_DONE_HTML)

        def log_message(self, *_):  # silence default logging
            return

    server = HTTPServer((target.hostname or "127.0.0.1", target.port or 80), _Handler)
    server.result = None
    server.timeout = 1.0  # re-check the deadline at least this often
    deadline = time.time() + timeout
    try:
        while server.result is None and time.time() < deadline:
            server.handle_request()
        result = server.result
    finally:
        server.server_close()

    if result is None:
        raise RuntimeError(f"no login callback on {redirect_uri} within {timeout:.0f}s")
    if "error" in result:
        raise RuntimeError(f"login failed at IdP: {result['error']}")
    if "code" not in result or "state" not in result:
        raise RuntimeError("login callback missing state/code")
    return result["state"], result["code"]
