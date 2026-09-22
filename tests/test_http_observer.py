"""Tests for the outbound observation point."""

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass

import httpx
import pytest

from agentdna import httpobserver
from agentdna.authevidence import METHOD_BEARER_JWT
from agentdna.evidence_sink import ENV_FILE
from agentdna.fingerprintkey import ENV_KEY
from agentdna.httpobserver import SOURCE_CLIENT_OUT, SOURCE_SERVER_OUT

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


class FakeDNA:
    def __init__(self, config_dir):
        self.config_dir = config_dir
        self.logger = FakeLogger()
        self.api_key = ""


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

    assert dna.logger.warnings
    assert dna.logger.warnings[0][0] == "agentdna.authevidence.outbound_failed"


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


# --- AgentDNA's own services -----------------------------------------------


class Provenance:
    provenance_url = "https://chain-connector-2-dev.rubix.net"


class WiredDNA(FakeDNA):
    """A FakeDNA that knows where AgentDNA's own services live."""

    agentdna_admin_url = "https://agentdna-admin-dev.agentdna.io"
    provenance = Provenance()


@pytest.fixture
def serving_wired(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "evidence.jsonl"))

    workflow = FakeWorkflow(FakeEnvelope(run_id="run-a41f", signature="sig-incoming"))
    monkeypatch.setattr(httpobserver, "_current_envelope", lambda: workflow.get_latest_envelope())

    with installed(WiredDNA(str(tmp_path))):
        yield tmp_path / "evidence.jsonl"


def _get(host):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        headers={"Authorization": CREDENTIAL},
    )
    return client.get(f"https://{host}/anything")


def test_the_admin_server_is_not_a_hop(serving_wired):
    """A whitelist check is AgentDNA's bookkeeping, not work the agent did."""
    _get("agentdna-admin-dev.agentdna.io")

    assert not serving_wired.exists()


def test_the_provenance_layer_is_not_a_hop(serving_wired):
    _get("chain-connector-2-dev.rubix.net")

    assert not serving_wired.exists()


def test_recording_does_not_record_itself(monkeypatch, tmp_path):
    """Evidence is posted with `requests`, and `requests` is patched. Without
    this, one record posts a record that posts a record, without end."""
    from agentdna.evidence_sink import ENV_URL

    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(tmp_path / "evidence.jsonl"))
    monkeypatch.setenv(ENV_URL, "https://middleware.internal")

    workflow = FakeWorkflow(FakeEnvelope(run_id="run-a41f", signature="sig-incoming"))
    monkeypatch.setattr(httpobserver, "_current_envelope", lambda: workflow.get_latest_envelope())

    with installed(WiredDNA(str(tmp_path))):
        _get("middleware.internal")

    assert not (tmp_path / "evidence.jsonl").exists()


def test_a_real_backend_is_still_recorded(serving_wired):
    """The skip list is three named hosts, not a general silence."""
    _get("analytics.internal")

    written = json.loads(serving_wired.read_text())
    assert written["destination"] == "analytics.internal"
