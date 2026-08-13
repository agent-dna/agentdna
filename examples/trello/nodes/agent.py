"""Agent node."""

from __future__ import annotations
import json

from langchain_core.language_models.chat_models import BaseChatModel

from state import WorkflowState
from dotenv import load_dotenv
from pathlib import Path

from config import settings


_HERE = Path(__file__).resolve().parent
SKILLS_FILE = _HERE / "skills.md"

from agentdna.core import AgentDNA
from agentdna.error import RESULT_OK

from langchain_core.messages import SystemMessage

SYSTEM_PROMPT = SystemMessage(
    content=(
        "You turn a changelog snippet into a short, friendly release announcement: "
        "a one-line title and exactly three bullet highlights written for end users "
        "(no internal jargon). If Trello tools are available, create a card in the "
        "Announcements list with that title and description; otherwise output the "
        "card title and description."
    )
)


TRELLO_RELEASE_AGENT = AgentDNA(
    name=settings.agent_name,
    type="agent",
    agent_policy_file=SKILLS_FILE,
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.agentdna_provenance_url,
)

def make_agent_node(
    llm: BaseChatModel,
    tools: list,
):
    """Create the workflow's agent node."""

    if tools:
        llm = llm.bind_tools(tools)

    def agent_node(state: WorkflowState):
        print(state["adna_envelope"])

        verification_res = TRELLO_RELEASE_AGENT.verify(state["adna_envelope"])
        if verification_res != RESULT_OK:
            workflow = TRELLO_RELEASE_AGENT.build(
                payload=json.dumps({"error": f"ADNA envelope verification failed: {verification_res}"}),
                previous_workflows=state["adna_envelope"],
                verification_code=verification_res,
            )

            print("==== AGENT ENVELOPE ====")
            print(workflow)
            TRELLO_RELEASE_AGENT.record(workflow)
            raise ValueError(f"ADNA envelope verification failed: {verification_res}")

        response = llm.invoke(
            [
                SYSTEM_PROMPT,
                *state["messages"],
            ]
        )

        workflow = TRELLO_RELEASE_AGENT.build(
            payload=json.dumps({
                "messages": [response.content],
            }),
            previous_workflows=state["adna_envelope"],
        )

        response_msg = {
            "messages": [response],
            "adna_envelope": workflow,
        }

        return response_msg

    return agent_node