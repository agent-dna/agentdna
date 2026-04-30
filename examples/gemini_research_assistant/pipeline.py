from __future__ import annotations

import asyncio
import json
from typing import Optional, Any

# Rubix verify-signature uses a GET request with the payload in the URL.
# Keep signed response text under this limit to avoid 414 errors.
_MAX_SIGN_RESPONSE_LEN = 1200

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
    def __init__(self, client: genai.Client, agentdna_api_key: Optional[str] = None):
        self.client = client
        self.last_trace_url: str | None = None
        self.last_nft_token: str | None = None

        self._coordinator_dna: Optional[Any] = None
        self._researcher_dnas: list[Any] = []
        self._synthesizer_dna: Optional[Any] = None

        # Per-run internal state (reset each research() call)
        # These hold the FULL cryptographic payloads needed for the trust chain,
        # but are NOT returned from @observe methods (so they never appear in Langfuse).
        self._host_built: list[dict] = []       # Full built dicts from coordinator
        self._last_verify: dict = {}            # Full verify info from last remote verify
        self._combined_jsons: list[str] = []    # combined_json from each remote build

        if _AGENTDNA_AVAILABLE and agentdna_api_key:
            try:
                self._coordinator_dna = AgentDNA(
                    alias="Research_Head_Coordinator",
                    api_key=agentdna_api_key,
                    role="host",
                )
                self._researcher_dnas = [
                    AgentDNA(
                        alias=f"Sub_Theory_Researcher_{i}",
                        api_key=agentdna_api_key,
                        role="remote",
                    )
                    for i in range(1, 4)
                ]
                self._synthesizer_dna = AgentDNA(
                    alias="Research_Result_Synthesizer",
                    api_key=agentdna_api_key,
                    role="remote",
                )
                self.last_nft_token = getattr(
                    getattr(self._coordinator_dna, "handler", None), "nft_token", None
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
        return self.client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction=system),
        ).text or ""

    def _json(self, obj: dict) -> str:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False)

    def _parse_subtopics(self, raw: str) -> list[str]:
        try:
            parsed = json.loads(raw.strip())
            if isinstance(parsed, list) and len(parsed) >= 3:
                return [str(s) for s in parsed[:3]]
        except Exception:
            pass
        lines = [
            line.strip().lstrip("-•123. ").strip('"')
            for line in raw.splitlines()
            if line.strip()
        ]
        result = [l for l in lines if l][:3]
        if len(result) != 3:
            raise RuntimeError(f"Coordinator failed to produce 3 subtopics. Raw: {raw}")
        return result

    # ── AgentDNA trust spans ─────────────────────────────────────────────────
    # Each @observe method returns ONLY sanitized metadata to Langfuse.
    # Full cryptographic payloads are stored in self._host_built / self._last_verify
    # / self._combined_jsons and never appear in traces.

    @observe(name="agentdna.host.build")
    def _host_build(self, task_json: str) -> dict:
        """Coordinator signs an outbound task. Logged output: metadata only."""
        result = self._coordinator_dna.build(  # type: ignore[union-attr]
            original_message=task_json,
            state={"channel": "research_pipeline"},
        )
        # Store full payload + original task_json so _host_verify_all can use the
        # exact same original_message string that was signed (avoids mismatch warning).
        self._host_built.append({**result, "_task_json": task_json})
        return {                          # Sanitized output logged to Langfuse
            "message_id": result.get("message_id"),
            "context_id": result.get("context_id"),
            "signed": True,
        }

    @observe(name="agentdna.remote.verify")
    def _remote_verify(self, dna, host_json: str) -> dict:
        """Remote agent verifies coordinator's signed envelope. Logged output: metadata only."""
        info = self._run(dna.handle(raw_text=host_json, verify_mode="light"))
        trust_issues = info.get("trust_issues") or []
        if trust_issues:
            raise RuntimeError(f"Host verification failed: {trust_issues}")
        if not info.get("host_block"):
            raise RuntimeError("Host verification failed: missing host_block")
        self._last_verify = info           # Full payload stored internally
        return {                           # Sanitized output logged to Langfuse
            "verified": True,
            "trust_issues": [],
        }

    @observe(name="agentdna.remote.build")
    def _remote_build(self, dna, original_message: str, response: str, host_block: dict) -> dict:
        """Remote agent signs its response. Logged output: metadata only.

        The Rubix verify-signature endpoint is a GET request — long responses cause
        a 414 URI Too Large error.  We cap the signed response text at
        _MAX_SIGN_RESPONSE_LEN characters; the full response is always returned to
        the caller and shown in the UI.
        """
        response_for_signing = (
            response[:_MAX_SIGN_RESPONSE_LEN] + " ...[truncated for signing]"
            if len(response) > _MAX_SIGN_RESPONSE_LEN
            else response
        )
        built = dna.build(
            original_message=original_message,
            response=response_for_signing,
            host_block=host_block,
            extra={},
        )
        combined = built.get("combined_json", "")
        if not combined:
            raise RuntimeError("Remote build failed: missing combined_json")
        self._combined_jsons.append(combined)  # Full payload stored internally
        return {                                # Sanitized output logged to Langfuse
            "signed": True,
            "agent_did": getattr(getattr(dna, "trust", None), "did", "unknown"),
        }

    @observe(name="agentdna.host.verify")
    def _host_verify_all(self, question: str) -> dict:
        """
        Coordinator verifies every agent response.
        execute_nft=True ONCE — only on the final (synthesizer) combined_json.
        Nobody else writes to chain.
        """
        if not self._combined_jsons:
            return {"verified_count": 0, "nft_executed": False}

        all_trust_issues: list[str] = []

        # remote_names: researcher_1/2/3 + synthesizer (last)
        remote_names = (
            [f"researcher_{i+1}" for i in range(len(self._combined_jsons) - 1)]
            + ["synthesizer"]
        )

        # Verify researcher responses — NO chain write
        for i, combined_json in enumerate(self._combined_jsons[:-1]):
            # Pass the exact task_json that was signed for this agent so that the
            # handler's original_message comparison passes without a mismatch warning.
            original_task = (
                self._host_built[i].get("_task_json", question)
                if i < len(self._host_built) else question
            )
            result = self._run(
                self._coordinator_dna.handle(  # type: ignore[union-attr]
                    resp_parts=[{"text": combined_json}],
                    original_task=original_task,
                    remote_name=remote_names[i],
                    execute_nft=False,
                )
            )
            all_trust_issues.extend(result.get("trust_issues") or [])

        # Verify synthesizer response + write to chain ONCE
        synth_idx = len(self._combined_jsons) - 1
        synth_original_task = (
            self._host_built[synth_idx].get("_task_json", question)
            if synth_idx < len(self._host_built) else question
        )
        final_result = self._run(
            self._coordinator_dna.handle(  # type: ignore[union-attr]
                resp_parts=[{"text": self._combined_jsons[-1]}],
                original_task=synth_original_task,
                remote_name="synthesizer",
                execute_nft=True,
            )
        )
        all_trust_issues.extend(final_result.get("trust_issues") or [])
        nft_result = final_result.get("nft_result")

        return {                          # Sanitized output logged to Langfuse
            "verified_count": len(self._combined_jsons),
            "trust_issues": all_trust_issues,
            "nft_executed": nft_result is not None,
            "nft_status": (
                nft_result.get("message") if isinstance(nft_result, dict) else str(nft_result)
            ) if nft_result else None,
        }

    # ── Pipeline stages ──────────────────────────────────────────────────────

    @observe(name="coordinator")
    def _plan_and_dispatch(self, question: str) -> list[str]:
        """
        Coordinator: (1) plans 3 subtopics via Gemini,
                     (2) signs one task envelope per researcher → agentdna.host.build × 3
        Returns subtopics list; signed envelopes are stored in self._host_built.
        """
        raw = self._call(COORDINATOR_SYSTEM, f"Research question: {question}")
        subtopics = self._parse_subtopics(raw)

        if self._dna_active:
            for i, subtopic in enumerate(subtopics):
                task = self._json({
                    "task_type": "research",
                    "researcher": f"researcher_{i + 1}",
                    "question": question,
                    "subtopic": subtopic,
                })
                self._host_build(task)  # agentdna.host.build span (child of coordinator)

        return subtopics

    @observe(name="researcher")
    def _research_subtopic(self, index: int, subtopic: str, question: str) -> str:
        """
        Researcher: (1) verifies coordinator's signed envelope,
                    (2) generates research via Gemini,
                    (3) signs response.
        """
        researcher_dna = self._researcher_dnas[index]
        original_message = subtopic
        host_block = None

        if self._dna_active and index < len(self._host_built):
            host_json: str = self._host_built[index].get("host_json", "")

            self._remote_verify(researcher_dna, host_json)  # agentdna.remote.verify span
            info = self._last_verify
            original_message = info.get("original_message") or subtopic
            host_block = info.get("host_block")

        # Extract subtopic/question from the signed task JSON
        try:
            payload = json.loads(original_message)
            subtopic_text = payload.get("subtopic", subtopic)
            question_text = payload.get("question", question)
        except Exception:
            subtopic_text = subtopic
            question_text = question

        response = self._call(
            RESEARCHER_SYSTEM,
            f"Overall research question: {question_text}\n\nYour subtopic: {subtopic_text}",
        )

        if self._dna_active and host_block is not None:
            self._remote_build(  # agentdna.remote.build span
                researcher_dna, original_message, response, host_block
            )

        return response

    @observe(name="synthesizer")
    def _synthesize(self, question: str, subtopics: list[str], findings: list[str]) -> str:
        """
        Synthesizer: (1) coordinator signs synthesis task → agentdna.host.build,
                     (2) synthesizer verifies,
                     (3) synthesizes via Gemini,
                     (4) synthesizer signs response.
        """
        original_message = question
        host_block = None

        if self._dna_active:
            # Coordinator signs the synthesis dispatch (host.build inside synthesizer span)
            task = self._json({
                "task_type": "synthesize",
                "question": question,
                "subtopics": subtopics,
            })
            self._host_build(task)          # agentdna.host.build span
            synth_envelope = self._host_built[-1]
            host_json: str = synth_envelope.get("host_json", "")

            self._remote_verify(self._synthesizer_dna, host_json)  # agentdna.remote.verify span
            info = self._last_verify
            original_message = info.get("original_message") or task
            host_block = info.get("host_block")

        # Extract question from signed task
        try:
            payload = json.loads(original_message)
            question_text = payload.get("question", question)
        except Exception:
            question_text = question

        sections = "\n\n".join(
            f"### Researcher findings on: {sub}\n{finding}"
            for sub, finding in zip(subtopics, findings)
        )
        synthesis = self._call(
            SYNTHESIZER_SYSTEM,
            f"Research question: {question_text}\n\nFindings:\n\n{sections}",
        )

        if self._dna_active and host_block is not None:
            self._remote_build(  # agentdna.remote.build span
                self._synthesizer_dna, original_message, synthesis, host_block
            )

        return synthesis

    # ── Top-level orchestration ───────────────────────────────────────────────

    @observe(name="research-assistant")
    def research(self, question: str) -> dict:
        """Full pipeline: coordinator → researchers → synthesizer → coordinator verifies + NFT."""
        # Reset per-run state
        self._host_built = []
        self._last_verify = {}
        self._combined_jsons = []

        # 1. Coordinator: plan subtopics + sign tasks for researchers
        subtopics = self._plan_and_dispatch(question)

        # 2. Researchers: verify → research → sign (using pre-built envelopes from coordinator)
        findings = [
            self._research_subtopic(i, subtopic, question)
            for i, subtopic in enumerate(subtopics)
        ]

        # 3. Synthesizer: coordinator signs task → synthesizer verifies → synthesize → sign
        synthesis = self._synthesize(question, subtopics, findings)

        # 4. Coordinator: verify ALL responses + write to chain ONCE (execute_nft=True)
        if self._dna_active:
            self._host_verify_all(question)  # agentdna.host.verify span

        trace_id = get_client().get_current_trace_id()
        self.last_trace_url = get_client().get_trace_url(trace_id=trace_id)

        return {"subtopics": subtopics, "findings": findings, "synthesis": synthesis}
