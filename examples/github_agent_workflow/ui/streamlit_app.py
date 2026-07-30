"""Streamlit demo UI for GithubAgent."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
import secrets
import string

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from app.agent_names import coordinator_name
from app.agents.graph import build_graph
from app.constants import ANONYMOUS_USER_EMAIL
from app.integrations.agentdna import UserSession, deploy_agent, deploy_user
from app.agents.agentdna_helpers import get_dna
from app.utils import (
    serialize_workflow,
    deserialize_workflow,
)

COORDINATOR_SKILLS_FILE = str(
    Path(__file__).resolve().parent.parent / "app" / "agents" / "coordinator" / "skills.md"
)

if not bool(os.environ.get("AGENTDNA_API_KEY")):
    st.error(
        "The AGENTDNA_API_KEY environment variable is not set. "
        "Please set it to your AgentDNA API key and restart the app."
    )
    st.stop()

st.set_page_config(page_title="GithubAgent", layout="wide")

st.title("GithubAgent")
st.caption(
    "Turn a plain-English request into a real GitHub action — handled by a team of "
    "AI agents, with a verified and auditable trail at every step."
)


def _random_agent_name(prefix: str) -> str:
    alphabet = string.ascii_letters + string.digits

    suffix = "".join(secrets.choice(alphabet) for _ in range(7))

    return f"{prefix}_{suffix}"


with st.sidebar:
    st.markdown("### What this does")
    st.markdown(
        "Turn a plain-English request into a real action on GitHub — such as opening "
        "an issue or a pull request — carried out by a small team of AI agents.\n\n"
        "A **Coordinator** reads your request and plans the work; a **Worker** carries "
        "it out.\n\n"
        "Everyone involved — you and each agent — has a verified identity, and every "
        "step is authorized and recorded. The result is a clear, trustworthy trail of "
        "who requested what, with proof that each action was approved before it happened."
    )
    st.markdown("### Steps")
    st.markdown(
        "1. Set up an identity for yourself and the two agents.\n"
        "2. Describe what you want done.\n"
        "3. Run it and review the result."
    )


def _render_identity(label: str, result: dict | None) -> None:
    """Show the outcome of a set-up button (persisted across reruns)."""
    if not result:
        st.caption(f"{label}: not set up yet")
        return
    if result.get("ok"):
        card_label = "User Card ID" if result.get("kind") == "user" else "Agent Card ID"
        lines = [
            f"Name      : {result.get('alias')}",
            f"Actor ID  : {result.get('actor_id')}",
        ]

        if result.get("card_id"):
            lines.append(f"{card_label} : {result.get('card_id')}")
        st.success(f"{label} ready")
        st.code("\n".join(lines), language="text")
    else:
        st.error(f"{label} couldn't be set up — {result.get('reason')}")


# ── 1 · Identities ────────────────────────────────────────────────────────────
st.header("1 · Set up identities")
st.caption("Create an identity for yourself and each agent before running a task.")

user_col, agent_col = st.columns(2)

with user_col:
    st.subheader("You")
    user_email = st.text_input(
        "Your email",
        value=ANONYMOUS_USER_EMAIL,
        help="The email that identifies you in this workflow.",
    )
    if st.button("Create my identity", type="primary", use_container_width=True):
        with st.spinner("Setting up your identity…"):
            st.session_state["user_identity"] = deploy_user(user_email)
    _render_identity("Your identity", st.session_state.get("user_identity"))

with agent_col:
    st.subheader("Agents")

    st.session_state.setdefault("coordinator_name_input", _random_agent_name("CoordinatorAgent"))
    coordinator_input = st.text_input(
        "Coordinator name",
        key="coordinator_name_input",
        help="A name for the Coordinator agent.",
    )
    if st.button("Create Coordinator Identity", use_container_width=True):
        with st.spinner("Setting up the Coordinator…"):
            st.session_state["coordinator_identity"] = deploy_agent(
                "coordinator", coordinator_input
            )
    _render_identity("Coordinator", st.session_state.get("coordinator_identity"))

    st.session_state.setdefault("worker_name_input", _random_agent_name("WorkerAgent"))
    worker_input = st.text_input(
        "Worker name",
        key="worker_name_input",
        help="A name for the Worker agent.",
    )
    if st.button("Create Worker Identity", use_container_width=True):
        with st.spinner("Setting up the Worker…"):
            st.session_state["worker_identity"] = deploy_agent("worker", worker_input)
    _render_identity("Worker", st.session_state.get("worker_identity"))


# ── 2 · Run a task ────────────────────────────────────────────────────────────
st.header("2 · Run a task")

user_input = st.text_area(
    "Describe what you want to do:",
    height=140,
    placeholder=(
        "e.g. Create an issue in octocat/hello-world titled "
        "'Typo in README' with body 'Line 3 has a typo.'"
    ),
)

st.caption("Example prompts:")

st.code(
    'Create an issue titled "Fix login bug" in the repository '
    "SynapzeCore/sample-repo with body "
    '"Users receive a 500 Internal Server Error after login."',
    language="text",
)

st.code(
    'Create an issue titled "Update documentation" in SynapzeCore/sample-repo.',
    language="text",
)

st.code(
    "Create a pull request in SynapzeCore/sample-repo "
    "from branch feature/auth to main titled "
    '"Add authentication flow" with body '
    '"Implements JWT authentication and login endpoints."',
    language="text",
)

if st.button("Run", type="primary"):
    if not user_input.strip():
        st.warning("Please enter a request first.")
    else:
        with st.spinner("Working on your request…"):

            async def _run():
                coordinator_dna = get_dna(
                    coordinator_name(),
                    COORDINATOR_SKILLS_FILE,
                )

                if coordinator_dna is None:
                    st.error("Failed to load Coordinator identity.")
                    return None, None

                session = await UserSession.open(
                    intent={
                        "intent": "github_task",
                        "workflow_type": "github_action",
                        "submitted_by": user_email,
                        "request_preview": user_input[:200],
                    },
                    submitted_by=user_email,
                    submitter_email=user_email,
                    first_agent=coordinator_dna,
                )

                graph = build_graph()

                initial_state: dict = {"user_input": user_input}
                if session is not None:
                    initial_state["_agentdna_workflow"] = serialize_workflow(session.workflow)
                    initial_state["_agentdna_user_id"] = session.user_id

                result = await graph.ainvoke(initial_state)

                if session is not None and result.get("_agentdna_workflow"):
                    session.complete(deserialize_workflow(result["_agentdna_workflow"]))

                return result, (session.user_id if session else None)

            try:
                result, user_id = asyncio.run(_run())
            except Exception as exc:
                st.exception(exc)
                st.stop()

            if result.get("_agentdna_terminal"):
                reason = result.get(
                    "_agentdna_terminal_reason",
                    "unknown_error",
                )

                st.error(f"Workflow terminated: {reason}")

        if user_id:
            st.info(f"Your User ID: `{user_id}`")

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Coordinator's plan")
            st.code(result.get("task_spec", "(empty)"), language="markdown")

        with col2:
            st.subheader("Result")
            st.success(result.get("final_response", "(no response)"))

        with st.expander("Activity details", expanded=False):
            for msg in result.get("worker_messages", []):
                content = getattr(msg, "content", "")
                if not isinstance(content, str):
                    content = str(content)
                if content:
                    st.text(content)

                tool_calls = getattr(msg, "tool_calls", None)
                if tool_calls:
                    for tc in tool_calls:
                        st.markdown(f"> action: `{tc.get('name', '?')}`")
                        st.json(tc.get("args", {}))
