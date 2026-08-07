import copy
from dataclasses import fields
import pytest

from agentdna.helpers import canonicalize_envelope
from agentdna.types import Envelope


def create_envelope():
    """
    Creates a minimal envelope for
    canonicalization tests.
    """
    return Envelope(
        from_="bafybeihdwdcefgh4dqkjv67uzcmw7ojee6xedzdetojuzjevtenxquvyku",
        to="bafybeig7r4m2l6s3v5kq9x8c1n0pahf6wzj2e4t7y8u9m3n5q6r1s2v4ya",
        payload="Hello World",
        epoch=1,
    )


def test_same_envelope_produces_same_digest():
    """Ensures canonicalization is deterministic."""
    envelope = create_envelope()
    digest1 = canonicalize_envelope(envelope)
    digest2 = canonicalize_envelope(envelope)
    assert digest1 == digest2


def test_payload_change_changes_digest():
    """Ensures payload mutations affect the digest."""
    envelope = create_envelope()
    digest1 = canonicalize_envelope(envelope)

    envelope.payload = "Modified"
    digest2 = canonicalize_envelope(envelope)

    assert digest1 != digest2


def test_sender_change_changes_digest():
    """Ensures sender changes affect the digest."""
    envelope = create_envelope()
    digest1 = canonicalize_envelope(envelope)

    envelope.from_ = "mallory"
    digest2 = canonicalize_envelope(envelope)

    assert digest1 != digest2


def test_recipient_change_changes_digest():
    """Ensures recipient changes affect the digest."""
    envelope = create_envelope()
    digest1 = canonicalize_envelope(envelope)

    envelope.to = "charlie"
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


def test_parent_hash_changes_digest():
    """
    Ensures that modifying a parent's hash invalidates
    the child's Merkle digest.
    """
    parent = create_envelope()
    parent.hash = canonicalize_envelope(parent)

    child = create_envelope()
    child.parent_envelope = [parent]

    digest1 = canonicalize_envelope(child)

    # Tamper with the parent's hash directly
    parent.hash = "tampered-hash"
    digest2 = canonicalize_envelope(child)

    assert digest1 != digest2


def test_parent_payload_changes_digest():
    """
    Ensures parent payload mutations invalidate the child digest,
    mirroring a real cryptographic cascade.
    """
    parent = create_envelope()
    parent.hash = canonicalize_envelope(parent)

    child = create_envelope()
    child.parent_envelope = [parent]
    digest1 = canonicalize_envelope(child)

    # 1. Tamper with parent payload
    parent.payload = "Tampered"

    # 2. In a Merkle DAG, this recalculates the parent's hash
    parent.hash = canonicalize_envelope(parent)

    # 3. Which immediately breaks the child's hash!
    digest2 = canonicalize_envelope(child)

    assert digest1 != digest2


def test_deep_copy_produces_same_digest():
    """Ensures equivalent envelopes always produce the same digest."""
    envelope = create_envelope()
    digest1 = canonicalize_envelope(envelope)
    digest2 = canonicalize_envelope(copy.deepcopy(envelope))
    assert digest1 == digest2


def test_grandparent_hash_changes_digest():
    """
    Ensures tampering at the root of the graph cascades
    all the way to the tip of the Merkle DAG.
    """
    # 1. Setup Grandparent
    grandparent = create_envelope()
    grandparent.hash = canonicalize_envelope(grandparent)

    # 2. Setup Parent
    parent = create_envelope()
    parent.parent_envelope = [grandparent]
    parent.hash = canonicalize_envelope(parent)

    # 3. Setup Child
    child = create_envelope()
    child.parent_envelope = [parent]
    digest1 = canonicalize_envelope(child)

    # 4. Tamper with the grandparent!
    grandparent.payload = "tampered"

    # 5. The Cascade: GP changes -> P changes -> C breaks
    grandparent.hash = canonicalize_envelope(grandparent)
    parent.hash = canonicalize_envelope(parent)

    digest2 = canonicalize_envelope(child)

    assert digest1 != digest2


def test_missing_parent_hash_raises_error():
    """
    Validates the safety mechanism that prevents canonicalizing
    a child if its parent hasn't been properly hashed yet.
    """
    parent = create_envelope()
    parent.hash = ""  # Explicitly empty

    child = create_envelope()
    child.parent_envelope = [parent]

    with pytest.raises(ValueError, match="lacks hash; cannot canonicalize"):
        canonicalize_envelope(child)


def test_all_intrinsic_fields_affect_digest():
    """
    Dynamically tests the Envelope struct to ensure EVERY intrinsic
    field affects the final hash (replacing the old _envelope_to_dict check).
    """
    base = create_envelope()
    base_digest = canonicalize_envelope(base)

    # These fields are metadata or DAG edges, not intrinsic content.
    ignored_fields = {"hash", "signature", "parent_envelope"}

    for field in fields(Envelope):
        if field.name in ignored_fields:
            continue

        env = copy.deepcopy(base)
        current_val = getattr(env, field.name)

        # Mutate the field based on its type
        if isinstance(current_val, int):
            setattr(env, field.name, current_val + 1)
        else:
            setattr(env, field.name, str(current_val) + "_mutated")

        new_digest = canonicalize_envelope(env)

        # If this fails, it means you added a new field to types.py
        # but forgot to add it to `_hash_content` in helpers.py!
        assert new_digest != base_digest, f"Field '{field.name}' did not affect canonicalization!"
