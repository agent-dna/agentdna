"""Wait until the AgentDNA MCP Gateway at AGENTDNA_GATEWAY_URL answers its health check."""

from __future__ import annotations

import os
import sys
import time
import urllib.request

from dotenv import load_dotenv

load_dotenv(".env")

URL = (os.getenv("AGENTDNA_GATEWAY_URL") or "http://127.0.0.1:8090").rstrip("/") + "/healthz"
TIMEOUT_SECONDS = 60

deadline = time.monotonic() + TIMEOUT_SECONDS
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(URL, timeout=2) as response:
            if response.status == 200:
                sys.exit(0)
    except OSError:
        pass
    time.sleep(0.5)

sys.exit(f"AgentDNA MCP Gateway did not answer {URL} within {TIMEOUT_SECONDS}s")
