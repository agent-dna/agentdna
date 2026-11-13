"""Sending: an httpx client that signs every call, and checks every answer."""

from __future__ import annotations

import asyncio
import threading

import httpx

from agentdna.auth import httpobserver
from agentdna.error import ANSWER_REFUSED, RESULT_OK
from agentdna.http.wire import (
    answer_from_body,
    body_to_text,
    build_http_call_payload,
    workflow_to_body,
)
from agentdna.types import IntentWorkflow


class SignedClient(httpx.Client):
    """
    An httpx client that signs every call onto the workflow. Read client.workflow after.
    options go to httpx.Client, e.g. timeout=60.

    A successful answer must come back signed, built on this call, with real
    signatures; otherwise the call raises AnswerRefused. It comes back as an
    ordinary response holding only the plain answer and its signed status, so
    response.json() works as usual. An error (400 or more) comes back as it is.

    workflow is None for the first hop (a person starting a task). Nothing is
    recorded on success: when the task is done, record it with
    agent.record(client.workflow). A refusal is recorded at once, by whoever refuses.
    """

    def __init__(self, agent, workflow: IntentWorkflow | None, server_url: str, **options):
        self.signer = _Signer(agent, workflow, server_url)
        super().__init__(base_url=server_url, auth=self.signer, **options)

    @property
    def workflow(self) -> IntentWorkflow | None:
        return self.signer.workflow

    def send(self, request: httpx.Request, **options) -> httpx.Response:
        # This client writes its own credential row; the MCP server's hook would write a second
        with httpobserver.not_observed():
            return _plain(super().send(request, **options))


class AsyncSignedClient(httpx.AsyncClient):
    """The same as SignedClient, for async code."""

    def __init__(self, agent, workflow: IntentWorkflow | None, server_url: str, **options):
        self.signer = _Signer(agent, workflow, server_url)
        super().__init__(base_url=server_url, auth=self.signer, **options)

    @property
    def workflow(self) -> IntentWorkflow | None:
        return self.signer.workflow

    async def send(self, request: httpx.Request, **options) -> httpx.Response:
        with httpobserver.not_observed():
            return _plain(await super().send(request, **options))


class AnswerRefused(Exception):
    """
    An answer that isn't signed, isn't for this call, or whose signatures don't
    check out. response: the answer as it came, for code that wants to look anyway.
    """

    def __init__(self, reason: str, code: int, response: httpx.Response):
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.response = response


def record_failure(
    agent, workflow: IntentWorkflow | list[IntentWorkflow], reason: str, code: int
) -> IntentWorkflow:
    """
    Sign a failure onto the workflow and record it: a failed workflow ends here.
    A failed record is logged, not raised: the failure must still reach the caller.
    """
    failed = agent.build(payload=reason, previous_workflows=workflow, verification_code=code)
    try:
        agent.record(failed)
    except Exception as error:
        agent.logger.error("agentdna.http.record_failed", error=str(error))
    return failed


def _plain(response: httpx.Response) -> httpx.Response:
    """A signed answer becomes an ordinary response: only the answer, with its signed status."""
    opened = answer_from_body(response.content)
    if opened is None:
        return response  # an error: untouched
    _, status, content_type, body = opened
    headers = {
        k: v
        for k, v in response.headers.items()
        if k.lower()
        not in ("content-type", "content-length", "content-encoding", "transfer-encoding")
    }
    headers["content-type"] = content_type
    content = body if isinstance(body, bytes) else body.encode()
    return httpx.Response(status, headers=headers, content=content, request=response.request)


class _Signer(httpx.Auth):
    """
    httpx's auth hook. On each call it:

        1. signs the call as written (method, path with query, body) onto the workflow
        2. sends only the signed workflow, always as POST
        3. checks a successful answer, and carries on from its workflow

    It signs only for server_url, so the workflow never goes anywhere else.
    Keep follow_redirects off (the httpx default): a redirect is followed
    after signing and would carry the workflow with it.

    Calls run one at a time, so the workflow stays one line. In async code the
    signing and checking run in a thread, so other tasks keep going.
    """

    def __init__(self, agent, workflow: IntentWorkflow | None, server_url: str):
        self.agent = agent
        self.workflow = workflow
        self.server = httpx.URL(server_url)
        self.lock = threading.Lock()
        self.async_lock = asyncio.Lock()

    def sync_auth_flow(self, request):
        with self.lock:
            request.read()
            response = yield self._sign(request)
            response.read()  # the workflow comes back in the answer's body
            self._receive(response)

    async def async_auth_flow(self, request):
        async with self.async_lock:
            await request.aread()
            response = yield await asyncio.to_thread(self._sign, request)
            await response.aread()
            await asyncio.to_thread(self._receive, response)

    def _sign(self, request: httpx.Request) -> httpx.Request:
        url = request.url
        if (url.scheme, url.host, url.port) != (
            self.server.scheme,
            self.server.host,
            self.server.port,
        ):
            raise ValueError(f"this client signs only for {self.server}, not {url}")

        # 1. Sign the call as written (a binary body as base64)
        body, encoding = body_to_text(request.content)
        payload = build_http_call_payload(
            request.method,
            url.raw_path.decode(),
            body,
            request.headers.get("content-type", ""),
            encoding,
        )
        self.call = self.agent.build(payload=payload, previous_workflows=self.workflow)
        self.workflow = self.call

        # 2. Send only the signed workflow, always as POST
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("content-type", "content-length")
        }
        return httpx.Request("POST", url, json=workflow_to_body(self.workflow), headers=headers)

    def _receive(self, response: httpx.Response):
        # Credential row (only when evidence is on): the key this call carried, and the answer's status
        sent = response.request
        httpobserver.record_call(
            self.agent,
            self.call.get_latest_envelope(),
            httpobserver.SOURCE_CLIENT_OUT,
            sent.headers,
            sent.url.host,
            response.status_code,
        )

        # 3. A successful answer must check out. An error carries no workflow:
        #    whoever produced it has it on record.
        if response.status_code < 400:
            self.workflow = self._checked(response)

    def _checked(self, response: httpx.Response) -> IntentWorkflow:
        """The answer's workflow, if it is signed, built on this call, and real. Otherwise refused."""
        call = self.call.get_latest_envelope()

        opened = answer_from_body(response.content)
        if opened is None:
            self._refuse("the answer isn't signed", ANSWER_REFUSED, response)
        workflow = opened[0]

        if not any(
            (step.hash, step.signature) == (call.hash, call.signature) for step in workflow.unwrap()
        ):
            self._refuse("the answer isn't for this call", ANSWER_REFUSED, response)

        code = self.agent.verify(workflow)
        if code != RESULT_OK:
            self._refuse("the answer's signatures don't check out", code, response)

        return workflow

    def _refuse(self, reason: str, code: int, response: httpx.Response):
        """Record the refusal on this call (the workflow ends here), and stop the caller."""
        record_failure(self.agent, self.call, reason, code)
        raise AnswerRefused(reason, code, response)
