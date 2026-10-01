"""Tests for the outbound observation point."""

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

import httpx
import pytest

from agentdna.auth import httpobserver, sink
from agentdna.auth.evidence import METHOD_BEARER_JWT
from agentdna.auth.httpobserver import SOURCE_CLIENT_OUT, SOURCE_SERVER_OUT
from agentdna.auth.key import ENV_KEY
from agentdna.auth.sink import ENV_FILE

TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJodHRwczovL2lkcCIsIm9pZCI6InByaXlhIn0.not-real"
CREDENTIAL = f"Bearer {TOKEN}"


@dataclass
class FakeEnvelope:
    run_id: str
    signature: str


class FakeWorkflow:
    def __init__(self, envelope):
        self._envelope = envelope

    def get_latest_envelope(self):
        return self._envelope


class FakeContext:
    def __init__(self, workflows):
        self.workflows = workflows


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


class BearerAuth(httpx.Auth):
    """Attaches the credential at send time, the way OAuth does."""

    def auth_flow(self, request):
        request.headers["Authorization"] = CREDENTIAL
        yield request


@contextmanager
def installed(dna, source=SOURCE_SERVER_OUT):
    """install(), with everything it patches put back afterwards."""
    from requests.adapters import HTTPAdapter

    saved = (httpx.Client.__init__, httpx.AsyncClient.__init__, HTTPAdapter.send)
    httpobserver._installed = False
    try:
        httpobserver.install(dna, source)
        yield
    finally:
        httpx.Client.__init__, httpx.AsyncClient.__init__, HTTPAdapter.send = saved
        httpobserver._installed = False


@pytest.fixture
def serving(monkeypatch, tmp_path):
    """An installed hook, inside a request, writing to a file."""
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "evidence.jsonl"))

    workflow = FakeWorkflow(FakeEnvelope(run_id="run-a41f", signature="sig-incoming"))
    monkeypatch.setattr(httpobserver, "_current_envelope", lambda: workflow.get_latest_envelope())

    with installed(FakeDNA(str(tmp_path))):
        yield tmp_path / "evidence.jsonl"


def _call(auth=None, headers=None):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        auth=auth,
        headers=headers or {},
    )
    client.get("http://backend.internal/rows")


def test_a_credential_applied_by_auth_is_seen(serving):
    """The reason this hooks the request event and not send().

    httpx runs the auth flow inside send(), so a hook on send() sees the
    request before the credential is on it - and every OAuth call would record
    no login at all.
    """
    _call(auth=BearerAuth())

    written = json.loads(serving.read_text())
    assert written["auth_method"] == METHOD_BEARER_JWT
    assert written["identity_id"]


def test_the_record_belongs_to_the_request_being_served(serving):
    """The outbound leg shares the incoming request's id, with its own source.
    That is what lets both legs sit on one request without colliding."""
    _call(headers={"Authorization": CREDENTIAL})

    written = json.loads(serving.read_text())
    assert written["request_id"] == "sig-incoming"
    assert written["run_id"] == "run-a41f"
    assert written["source"] == SOURCE_SERVER_OUT
    assert written["destination"] == "backend.internal"


def test_no_credential_reaches_the_file(serving):
    _call(headers={"Authorization": CREDENTIAL, "X-Api-Key": "a-plain-key"})

    written = serving.read_text()
    assert TOKEN not in written
    assert "a-plain-key" not in written
    assert "Bearer" not in written


def test_calls_outside_a_request_are_ignored(monkeypatch, tmp_path):
    """The whole safety story: no workflow in context, nothing read.

    This is every call to the LLM, to telemetry, to a package index.
    """
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "evidence.jsonl"))
    monkeypatch.setattr(httpobserver, "_current_envelope", lambda: None)

    with installed(FakeDNA(str(tmp_path))):
        _call(headers={"Authorization": CREDENTIAL})

    assert not (tmp_path / "evidence.jsonl").exists()


def test_a_caller_keeps_its_own_event_hooks(serving):
    """Installing must not quietly drop hooks the application already set."""
    theirs = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        event_hooks={"request": [lambda request: theirs.append(request.url.path)]},
    )
    client.get("http://backend.internal/rows")

    assert theirs == ["/rows"]
    assert serving.exists()


