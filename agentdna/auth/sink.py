"""Where evidence records go.

A file, the middleware, or both. Nothing is collected unless one is set.
"""

from __future__ import annotations

import json
import os

import requests

ENV_FILE = "AGENTDNA_AUTH_EVIDENCE_FILE"
ENV_URL = "AGENTDNA_AUTH_EVIDENCE_URL"

PATH = "/core/v1/auth-evidence"


def enabled() -> bool:
    return bool(os.environ.get(ENV_FILE, "") or os.environ.get(ENV_URL, ""))


def send(evidence, api_key: str) -> None:
    """Write one record. Raises on failure - callers decide what that means."""
    record = evidence.as_dict()

    path = os.environ.get(ENV_FILE, "")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    url = os.environ.get(ENV_URL, "")
    if url:
        requests.post(
            url.rstrip("/") + PATH,
            json={"records": [record]},
            headers={"X-API-Key": api_key},
            timeout=5,
        )
