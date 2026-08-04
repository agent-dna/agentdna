from .provenance import Provenance
from .types import IntentWorkflow


def verify_light(
    provenance: Provenance,
    workflow: IntentWorkflow,
) -> bool:
    """
    Verifies only the latest envelope (the tip).
    """
    logger = provenance.logger
    envelope = workflow.get_latest_envelope()

    try:
        result = provenance.verify_envelope(envelope)
        if not result:
            logger.warning(
                "verify.light.failed",
                envelope_from=envelope.from_,
                epoch=envelope.epoch,
                position="tip",
            )
        return result
    except Exception as ex:
        logger.error(
            "verify.light.error",
            envelope_from=envelope.from_,
            epoch=envelope.epoch,
            error=str(ex),
        )
        raise RuntimeError(f"Error occurred while verifying envelope tip: {ex}") from ex


def verify_heavy(
    provenance: Provenance,
    workflow: IntentWorkflow,
) -> bool:
    """
    Verifies the complete envelope chain.

    Every unique envelope in the workflow DAG
    is verified, starting from the latest
    envelope and traversing back to the root.
    """
    logger = provenance.logger
    envelopes = workflow.unwrap()

    if not envelopes:
        logger.warning("verify.heavy.failed", reason="empty_workflow")
        return False

    for idx, envelope in enumerate(envelopes):
        try:
            valid = provenance.verify_envelope(envelope)
            if not valid:
                logger.warning(
                    "verify.heavy.failed",
                    envelope_from=envelope.from_,
                    epoch=envelope.epoch,
                    position=idx,
                    chain_length=len(envelopes),
                )
                return False
        except Exception as ex:
            logger.error(
                "verify.heavy.error",
                envelope_from=envelope.from_,
                epoch=envelope.epoch,
                position=idx,
                chain_length=len(envelopes),
                error=str(ex),
            )
            raise RuntimeError(f"Error occurred while verifying envelope chain: {ex}") from ex

    return True


def verify_boundary(
    provenance: Provenance,
    workflow: IntentWorkflow,
) -> bool:
    """
    Verifies the latest envelope and the root envelope.

    Optimized approach for deep DAGs. Secures the boundaries
    (initial human intent and final output) without the compute overhead
    of verifying every intermediate node.
    """
    logger = provenance.logger
    latest_env = workflow.get_latest_envelope()
    root_env = workflow.get_root_envelope()

    envelopes_to_verify = [("tip", latest_env)]

    # Ensure we don't double-verify if the DAG is only 1 layer deep (tip is root)
    if id(root_env) != id(latest_env):
        envelopes_to_verify.append(("root", root_env))

    for position, envelope in envelopes_to_verify:
        try:
            valid = provenance.verify_envelope(envelope)
            if not valid:
                logger.warning(
                    "verify.boundary.failed",
                    envelope_from=envelope.from_,
                    epoch=envelope.epoch,
                    position=position,
                )
                return False
        except Exception as ex:
            logger.error(
                "verify.boundary.error",
                envelope_from=envelope.from_,
                epoch=envelope.epoch,
                position=position,
                error=str(ex),
            )
            raise RuntimeError(f"Error occurred while verifying hybrid boundaries: {ex}") from ex

    return True