def test_install_is_idempotent(serving, tmp_path):
    """Called from every request, so it must not stack hooks."""
    httpobserver.install(FakeDNA(str(tmp_path)))
    httpobserver.install(FakeDNA(str(tmp_path)))

    _call(headers={"Authorization": CREDENTIAL})

    assert len(serving.read_text().strip().splitlines()) == 1


def test_a_failure_never_breaks_the_call(monkeypatch, tmp_path):
    """Evidence is not worth failing a backend call for."""
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "no" / "such" / "dir" / "evidence.jsonl"))
    monkeypatch.setattr(
        httpobserver,
        "_current_envelope",
        lambda: FakeEnvelope(run_id="r", signature="s"),
    )

    dna = FakeDNA(str(tmp_path))
    with installed(dna):
        _call(headers={"Authorization": CREDENTIAL})  # must not raise

    events = [event for event, _ in dna.logger.warnings]
    assert "agentdna.authevidence.outbound_failed" in events


# --- requests ------------------------------------------------------------


class RequestsBearerAuth:
    """A requests auth object. Applies the credential while preparing."""

    def __call__(self, request):
        request.headers["Authorization"] = CREDENTIAL
        return request


def _requests_call(auth=None, headers=None):
    """A real requests call against a throwaway server.

    Deliberately not a stub adapter: an HTTPAdapter subclass that overrides
    send() without calling super() bypasses the hook, so a stubbed test would
    pass while proving nothing about the real path.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import requests

    class Quiet(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        requests.get(
            f"http://127.0.0.1:{server.server_address[1]}/rows",
            auth=auth,
            headers=headers or {},
            timeout=10,
        )
    finally:
        server.shutdown()


def test_requests_calls_are_recorded(serving):
    """HTTPAdapter.send is the seam - requests applies auth while preparing,
    so the headers are complete by the time the adapter sees them."""
    _requests_call(auth=RequestsBearerAuth())

    written = json.loads(serving.read_text())
    assert written["auth_method"] == METHOD_BEARER_JWT
    assert written["identity_id"]
    assert written["destination"] == "127.0.0.1"
    assert written["source"] == SOURCE_SERVER_OUT


def test_a_requests_call_is_recorded_once(serving):
    """requests runs on urllib3 underneath. Hooking both layers would record
    every call twice, so only the requests seam is hooked."""
    _requests_call(headers={"Authorization": CREDENTIAL})

    assert len(serving.read_text().strip().splitlines()) == 1


def test_requests_calls_outside_a_request_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "evidence.jsonl"))
    monkeypatch.setattr(httpobserver, "_current_envelope", lambda: None)

    with installed(FakeDNA(str(tmp_path))):
        _requests_call(headers={"Authorization": CREDENTIAL})

    assert not (tmp_path / "evidence.jsonl").exists()


# --- the call's own signature ---------------------------------------------
#
# An MCP tool call carries its workflow in `params._meta`, so it already says
# which request it is. These pin the wire format with literals rather than the
# SDK's constants, so a change to either side shows up here.


def _tool_call_body(signature, padding=""):
    """The JSON-RPC body an AgentDNA MCP client actually sends."""
    workflow = {
        "id": "intent-1",
        "envelope": {
            "from": "did:rubix:agent",
            "payload": "call sqlite_query" + padding,
            "signature": signature,
        },
    }
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "sqlite_query",
                "arguments": {},
                "_meta": {"agentdna": {"intent_workflow": json.dumps(workflow)}},
            },
        }
    ).encode()


def _post(body):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        headers={"Authorization": CREDENTIAL},
    )
    return client.post("http://backend.internal/mcp", content=body)


def test_a_tool_call_records_its_own_signature(serving):
    """The point of the whole change: the id the receiving server will file
    under, so both ends of one hop can be compared."""
    _post(_tool_call_body("sig-this-very-call"))

    written = json.loads(serving.read_text())
    assert written["request_id"] == "sig-this-very-call"


def test_a_call_carrying_no_workflow_falls_back_to_the_hop(serving):
    """Right for anything that never sees a workflow - a plain backend call."""
    _post(json.dumps({"jsonrpc": "2.0", "method": "tools/list"}).encode())

    written = json.loads(serving.read_text())
    assert written["request_id"] == "sig-incoming"


def test_an_oversized_body_is_not_parsed(serving):
    """An observer must not spend a request's time on a large payload, even one
    with a perfectly good signature in it."""
    body = _tool_call_body("sig-buried", padding="x" * httpobserver.MAX_BODY_BYTES)
    assert len(body) > httpobserver.MAX_BODY_BYTES

    _post(body)

    written = json.loads(serving.read_text())
    assert written["request_id"] == "sig-incoming"


def test_an_unreadable_body_never_breaks_the_call(serving):
    """Evidence is never worth failing a request for."""
    response = _post(b"\x80 not json at all")

    assert response.status_code == 200
    written = json.loads(serving.read_text())
    assert written["request_id"] == "sig-incoming"


# --- the client side needs a workflow on the call --------------------------


@pytest.fixture
def serving_client(monkeypatch, tmp_path):
    """The same as `serving`, but observed from the agent side."""
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "evidence.jsonl"))

    workflow = FakeWorkflow(FakeEnvelope(run_id="run-a41f", signature="sig-incoming"))
    monkeypatch.setattr(httpobserver, "_current_envelope", lambda: workflow.get_latest_envelope())

    with installed(FakeDNA(str(tmp_path)), source=SOURCE_CLIENT_OUT):
        yield tmp_path / "evidence.jsonl"


def test_a_client_tool_call_is_recorded(serving_client):
    _post(_tool_call_body("sig-this-very-call"))

    written = json.loads(serving_client.read_text())
    assert written["request_id"] == "sig-this-very-call"
    assert written["source"] == SOURCE_CLIENT_OUT


def test_client_transport_traffic_is_not_recorded(serving_client):
    """A tool call is five HTTP requests and only one carries a workflow.

    Recording the other four would file them against the hop being served -
    a request none of them are.
    """
    _post(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode())
    _post(json.dumps({"jsonrpc": "2.0", "method": "tools/list"}).encode())

    assert not serving_client.exists()


def test_the_server_side_still_falls_back_to_the_hop(serving):
    """The gate is client-side only. A server calling its own backend carries
    no workflow either, and that record is the point of the outbound leg."""
    _post(json.dumps({"jsonrpc": "2.0", "method": "anything"}).encode())

    written = json.loads(serving.read_text())
    assert written["request_id"] == "sig-incoming"
    assert written["source"] == SOURCE_SERVER_OUT


# --- installing before there is an AgentDNA --------------------------------
#
# `install_mcp_client()` runs at import, long before a run starts, so the
# client side has no instance to hand over. It borrows one from the context.


def test_the_instance_is_borrowed_from_the_context(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "evidence.jsonl"))

    workflow = FakeWorkflow(FakeEnvelope(run_id="run-a41f", signature="sig-incoming"))
    monkeypatch.setattr(httpobserver, "_current_envelope", lambda: workflow.get_latest_envelope())
    monkeypatch.setattr(httpobserver, "_current_dna", lambda: FakeDNA(str(tmp_path)))

    with installed(None, source=SOURCE_CLIENT_OUT):
        _post(_tool_call_body("sig-this-very-call"))

    written = json.loads((tmp_path / "evidence.jsonl").read_text())
    assert written["request_id"] == "sig-this-very-call"
    assert written["source"] == SOURCE_CLIENT_OUT


def test_no_instance_anywhere_is_not_an_error(monkeypatch, tmp_path):
    """Installed with none and no run in context. Nothing to fingerprint with,
    and nowhere to log it - so the call just goes through untouched."""
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "evidence.jsonl"))
    monkeypatch.setattr(httpobserver, "_current_dna", lambda: None)

    with installed(None, source=SOURCE_CLIENT_OUT):
        response = _post(_tool_call_body("sig-this-very-call"))

    assert response.status_code == 200
    assert not (tmp_path / "evidence.jsonl").exists()


def test_an_explicit_instance_still_wins(serving):
    """The server side passes one in, and that path is unchanged."""
    _post(json.dumps({"jsonrpc": "2.0", "method": "anything"}).encode())

    written = json.loads(serving.read_text())
    assert written["source"] == SOURCE_SERVER_OUT


# --- threads that cannot see the context -----------------------------------
#
# MCP clients run tool calls on a thread of their own. A ContextVar set on the
# thread that started the run is invisible there, so the client side takes the
# workflow off the wire and the instance from remember_dna().


def test_a_tool_call_is_recorded_from_a_foreign_thread(monkeypatch, tmp_path):
    """The real failure this feature hit: mcpadapt's own thread saw no context,
    so nothing was recorded at all."""
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "evidence.jsonl"))

    # nothing in context, anywhere - exactly what the foreign thread sees
    monkeypatch.setattr(httpobserver, "_current_dna", lambda: None)
    monkeypatch.setattr(httpobserver, "_current_envelope", lambda: None)
    monkeypatch.setattr(httpobserver, "_remembered_dna", FakeDNA(str(tmp_path)))

    with installed(None, source=SOURCE_CLIENT_OUT):
        done = []
        thread = threading.Thread(
            target=lambda: done.append(_post(_tool_call_body("sig-from-a-thread")))
        )
        thread.start()
        thread.join()

    assert done[0].status_code == 200
    written = json.loads((tmp_path / "evidence.jsonl").read_text())
    assert written["request_id"] == "sig-from-a-thread"
    assert written["source"] == SOURCE_CLIENT_OUT


def test_remember_dna_survives_the_thread_hop(monkeypatch, tmp_path):
    """remember_dna is called where the context is visible, and read where it
    is not. A module global crosses threads; a ContextVar does not."""
    dna = FakeDNA(str(tmp_path))
    monkeypatch.setattr(httpobserver, "_remembered_dna", None)

    httpobserver.remember_dna(dna)

    seen = []
    thread = threading.Thread(target=lambda: seen.append(httpobserver._remembered_dna))
    thread.start()
    thread.join()

    assert seen[0] is dna


def test_run_id_comes_off_the_wire_too(serving_client):
    """Not just the signature - the client side never reads the context."""
    body = _tool_call_body("sig-with-a-run")
    message = json.loads(body)
    workflow = json.loads(message["params"]["_meta"]["agentdna"]["intent_workflow"])
    workflow["envelope"]["run_id"] = "run-from-the-wire"
    message["params"]["_meta"]["agentdna"]["intent_workflow"] = json.dumps(workflow)

    _post(json.dumps(message).encode())

    written = json.loads(serving_client.read_text())
    assert written["run_id"] == "run-from-the-wire"


# --- AgentDNA's own work ---------------------------------------------------
#
# The server middleware wraps its checks, CBAC and signing in not_observed(),
# and only the tool in observed(). Marked where the work happens, not by host:
# a host list missed the CBAC service on the first real run.


def _get(host):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        headers={"Authorization": CREDENTIAL},
    )
    return client.get(f"https://{host}/anything")


def test_agentdna_own_work_is_not_a_hop(serving):
    """A whitelist check is AgentDNA's bookkeeping, not work the agent did."""
    with httpobserver.not_observed():
        _get("agentdna-admin-dev.agentdna.io")

    assert not serving.exists()


