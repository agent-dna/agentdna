"""Fingerprint key management.

This is the only part of authentication evidence that reads configuration or
touches disk. Fingerprinting and request observation stay separate from it.

For evidence to be comparable across hops, every hop in a deployment
must use the same key. Set `AGENTDNA_FINGERPRINT_KEY` to achieve that.

If it is not set, a key is generated and kept locally so one installation
remains consistent with itself. That is useful for development, but records
from different installations will not be comparable.
"""

from __future__ import annotations

import hashlib
import os
import secrets

ENV_KEY = "AGENTDNA_FINGERPRINT_KEY"

# Where a locally generated key is kept, under the actor config directory.
KEY_FILE = "fingerprint.key"


def load_key(config_dir: str, logger=None) -> bytes:
    """Return the fingerprint key for this installation.

    Uses `AGENTDNA_FINGERPRINT_KEY` when configured. Otherwise, loads a persistent
    local key, generating one on first use.

    A local key keeps one installation consistent with itself but will not match
    other installations. If the local key cannot be read or saved, a temporary
    process-only key is used instead.
    """

    configured = os.environ.get(ENV_KEY, "").strip()
    if configured:
        return hashlib.sha256(configured.encode("utf-8")).digest()

    key = _local_key(config_dir)
    if logger is not None:
        logger.warning(
            "agentdna.authevidence.local_key",
            hint=f"no {ENV_KEY} set, so this installation fingerprints with a key of "
            "its own; evidence from elsewhere will not be comparable",
        )
    return key


def _local_key(config_dir: str) -> bytes:
    """Read this machine's key, generating and saving one the first time."""
    path = os.path.join(config_dir, KEY_FILE)
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                return bytes.fromhex(handle.read().strip())

        key = secrets.token_bytes(32)
        os.makedirs(config_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(key.hex())
        os.chmod(path, 0o600)
        return key
    except Exception:
        # Cannot read or save one. Carry on with a key for this process only:
        # evidence is never worth failing a server's startup for. Nothing will
        # compare across restarts, and key_version makes that visible.
        return secrets.token_bytes(32)
