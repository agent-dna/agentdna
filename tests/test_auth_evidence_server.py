"""Tests for the server observation point.

The one that matters most is test_headers_reach_the_middleware_over_http. It
runs a real server and makes a real call, because the two things that went
wrong here were both accessors that looked right and returned nothing.
"""

import asyncio
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass

import httpx
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware

from agentdna.authevidence import METHOD_BEARER_JWT, METHOD_NONE
from agentdna.evidence_sink import ENV_FILE, ENV_URL
from agentdna.fingerprintkey import ENV_KEY, load_key
from agentdna.mcp.server.observer import (
    SOURCE_SERVER_IN,
    capture_headers,
    record_request,
    request_headers,
)

TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJodHRwczovL2lkcCIsIm9pZCI6InByaXlhIn0.not-a-real-signature"
CREDENTIAL = f"Bearer {TOKEN}"


# --- the fingerprint key --------------------------------------------------


def test_a_configured_key_is_the_same_everywhere(monkeypatch, tmp_path):
    """The deployment sets one value and every observer derives the same key."""
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    assert load_key(str(tmp_path)) == load_key(str(tmp_path / "another-machine"))


def test_a_local_key_survives_a_restart(monkeypatch, tmp_path):
    """With nothing configured we keep one, so a single process stays
    consistent with itself. It will not match another machine - and that shows
    up as unusable evidence, never as a false identity change."""
    monkeypatch.delenv(ENV_KEY, raising=False)
    first = load_key(str(tmp_path))
    assert first == load_key(str(tmp_path))
    assert first != load_key(str(tmp_path / "another-machine"))


def test_a_local_key_is_not_world_readable(monkeypatch, tmp_path):
    monkeypatch.delenv(ENV_KEY, raising=False)
    load_key(str(tmp_path))
    mode = (tmp_path / "fingerprint.key").stat().st_mode & 0o777
    assert mode == 0o600


# --- transport capture ----------------------------------------------------


def test_capture_headers_keeps_only_auth_headers():
    """Everything else in the request never enters the variable."""
    seen = {}

    async def app(scope, receive, send):
        seen.update(request_headers(context=None))

    wrapped = capture_headers(app)
    asyncio.run(
        wrapped(
            {
                "type": "http",
                "headers": [
                    (b"authorization", CREDENTIAL.encode()),
                    (b"cookie", b"session=secret"),
                    (b"x-internal-token", b"another-secret"),
                    (b"host", b"example.com"),
                ],
            },
            None,
            None,
        )
    )

    assert seen == {"authorization": CREDENTIAL}


# --- against a real server ------------------------------------------------


def _run(app, port):
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="critical")


def _call_tool(port: int, headers: dict) -> int:
    """An MCP tool call over plain HTTP, so the test controls every header."""
    base = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **headers,
    }
    url = f"http://127.0.0.1:{port}/mcp/"
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        started = client.post(
            url,
            headers=base,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        if started.headers.get("mcp-session-id"):
            base["Mcp-Session-Id"] = started.headers["mcp-session-id"]
        client.post(
            url, headers=base, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        answered = client.post(
            url,
            headers=base,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "ping", "arguments": {}},
            },
        )
        return answered.status_code


def _server(port: int, seen: dict, wrap_transport: bool, blind_protocol_layer: bool):
    class Probe(Middleware):
        async def on_call_tool(self, context, call_next):
            if blind_protocol_layer:
                # Stand in for a server with no request context variable, which
                # is what the official MCP SDK is.
                import fastmcp.server.dependencies as dependencies

                original = dependencies.get_http_headers
                dependencies.get_http_headers = lambda **kwargs: {}
                try:
                    seen.update(request_headers(context))
                finally:
                    dependencies.get_http_headers = original
            else:
                seen.update(request_headers(context))
            return await call_next(context)

    mcp = FastMCP(f"probe-{port}")
    mcp.add_middleware(Probe())

    @mcp.tool
    def ping() -> str:
        """Does nothing."""
        return "pong"

    app = mcp.http_app()
    if wrap_transport:
        app = capture_headers(app)

    threading.Thread(target=_run, args=(app, port), daemon=True).start()
    time.sleep(3)