def test_the_tool_inside_agentdna_work_is_a_hop(serving):
    """The middleware's shape: everything unobserved except the tool."""
    with httpobserver.not_observed():
        _get("agentdna-admin-dev.agentdna.io")
        with httpobserver.observed():
            _get("api.github.com")
        _get("cbac-service.agentdna.io")

    rows = [json.loads(line) for line in serving.read_text().splitlines()]
    assert [row["destination"] for row in rows] == ["api.github.com"]


def test_agentdna_own_work_on_a_worker_thread_is_not_a_hop(serving):
    """CBAC posts from asyncio.to_thread. The mark has to follow it there."""
    import asyncio

    async def check():
        with httpobserver.not_observed():
            await asyncio.to_thread(_get, "cbac-service.agentdna.io")

    asyncio.run(check())

    assert not serving.exists()


def test_the_same_host_outside_agentdna_work_is_recorded(serving):
    """Nothing is skipped by host any more. A tool calling the admin server
    itself is a real hop."""
    _get("agentdna-admin-dev.agentdna.io")

    assert json.loads(serving.read_text())["destination"] == "agentdna-admin-dev.agentdna.io"


def _evidence_server(on_post):
    """A throwaway middleware. `on_post(handler)` answers each post."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Middleware(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            on_post(self)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Middleware)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _ok(handler):
    handler.send_response(200)
    handler.end_headers()


def _post_to(monkeypatch, server):
    """Turn posting on, with this server as the middleware."""
    monkeypatch.setenv(sink.ENV_ON, "true")
    monkeypatch.setattr(
        FakeProvenance, "provenance_url", f"http://127.0.0.1:{server.server_address[1]}"
    )


def test_recording_does_not_record_itself(serving, monkeypatch):
    """Evidence is posted with `requests`, and `requests` is patched. Without
    this, one record posts a record that posts a record, without end.

    Through a redirect to another hostname, on purpose: a skip-by-hostname
    list missed the redirected post, and one tool call became 49 posts.
    """
    hits = []

    def redirect_once(handler):
        hits.append(handler.path)
        if handler.path == sink.PATH:
            handler.send_response(307)
            handler.send_header("Location", f"http://localhost:{port}/landed")
            handler.end_headers()
        else:
            _ok(handler)

    server = _evidence_server(redirect_once)
    port = server.server_address[1]
    _post_to(monkeypatch, server)
    try:
        _call(headers={"Authorization": CREDENTIAL})
        sink._waiting.join()
    finally:
        server.shutdown()

    assert hits == [sink.PATH, "/landed"]  # one post, followed once
    assert len(serving.read_text().strip().splitlines()) == 1


def test_a_backend_on_the_middleware_host_is_recorded(serving, monkeypatch):
    """Same machine, different service. Common in dev, where everything is on
    127.0.0.1, and behind one gateway host in production."""
    server = _evidence_server(_ok)
    _post_to(monkeypatch, server)
    try:
        _get("127.0.0.1:5000")
        sink._waiting.join()
    finally:
        server.shutdown()

    assert json.loads(serving.read_text())["destination"] == "127.0.0.1"


def test_a_slow_middleware_does_not_hold_up_the_call(serving, monkeypatch):
    """The post runs on its own thread. Inline, a one-second middleware made
    every observed call one second slower - and froze an async server's loop."""

    def slow(handler):
        time.sleep(1.0)
        _ok(handler)

    server = _evidence_server(slow)
    _post_to(monkeypatch, server)
    try:
        started = time.perf_counter()
        _call(headers={"Authorization": CREDENTIAL})
        took = time.perf_counter() - started
        sink._waiting.join()
    finally:
        server.shutdown()

    assert took < 0.5


