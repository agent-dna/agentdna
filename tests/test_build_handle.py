import pytest

from agentdna.types import VERIFY_LIGHT, VERIFY_HEAVY, VerificationResult, Issue


def test_simple_build_handle_ideal_flow(user, agent):
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    result = agent.handle(workflow)

    assert result.verification.valid
    assert result.envelope == workflow.envelope
    assert result.workflow == workflow


def test_two_step_build_handle_success(user, agent):
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    result = agent.handle(workflow)
    assert result.verification.valid

    workflow = agent.build(
        recipient_actor_id=user.get_actor_id(),
        recipient_actor_name=user.name,
        recipient_actor_type=user.type,
        payload="Acknowledged",
        workflow=workflow,
        verification_result=result.verification,
    )

    result = user.handle(workflow)

    assert result.verification.valid
    assert workflow.envelope.parent_envelope.payload == "MFA is mandatory"


def test_two_step_build_handle_failure(user, agent):
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    result = agent.handle(workflow)
    assert result.verification.valid

    workflow = agent.build(
        recipient_actor_id=user.get_actor_id(),
        recipient_actor_name=user.name,
        recipient_actor_type=user.type,
        payload="Acknowledged",
        workflow=workflow,
        verification_result=result.verification,
    )

    workflow.envelope.payload = ""

    result = user.handle(workflow)

    assert not result.verification.valid
    assert workflow.envelope.parent_envelope.payload == "MFA is mandatory"


def test_three_step_build_handle_success(user, agent, second_agent):
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    result = agent.handle(workflow)
    assert result.verification.valid

    workflow = agent.build(
        recipient_actor_id=second_agent.get_actor_id(),
        recipient_actor_name=second_agent.name,
        recipient_actor_type=second_agent.type,
        payload="Forwarding request",
        workflow=workflow,
        verification_result=result.verification,
    )

    result = second_agent.handle(workflow)

    assert result.verification.valid


def test_three_step_build_handle_failure(user, agent, second_agent):
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    result = agent.handle(workflow)
    assert result.verification.valid

    workflow = agent.build(
        recipient_actor_id=second_agent.get_actor_id(),
        recipient_actor_name=second_agent.name,
        recipient_actor_type=second_agent.type,
        payload="Forwarding request",
        workflow=workflow,
        verification_result=result.verification,
    )

    workflow.envelope.payload = ""

    result = second_agent.handle(workflow)

    assert not result.verification.valid


def test_handle_without_envelope_raises(agent):
    from agentdna.types import IntentWorkflow, CURRENT_VERSION

    workflow = IntentWorkflow(
        type="intent_workflow",
        version=CURRENT_VERSION,
        remarks="",
    )

    with pytest.raises(ValueError, match="workflow does not contain an envelope"):
        agent.handle(workflow)


def test_invalid_verification_mode_raises(agent, user):
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    agent.verification_mode = "invalid"

    with pytest.raises(ValueError, match="unsupported verification mode"):
        agent.handle(workflow)


def test_light_verification_mode(user, agent):
    agent.verification_mode = VERIFY_LIGHT

    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    result = agent.handle(workflow)

    assert result.verification.valid


def test_heavy_verification_mode(user, agent, second_agent):
    agent.verification_mode = VERIFY_HEAVY
    second_agent.verification_mode = VERIFY_HEAVY

    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    result = agent.handle(workflow)

    assert result.verification.valid

    workflow = agent.build(
        recipient_actor_id=second_agent.get_actor_id(),
        recipient_actor_name=second_agent.name,
        recipient_actor_type=second_agent.type,
        payload="2FA approach",
        workflow=workflow,
    )

    # Tamper the payload of first envelope
    workflow.envelope.parent_envelope.payload = ""

    result = second_agent.handle(workflow)

    assert not result.verification.valid


def test_issues_tamper_in_envelope(user, agent):
    """
    Ensures the signature verification
    fails upon `issues` attribute of
    envelope getting tampered.
    """
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
        verification_result=VerificationResult(
            valid=False,
            chain_depth=1,
            issues=[Issue(depth=1, reason="CoCA error: signature verification failed")],
        ),
    )

    workflow.envelope.issues = None

    result = agent.handle(workflow)

    assert not result.verification.valid


def test_epoch_tamper_in_envelope(user, agent):
    """
    Ensures the signature verification
    fails upon `issues` attribute of
    envelope getting tampered.
    """

    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    workflow.envelope.epoch = 2

    result = agent.handle(workflow)

    assert not result.verification.valid
