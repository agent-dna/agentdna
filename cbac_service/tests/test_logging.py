import io
import json
import logging

import structlog

from cbac_service.logging_config import setup_logging


def capture() -> io.StringIO:
    """Configure logging, then point the single root handler at a buffer."""
    setup_logging("DEBUG")
    stream = io.StringIO()
    logging.getLogger().handlers[0].setStream(stream)  # type: ignore[attr-defined]
    structlog.contextvars.clear_contextvars()
    return stream


def test_every_line_is_json_carrying_the_bound_context():
    stream = capture()
    structlog.contextvars.bind_contextvars(request_id="r1", agent_id="did:agent")

    structlog.get_logger("test").info("cbac decision", decision="deny")
    logging.getLogger("uvicorn.access").info("%s %d", "POST /authorize-cbac", 200)

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [r["event"] for r in records] == ["cbac decision", "POST /authorize-cbac 200"]
    # Bound at the request boundary — present on the foreign uvicorn record too.
    assert all(r["request_id"] == "r1" and r["agent_id"] == "did:agent" for r in records)
    assert records[0]["decision"] == "deny"
    assert records[0]["level"] == "info"


def test_exception_is_rendered_into_the_json():
    stream = capture()
    try:
        raise RuntimeError("policy lookup failed")
    except RuntimeError:
        structlog.get_logger("test").exception("cbac authorization failed")

    record = json.loads(stream.getvalue())
    assert record["level"] == "error"
    assert "policy lookup failed" in record["exception"]