# --- the destination's verdict ---------------------------------------------
#
# Recorded from the response, not a probe: the destination already answered,
# and replaying a user's credential elsewhere to ask again would be worse than
# not knowing.


def _post_returning(status):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={})),
        headers={"Authorization": CREDENTIAL},
    )
    return client.post("http://backend.internal/rows", content=b"{}")


def test_a_credential_the_destination_took(serving):
    _post_returning(200)

    assert json.loads(serving.read_text())["auth_status"] == "accepted"


def test_a_credential_the_destination_refused(serving):
    """401 and 403 are the only answers that judge the credential."""
    for code in (401, 403):
        serving.unlink(missing_ok=True)
        _post_returning(code)

        assert json.loads(serving.read_text())["auth_status"] == "rejected"


def test_a_failure_that_judged_nothing_stays_unknown(serving):
    """A 500 is the destination breaking, a 404 is the wrong path, a redirect
    is neither. Calling any of them "accepted" would claim more than we saw."""
    for code in (500, 404, 302):
        serving.unlink(missing_ok=True)
        _post_returning(code)

        assert json.loads(serving.read_text())["auth_status"] == "unknown"


def test_the_credential_is_still_seen_on_the_response_hook(serving):
    """The reason the hook used to be on the request: httpx applies `auth`
    inside send(). By the response the request is as it was sent, so the
    credential is there and the verdict is too."""
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        auth=BearerAuth(),
    )
    client.post("http://backend.internal/rows", content=b"{}")

    written = json.loads(serving.read_text())
    assert written["auth_method"] == METHOD_BEARER_JWT
    assert written["identity_id"]
    assert written["auth_status"] == "accepted"


def test_a_call_that_never_arrived_is_recorded_without_a_verdict(serving):
    """The record still lands - the credential was presented - but nothing
    judged it, so there is no verdict to report."""
    import requests

    session = requests.Session()
    try:
        session.get("http://127.0.0.1:9/never", timeout=0.4)
    except Exception:
        pass

    written = json.loads(serving.read_text())
    assert written["auth_status"] == "unknown"
