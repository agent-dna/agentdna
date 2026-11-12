import asyncio
import json
import threading
import time

import httpx
import pytest

from agentdna.auth import httpobserver
from agentdna.auth.httpobserver import SOURCE_CLIENT_OUT
from agentdna.auth.key import ENV_KEY
from agentdna.auth.sink import ENV_FILE
from agentdna.error import (
    ANSWER_REFUSED,
    CALL_MISMATCH,
    GRADE_VERIFICATION_FAILED_BOUNDARY,
    TOOL_EXECUTION_FAILED,
)
from agentdna.http import (
    AnswerRefused,
    AsyncSignedClient,
    RequestRefused,
    SignedClient,
    SignedRequest,
    answer_from_body,
    build_http_answer_payload,
    build_http_call_payload,
    workflow_from_body,
    workflow_to_body,
)
from agentdna.mcp.context import agentdna_context

AGENT_URL = "http://agent.test"
GATEWAY_URL = "http://gateway.test"
NOTES_URL = "http://notes.test"


def fake_gateway(gateway):
    """Opens each call, checks its path, signs "allowed" and then the app's answer. Keeps what it saw."""
    seen = []

    def handle(request):
        workflow, call = workflow_from_body(request.content)
        seen.append((request.method, call))
        assert call["path"] == request.url.raw_path.decode()
        allowed = gateway.build(payload='{"allowed": true}', previous_workflows=workflow)
        answer = gateway.build(
            payload=build_http_answer_payload('{"number": 12}', "application/json", 201),
            previous_workflows=allowed,
        )
        return httpx.Response(
            200, json=workflow_to_body(answer), headers={"X-RateLimit-Remaining": "99"}
        )

    return seen, httpx.MockTransport(handle)


def answering_agent(answerer, data=None, status=200):
    """A receiver that signs its answer onto the call it gets, as respond() does."""

    def handle(request):
        workflow, _ = workflow_from_body(request.content)
        payload = build_http_answer_payload(
            json.dumps(data or {"result": "done"}), "application/json", status
        )
        return httpx.Response(
            200, json=workflow_to_body(answerer.build(payload=payload, previous_workflows=workflow))
        )

    return httpx.MockTransport(handle)


def signers_in_line(workflow):
    """Who signed each step, newest first. Fails if the workflow ever branches."""
    signers = []
    step = workflow.get_latest_envelope()
    while step:
        signers.append(step.from_)
        assert len(step.parent_envelope or []) <= 1
        step = step.parent_envelope[0] if step.parent_envelope else None
    return signers


def signed_body(user, path, data):
    payload = build_http_call_payload("POST", path, json.dumps(data), "application/json")
    return json.dumps(workflow_to_body(user.build(payload=payload)))


def test_person_to_agent_to_gateway(user, agent, second_agent, monkeypatch):
    _, gateway = fake_gateway(second_agent)
    person_records = []
    monkeypatch.setattr(user, "record", person_records.append)

    def agent_endpoint(request):
        signed_request = SignedRequest(agent, request.content, str(request.url))
        assert signed_request.json() == {"title": "Login bug"}
        response = signed_request.client(GATEWAY_URL, transport=gateway).post(
            "/github/issues", json={}
        )
        return httpx.Response(
            200,
            json=signed_request.respond({"result": f"Opened issue #{response.json()['number']}"}),
        )

    client = SignedClient(user, None, AGENT_URL, transport=httpx.MockTransport(agent_endpoint))
    response = client.post("/issues", json={"title": "Login bug"})

    assert response.json() == {"result": "Opened issue #12"}
    # newest first: agent's answer, gateway's signed app answer, gateway's "allowed", agent's call, person
    assert signers_in_line(client.workflow) == [
        agent.get_actor_id(),
        second_agent.get_actor_id(),
        second_agent.get_actor_id(),
        agent.get_actor_id(),
        user.get_actor_id(),
    ]
    # nothing recorded on success: the developer records the finished task
    assert person_records == []


def test_the_first_hop_records_nothing_itself(user, monkeypatch):
    person_records = []
    monkeypatch.setattr(user, "record", person_records.append)

    refusing = httpx.MockTransport(lambda request: httpx.Response(403, json={"error": "no"}))
    assert (
        SignedClient(user, None, AGENT_URL, transport=refusing).post("/issues", json={}).status_code
        == 403
    )

    def no_answer(request):
        raise httpx.ReadTimeout("the agent took too long")

    with pytest.raises(httpx.ReadTimeout):
        SignedClient(user, None, AGENT_URL, transport=httpx.MockTransport(no_answer)).post(
            "/issues", json={}
        )

    assert person_records == []  # its own code gets the error, and records the task if it wants


