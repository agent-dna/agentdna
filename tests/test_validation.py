import pytest


def test_build_with_invalid_recipient_type_raises(user):
    """
    Ensures build() rejects unsupported
    recipient actor types.
    """
    with pytest.raises(ValueError, match="unsupported actor type"):
        user.build(
            recipient_actor_id="receiver",
            recipient_actor_name="Receiver",
            recipient_actor_type="invalid",
            payload="Hello",
        )


def test_build_with_empty_payload(user, agent):
    """
    Ensures empty payloads can be signed and
    verified successfully.
    """
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="",
    )

    result = agent.handle(workflow)

    assert result.verification.valid


def test_invalid_verification_mode_raises(user, agent):
    """
    Ensures unsupported verification modes
    are rejected.
    """
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="Hello",
    )

    agent.verification_mode = "invalid"

    with pytest.raises(ValueError, match="unsupported verification mode"):
        agent.handle(workflow)


def test_handle_with_invalid_signature_returns_invalid(user, agent):
    """
    Ensures malformed signatures are rejected.
    """
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="Hello",
    )

    workflow.envelope.signature = "xyz"

    result = agent.handle(workflow)

    assert not result.verification.valid


def test_handle_with_missing_signature_returns_invalid(user, agent):
    """
    Ensures missing signatures are rejected.
    """
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="Hello",
    )

    workflow.envelope.signature = ""

    result = agent.handle(workflow)

    assert not result.verification.valid
