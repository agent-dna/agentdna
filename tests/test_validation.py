import pytest

from agentdna.error import RESULT_OK


def test_build_with_empty_payload(user, agent):
    """
    Ensures empty payloads can be signed and
    verified successfully.
    """
    workflow = user.build(
        payload="",
    )

    result = agent.verify(workflow)

    assert result == RESULT_OK


def test_invalid_verification_mode_raises(user, agent):
    """
    Ensures unsupported verification modes
    are rejected.
    """
    workflow = user.build(
        payload="Hello",
    )

    agent.verification_mode = "invalid"

    with pytest.raises(ValueError, match="unsupported verification mode"):
        agent.verify(workflow)


def test_handle_with_invalid_signature_returns_invalid(user, agent):
    """
    Ensures malformed signatures are rejected.
    """
    workflow = user.build(
        payload="Hello",
    )

    workflow.envelope.signature = "xyz"

    result = agent.verify(workflow)

    assert result is not RESULT_OK


def test_handle_with_missing_signature_returns_invalid(user, agent):
    """
    Ensures missing signatures are rejected.
    """
    workflow = user.build(
        payload="Hello",
    )

    workflow.envelope.signature = ""

    result = agent.verify(workflow)

    assert result is not RESULT_OK