def test_async_signing_and_checking_run_off_the_event_loop(user, agent, second_agent, monkeypatch):
    threads = []
    build, verify = agent.build, agent.verify
    monkeypatch.setattr(
        agent, "build", lambda **kw: threads.append(threading.current_thread()) or build(**kw)
    )
    monkeypatch.setattr(
        agent, "verify", lambda *a: threads.append(threading.current_thread()) or verify(*a)
    )
    _, gateway = fake_gateway(second_agent)

    async def run():
        async with AsyncSignedClient(
            agent, user.build(payload="x"), GATEWAY_URL, transport=gateway
        ) as client:
            await client.get("/a")

    asyncio.run(run())
    assert len(threads) == 2 and threading.main_thread() not in threads


def test_later_hops_do_not_record(user, agent, second_agent, monkeypatch):
    agent_records = []
    monkeypatch.setattr(agent, "record", agent_records.append)
    _, gateway = fake_gateway(second_agent)

    SignedClient(agent, user.build(payload="x"), GATEWAY_URL, transport=gateway).get("/a")

    assert agent_records == []


def test_every_call_is_sent_as_post_with_the_real_call_signed(user, agent, second_agent):
    seen, gateway = fake_gateway(second_agent)
    client = SignedClient(agent, user.build(payload="x"), GATEWAY_URL, transport=gateway)

    client.get("/notes?day=mon")

    assert seen == [
        ("POST", {"method": "GET", "path": "/notes?day=mon", "content_type": "", "body": ""})
    ]


def test_signs_only_for_its_server(user, agent, second_agent):
    seen, gateway = fake_gateway(second_agent)
    task = user.build(payload="x")
    client = SignedClient(agent, task, GATEWAY_URL, transport=gateway)

    with pytest.raises(ValueError):
        client.get("http://elsewhere.test/a")

    assert seen == []
    assert client.workflow is task


def test_parallel_calls_keep_one_line(user, agent, second_agent):
    _, instant = fake_gateway(second_agent)

    async def slow(request):
        await asyncio.sleep(0.05)  # let the other calls start meanwhile
        return instant.handler(request)

    async def run():
        async with AsyncSignedClient(
            agent, user.build(payload="x"), GATEWAY_URL, transport=httpx.MockTransport(slow)
        ) as client:
            await asyncio.gather(client.get("/a"), client.get("/b"), client.get("/c"))
            return client.workflow

    gateway, caller = second_agent.get_actor_id(), agent.get_actor_id()
    assert signers_in_line(asyncio.run(run())) == [gateway, gateway, caller] * 3 + [
        user.get_actor_id()
    ]


def test_calls_to_two_places_are_joined(user, agent, second_agent, worker_1_agent):
    _, gateway = fake_gateway(second_agent)
    _, notes = fake_gateway(worker_1_agent)
    signed_request = SignedRequest(agent, signed_body(user, "/task", {}), "/task")

    signed_request.client(GATEWAY_URL, transport=gateway).post("/github/issues", json={})
    signed_request.client(NOTES_URL, transport=notes).post("/notes", json={})
    signed_request.respond({"result": "done"})

    parents = signed_request.workflow.get_latest_envelope().parent_envelope
    assert {parent.from_ for parent in parents} == {
        second_agent.get_actor_id(),
        worker_1_agent.get_actor_id(),
    }


@pytest.fixture
def agent_records(agent, monkeypatch):
    """What the agent records, kept here instead of sent to the provenance layer."""
    steps = []
    monkeypatch.setattr(agent, "record", steps.append)
    return steps


def test_refuses_a_body_with_no_signed_call_without_recording(user, agent, agent_records):
    with pytest.raises(RequestRefused):
        SignedRequest(agent, b'{"title": "x"}', "/task")
    with pytest.raises(RequestRefused):
        SignedRequest(
            agent, json.dumps(workflow_to_body(user.build(payload="plain text"))), "/task"
        )

    assert agent_records == []  # no workflow to sign onto


def test_refuses_a_changed_workflow_without_recording(user, agent, agent_records):
    body = signed_body(user, "/task", {"title": "Open an issue"}).replace(
        "Open an issue", "Delete the repo"
    )

    with pytest.raises(RequestRefused) as refused:
        SignedRequest(agent, body, "/task")

    assert refused.value.code == GRADE_VERIFICATION_FAILED_BOUNDARY
    assert (
        agent_records == []
    )  # anyone can send a forged workflow: it must not make us write records


def test_a_failed_record_still_refuses_properly(user, agent, monkeypatch):
    def failing_record(workflow):
        raise RuntimeError("parent NFT not found in wallet")

    monkeypatch.setattr(agent, "record", failing_record)
    with pytest.raises(RequestRefused) as refused:
        SignedRequest(agent, signed_body(user, "/task", {}), "/admin")

    assert refused.value.code == CALL_MISMATCH


