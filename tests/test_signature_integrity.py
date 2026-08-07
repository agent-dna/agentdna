from agentdna.error import RESULT_OK
from agentdna.types import VERIFY_BOUNDARY, VERIFY_HEAVY


def test_payload_tampering(user, agent):
    """
    Ensures payload tampering invalidates
    the envelope signature.
    """
    workflow = user.build(
        payload="MFA is mandatory",
    )

    workflow.envelope.payload = "MFA is optional"

    result = agent.verify(workflow)

    assert not result is RESULT_OK


def test_sender_from_tampering(user, agent):
    """
    Ensures sender identifier tampering
    invalidates the envelope signature.
    """
    workflow = user.build(
        payload="MFA is mandatory",
    )

    workflow.envelope.from_ = "bafkreigbhfysrr5ruxxmjtewctujv3gzn4mcl445jhlklaxh4pbfvjqunq"

    result = agent.verify(workflow)

    assert not result is RESULT_OK


def test_recipient_to_tampering(user, agent):
    """
    Ensures recipient identifier tampering
    invalidates the envelope signature.
    """
    workflow = user.build(
        payload="MFA is mandatory",
    )

    workflow.envelope.to = "bafkreidv7y3s5vhlitj6m625u6nmgw2lyy645q3v2qffn46j5qly47j4mq"

    result = agent.verify(workflow)

    assert not result is RESULT_OK


def test_signature_tampering(user, agent):
    """
    Ensures signature tampering invalidates
    the envelope.
    """
    workflow = user.build(
        payload="MFA is mandatory",
    )

    workflow.envelope.signature = "deadbeef"

    result = agent.verify(workflow)

    assert not result is RESULT_OK


def test_missing_signature(user, agent):
    """
    Ensures missing signatures are rejected.
    """
    workflow = user.build(
        payload="MFA is mandatory",
    )

    workflow.envelope.signature = ""

    result = agent.verify(workflow)

    assert not result is RESULT_OK


def test_parent_signature_tampering(user, agent):
    """
    Ensures parent signature tampering
    invalidates the workflow.
    """
    workflow = user.build(
        payload="MFA is mandatory",
    )

    result = agent.verify(workflow)
    assert result is RESULT_OK

    workflow = agent.build(
        payload="Acknowledged",
        previous_workflows=workflow,
        verification_code=result,
    )

    workflow.envelope.parent_envelope[0].signature = "Vulture"

    for verify_method in [VERIFY_BOUNDARY, VERIFY_HEAVY]:
        user.verification_mode = verify_method
        result = user.verify(workflow)

        assert result is not RESULT_OK, (
            f"failed to detect signature tampering for method: {verify_method}"
        )


def test_parent_payload_tampering(user, agent):
    """
    Ensures parent payload tampering
    invalidates the workflow.
    """
    workflow = user.build(
        payload="MFA is mandatory",
    )

    result = agent.verify(workflow)
    assert result is RESULT_OK

    workflow = agent.build(
        payload="Acknowledged",
        previous_workflows=workflow,
        verification_code=result,
    )

    workflow.envelope.parent_envelope[0].payload = "Tampered"

    result = user.verify(workflow)

    assert not result is RESULT_OK
