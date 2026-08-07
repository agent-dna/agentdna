import pytest

from agentdna.types import VERIFY_LIGHT, VERIFY_HEAVY
from agentdna.error import RESULT_OK


def test_simple_build_verify_ideal_flow(user, agent):
    workflow = user.build(
        payload="MFA is mandatory",
    )

    result = agent.verify(workflow)

    assert result == RESULT_OK


def test_orchestrator_pattern_build_verify_success(
    user, orchestrator_agent, worker_1_agent, worker_2_agent
):
    user_workflow = user.build(
        payload="Perform some stuff",
    )

    result = orchestrator_agent.verify(user_workflow)
    assert result == RESULT_OK

    workflow_for_w1 = orchestrator_agent.build(
        payload="Acknowledged, deletgating to worker1",
        previous_workflows=user_workflow,
        verification_code=result,
    )

    workflow_for_w2 = orchestrator_agent.build(
        payload="Acknowledged, deletgating to worker2",
        previous_workflows=user_workflow,
        verification_code=result,
    )

    #### Worker 1 verification and build #####
    worker_1_result = worker_1_agent.verify(workflow_for_w1)
    assert worker_1_result == RESULT_OK

    workflow_by_worker1 = worker_1_agent.build(
        payload="Worker1 completed the task",
        previous_workflows=workflow_for_w1,
        verification_code=worker_1_result,
    )
    ##########################################

    #### Worker 2 verification and build #####
    worker_2_result = worker_2_agent.verify(workflow_for_w2)
    assert worker_2_result == RESULT_OK

    workflow_by_worker2 = worker_2_agent.build(
        payload="Worker2 completed the task",
        previous_workflows=workflow_for_w2,
        verification_code=worker_2_result,
    )
    ##########################################

    #### Orchestrator final verification #####

    orchestrator_result_worker1 = orchestrator_agent.verify(
        workflow_by_worker1,
    )
    assert orchestrator_result_worker1 == RESULT_OK

    orchestrator_result_worker2 = orchestrator_agent.verify(
        workflow_by_worker2,
    )
    assert orchestrator_result_worker2 == RESULT_OK

    orchestrator_final_workflow = orchestrator_agent.build(
        payload="Orchestrator completed the task",
        previous_workflows=[workflow_by_worker1, workflow_by_worker2],
    )

    ##########################################

    result = user.verify(orchestrator_final_workflow)

    assert result == RESULT_OK
    assert orchestrator_final_workflow.get_root_envelope().payload == "Perform some stuff"
    assert len(orchestrator_final_workflow.envelope.parent_envelope) == 2


def test_two_step_build_verify_failure(user, agent):
    workflow = user.build(
        payload="MFA is mandatory",
    )

    result = agent.verify(workflow)
    assert result == RESULT_OK

    workflow = agent.build(
        payload="Acknowledged",
        previous_workflows=workflow,
        verification_code=result,
    )

    workflow.envelope.payload = ""

    result = user.verify(workflow)

    assert not result == RESULT_OK
    assert workflow.envelope.parent_envelope[0].payload == "MFA is mandatory"


def test_two_step_build_verify_success(user, agent):
    workflow = user.build(
        payload="MFA is mandatory",
    )

    result = agent.verify(workflow)
    assert result == RESULT_OK

    workflow = agent.build(
        payload="Acknowledged",
        previous_workflows=workflow,
        verification_code=result,
    )

    result = user.verify(workflow)

    assert result == RESULT_OK
    assert workflow.envelope.parent_envelope[0].payload == "MFA is mandatory"


def test_three_step_build_verify_success(user, agent, second_agent):
    workflow = user.build(
        payload="MFA is mandatory",
    )

    result = agent.verify(workflow)
    assert result == RESULT_OK

    workflow = agent.build(
        payload="Forwarding request",
        previous_workflows=workflow,
        verification_code=result,
    )

    result = second_agent.verify(workflow)

    assert result == RESULT_OK


def test_three_step_build_verify_failure(user, agent, second_agent):
    workflow = user.build(
        payload="MFA is mandatory",
    )

    result = agent.verify(workflow)
    assert result == RESULT_OK

    workflow = agent.build(
        payload="Forwarding request",
        previous_workflows=workflow,
        verification_code=result,
    )

    workflow.envelope.payload = ""

    result = second_agent.verify(workflow)

    assert not result == RESULT_OK


def test_verify_without_envelope_raises(agent):
    from agentdna.types import IntentWorkflow, CURRENT_VERSION

    workflow = IntentWorkflow(
        type="intent_workflow",
        version=CURRENT_VERSION,
    )

    with pytest.raises(ValueError, match="workflow does not contain an envelope"):
        agent.verify(workflow)


def test_invalid_verification_mode_raises(agent, user):
    workflow = user.build(
        payload="MFA is mandatory",
    )

    agent.verification_mode = "invalid"

    with pytest.raises(ValueError, match="unsupported verification mode"):
        agent.verify(workflow)


def test_light_verification_mode(user, agent):
    agent.verification_mode = VERIFY_LIGHT

    workflow = user.build(
        payload="MFA is mandatory",
    )

    result = agent.verify(workflow)

    assert result == RESULT_OK


def test_heavy_verification_mode(user, agent, second_agent):
    agent.verification_mode = VERIFY_HEAVY
    second_agent.verification_mode = VERIFY_HEAVY

    workflow = user.build(
        payload="MFA is mandatory",
    )

    result = agent.verify(workflow)

    assert result == RESULT_OK

    workflow = agent.build(
        payload="2FA approach",
        previous_workflows=workflow,
    )

    # Tamper the payload of first envelope
    workflow.envelope.parent_envelope[0].payload = ""

    result = second_agent.verify(workflow)

    assert not result == RESULT_OK


def test_epoch_tamper_in_envelope(user, agent):
    """
    Ensures the signature verification
    fails upon `issues` attribute of
    envelope getting tampered.
    """

    workflow = user.build(
        payload="MFA is mandatory",
    )

    workflow.envelope.epoch = 2

    result = agent.verify(workflow)

    assert not result == RESULT_OK
