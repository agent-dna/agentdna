"""AgentDNA.login / agentdna.login: a browser login that returns an AgentDNA
ready to build — no identity args. The actor's `name` is the human's email
(the signing key is stored by email), `type` is "user", and it carries run_id.

Origination and the human-card/signer are stubbed so this runs without a live
IdP or the provenance node.
"""

import socket
import time

import pytest

import agentdna
from agentdna import AgentDNA, core, origination
from agentdna.provenance import Provenance


def _offline(monkeypatch, email="alice@corp.com", run_id="RUN-123"):
    monkeypatch.setattr(core, "originate", lambda **k: (run_id, email))
    monkeypatch.setattr(AgentDNA, "create_user_card", lambda self: "card-offline")  # no network


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


def test_later_envelopes_copy_the_run_id(monkeypatch, tmp_path):
    _offline(monkeypatch)
    monkeypatch.setattr(Provenance, "sign_envelope", lambda self, env: "sig")

    user = agentdna.login(skip_actor_id_registration=True, config_dir=str(tmp_path))
    asked = user.build(payload="p", recipient_id="svc")
    answer = user.build(payload="done", previous_workflows=asked)

    assert answer.envelope.run_id == "RUN-123"


def test_login_uses_the_api_key_it_is_given(monkeypatch, tmp_path):
    _offline(monkeypatch)

    user = agentdna.login(
        api_key="KEY-abc", skip_actor_id_registration=True, config_dir=str(tmp_path)
    )

    assert user.api_key == "KEY-abc"


def test_login_uses_the_default_auth_server(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        core, "originate", lambda **k: seen.update(k) or ("RUN-123", "alice@corp.com")
    )
    monkeypatch.setattr(AgentDNA, "create_user_card", lambda self: "card-offline")

    agentdna.login(skip_actor_id_registration=True, config_dir=str(tmp_path))

    assert seen["auth_server_url"] == "https://user-auth.agentdna.io"


def test_a_login_result_without_run_id_fails():
    # Without a run_id, envelopes could not be traced back to the login.
    with pytest.raises(KeyError):
        origination.get_login_result({"email": "alice@corp.com"})


def test_the_login_wait_gives_up_after_its_timeout():
    with socket.socket() as probe:  # a free port nobody will call back on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    started = time.time()
    with pytest.raises(RuntimeError, match="timed out"):
        origination.wait_for_login(f"http://127.0.0.1:{port}/callback", timeout=1)

    assert time.time() - started < 5


@pytest.mark.parametrize(
    "redirect_url",
    ["http://localhost/callback", "https://app.corp.com:8400/callback"],
)
def test_a_redirect_url_not_on_this_computer_with_a_port_fails_at_once(redirect_url):
    started = time.time()
    with pytest.raises(RuntimeError, match="redirect URL must look like"):
        origination.wait_for_login(redirect_url, timeout=60)

    assert time.time() - started < 1  # no waiting for the timeout