def test_refuses_and_records_a_workflow_signed_for_another_path(user, agent, agent_records):
    with pytest.raises(RequestRefused) as refused:
        SignedRequest(agent, signed_body(user, "/task", {}), "http://agent.test/admin")

    assert refused.value.code == CALL_MISMATCH
    assert agent_records[0].get_latest_envelope().from_ == agent.get_actor_id()


def test_an_error_response_is_recorded_not_sent_back(user, agent, agent_records):
    signed_request = SignedRequest(agent, signed_body(user, "/task", {}), "/task")

    body = signed_request.respond({"error": "GitHub is down"}, status=500)

    assert body == {"error": "GitHub is down"}  # the plain error, no workflow
    step = agent_records[0].get_latest_envelope()
    assert (step.from_, step.status_code) == (agent.get_actor_id(), TOOL_EXECUTION_FAILED)


def test_a_success_response_is_only_the_signed_workflow(user, agent, agent_records):
    signed_request = SignedRequest(agent, signed_body(user, "/task", {}), "/task")

    body = signed_request.respond({"result": "done"})

    assert list(body) == ["agentdna"]  # nothing else: the answer is inside the signed step
    workflow, status, content_type, answer = answer_from_body(json.dumps(body))
    assert workflow.get_latest_envelope().from_ == agent.get_actor_id()
    assert (status, content_type, json.loads(answer)) == (
        200,
        "application/json",
        {"result": "done"},
    )
    assert agent_records == []


PDF = b"%PDF-1.7\n\xff\xfe\x00\x01 not text"


def test_a_binary_body_and_answer_travel_signed(user, agent, monkeypatch):
    monkeypatch.setattr(user, "record", lambda workflow: None)

    def agent_endpoint(request):
        signed_request = SignedRequest(agent, request.content, str(request.url))
        assert signed_request.body == PDF  # the bytes, as sent
        return httpx.Response(200, json=signed_request.respond(PDF[::-1]))

    client = SignedClient(user, None, AGENT_URL, transport=httpx.MockTransport(agent_endpoint))
    response = client.post("/upload", content=PDF, headers={"content-type": "application/pdf"})

    assert response.content == PDF[::-1]
    assert response.headers["content-type"] == "application/octet-stream"
    call = json.loads(client.workflow.get_latest_envelope().parent_envelope[0].payload)
    assert (call["content_type"], call["encoding"]) == ("application/pdf", "base64")


def test_a_text_body_is_signed_as_before(user, agent):
    client = SignedClient(user, None, AGENT_URL, transport=answering_agent(agent))
    client.post("/notes", content="plain notes")

    call = json.loads(client.workflow.get_latest_envelope().parent_envelope[0].payload)
    assert call["body"] == "plain notes" and "encoding" not in call


# Answers


def test_an_agent_answer_opens_to_its_signed_answer(user, agent):
    payload = build_http_answer_payload('{"result": "Opened issue #12"}', "application/json", 201)
    workflow = agent.build(payload=payload, previous_workflows=user.build(payload="x"))

    body = json.dumps(workflow_to_body(workflow))  # only {"agentdna": ...}
    opened, status, content_type, answer = answer_from_body(body)

    assert opened.serialize() == workflow.serialize()
    assert (status, content_type, answer) == (
        201,
        "application/json",
        '{"result": "Opened issue #12"}',
    )


def test_an_answer_without_a_workflow_opens_to_nothing(user):
    assert answer_from_body(b'{"error": "no", "code": 1001}') is None
    assert answer_from_body(b"not json") is None
    # a workflow whose newest step is not an answer
    assert answer_from_body(json.dumps(workflow_to_body(user.build(payload="plain text")))) is None


def test_the_developer_gets_only_the_plain_answer(user, agent, second_agent):
    _, gateway = fake_gateway(second_agent)
    client = SignedClient(agent, user.build(payload="x"), GATEWAY_URL, transport=gateway)

    response = client.post("/github/issues", json={"title": "x"})

    assert response.json() == {"number": 12}  # not the workflow
    assert response.status_code == 201  # the signed status, not the outer 200
    assert response.headers["content-type"] == "application/json"  # the app's own content type
    assert response.headers["x-ratelimit-remaining"] == "99"  # the other headers kept
    assert client.workflow.get_latest_envelope().from_ == second_agent.get_actor_id()


def test_a_plain_error_passes_through_untouched(user, agent):
    refusing = httpx.MockTransport(
        lambda request: httpx.Response(403, json={"error": "no", "code": 1001})
    )
    task = user.build(payload="x")
    client = SignedClient(agent, task, GATEWAY_URL, transport=refusing)

    response = client.post("/github/issues", json={"title": "x"})

    assert (response.status_code, response.json()) == (403, {"error": "no", "code": 1001})
    assert (
        client.workflow.get_latest_envelope().from_ == agent.get_actor_id()
    )  # only its own call, nothing added


