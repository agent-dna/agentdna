"""Tests for authentication facts.

Two of these are the module's reason to exist:

    test_a_refresh_keeps_the_same_identity   - the false positive it avoids
    test_no_credential_survives_the_facts    - the promise it makes
"""

import base64
import hashlib
import json
from dataclasses import asdict

from agentdna.authevidence import (
    METHOD_API_KEY,
    METHOD_BASIC,
    METHOD_BEARER_JWT,
    METHOD_BEARER_OPAQUE,
    METHOD_NONE,
    METHOD_UNKNOWN,
    classify,
    find_auth_header,
    fingerprint,
    identity_fingerprint,
    key_version,
    observe,
    read_jwt_claims,
)


def method(header_name, header_value):
    return classify(header_name, header_value)[0]


def credential_id(header_value, key):
    return fingerprint(key, header_value.strip())


def identity_id(header_value, key):
    _, claims = classify("authorization", header_value)
    return identity_fingerprint(claims, key)


KEY = hashlib.sha256(b"one-deployment-key").digest()
OTHER_KEY = hashlib.sha256(b"a-different-deployment-key").digest()


def jwt(claims: dict) -> str:
    """A token with a real payload and a nonsense signature.

    The signature is nonsense on purpose: nothing in this module verifies it,
    and a test that passed only with a valid signature would be testing the
    wrong thing.
    """

    def segment(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{segment({'alg': 'RS256'})}.{segment(claims)}.not-a-real-signature"


# --- what kind of authentication was it ----------------------------------


def test_classify_names_each_method():
    assert method("authorization", f"Bearer {jwt({'iss': 'i', 'sub': 's'})}") == METHOD_BEARER_JWT
    assert method("authorization", "Bearer gho_anopaquestring") == METHOD_BEARER_OPAQUE
    assert method("authorization", "Basic dXNlcjpwYXNz") == METHOD_BASIC
    assert method("x-api-key", "abc123") == METHOD_API_KEY
    assert method("authorization", "") == METHOD_NONE


# --- which credential was it ---------------------------------------------


def test_the_same_credential_fingerprints_the_same():
    header = "Bearer sometokenvalue"
    assert credential_id(header, KEY) == credential_id(header, KEY)


def test_different_credentials_fingerprint_differently():
    assert credential_id("Bearer aaa", KEY) != credential_id("Bearer bbb", KEY)


def test_a_different_key_changes_every_fingerprint():
    """Why one key per deployment is not optional: with two keys the same
    credential looks like two, and every hop reads as a change."""
    header = "Bearer sometokenvalue"
    assert credential_id(header, KEY) != credential_id(header, OTHER_KEY)
    assert key_version(KEY) != key_version(OTHER_KEY)


# --- which identity did it represent -------------------------------------


def test_a_refresh_keeps_the_same_identity():
    """A new token for the same person is a refresh, not a handoff. This is the
    false positive the two fingerprints exist to avoid."""
    before = f"Bearer {jwt({'iss': 'https://idp', 'oid': 'priya', 'exp': 1000})}"
    after = f"Bearer {jwt({'iss': 'https://idp', 'oid': 'priya', 'exp': 4600})}"

    assert credential_id(before, KEY) != credential_id(after, KEY)
    assert identity_id(before, KEY) == identity_id(after, KEY)


def test_different_people_fingerprint_differently():
    priya = f"Bearer {jwt({'iss': 'https://idp', 'oid': 'priya'})}"
    hari = f"Bearer {jwt({'iss': 'https://idp', 'oid': 'hari'})}"
    assert identity_id(priya, KEY) != identity_id(hari, KEY)


def test_oid_is_preferred_over_sub():
    """Entra issues a pairwise `sub` per application, so the same person carries
    a different `sub` at each service. Fingerprinting `sub` would never match
    across a run; `oid` is stable across the tenant."""
    at_one_service = f"Bearer {jwt({'iss': 'https://idp', 'oid': 'priya', 'sub': 'app-a-xyz'})}"
    at_another = f"Bearer {jwt({'iss': 'https://idp', 'oid': 'priya', 'sub': 'app-b-pqr'})}"
    assert identity_id(at_one_service, KEY) == identity_id(at_another, KEY)


def test_the_same_subject_at_another_issuer_is_another_identity():
    one = f"Bearer {jwt({'iss': 'https://idp-a', 'oid': 'priya'})}"
    two = f"Bearer {jwt({'iss': 'https://idp-b', 'oid': 'priya'})}"
    assert identity_id(one, KEY) != identity_id(two, KEY)


def test_an_opaque_credential_has_no_identity():
    """None means "cannot tell", and must never be read as "changed"."""
    assert identity_id("Bearer gho_anopaquestring", KEY) is None
    assert identity_id("abc123", KEY) is None


def test_a_readable_token_without_the_claims_we_need_has_no_identity():
    assert identity_id(f"Bearer {jwt({'scope': 'read'})}", KEY) is None
    assert identity_id(f"Bearer {jwt({'iss': 'https://idp'})}", KEY) is None


def test_a_signature_is_never_verified():
    """We classify what was presented. Whether it is valid is the
    destination's verdict, and arrives separately."""
    forged = f"Bearer {jwt({'iss': 'https://idp', 'oid': 'priya'})}"
    assert method("authorization", forged) == METHOD_BEARER_JWT
    assert identity_id(forged, KEY) is not None


# --- which headers get read ----------------------------------------------


def test_only_auth_headers_are_read():
    name, value = find_auth_header(
        {
            "Cookie": "session=secret",
            "X-Internal-Token": "another-secret",
            "Authorization": "Bearer abc",
        }
    )
    assert (name, value) == ("authorization", "Bearer abc")


def test_authorization_is_preferred_over_an_api_key():
    name, _ = find_auth_header({"X-Api-Key": "k", "Authorization": "Bearer abc"})
    assert name == "authorization"


# --- observe -------------------------------------------------------------


def test_observe_returns_the_four_facts():
    facts = observe({"Authorization": f"Bearer {jwt({'iss': 'https://idp', 'oid': 'priya'})}"}, KEY)
    assert facts.auth_method == METHOD_BEARER_JWT
    assert facts.credential_id
    assert facts.identity_id
    assert facts.key_version == key_version(KEY)


def test_no_headers_is_not_an_error():
    """stdio transport carries no headers at all, and that is an answer:
    no credential crossed anything."""
    facts = observe({}, KEY)
    assert facts.auth_method == METHOD_NONE
    assert facts.credential_id == ""
    assert facts.identity_id is None


def test_no_credential_survives_the_facts():
    """Serialise everything the module produces and search it for the real
    values. This is the check to hand a security reviewer, because it can fail.
    """
    token = jwt({"iss": "https://idp", "oid": "priya"})
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Api-Key": "a-plain-api-key-value",
        "Cookie": "session=secret",
    }

    written = json.dumps(asdict(observe(headers, KEY)))

    assert token not in written
    assert "a-plain-api-key-value" not in written
    assert "secret" not in written
    assert "Bearer" not in written


def test_an_unrecognised_scheme_is_not_called_an_api_key():
    """Digest and Negotiate are not API keys. Saying so in an audit record
    would be a lie, and one that discredits the rest of the table."""
    assert method("authorization", "Digest username=alice, realm=corp") == METHOD_UNKNOWN
    assert method("authorization", "Negotiate YIIZ...") == METHOD_UNKNOWN


def test_a_token_is_read_once():
    """classify returns the claims it already read, so nothing parses the
    token a second time to pull the identity out of it."""
    token = f"Bearer {jwt({'iss': 'https://idp', 'oid': 'priya'})}"
    auth_method, claims = classify("authorization", token)

    assert auth_method == METHOD_BEARER_JWT
    assert claims == read_jwt_claims(token.partition(" ")[2])
    assert identity_fingerprint(claims, KEY) is not None
