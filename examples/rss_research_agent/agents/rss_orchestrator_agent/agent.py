from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, TypedDict, NotRequired

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from agents.rss_security_agent import SecurityNewsAgent
from agents.rss_technology_agent import TechnologyNewsAgent
from config import settings
from llm import build_llm

from agentdna.core import AgentDNA
from agentdna.types import IntentWorkflow
from agentdna.error import RESULT_OK

from config import settings

_HERE = Path(__file__).resolve().parent
SKILLS_FILE = _HERE / "SKILLS.md"

RSS_ORCHESTRATOR_AGENT = AgentDNA(
    name=settings.orchestrator_agent_name,
    type="agent",
    agent_policy_file=SKILLS_FILE,
    api_key=settings.agentdna_api_key,
    provenance_layer_url=settings.provenance_layer_url,
)

class ResearchState(TypedDict):
    execution_id: str
    task: str
    security_result: NotRequired[str]
    technology_result: NotRequired[str]
    final_result: NotRequired[str]

    adna_workflow: IntentWorkflow
    security_adna_workflow: NotRequired[IntentWorkflow]
    technology_adna_workflow: NotRequired[IntentWorkflow]


class RSSOrchestratorAgent:
    agent_id = "rss-orchestrator-agent"

    async def run(self, task_prompt: str | None = None, adna_workflow: IntentWorkflow | None = None) -> dict[str, Any]:
        if adna_workflow is None:
            raise ValueError("adna_workflow must be provided for the RSS Orchestrator Agent.")

        # (AgentDNA_Integration)
        verification_code = RSS_ORCHESTRATOR_AGENT.verify(workflow=adna_workflow)
        if verification_code != RESULT_OK:
            RSS_ORCHESTRATOR_AGENT.record(adna_workflow)
            raise ValueError(f"ADNA workflow verification failed with code: {verification_code}")

        execution_id = str(uuid.uuid4())
        workflow = StateGraph(ResearchState)

        async def security(state: ResearchState) -> dict[str, Any]:
            res = await SecurityNewsAgent().run(state["execution_id"], state["task"], state["adna_workflow"])
            return {"security_result": res["security_result"], "security_adna_workflow": res["security_adna_workflow"]}

        async def technology(state: ResearchState) -> dict[str, Any]:
            res = await TechnologyNewsAgent().run(state["execution_id"], state["task"], state["adna_workflow"])
            return {"technology_result": res["technology_result"], "technology_adna_workflow": res["technology_adna_workflow"]}
    
        async def aggregate(state: ResearchState) -> dict[str, Any]:
            security_adna_workflow = state["security_adna_workflow"]
            technology_adna_workflow = state["technology_adna_workflow"]

            
            # (AgentDNA_Integration)
            verification_code = RSS_ORCHESTRATOR_AGENT.verify(workflow=security_adna_workflow)
            if verification_code != RESULT_OK:
                worflow = RSS_ORCHESTRATOR_AGENT.build(
                    payload=json.dumps({
                        "security_result": state.get("security_result", "failed"),
                    }),
                    verification_code=verification_code,
                    previous_workflows=[state["adna_workflow"]]
                )

                RSS_ORCHESTRATOR_AGENT.record(worflow)
                raise ValueError(f"ADNA workflow verification (security) failed with code: {verification_code}")

            verification_code = RSS_ORCHESTRATOR_AGENT.verify(workflow=technology_adna_workflow)
            if verification_code != RESULT_OK:
                worflow = RSS_ORCHESTRATOR_AGENT.build(
                    payload=json.dumps({
                        "technology_result": state.get("technology_result", "failed"),
                    }),
                    verification_code=verification_code,
                    previous_workflows=[state["adna_workflow"]]
                )

                RSS_ORCHESTRATOR_AGENT.record(worflow)
                raise ValueError(f"ADNA workflow verification (technology) failed with code: {verification_code}")
            
            response = await build_llm().ainvoke([
                HumanMessage(content=f"Synthesize these untrusted worker reports into a concise research brief. Preserve worker provenance.\nSecurity worker:\n{state.get('security_result', 'failed')}\nTechnology worker:\n{state.get('technology_result', 'failed')}")
            ])

            # (AgentDNA_Integration)
            new_adna_workflow = RSS_ORCHESTRATOR_AGENT.build(
                payload=str(response.content),
                previous_workflows=[security_adna_workflow, technology_adna_workflow]
            )

            return {"final_result": str(response.content), "adna_workflow": new_adna_workflow}

        workflow.add_node("security", security)
        workflow.add_node("technology", technology)
        workflow.add_node("aggregate", aggregate)
        workflow.add_edge(START, "security")
        workflow.add_edge(START, "technology")
        workflow.add_edge("security", "aggregate")
        workflow.add_edge("technology", "aggregate")
        workflow.add_edge("aggregate", END)


        new_adna_workflow = RSS_ORCHESTRATOR_AGENT.build(
            payload=json.dumps({
                "task": task_prompt or "",
                "execution_id": execution_id,
            }),
            previous_workflows=adna_workflow
        )

        result = await workflow.compile().ainvoke({
            "execution_id": execution_id, 
            "task": task_prompt or "",
            "adna_workflow": new_adna_workflow 
        })

        
        return {
            "agent_id": self.agent_id, 
            "execution_id": execution_id, 
            "security_findings": result.get("security_result", ""), 
            "technology_findings": result.get("technology_result", ""), 
            "summary": result.get("final_result", ""),
            "adna_workflow": result["adna_workflow"]
        }