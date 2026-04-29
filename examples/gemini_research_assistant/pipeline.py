from __future__ import annotations

import asyncio
import json
from typing import Optional, Any
import hashlib
from google import genai
from google.genai import types
from langfuse import observe, get_client

try:
    import nest_asyncio
    nest_asyncio.apply()
    from agentdna import AgentDNA
    _AGENTDNA_AVAILABLE = True
except ImportError:
    _AGENTDNA_AVAILABLE = False


COORDINATOR_SYSTEM = """You are a research coordinator.

Break the research question into exactly 3 focused subtopics.

Return ONLY a valid JSON array of 3 strings.
No markdown. No explanation.
"""

RESEARCHER_SYSTEM = """You are a research specialist.

Analyze the assigned subtopic in the context of the full research question.
Include:
- Key facts
- Nuances or debates
- Examples or data where useful

Write 3-5 paragraphs.
"""

SYNTHESIZER_SYSTEM = """You are a research synthesizer.

Combine the specialist findings into one coherent report:

## Executive Summary
## Key Findings
## Analysis
## Conclusion
"""


class ResearchPipeline:
    def _sha256(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def __init__(self, client: genai.Client, agentdna_api_key: Optional[str] = None):
        self.client = client
        self.last_trace_url: str | None = None

        self._coordinator_dna: Optional[AgentDNA] = None  # type: ignore
        self._researcher_dnas: list[AgentDNA] = []        # type: ignore
        self._synthesizer_dna: Optional[AgentDNA] = None  # type: ignore

        if _AGENTDNA_AVAILABLE and agentdna_api_key:
            try:
                self._coordinator_dna = AgentDNA(
                    alias="coordinator",
                    api_key=agentdna_api_key,
                    role="host",
                )

                self._researcher_dnas = [
                    AgentDNA(
                        alias=f"researcher_{i}",
                        api_key=agentdna_api_key,
                        role="remote",
                    )
                    for i in range(1, 4)
                ]

                self._synthesizer_dna = AgentDNA(
                    alias="synthesizer",
                    api_key=agentdna_api_key,
                    role="remote",
                )

            except Exception as exc:
                print(f"[AgentDNA] init failed, running without trust layer: {exc}")
                self._coordinator_dna = None
                self._researcher_dnas = []
                self._synthesizer_dna = None

    @property
    def _dna_active(self) -> bool:
        return (
            self._coordinator_dna is not None
            and len(self._researcher_dnas) == 3
            and self._synthesizer_dna is not None
        )

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _call(self, system: str, prompt: str, model: str = "gemini-2.5-flash") -> str:
        response = self.client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction=system),
        )
        return response.text or ""

    def _json(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)

    # ── AgentDNA trust-layer helpers ────────────────────────────────────────

    @observe(name="agentdna.host.build")
    def _host_build(self, message: str) -> dict:
        return self._coordinator_dna.build(  # type: ignore
            original_message=message,
            state={"channel": "research_agent"},
        )

    @observe(name="agentdna.remote.verify")
    def _remote_verify(self, dna, host_json: str) -> dict:
        info = self._run(
            dna.handle(
                raw_text=host_json,
                verify_mode="light",
            )
        )

        trust_issues = info.get("trust_issues") or []
        if trust_issues:
            raise RuntimeError(f"AgentDNA host verification failed: {trust_issues}")

        if not info.get("host_block"):
            raise RuntimeError("AgentDNA host verification failed: missing host_block")

        return info

    @observe(name="agentdna.remote.build")
    def _remote_build(
        self,
        dna,
        original_message: str,
        response: str,
        host_block: dict,
        extra: Optional[dict] = None,
    ) -> str:
        built = dna.build(
            original_message=original_message,
            response=response,
            host_block=host_block,
            extra=extra or {},
        )

        combined_json = built.get("combined_json")
        if not combined_json:
            raise RuntimeError("AgentDNA remote build failed: missing combined_json")

        return combined_json

    @observe(name="agentdna.host.verify")
    def _host_verify(
        self,
        combined_json: str,
        original_task: str,
        remote_name: str,
        execute_nft: bool = False,
    ) -> dict:
        result = self._run(
            self._coordinator_dna.handle(
                resp_parts=[{"text": combined_json}],
                original_task=original_task,
                remote_name=remote_name,
                execute_nft=execute_nft,
            )
        )

        trust_issues = result.get("trust_issues") or []
        if trust_issues:
            raise RuntimeError(f"AgentDNA remote verification failed: {trust_issues}")

        return result

    # ── Pipeline stages ─────────────────────────────────────────────────────

    @observe(name="coordinator")
    def _plan_subtopics(self, question: str) -> list[str]:
        raw = self._call(
            COORDINATOR_SYSTEM,
            f"Research question: {question}",
        )

        try:
            subtopics = json.loads(raw.strip())
            if isinstance(subtopics, list) and len(subtopics) >= 3:
                return [str(s) for s in subtopics[:3]]
        except Exception:
            pass

        lines = [
            line.strip().lstrip("-•123. ").strip('"')
            for line in raw.splitlines()
            if line.strip()
        ]

        subtopics = lines[:3]

        if len(subtopics) != 3:
            raise RuntimeError(f"Coordinator failed to produce 3 subtopics. Raw output: {raw}")

        return subtopics

    @observe(name="researcher")
    def _research_subtopic(self, index: int, subtopic: str, question: str) -> str:
        researcher_name = f"researcher_{index + 1}"

        host_message = {
            "task_type": "research_subtopic",
            "researcher": researcher_name,
            "question": question,
            "subtopic": subtopic,
        }

        signed_task = self._json(host_message)
        original_message = signed_task
        host_block = None

        if self._dna_active:
            built = self._host_build(signed_task)
            host_json = built.get("host_json", "")

            verify_info = self._remote_verify(
                self._researcher_dnas[index],
                host_json,
            )

            original_message = verify_info.get("original_message") or signed_task
            host_block = verify_info.get("host_block")

            verified_payload = json.loads(original_message)
            question = verified_payload["question"]
            subtopic = verified_payload["subtopic"]

        prompt = f"""
Overall research question:
{question}

Assigned subtopic:
{subtopic}
"""

        response = self._call(RESEARCHER_SYSTEM, prompt)

        if self._dna_active and host_block is not None:
            combined_json = self._remote_build(
                self._researcher_dnas[index],
                original_message=original_message,
                response=response,
                host_block=host_block,
                extra={"agent_role": researcher_name},
            )

            self._host_verify(
                combined_json=combined_json,
                original_task=signed_task,
                remote_name=researcher_name,
                execute_nft=False,
            )

        return response

    @observe(name="synthesizer")
    def _synthesize(self, question: str, subtopics: list[str], findings: list[str]) -> str:
        finding_hashes = [
            {
                "subtopic": subtopic,
                "sha256": self._sha256(finding),
            }
            for subtopic, finding in zip(subtopics, findings)
        ]

        host_message = {
            "task_type": "synthesize_research",
            "question": question,
            "subtopics": subtopics,
            "finding_hashes": finding_hashes,
        }

        signed_task = self._json(host_message)
        original_message = signed_task
        host_block = None

        if self._dna_active:
            built = self._host_build(signed_task)
            host_json = built.get("host_json", "")

            verify_info = self._remote_verify(
                self._synthesizer_dna,
                host_json,
            )

            original_message = verify_info.get("original_message") or signed_task
            host_block = verify_info.get("host_block")

            verified_payload = json.loads(original_message)
            question = verified_payload["question"]
            subtopics = verified_payload["subtopics"]

        sections = "\n\n".join(
            f"### Researcher findings on: {subtopic}\n{finding}"
            for subtopic, finding in zip(subtopics, findings)
        )

        prompt = f"""
    Research question:
    {question}

    Specialist findings:
    {sections}
    """

        synthesis = self._call(SYNTHESIZER_SYSTEM, prompt)

        if self._dna_active and host_block is not None:
            synthesis_hash = self._sha256(synthesis)

            audit_response = self._json({
                "ok": True,
                "agent_role": "synthesizer",
                "output_type": "final_research_report",
                "synthesis_sha256": synthesis_hash,
                "finding_hashes": finding_hashes,
            })

            combined_json = self._remote_build(
                self._synthesizer_dna,
                original_message=original_message,
                response=audit_response,
                host_block=host_block,
                extra={
                    "agent_role": "synthesizer",
                    "synthesis_sha256": synthesis_hash,
                },
            )

            self._host_verify(
                combined_json=combined_json,
                original_task=signed_task,
                remote_name="synthesizer",
                execute_nft=True,
            )

        return synthesis

    @observe(name="research-assistant")
    def research(self, question: str) -> dict:
        subtopics = self._plan_subtopics(question)

        findings = [
            self._research_subtopic(i, subtopic, question)
            for i, subtopic in enumerate(subtopics)
        ]

        synthesis = self._synthesize(question, subtopics, findings)

        trace_id = get_client().get_current_trace_id()
        self.last_trace_url = get_client().get_trace_url(trace_id=trace_id)

        return {
            "subtopics": subtopics,
            "findings": findings,
            "synthesis": synthesis,
        }