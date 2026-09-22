"""Authentication evidence.

Headers in, authentication evidence out. The credential itself is discarded.

This module deliberately does not:

* know about MCP
* make network calls
* validate credentials
* make access decisions

For each request, it answers four questions:

```
auth_method    How was the request authenticated?
credential_id Which credential was presented?
identity_id   Which identity did that credential represent?
auth_status   Did the destination accept it? This is supplied by the caller,
              because only the caller sees the destination's response.
```

`credential_id` and `identity_id` are intentionally separate.

A credential can change without the identity changing. For example, when an
access token is refreshed, the credential bytes are new but it may still
represent the same person or agent. Tracking only the credential would make
every token refresh look like an identity handoff.

The module has three important rules:

1. Never verify credential signatures.
   We only classify what was presented. Credential validity is the
   destination's decision, not ours.

2. Never return, log, or store credential values.
   Only keyed hashes (fingerprints) are recorded.

3. Read only the headers listed in `AUTH_HEADERS`.
   No other request data is inspected.

Fingerprints are keyed hashes, so every observer must use the same key.
Otherwise the same credential or identity produces different fingerprints
at different hops and falsely appears to change.

`key_version` travels with each fingerprint so records created with different
keys can be distinguished instead of being silently compared.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import asdict, dataclass

# How the request authenticated.
METHOD_NONE = "none"
METHOD_BEARER_JWT = "bearer_jwt"
METHOD_BEARER_OPAQUE = "bearer_opaque"
METHOD_API_KEY = "api_key"
METHOD_BASIC = "basic"
# A scheme we do not recognise. Better than guessing: calling a Digest or
# Negotiate credential an "api_key" would be a lie in an audit record.
METHOD_UNKNOWN = "unknown"

# Whether the destination accepted it. Set by the caller - this module never
# sees a response.
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_UNKNOWN = "unknown"

# The only headers this module reads.
AUTH_HEADERS = ("authorization", "x-api-key", "api-key")

# Which claim identifies the subject, in order of preference.
#
# `oid` first, deliberately. Entra issues a pairwise `sub` per application, so
# the same person carries a different `sub` at every service - fingerprinting
# that would never match across a run. `oid` is stable across the tenant.
SUBJECT_CLAIMS = ("oid", "sub")


@dataclass(frozen=True)
class AuthFacts:
    """What was observed about one request's authentication.

    Frozen: an observation is not edited after the fact.

    `identity_id` is None whenever the credential carries no readable identity.
    None means "cannot tell" - an opaque token may well be the same person - and
    must never be read as "changed".
    """

    auth_method: str
    credential_id: str
    identity_id: str | None
    key_version: str


def fingerprint(key: bytes, value: str) -> str:
    """Keyed, one-way, and short enough to read in a log line."""
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def key_version(key: bytes) -> str:
    """A short, non-secret label for the key in use.

    Travels on every record so a key change, or one misconfigured observer,
    surfaces as records that cannot be compared - rather than as a run full of
    identity changes that never happened.
    """
    return hashlib.sha256(key).hexdigest()[:8]


def read_jwt_claims(token: str) -> dict | None:
    """Read a JWT's payload, or None if this is not a readable token.

    Read, not verify. The signature is deliberately never checked: this module
    classifies what was presented, and whether it is valid is the destination's
    verdict, recorded separately as auth_status.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None
    return claims if isinstance(claims, dict) else None


def classify(header_name: str, header_value: str) -> tuple[str, dict | None]:
    """Name the authentication method, and return any claims read along the way.

    Returns the claims too so the token is read once rather than once here and
    again to pull the identity out of it.
    """
    value = (header_value or "").strip()
    if not value:
        return METHOD_NONE, None

    # The only other headers we read are api-key headers, so this is exact.
    if header_name.lower() != "authorization":
        return METHOD_API_KEY, None

    scheme, _, token = value.partition(" ")
    scheme = scheme.lower()
    if scheme == "basic":
        return METHOD_BASIC, None
    if scheme == "bearer":
        claims = read_jwt_claims(token)
        return (METHOD_BEARER_JWT, claims) if claims else (METHOD_BEARER_OPAQUE, None)
    return METHOD_UNKNOWN, None


def identity_fingerprint(claims: dict | None, key: bytes) -> str | None:
    """The identity a credential represents, or None when it exposes none.

    Built from issuer + subject, so refreshing a token does not change it as
    long as those claims stay the same. A different issuer, or a provider that
    varies the subject per audience, gives a different fingerprint.
    """
    if not claims:
        return None
    issuer = claims.get("iss")
    subject = next((claims[name] for name in SUBJECT_CLAIMS if claims.get(name)), None)
    if not issuer or not subject:
        return None
    return fingerprint(key, f"{issuer}|{subject}")


def find_auth_header(headers: dict | None) -> tuple[str, str]:
    """The first auth header present, as (name, value), or ("", "").

    Everything else in the request is ignored.
    """
    if not headers:
        return "", ""
    lowered = {str(name).lower(): value for name, value in headers.items()}
    for name in AUTH_HEADERS:
        value = lowered.get(name)
        if value:
            return name, str(value)
    return "", ""


def observe(headers: dict | None, key: bytes) -> AuthFacts:
    """Headers in, facts out. The credential does not leave this function.

    The whole flow is here: find the header, classify it, fingerprint the
    credential, and fingerprint the identity if the credential exposes one.
    """
    version = key_version(key)

    name, value = find_auth_header(headers)
    if not value:
        return AuthFacts(METHOD_NONE, "", None, version)

    method, claims = classify(name, value)

    return AuthFacts(
        auth_method=method,
        credential_id=fingerprint(key, value.strip()),
        identity_id=identity_fingerprint(claims, key),
        key_version=version,
    )


@dataclass(frozen=True)
class AuthEvidence:
    """One complete observation, ready to be written down.

    The four facts, plus the context only the observation point knows: which
    run and request this was, who saw it, where it was going, and what the
    destination answered.
    """

    run_id: str
    request_id: str
    auth_method: str
    credential_id: str
    identity_id: str | None
    auth_status: str
    source: str
    key_version: str
    destination: str
    observed_at: float

    @classmethod
    def from_facts(
        cls,
        facts: AuthFacts,
        *,
        run_id: str,
        request_id: str,
        source: str,
        destination: str,
        auth_status: str,
        observed_at: float,
    ) -> AuthEvidence:
        """The facts, plus the context only an observation point knows."""
        return cls(
            run_id=run_id,
            request_id=request_id,
            auth_method=facts.auth_method,
            credential_id=facts.credential_id,
            identity_id=facts.identity_id,
            auth_status=auth_status,
            source=source,
            key_version=facts.key_version,
            destination=destination,
            observed_at=observed_at,
        )

    def as_dict(self) -> dict:
        return asdict(self)