def test_headers_reach_the_middleware_over_http():
    """Both ways of reaching the headers, against a real server.

    The FastMCP path needs `include`: without it the accessor strips
    `authorization` and returns everything except the header this is for.

    The transport path is for servers with no request context variable, where
    the protocol layer has nothing to read at all.
    """
    protocol_layer, transport_layer = {}, {}

    _server(8951, protocol_layer, wrap_transport=False, blind_protocol_layer=False)
    _server(8952, transport_layer, wrap_transport=True, blind_protocol_layer=True)

    assert _call_tool(8951, {"Authorization": CREDENTIAL}) == 200
    assert _call_tool(8952, {"Authorization": CREDENTIAL}) == 200

    assert protocol_layer == {"authorization": CREDENTIAL}
    assert transport_layer == {"authorization": CREDENTIAL}


# --- the record -----------------------------------------------------------


@dataclass
class FakeEnvelope:
    run_id: str
    signature: str


class FakeWorkflow:
    def __init__(self, envelope):
        self._envelope = envelope

    def get_latest_envelope(self):
        return self._envelope


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, event, **fields):
        self.warnings.append((event, fields))


class FakeDNA:
    def __init__(self, config_dir):
        self.config_dir = config_dir
        self.logger = FakeLogger()
        self.api_key = ""

    def get_actor_id(self):
        return "did:rubix:tickets-server"


def test_a_record_carries_the_ids_from_the_chain(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "evidence.jsonl"))
    monkeypatch.setattr(
        "agentdna.mcp.server.observer.request_headers",
        lambda context: {"authorization": CREDENTIAL},
    )

    workflow = FakeWorkflow(FakeEnvelope(run_id="run-a41f", signature="sig-6f1a2b"))
    record_request(FakeDNA(str(tmp_path)), workflow, context=None)

    written = json.loads((tmp_path / "evidence.jsonl").read_text())
    assert written["run_id"] == "run-a41f"
    assert written["request_id"] == "sig-6f1a2b"
    assert written["source"] == SOURCE_SERVER_IN
    assert written["auth_method"] == METHOD_BEARER_JWT
    assert written["destination"] == "did:rubix:tickets-server"
    assert written["identity_id"]


def test_no_credential_reaches_the_file(monkeypatch, tmp_path):
    """The check to hand a security reviewer, because it can fail."""
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "evidence.jsonl"))
    monkeypatch.setattr(
        "agentdna.mcp.server.observer.request_headers",
        lambda context: {"authorization": CREDENTIAL, "x-api-key": "a-plain-key"},
    )

    workflow = FakeWorkflow(FakeEnvelope(run_id="r", signature="s"))
    record_request(FakeDNA(str(tmp_path)), workflow, context=None)

    written = (tmp_path / "evidence.jsonl").read_text()
    assert TOKEN not in written
    assert CREDENTIAL not in written
    assert "a-plain-key" not in written
    assert "Bearer" not in written


def test_nothing_is_written_when_the_feature_is_off(monkeypatch, tmp_path):
    """Install the library, change nothing, and no header is ever read."""
    monkeypatch.delenv(ENV_FILE, raising=False)
    calls = []
    monkeypatch.setattr(
        "agentdna.mcp.server.observer.request_headers",
        lambda context: calls.append(1) or {},
    )

    workflow = FakeWorkflow(FakeEnvelope(run_id="r", signature="s"))
    record_request(FakeDNA(str(tmp_path)), workflow, context=None)

    assert calls == []
    assert not os.path.exists(tmp_path / "evidence.jsonl")


def test_a_failure_never_breaks_the_request(monkeypatch, tmp_path):
    """Evidence is not worth breaking a tool call for."""
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "no" / "such" / "dir" / "evidence.jsonl"))

    class Broken:
        def get_latest_envelope(self):
            raise RuntimeError("chain unreadable")

    dna = FakeDNA(str(tmp_path))
    record_request(dna, Broken(), context=None)  # must not raise

    assert dna.logger.warnings
    assert dna.logger.warnings[0][0] == "agentdna.authevidence.record_failed"


