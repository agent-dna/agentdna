"""Where evidence records go.

A file, the middleware, or both. Nothing is collected unless one is turned on:
AGENTDNA_AUTH_EVIDENCE for the middleware, AGENTDNA_AUTH_EVIDENCE_FILE for a file.

The middleware is the one this AgentDNA instance already talks to, its
`provenance_layer_url`, so there is no second URL to set.

The file is written straight away - it is a local append. The middleware is
posted to from one background thread, so a slow or missing middleware never
holds up the call being observed.
"""

from __future__ import annotations

import json
import os
import queue
import threading

import requests

from agentdna.log import get_logger

ENV_FILE = "AGENTDNA_AUTH_EVIDENCE_FILE"
ENV_ON = "AGENTDNA_AUTH_EVIDENCE"

PATH = "/core/v1/auth-evidence"

# Records waiting to be posted. Bounded, so a middleware that is down cannot
# grow memory without end: once full, new records are dropped, not waited on.
_waiting: queue.Queue = queue.Queue(maxsize=1000)
_sender: threading.Thread | None = None
_sender_lock = threading.Lock()

logger = get_logger("agentdna.auth.sink")


def enabled() -> bool:
    return bool(os.environ.get(ENV_FILE, "")) or _posting()


def send(evidence, dna) -> None:
    """Write one record. Raises on failure - callers decide what that means.

    Only the file write can raise here. The post happens later, on the sender
    thread, and its failures are logged there.
    """
    record = evidence.as_dict()

    path = os.environ.get(ENV_FILE, "")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    if _posting():
        _start_sender()
        url = dna.provenance.provenance_url.rstrip("/") + PATH
        try:
            _waiting.put_nowait((url, record, dna.api_key))
        except queue.Full:
            logger.warning("agentdna.authevidence.queue_full", dropped=record["request_id"])


def is_sender_thread() -> bool:
    """True inside the thread that posts evidence.

    Every HTTP call on that thread is AgentDNA posting evidence, so the
    observer skips them - otherwise recording a post would post a record.
    """
    return threading.current_thread() is _sender


def _posting() -> bool:
    return os.environ.get(ENV_ON, "").strip().lower() in ("1", "true", "on", "yes")


def _start_sender() -> None:
    global _sender
    with _sender_lock:
        if _sender is None:
            _sender = threading.Thread(target=_post_forever, name="agentdna-evidence", daemon=True)
            _sender.start()


def _post_forever() -> None:
    """Post records one at a time, for the life of the process.

    A daemon thread, so records still waiting when the process exits are lost.
    """
    while True:
        url, record, api_key = _waiting.get()
        try:
            response = requests.post(
                url,
                json={"records": [record]},
                headers={"X-API-Key": api_key},
                timeout=5,
            )
            # requests does not raise on an error status. Without this, a
            # middleware with no such endpoint drops every record silently.
            response.raise_for_status()
        except Exception as exc:
            logger.warning("agentdna.authevidence.post_failed", error=str(exc))
        finally:
            _waiting.task_done()
