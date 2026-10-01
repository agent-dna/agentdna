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

from agentdna.auth import sink
from agentdna.auth.evidence import METHOD_BEARER_JWT, METHOD_NONE
from agentdna.auth.key import ENV_KEY, load_key
from agentdna.auth.sink import ENV_FILE, ENV_ON
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


def test_a_short_key_is_warned_about_once(monkeypatch):
    """A short key can be guessed from key_version. Warn - once - but still use
    it, because evidence never stops a server from starting."""
    import agentdna.auth.key as key_module

    monkeypatch.setattr(key_module, "_warned_short", False)
    monkeypatch.setenv(ENV_KEY, "step-three")
    logger = FakeLogger()

    for _ in range(50):
        load_key("unused", logger)

    assert [event for event, _ in logger.warnings] == ["agentdna.authevidence.short_key"]


def test_a_long_key_is_not_warned_about(monkeypatch):
    import agentdna.auth.key as key_module

    monkeypatch.setattr(key_module, "_warned_short", False)
    monkeypatch.setenv(ENV_KEY, "x" * 32)
    logger = FakeLogger()

    load_key("unused", logger)

    assert logger.warnings == []


def test_a_local_key_is_loaded_and_warned_about_once(monkeypatch, tmp_path):
    """Every observed call asks for the key. Reading the file and warning on
    each one filled the log and put a disk read on every request."""
    monkeypatch.delenv(ENV_KEY, raising=False)
    logger = FakeLogger()

    keys = {load_key(str(tmp_path), logger) for _ in range(50)}

    assert len(keys) == 1
    assert len(logger.warnings) == 1


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


class FakeProvenance:
    provenance_url = ""


class FakeDNA:
    def __init__(self, config_dir):
        self.config_dir = config_dir
        self.logger = FakeLogger()
        self.api_key = ""
        self.provenance = FakeProvenance()

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
        monkeypatch.setenv(ENV_ON, "true")
        monkeypatch.setattr(
            "agentdna.mcp.server.observer.request_headers",
            lambda context: {"authorization": CREDENTIAL},
        )

        dna = FakeDNA(str(tmp_path))
        dna.api_key = "an-api-key"
        dna.provenance.provenance_url = f"http://127.0.0.1:{server.server_address[1]}"
        workflow = FakeWorkflow(FakeEnvelope(run_id="run-a41f", signature="sig-6f1a2b"))
        record_request(dna, workflow, context=None)
        sink._waiting.join()  # the post happens on the sender thread
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
    monkeypatch.delenv(ENV_ON, raising=False)
    calls = []
    monkeypatch.setattr(
        "agentdna.mcp.server.observer.request_headers",
        lambda context: calls.append(1) or {},
    )

    workflow = FakeWorkflow(FakeEnvelope(run_id="r", signature="s"))
    record_request(FakeDNA(str(tmp_path)), workflow, context=None)

    assert calls == []


def test_a_full_queue_drops_the_record_and_says_so(monkeypatch):
    """A middleware that stays down fills the queue. The next record is
    dropped - never waited on - and the drop is logged by name."""
    import queue

    full = queue.Queue(maxsize=1)
    full.put("already waiting")
    monkeypatch.setattr(sink, "_waiting", full)
    monkeypatch.setattr(sink, "_start_sender", lambda: None)
    monkeypatch.delenv(ENV_FILE, raising=False)
    monkeypatch.setenv(ENV_ON, "true")

    warnings = []
    monkeypatch.setattr(sink.logger, "warning", lambda event, **f: warnings.append(event))

    class Evidence:
        def as_dict(self):
            return {"request_id": "sig-dropped"}

    sink.send(Evidence(), FakeDNA(""))  # must not raise

    assert warnings == ["agentdna.authevidence.queue_full"]


def test_the_switch_is_on_or_off_never_a_url(monkeypatch):
    """Only an explicit yes turns posting on. "false" is not an empty string,
    so a check for "is it set" would have read it as on."""
    monkeypatch.delenv(ENV_FILE, raising=False)
    for value in ("true", "TRUE", "1", "on", "yes"):
        monkeypatch.setenv(ENV_ON, value)
        assert sink.enabled(), value
    for value in ("", "false", "0", "off", "no", "http://127.0.0.1:8080"):
        monkeypatch.setenv(ENV_ON, value)
        assert not sink.enabled(), value


def test_a_middleware_that_refuses_the_post_is_logged(monkeypatch, tmp_path):
    """requests does not raise on a 404. Without a status check, a middleware
    with no evidence endpoint dropped every record and said nothing."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class NoSuchEndpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), NoSuchEndpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    warnings = []
    monkeypatch.setattr(sink.logger, "warning", lambda event, **f: warnings.append(event))
    monkeypatch.delenv(ENV_FILE, raising=False)
    monkeypatch.setenv(ENV_ON, "true")

    class Evidence:
        def as_dict(self):
            return {"request_id": "sig-refused"}

    dna = FakeDNA(str(tmp_path))
    dna.provenance.provenance_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        sink.send(Evidence(), dna)
        sink._waiting.join()  # the post happens on the sender thread
    finally:
        server.shutdown()

    assert warnings == ["agentdna.authevidence.post_failed"]