def test_stdio_has_no_headers_and_that_is_an_answer(monkeypatch, tmp_path):
    """Nothing crossed a network, so no credential was presented."""
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "evidence.jsonl"))
    monkeypatch.setattr("agentdna.mcp.server.observer.request_headers", lambda context: {})

    workflow = FakeWorkflow(FakeEnvelope(run_id="r", signature="s"))
    record_request(FakeDNA(str(tmp_path)), workflow, context=None)

    written = json.loads((tmp_path / "evidence.jsonl").read_text())
    assert written["auth_method"] == METHOD_NONE
    assert written["credential_id"] == ""
    assert written["identity_id"] is None


def test_records_from_different_keys_are_marked_as_such(monkeypatch, tmp_path):
    """key_version is what stops two keys being silently compared."""
    monkeypatch.setattr(
        "agentdna.mcp.server.observer.request_headers",
        lambda context: {"authorization": CREDENTIAL},
    )
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "evidence.jsonl"))
    workflow = FakeWorkflow(FakeEnvelope(run_id="r", signature="s"))

    monkeypatch.setenv(ENV_KEY, "key-on-machine-a")
    record_request(FakeDNA(str(tmp_path)), workflow, context=None)
    monkeypatch.setenv(ENV_KEY, "key-on-machine-b")
    record_request(FakeDNA(str(tmp_path)), workflow, context=None)

    a, b = [json.loads(line) for line in (tmp_path / "evidence.jsonl").read_text().splitlines()]
    assert a["key_version"] != b["key_version"]
    assert a["identity_id"] != b["identity_id"]
    assert hashlib.sha256(b"x").hexdigest()  # sanity: hashlib in use


def test_auth_status_is_unknown_not_accepted(monkeypatch, tmp_path):
    """This point runs where the request arrived, not where the credential was
    judged. Recording `accepted` would infer something we never observed."""
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "evidence.jsonl"))
    monkeypatch.setattr(
        "agentdna.mcp.server.observer.request_headers",
        lambda context: {"authorization": CREDENTIAL},
    )

    workflow = FakeWorkflow(FakeEnvelope(run_id="r", signature="s"))
    record_request(FakeDNA(str(tmp_path)), workflow, context=None)

    written = json.loads((tmp_path / "evidence.jsonl").read_text())
    assert written["auth_status"] == "unknown"


def test_records_post_to_the_middleware(monkeypatch, tmp_path):
    """The wire format, against a server that captures what arrives.

    Checks the path, the API key header, the batch shape, and that no
    credential rides along with it.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    arrived = []

    class Capture(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            arrived.append(
                {
                    "path": self.path,
                    "api_key": self.headers.get("X-API-Key"),
                    "body": json.loads(self.rfile.read(length) or b"{}"),
                }
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":true}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv(ENV_KEY, "one-shared-value")
        monkeypatch.delenv(ENV_FILE, raising=False)
        monkeypatch.setenv(ENV_URL, f"http://127.0.0.1:{server.server_address[1]}")
        monkeypatch.setattr(
            "agentdna.mcp.server.observer.request_headers",
            lambda context: {"authorization": CREDENTIAL},
        )

        dna = FakeDNA(str(tmp_path))
        dna.api_key = "an-api-key"
        workflow = FakeWorkflow(FakeEnvelope(run_id="run-a41f", signature="sig-6f1a2b"))
        record_request(dna, workflow, context=None)
    finally:
        server.shutdown()

    assert len(arrived) == 1
    assert arrived[0]["path"] == "/core/v1/auth-evidence"
    assert arrived[0]["api_key"] == "an-api-key"

    records = arrived[0]["body"]["records"]
    assert len(records) == 1
    assert records[0]["request_id"] == "sig-6f1a2b"
    assert records[0]["source"] == SOURCE_SERVER_IN

    on_the_wire = json.dumps(arrived[0]["body"])
    assert TOKEN not in on_the_wire
    assert "Bearer" not in on_the_wire


def test_nothing_is_sent_when_neither_sink_is_configured(monkeypatch, tmp_path):
    monkeypatch.delenv(ENV_FILE, raising=False)
    monkeypatch.delenv(ENV_URL, raising=False)
    calls = []
    monkeypatch.setattr(
        "agentdna.mcp.server.observer.request_headers",
        lambda context: calls.append(1) or {},
    )

    workflow = FakeWorkflow(FakeEnvelope(run_id="r", signature="s"))
    record_request(FakeDNA(str(tmp_path)), workflow, context=None)

    assert calls == []
