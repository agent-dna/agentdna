from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Optional, Any

# The Rubix verify-signature endpoint is a GET, so long signed payloads cause
# 414 URI Too Long. The synthesizer's signed envelope ends up carrying nested
# digests of the 3 researcher replies, so we cap each signed reply below the
# limit; the full unsigned response is still shown to the user and traced.
_MAX_SIGN_RESPONSE_LEN = 600


def _compact_research_envelope(combined_json: str) -> dict:
    """
    Compact a researcher's signed envelope for nesting inside the synthesizer's
    signed payload. Keeps the DID, the signature, and a sha256 of the full
    response — enough for cryptographic commitment without overflowing the
    Rubix verify URL.
    """
    try:
        obj = json.loads(combined_json)
    except Exception:
        return {"error": "unparseable envelope"}
    agent_block = obj.get("agent", {}) or {}
    envelope = agent_block.get("envelope", {}) or {}
    response = envelope.get("response", "") or ""
    try:
        original = json.loads(envelope.get("original_message", "") or "")
    except Exception:
        original = {}
    return {
        "agent_did": agent_block.get("agent"),
        "researcher": original.get("researcher"),
        "subtopic": original.get("subtopic"),
        "response_sha256": hashlib.sha256(response.encode("utf-8")).hexdigest(),
        "signature": agent_block.get("signature"),
    }


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
        self._host_built: list[Any] = []        # SignedEnvelope per coordinator-signed task
        self._combined_jsons: list[str] = []    # wire string per remote-signed reply

        if _AGENTDNA_AVAILABLE and agentdna_api_key:
            try:
                # Coordinator is the host — it writes audit-log NFTs per run.
                self._coordinator_dna = AgentDNA(
                    alias="Research_Head_Coordinator",
                    api_key=agentdna_api_key,
                )
                # Researchers + synthesizer are pure remotes — never write to chain.
                self._researcher_dnas = [
                    AgentDNA(
                        alias=f"Sub_Theory_Researcher_{i}",
                        api_key=agentdna_api_key,
                        enable_nft=False,
                    )
                    for i in range(1, 4)
                ]
                self._synthesizer_dna = AgentDNA(
                    alias="Research_Result_Synthesizer",
                    api_key=agentdna_api_key,
                    enable_nft=False,
                )
                self.last_nft_token = self._coordinator_dna.nft_token
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

    def history(self) -> list[dict]:
        """Decoded NFT chain history for the coordinator's audit-log NFT."""
        if self._coordinator_dna is None:
            return []
        return self._coordinator_dna.history()

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

    def _sign_truncated(self, dna, response: str, ctx, extra: dict | None = None) -> str:
        """
        Truncate ``response`` to fit Rubix's GET signature-verify URL limit
        (~414), then sign under ``ctx``. The full unsigned response is still
        returned to the UI / Langfuse trace untouched — only the signed copy
        is truncated.
        """
        response_for_signing = (
            response[:_MAX_SIGN_RESPONSE_LEN] + " ...[truncated for signing]"
            if len(response) > _MAX_SIGN_RESPONSE_LEN
            else response
        )
        return dna.build(response_for_signing, ctx=ctx, extra=extra or {})

    # ── Pipeline stages ──────────────────────────────────────────────────────

    @observe(name="coordinator")
    def _plan_and_dispatch(self, question: str) -> list[str]:
        """Coordinator plans 3 subtopics + signs one task envelope per researcher."""
        raw = self._call(COORDINATOR_SYSTEM, f"Research question: {question}")
        subtopics = self._parse_subtopics(raw)

        if self._dna_active:
            for i, subtopic in enumerate(subtopics):
                task_json = self._json({
                    "task_type": "research",
                    "researcher": f"researcher_{i + 1}",
                    "question": question,
                    "subtopic": subtopic,
                })
                env = self._coordinator_dna.build(task_json)
                self._host_built.append(env)

        return subtopics

    @observe(name="researcher")
    def _research_subtopic(self, index: int, subtopic: str, question: str) -> str:
        """Researcher verifies the coordinator's signed envelope → researches → signs."""
        researcher_dna = self._researcher_dnas[index]
        ctx = None

        if self._dna_active and index < len(self._host_built):
            env = self._host_built[index]
            ctx = self._run(researcher_dna.handle(env))
            if not ctx.verified:
                raise RuntimeError(f"Host verification failed: {ctx.trust_issues}")

        # Pull subtopic/question out of the signed task JSON (or fall back).
        signed_task = ctx.original_message if ctx else ""
        try:
            payload = json.loads(signed_task) if signed_task else {}
            subtopic_text = payload.get("subtopic", subtopic)
            question_text = payload.get("question", question)
        except Exception:
            subtopic_text, question_text = subtopic, question

        response = self._call(
            RESEARCHER_SYSTEM,
            f"Overall research question: {question_text}\n\nYour subtopic: {subtopic_text}",
        )

        if ctx is not None:
            combined = self._sign_truncated(researcher_dna, response, ctx)
            self._combined_jsons.append(combined)

        return response

    @observe(name="synthesizer")
    def _synthesize(self, question: str, subtopics: list[str], findings: list[str]) -> str:
        """Coordinator signs synthesis task → synthesizer verifies → synthesizes → signs (with researcher digests nested in extra)."""
        ctx = None

        if self._dna_active:
            task_json = self._json({
                "task_type": "synthesize",
                "question": question,
                "subtopics": subtopics,
            })
            env = self._coordinator_dna.build(task_json)
            self._host_built.append(env)

            ctx = self._run(self._synthesizer_dna.handle(env))
            if not ctx.verified:
                raise RuntimeError(f"Host verification failed: {ctx.trust_issues}")

        signed_task = ctx.original_message if ctx else ""
        try:
            payload = json.loads(signed_task) if signed_task else {}
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

        if ctx is not None:
            # Compact summaries of the 3 researcher envelopes — keeps DID +
            # signature + response_sha256 so the chain block cryptographically
            # commits to each researcher's exact output without overflowing the
            # Rubix verify URL. (Same shape as `extra=` everywhere else.)
            researcher_summaries = [
                _compact_research_envelope(c) for c in self._combined_jsons[:3]
            ]
            combined = self._sign_truncated(
                self._synthesizer_dna,
                synthesis,
                ctx,
                extra={"researcher_envelopes": researcher_summaries},
            )
            self._combined_jsons.append(combined)

        return synthesis

    # ── Top-level orchestration ───────────────────────────────────────────────

    @observe(name="research-assistant")
    def research(self, question: str) -> dict:
        """Full pipeline: coordinator → researchers → synthesizer → coordinator verifies + NFT."""
        # Reset per-run state
        self._host_built = []
        self._combined_jsons = []

        subtopics = self._plan_and_dispatch(question)

        findings = [
            self._research_subtopic(i, subtopic, question)
            for i, subtopic in enumerate(subtopics)
        ]

        synthesis = self._synthesize(question, subtopics, findings)

        # Coordinator verifies every signed reply; only the last triggers an NFT
        # write so the synthesizer's envelope (with nested researcher digests in
        # `extra`) is what lands on chain — one record per run.
        if self._dna_active and self._combined_jsons:
            remote_names = (
                [f"researcher_{i+1}" for i in range(len(self._combined_jsons) - 1)]
                + ["synthesizer"]
            )
            for i, combined_json in enumerate(self._combined_jsons):
                is_last = i == len(self._combined_jsons) - 1
                self._run(self._coordinator_dna.handle(
                    combined_json,
                    original=self._host_built[i],
                    remote_name=remote_names[i],
                    execute_nft=is_last,
                ))
            self.last_nft_token = self._coordinator_dna.nft_token

        trace_id = get_client().get_current_trace_id()
        self.last_trace_url = get_client().get_trace_url(trace_id=trace_id)

        return {"subtopics": subtopics, "findings": findings, "synthesis": synthesis}