# Credential rows


@pytest.fixture
def evidence_rows(monkeypatch, tmp_path):
    """Credential rows go to a local file. Whatever recording patches is put back afterwards."""
    from requests.adapters import HTTPAdapter

    rows = tmp_path / "evidence.jsonl"
    monkeypatch.setenv(ENV_KEY, "one-shared-value")
    monkeypatch.setenv(ENV_FILE, str(rows))
    monkeypatch.setattr(httpobserver, "_installed", False)
    saved = (httpx.Client.__init__, httpx.AsyncClient.__init__, HTTPAdapter.send)
    yield (
        lambda: (
            [json.loads(line) for line in rows.read_text().splitlines()] if rows.exists() else []
        )
    )
    httpx.Client.__init__, httpx.AsyncClient.__init__, HTTPAdapter.send = saved


def test_a_signed_call_writes_a_client_out_row_with_no_extra_code(
    user, agent, second_agent, evidence_rows
):
    _, gateway = fake_gateway(second_agent)
    client = SignedClient(agent, user.build(payload="x"), GATEWAY_URL, transport=gateway)

    client.post("/github/issues", json={"title": "x"})

    agent_call = (
        client.workflow.get_latest_envelope().parent_envelope[0].parent_envelope[0]
    )  # before the gate's two steps
    [row] = evidence_rows()
    assert (row["source"], row["request_id"]) == (SOURCE_CLIENT_OUT, agent_call.signature)
    assert row["auth_method"] == "none"  # the agent sent no key


def test_one_row_per_call_inside_an_mcp_server(user, agent, second_agent, evidence_rows):
    # An MCP server hooks every HTTP client (server_out); a tool calling through the
    # gateway with SignedClient must still give one row, not two
    httpobserver.install(agent)
    task = user.build(payload="x")
    _, gateway = fake_gateway(second_agent)

    with agentdna_context(agent, task):  # serving a tool call
        SignedClient(agent, task, GATEWAY_URL, transport=gateway).post("/github/issues", json={})

    assert [row["source"] for row in evidence_rows()] == [SOURCE_CLIENT_OUT]


def test_rows_still_waiting_are_sent_before_the_program_exits(monkeypatch):
    from agentdna.auth import sink

    posted = []

    class Posted:
        def raise_for_status(self):
            pass

    def slow_post(url, json, headers, timeout):
        time.sleep(0.2)  # the middleware takes a moment
        posted.append(json["records"][0]["request_id"])
        return Posted()

    monkeypatch.setattr(sink.requests, "post", slow_post)
    sink._start_sender()
    sink._waiting.put(("http://middleware.test", {"request_id": "the-last-call"}, "key"))

    sink._drain()  # what runs at exit

    assert posted == ["the-last-call"]


# Checking answers


def test_an_unsigned_success_is_refused_and_recorded(user, agent, agent_records):
    unsigned = httpx.MockTransport(lambda request: httpx.Response(200, json={"result": "done"}))
    client = SignedClient(agent, user.build(payload="x"), GATEWAY_URL, transport=unsigned)

    with pytest.raises(AnswerRefused) as refused:
        client.post("/github/issues", json={})

    assert refused.value.code == ANSWER_REFUSED
    assert refused.value.response.json() == {
        "result": "done"
    }  # still there, for code that wants to look
    assert agent_records[0].get_latest_envelope().status_code == ANSWER_REFUSED


def test_an_answer_for_another_call_is_refused(user, agent, second_agent, agent_records):
    other_call = agent.build(
        payload=build_http_call_payload("POST", "/other", "{}", "application/json"),
        previous_workflows=user.build(payload="y"),
    )
    replayed = second_agent.build(
        payload=build_http_answer_payload("{}", "application/json", 200),
        previous_workflows=other_call,
    )
    replaying = httpx.MockTransport(
        lambda request: httpx.Response(200, json=workflow_to_body(replayed))
    )
    client = SignedClient(agent, user.build(payload="x"), GATEWAY_URL, transport=replaying)

    with pytest.raises(AnswerRefused, match="isn't for this call"):
        client.post("/github/issues", json={})
    assert len(agent_records) == 1


def test_a_changed_answer_is_refused(user, agent, second_agent, agent_records):
    honest = answering_agent(second_agent, data={"result": "done"})

    def tampering(request):
        body = honest.handler(request).content.decode().replace("done", "hacked")
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    client = SignedClient(
        agent, user.build(payload="x"), GATEWAY_URL, transport=httpx.MockTransport(tampering)
    )

    with pytest.raises(AnswerRefused, match="signatures don't check out"):
        client.post("/github/issues", json={})
    assert len(agent_records) == 1
