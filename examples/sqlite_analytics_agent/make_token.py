"""Print a token for testing authentication evidence.

    python make_token.py priya

Nothing verifies the signature - AgentDNA reads the claims and never validates
them - so a stand-in is fine here. What matters is `iss` and `oid`, because the
identity fingerprint is built from those two.
"""

import base64
import json
import sys


def segment(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


who = sys.argv[1] if len(sys.argv) > 1 else "priya"
issuer = sys.argv[2] if len(sys.argv) > 2 else "https://login.example.com"

header = segment({"alg": "RS256", "typ": "JWT"})
claims = segment({"iss": issuer, "oid": who, "aud": "sqlite-analytics", "exp": 9999999999})

print(f"{header}.{claims}.stand-in-signature")
