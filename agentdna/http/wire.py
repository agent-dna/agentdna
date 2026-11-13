"""What travels on the wire, for any receiver, including gateways that can't import Python.

Requests and answers have the same body: only the signed workflow.

    {"agentdna": {"intent_workflow": "<serialized workflow>"}}

In a request the newest step is the signed call; in an answer, the signed answer.
A binary body (a PDF, an image) is signed as base64, with "encoding": "base64".
A call is always sent as POST; the real method is signed inside. A receiver refuses
it unless the signed path (with its query string) is its own (CALL_MISMATCH), then
uses the signed method and body, never the request's own.
"""

from __future__ import annotations

import base64
import json

from agentdna.types import IntentWorkflow, load_workflow


def body_to_text(body: str | bytes) -> tuple[str, str]:
    """A body as text, to sign, and its encoding: "" for text, "base64" for binary."""
    if isinstance(body, str):
        return body, ""
    try:
        return body.decode(), ""
    except UnicodeDecodeError:
        return base64.b64encode(body).decode(), "base64"


def body_from_text(text: str, encoding: str) -> str | bytes:
    """The other way: the text, or the bytes of a binary body."""
    return base64.b64decode(text) if encoding == "base64" else text


def build_http_call_payload(
    method: str, path: str, body: str, content_type: str, encoding: str = ""
) -> str:
    """The call an agent signs: one fixed form, so every receiver reads it the same way."""
    call = {"method": method, "path": path, "content_type": content_type, "body": body}
    if encoding:
        call["encoding"] = encoding
    return json.dumps(call, separators=(",", ":"), sort_keys=True)


def build_http_answer_payload(body: str, content_type: str, status: int, encoding: str = "") -> str:
    """The answer an agent or validation service signs: one fixed form, like a call. The status is signed too."""
    answer = {"body": body, "content_type": content_type, "status": status}
    if encoding:
        answer["encoding"] = encoding
    return json.dumps(answer, separators=(",", ":"), sort_keys=True)


def workflow_to_body(workflow: IntentWorkflow) -> dict:
    """The body of a request or an answer: only the signed workflow."""
    return {"agentdna": {"intent_workflow": workflow.serialize()}}


def workflow_from_body(body: bytes | str) -> tuple[IntentWorkflow, dict] | None:
    """The workflow in a request body, and the call its newest step signed. None if either is missing."""
    try:
        workflow = load_workflow(json.loads(json.loads(body)["agentdna"]["intent_workflow"]))
        call = json.loads(workflow.get_latest_envelope().payload)
        if not {"method", "path", "body"} <= call.keys():  # a signed call has all three
            return None
        return workflow, call
    except Exception:
        return None


def answer_from_body(body: bytes | str) -> tuple[IntentWorkflow, int, str, str | bytes] | None:
    """The workflow in an answer, and the answer its newest step signed (status, content_type, body).
    The body is text, or bytes for a binary answer. None if missing."""
    try:
        workflow = load_workflow(json.loads(json.loads(body)["agentdna"]["intent_workflow"]))
        answer = json.loads(workflow.get_latest_envelope().payload)
        body = body_from_text(answer["body"], answer.get("encoding", ""))
        return workflow, answer["status"], answer["content_type"], body
    except Exception:
        return None
