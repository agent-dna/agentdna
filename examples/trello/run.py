import asyncio
from mcp_client import load_tools
from llm import make_llm
from dotenv import load_dotenv
from pathlib import Path
from agentdna.core import AgentDNA
import asyncio
import random
import sys

from config import settings
from graph import build_graph
from llm import make_llm
from state import make_initial_state

load_dotenv(Path(__file__).with_name(".env"))

APP = "Trello"

SAMPLE_INPUTS = [
    "Changelog v2.4.0:\n"
    "- feat: dark mode across the dashboard\n"
    "- feat: CSV export on all report tables\n"
    "- fix: 40% faster initial load\n"
    "- chore: bumped internal deps",
    "Changelog v3.1.0:\n"
    "- feat: SAML single sign-on\n"
    "- feat: bulk-edit for tasks\n"
    "- fix: timezone bug in reminders\n"
    "- refactor: rewrote the notifications service",
    "Changelog v1.8.2:\n"
    "- fix: crash when uploading files over 100MB\n"
    "- fix: broken links in the help center\n"
    "- perf: search is now 2x faster\n"
    "- chore: internal logging cleanup",
    "Changelog v4.0.0:\n"
    "- feat: brand-new mobile app\n"
    "- feat: offline mode\n"
    "- feat: customizable dashboards\n"
    "- breaking: legacy v1 API removed",
    "Changelog v2.9.0:\n"
    "- feat: Slack and Teams integrations\n"
    "- feat: weekly summary emails\n"
    "- fix: duplicate notifications on mobile\n"
    "- chore: dependency upgrades",
]

USER = AgentDNA(
    name=settings.user_name,
    type="user",
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.agentdna_provenance_url
)

async def run(user_input: str):
    """Execute the workflow."""
    tools = await load_tools()

    llm = make_llm()

    workflow = build_graph(
        llm=llm,
        tools=tools,
    )

    adna_workflow = USER.build(
        payload=user_input,
    )

    state = make_initial_state(user_input, adna_workflow)

    result = await workflow.ainvoke(state)

    try:
        USER.record(result["adna_envelope"])
    except Exception as e:
        print(f"Error recording ADNA envelope: {e}") 
    return result

    
def pick_input() -> str:
    """Explicit CLI arg > stdin > random sample."""

    positional = [a for a in sys.argv[1:] if not a.startswith("--")]

    if positional:
        return positional[0]

    if "--stdin" in sys.argv:
        return sys.stdin.read().strip()

    random.seed()

    return random.choice(SAMPLE_INPUTS)

def main():
    asyncio.run(
        run(user_input=pick_input())
    )

if __name__=="__main__":
    main()