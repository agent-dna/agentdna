def test_payload_tampering(user, agent):
    """
    Ensures payload tampering invalidates
    the envelope signature.
    """
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    workflow.envelope.payload = "MFA is optional"

    result = agent.handle(workflow)

    assert not result.verification.valid


def test_metadata_tampering(user, agent):
    """
    Ensures metadata tampering invalidates
    the envelope signature.
    """
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    workflow.envelope.metadata["version"] = "2"

    result = agent.handle(workflow)

    assert not result.verification.valid


def test_sender_id_tampering(user, agent):
    """
    Ensures sender identifier tampering
    invalidates the envelope signature.
    """
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    workflow.envelope.from_.id = "bafkreigbhfysrr5ruxxmjtewctujv3gzn4mcl445jhlklaxh4pbfvjqunq"

    result = agent.handle(workflow)

    assert not result.verification.valid


def test_sender_name_tampering(user, agent):
    """
    Ensures sender name tampering invalidates
    the envelope signature.
    """
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    workflow.envelope.from_.name = "Mallory"

    result = agent.handle(workflow)

    assert not result.verification.valid


def test_sender_type_tampering(user, agent):
    """
    Ensures sender type tampering invalidates
    the envelope signature.
    """
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    workflow.envelope.from_.type = "agent"

    result = agent.handle(workflow)

    assert not result.verification.valid


def test_recipient_id_tampering(user, agent):
    """
    Ensures recipient identifier tampering
    invalidates the envelope signature.
    """
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    workflow.envelope.to.id = "bafkreidv7y3s5vhlitj6m625u6nmgw2lyy645q3v2qffn46j5qly47j4mq"

    result = agent.handle(workflow)

    assert not result.verification.valid


def test_recipient_name_tampering(user, agent):
    """
    Ensures recipient name tampering invalidates
    the envelope signature.
    """
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    workflow.envelope.to.name = "Mallory"

    result = agent.handle(workflow)

    assert not result.verification.valid


def test_recipient_type_tampering(user, agent):
    """
    Ensures recipient type tampering invalidates
    the envelope signature.
    """
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    workflow.envelope.to.type = "human"

    result = agent.handle(workflow)

    assert not result.verification.valid


def test_signature_tampering(user, agent):
    """
    Ensures signature tampering invalidates
    the envelope.
    """
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    workflow.envelope.signature = "deadbeef"

    result = agent.handle(workflow)

    assert not result.verification.valid


def test_missing_signature(user, agent):
    """
    Ensures missing signatures are rejected.
    """
    workflow = user.build(
        recipient_actor_id=agent.get_actor_id(),
        recipient_actor_name=agent.name,
        recipient_actor_type=agent.type,
        payload="MFA is mandatory",
    )

    workflow.envelope.signature = ""

    result = agent.handle(workflow)

    assert not result.verification.valid


def test_parent_signature_tampering(user, agent):
    """
    Ensures parent signature tampering
    invalidates the workflow.
    """
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

    workflow.envelope.parent_envelope.signature = "deadbeef"

    result = user.handle(workflow)

    assert not result.verification.valid


def test_parent_payload_tampering(user, agent):
    """
    Ensures parent payload tampering
    invalidates the workflow.
    """
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

    workflow.envelope.parent_envelope.payload = "Tampered"

    result = user.handle(workflow)

    assert not result.verification.valid
