import copy

from agentdna.helpers import canonicalize_envelope
from agentdna.types import Actor, Envelope


def create_envelope():
    """
    Creates a minimal envelope for canonicalization
    tests.
    """
    return Envelope(
        from_=Actor(
            id="alice",
            name="Alice",
            type="human",
        ),
        to=Actor(
            id="bob",
            name="Bob",
            type="agent",
        ),
        payload="Hello World",
        metadata={},
        epoch=1,
    )


def test_same_envelope_produces_same_digest():
    """
    Ensures canonicalization is deterministic.
    """
    envelope = create_envelope()

    digest1 = canonicalize_envelope(envelope)
    digest2 = canonicalize_envelope(envelope)

    assert digest1 == digest2


def test_payload_change_changes_digest():
    """
    Ensures payload mutations affect the digest.
    """
    envelope = create_envelope()

    digest1 = canonicalize_envelope(envelope)

    envelope.payload = "Modified"

    digest2 = canonicalize_envelope(envelope)

    assert digest1 != digest2


def test_metadata_change_changes_digest():
    """
    Ensures metadata mutations affect the digest.
    """
    envelope = create_envelope()

    digest1 = canonicalize_envelope(envelope)

    envelope.metadata["version"] = "1"

    digest2 = canonicalize_envelope(envelope)

    assert digest1 != digest2


def test_sender_change_changes_digest():
    """
    Ensures sender changes affect the digest.
    """
    envelope = create_envelope()

    digest1 = canonicalize_envelope(envelope)

    envelope.from_.id = "mallory"

    digest2 = canonicalize_envelope(envelope)

    assert digest1 != digest2


def test_recipient_change_changes_digest():
    """
    Ensures recipient changes affect the digest.
    """
    envelope = create_envelope()

    digest1 = canonicalize_envelope(envelope)

    envelope.to.id = "charlie"

    digest2 = canonicalize_envelope(envelope)

    assert digest1 != digest2


def test_current_signature_does_not_change_digest():
    """
    Ensures the current envelope signature is excluded
    from canonicalization.
    """
    envelope = create_envelope()

    digest1 = canonicalize_envelope(envelope)

    envelope.signature = "abcdef"

    digest2 = canonicalize_envelope(envelope)

    assert digest1 == digest2


def test_parent_signature_changes_digest():
    """
    Ensures ancestor signatures are included in the
    canonical representation.
    """
    parent = create_envelope()
    parent.signature = "parent-signature"

    child = create_envelope()
    child.parent_envelope = parent

    digest1 = canonicalize_envelope(child)

    parent.signature = "tampered"

    digest2 = canonicalize_envelope(child)

    assert digest1 != digest2


def test_parent_payload_changes_digest():
    """
    Ensures parent payload mutations invalidate the
    child digest.
    """
    parent = create_envelope()

    child = create_envelope()
    child.parent_envelope = parent

    digest1 = canonicalize_envelope(child)

    parent.payload = "Tampered"
    digest2 = canonicalize_envelope(child)

    assert digest1 != digest2


def test_deep_copy_produces_same_digest():
    """
    Ensures equivalent envelopes always produce the
    same digest.
    """
    envelope = create_envelope()

    digest1 = canonicalize_envelope(envelope)
    digest2 = canonicalize_envelope(copy.deepcopy(envelope))

    assert digest1 == digest2

def test_grandparent_signature_changes_digest():
    """
    Ensures grandparent signatures are recursively
    included in the canonical representation.
    """
    grandparent = create_envelope()
    grandparent.signature = "grandparent-signature"

    parent = create_envelope()
    parent.signature = "parent-signature"
    parent.parent_envelope = grandparent

    child = create_envelope()
    child.parent_envelope = parent

    digest1 = canonicalize_envelope(child)

    # Tamper with the grandparent signature.
    grandparent.signature = "tampered"

    digest2 = canonicalize_envelope(child)

    assert digest1 != digest2