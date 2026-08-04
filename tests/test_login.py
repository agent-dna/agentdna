"""AgentDNA.login / agentdna.login: a browser login that returns an AgentDNA
ready to build — no identity args. The actor's `name` is the human's email
(the signing key is stored by email), `type` is "user", and it carries run_id.

Origination and the human-card/signer are stubbed so this runs without a live
IdP or the provenance node.
"""

import agentdna
from agentdna import AgentDNA, core
from agentdna.provenance import Provenance


def _offline(monkeypatch, email="alice@corp.com", run_id="RUN-123"):
    monkeypatch.setattr(core, "originate", lambda **k: (run_id, email))
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


def test_carried_run_id_lands_on_the_envelope(monkeypatch, tmp_path):
    _offline(monkeypatch)
    monkeypatch.setattr(Provenance, "sign_envelope", lambda self, env: "sig")

    user = agentdna.login(skip_actor_id_registration=True, config_dir=str(tmp_path))
    wf = user.build(payload="p", recipient_id="svc")

    assert wf.envelope.run_id == "RUN-123"
