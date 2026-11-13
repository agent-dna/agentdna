"""Receiving: open a signed request, check it, and respond."""

from __future__ import annotations

import json
from typing import Any

import httpx

from agentdna.error import CALL_MISMATCH, RESULT_OK, TOOL_EXECUTION_FAILED
from agentdna.http.client import AsyncSignedClient, SignedClient, record_failure
from agentdna.http.wire import (
    body_from_text,
    body_to_text,
    build_http_answer_payload,
    workflow_from_body,
    workflow_to_body,
)
from agentdna.types import VERIFY_BOUNDARY


class RequestRefused(Exception):
    """The request's signed workflow is missing, not real, or not for this call."""

    def __init__(self, reason: str, code: int = 0):
        super().__init__(reason)
        self.reason = reason
        self.code = code


class SignedRequest:
    """
    A request that arrived with a signed workflow, opened and checked. It holds
    the workflow, so the code using it never has to.

    body is text, or bytes if the caller sent a binary body.

    url is this request's URL, or its path with the query string. mode is how
    the workflow is verified (the validation service uses VERIFY_HEAVY). Raises
    RequestRefused if there is no signed call, the workflow does not check out,
    or it was signed for another path. A refusal is signed and recorded, but
    only on a workflow that checks out: anyone can send a forged one.
    """

    def __init__(self, agent, body: bytes | str, url: str, mode: str = VERIFY_BOUNDARY):
        opened = workflow_from_body(body)
        if opened is None:
            raise RequestRefused("no signed call in the body")
        self.workflow, call = opened

        code = agent.verify(self.workflow, mode=mode)
        if code != RESULT_OK:
            raise RequestRefused("the signed workflow does not check out", code)  # verify logs it

        path = httpx.URL(url).raw_path.decode()
        if call["path"] != path:
            self._refuse(agent, f"signed for {call['path']}, not {path}", CALL_MISMATCH)

        self.agent = agent
        self.method = call["method"]  # the request always arrives as POST; this is the real one
        self.path = path
        self.content_type = call.get("content_type", "")
        self.body = body_from_text(call["body"], call.get("encoding", ""))
        self.clients = []

    def json(self) -> Any:
        return json.loads(self.body)

    def client(self, server_url: str, **options) -> SignedClient:
        """A client whose calls are signed onto this request's workflow."""
        client = SignedClient(self.agent, self.workflow, server_url, **options)
        self.clients.append(client)
        return client

    def async_client(self, server_url: str, **options) -> AsyncSignedClient:
        """The same as client, for async code."""
        client = AsyncSignedClient(self.agent, self.workflow, server_url, **options)
        self.clients.append(client)
        return client

    def respond(self, data: Any, status: int = 200) -> Any:
        """
        Sign the answer onto the workflow, and return the body to send back.
        Calls made to several places are joined into one workflow here.

        On success the whole answer is signed inside this agent's step, and the
        body is only the signed workflow. On an error (status 400 or more) it is
        recorded instead, and the plain data is returned: the workflow ends here.
        """
        workflows = [
            client.workflow for client in self.clients if client.workflow is not self.workflow
        ]
        encoding = ""
        if isinstance(data, bytes):
            (body, encoding), content_type = body_to_text(data), "application/octet-stream"
        elif isinstance(data, str):
            body, content_type = data, "text/plain"
        else:
            body, content_type = json.dumps(data), "application/json"
        if status >= 400:
            self.workflow = record_failure(
                self.agent, workflows or self.workflow, body, TOOL_EXECUTION_FAILED
            )
            return data
        self.workflow = self.agent.build(
            payload=build_http_answer_payload(body, content_type, status, encoding),
            previous_workflows=workflows or self.workflow,
        )
        return workflow_to_body(self.workflow)

    def _refuse(self, agent, reason: str, code: int):
        record_failure(agent, self.workflow, reason, code)
        raise RequestRefused(reason, code)
