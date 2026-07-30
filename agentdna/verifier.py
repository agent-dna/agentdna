from .helpers import get_latest_envelope, unwrap_workflow
from .provenance import Provenance
from .types import IntentWorkflow, Issue, VerificationResult


def verify_light(
    provenance: Provenance,
    workflow: IntentWorkflow,
) -> VerificationResult:
    """
    Verifies only the latest envelope.
    """

    envelope = get_latest_envelope(workflow)

    issues: list[Issue] = []

    try:
        valid = provenance.verify_envelope(envelope)

        if not valid:
            issues.append(
                Issue(
                    depth=0,
                    reason=f"invalid signature for actor {envelope.from_.name} (id: {envelope.from_.id})",
                )
            )

    except Exception as ex:
        issues.append(
            Issue(
                depth=0,
                reason=f"error occured while verifying signature of actor {envelope.from_.name} (id: {envelope.from_.id}): {str(ex)}",
            )
        )

    return VerificationResult(
        valid=len(issues) == 0,
        chain_depth=1,
        issues=issues,
    )


def verify_heavy(
    provenance: Provenance,
    workflow: IntentWorkflow,
) -> VerificationResult:
    """
    Verifies the complete envelope chain.

    Every envelope in the workflow lineage
    is verified, starting from the latest
    envelope and traversing back to the root.
    """

    envelopes = unwrap_workflow(workflow)

    chain_depth = len(envelopes)

    issues: list[Issue] = []

    current_depth = 0
    for envelope in envelopes:
        try:
            valid = provenance.verify_envelope(envelope)
            if not valid:
                issues.append(
                    Issue(
                        depth=current_depth,
                        reason=f"invalid signature for actor {envelope.from_.name} (id: {envelope.from_.id})",
                    )
                )

        except Exception as ex:
            issues.append(
                Issue(
                    depth=current_depth,
                    reason=f"error occured while verifying signature of actor {envelope.from_.name} (id: {envelope.from_.id}): {str(ex)}",
                )
            )

        current_depth += 1
    return VerificationResult(
        valid=len(issues) == 0,
        chain_depth=chain_depth,
        issues=issues,
    )
