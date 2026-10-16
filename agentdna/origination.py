"""Start a human login and create an AgentDNA run."""

import os
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urljoin, urlparse

import requests

LOGIN_PATH = "/agent-admin/v1/run/auth/login"
COMPLETE_PATH = "/agent-admin/v1/run/auth/complete"
EXCHANGE_PATH = "/agent-admin/v1/run/auth/exchange"

HTTP_TIMEOUT = 30

DONE_PAGE = b"""
<html>
    <body>
        <h2>Sign-in complete.</h2>
        <p>You can close this tab and return to your app.</p>
    </body>
</html>
"""


def originate(
    *,
    auth_server_url: str | None = None,
    timeout: float = 300,
    token: str | None = None,
) -> tuple[str, str, str]:
    """
    Log in a human and start a run.

    Returns:
        (run_id, email, api_key)
    """

    server_url = auth_server_url or os.getenv("AGENTDNA_AUTH_SERVER_URL")

    if not server_url:
        raise RuntimeError("AGENTDNA_AUTH_SERVER_URL is not set")

    # If the app already has a user's access token,
    # we don't need to open a browser.
    if token:
        result = post(server_url, EXCHANGE_PATH, {"token": token})
        return get_login_result(result)

    # 1. Ask IBS to start the login.
    login = post(server_url, LOGIN_PATH, {})
    login_url = login["authorize_url"]

    # 2. Open the human's browser (or show the link if we can't).
    if not webbrowser.open(login_url):
        print(f"Open this link to sign in:\n{login_url}")

    # 3. Wait for the browser to redirect back to us.
    redirect_url = get_redirect_url(login_url)
    state, code = wait_for_login(redirect_url, timeout)

    # 4. Give the code back to IBS to finish the login.
    result = post(
        server_url,
        COMPLETE_PATH,
        {
            "state": state,
            "code": code,
        },
    )

    return get_login_result(result)


def get_login_result(result: dict) -> tuple[str, str, str]:
    """Extract the values returned after login."""
    return (
        result.get("run_id", ""),
        result["email"],
        result.get("api_key", ""),
    )


def post(server_url: str, path: str, data: dict) -> dict:
    """Send a request to IBS and return its data."""
    response = requests.post(
        urljoin(server_url, path),
        json=data,
        timeout=HTTP_TIMEOUT,
    )

    response.raise_for_status()

    body = response.json()

    if not body.get("status"):
        raise RuntimeError(body.get("message", "Request failed"))

    return body.get("data") or {}


def get_redirect_url(login_url: str) -> str:
    """Get the browser callback URL from the login URL."""
    query = parse_qs(urlparse(login_url).query)
    return query["redirect_uri"][0]


def wait_for_login(
    redirect_url: str,
    timeout: float,
) -> tuple[str, str]:
    """Wait for the browser to redirect back with state and code."""

    target = urlparse(redirect_url)
    result = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            request = urlparse(self.path)

            # Ignore requests that aren't for our callback.
            if request.path != (target.path or "/"):
                self.send_response(404)
                self.end_headers()
                return

            result.update({key: value[0] for key, value in parse_qs(request.query).items()})

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(DONE_PAGE)))
            self.end_headers()

            self.wfile.write(DONE_PAGE)

        def log_message(self, *_):
            pass

    server = HTTPServer(
        (target.hostname or "127.0.0.1", target.port or 80),
        CallbackHandler,
    )
    server.timeout = 1  # wake up every second to check the deadline

    deadline = time.time() + timeout
    try:
        while not result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if not result:
        raise RuntimeError(f"Login timed out after {timeout:.0f} seconds")

    if "error" in result:
        raise RuntimeError(f"Login failed: {result['error']}")

    if "state" not in result or "code" not in result:
        raise RuntimeError("Login callback is missing state or code")

    return result["state"], result["code"]
