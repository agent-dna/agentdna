"""AgentDNA.login / agentdna.login: a browser login that returns an AgentDNA
ready to build — no identity args. The actor's `name` is the human's email
(the signing key is stored by email), `type` is "user", and it carries run_id.

Origination and the human-card/signer are stubbed so this runs without a live
IdP or the provenance node.
"""

import agentdna
from agentdna import AgentDNA, core
from agentdna.provenance import Provenance


def _offline(monkeypatch, email="alice@corp.com", run_id="RUN-123", api_key="KEY-abc"):
    monkeypatch.setattr(core, "originate", lambda **k: (run_id, email, api_key))
    monkeypatch.setattr(AgentDNA, "create_user_card", lambda self: "")  # no network


def test_top_level_login_is_the_classmethod():
    assert agentdna.login.__func__ is AgentDNA.login.__func__


def test_login_returns_human_actor_named_by_email(monkeypatch, tmp_path):
    _offline(monkeypatch)

    user = agentdna.login(skip_actor_id_registration=True, config_dir=str(tmp_path))

    assert isinstance(user, AgentDNA)
    assert user.name == "alice@corp.com"  # email is the name / key alias
    assert user.type == "user"  # refactor renamed the human actor type: HUMAN -> USER
    assert user.run_id == "RUN-123"
    assert user.api_key == "KEY-abc"  # the key the server handed back is used


def test_carried_run_id_lands_on_the_envelope(monkeypatch, tmp_path):
    _offline(monkeypatch)
    monkeypatch.setattr(Provenance, "sign_envelope", lambda self, env: "sig")

    user = agentdna.login(skip_actor_id_registration=True, config_dir=str(tmp_path))
    wf = user.build(payload="p", recipient_id="svc")

    assert wf.envelope.run_id == "RUN-123"


def test_no_idp_login_has_empty_run_id_and_carries_key(monkeypatch, tmp_path):
    # No IdP configured: server returns a key but no run_id — must still build.
    _offline(monkeypatch, run_id="", api_key="KEY-xyz")
    monkeypatch.setattr(Provenance, "sign_envelope", lambda self, env: "sig")

    user = agentdna.login(skip_actor_id_registration=True, config_dir=str(tmp_path))
    wf = user.build(payload="p", recipient_id="svc")

    assert user.api_key == "KEY-xyz"
    assert user.run_id == ""
    assert wf.envelope.run_id == ""


def test_explicit_api_key_overrides_server(monkeypatch, tmp_path):
    _offline(monkeypatch, api_key="SERVER-KEY")

    user = agentdna.login(
        api_key="EXPLICIT", skip_actor_id_registration=True, config_dir=str(tmp_path)
    )

    assert user.api_key == "EXPLICIT"  # explicit arg wins over the server-returned key
