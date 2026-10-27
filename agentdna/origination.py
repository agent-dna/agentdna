"""Start a human login and create an AgentDNA run."""

import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urljoin, urlparse

import requests

# The AgentDNA auth server, used unless the app passes its own.
DEFAULT_AUTH_SERVER_URL = "https://user-auth.agentdna.io"

LOGIN_PATH = "/user-auth/v1/login"
COMPLETE_PATH = "/user-auth/v1/complete"

HTTP_TIMEOUT = 30  # seconds, for each call to the auth server

# Shown in the browser once sign-in is done.
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
    auth_server_url: str = DEFAULT_AUTH_SERVER_URL,
    timeout: float = 300,
) -> tuple[str, str]:
    """
    Log in a human and start a run.

    Returns:
        (run_id, email)
    """

    # 1. Ask the auth server to start the login.
    login = post(auth_server_url, LOGIN_PATH, {})
    login_url = login["authorize_url"]

    # 2. Open the human's browser (or show the link if we can't).
    if not webbrowser.open(login_url):
        print(f"Open this link to sign in:\n{login_url}")

    # 3. Wait for the browser to redirect back to us.
    redirect_url = get_redirect_url(login_url)
    state, code = wait_for_login(redirect_url, timeout)

    # 4. Give the code back to the auth server to finish the login.
    result = post(
        auth_server_url,
        COMPLETE_PATH,
        {
            "state": state,
            "code": code,
        },
    )

    return get_login_result(result)


def get_login_result(result: dict) -> tuple[str, str]:
    """Extract the run_id and email returned after login. Both are required."""
    return result["run_id"], result["email"]


def post(server_url: str, path: str, data: dict) -> dict:
    """Send a request to the auth server and return its data."""
    response = requests.post(
        urljoin(server_url, path),
        json=data,
        timeout=HTTP_TIMEOUT,
    )

    response.raise_for_status()

    body = response.json()

    # The server answers {"status": true/false, "data": ..., "message": ...}.
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

    # Handles the browser's visit to the redirect URL.
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            request = urlparse(self.path)

            # Ignore requests that aren't for our callback.
            if request.path != (target.path or "/"):
                self.send_response(404)
                self.end_headers()
                return

            # Keep what the browser brought back: state and code, or error.
            result.update({key: value[0] for key, value in parse_qs(request.query).items()})

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(DONE_PAGE)))
            self.end_headers()

            self.wfile.write(DONE_PAGE)

        def log_message(self, *_):
            pass  # keep the terminal quiet

    # The browser must come back to this computer, on a set port.
    if target.hostname not in ("localhost", "127.0.0.1") or not target.port:
        raise RuntimeError(
            f"The redirect URL must look like http://localhost:<port>/..., got {redirect_url}"
        )

    # A small local web server at the redirect URL's host and port.
    server = HTTPServer((target.hostname, target.port), CallbackHandler)
    server.timeout = 1  # wake up every second to check the deadline

    # Handle visits until the callback arrives or time runs out.
    deadline = time.time() + timeout
    try:
        while not result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    # Check what came back.
    if not result:
        raise RuntimeError(f"Login timed out after {timeout:.0f} seconds")

    if "error" in result:
        raise RuntimeError(f"Login failed: {result['error']}")

    if "state" not in result or "code" not in result:
        raise RuntimeError("Login callback is missing state or code")

    return result["state"], result["code"]
