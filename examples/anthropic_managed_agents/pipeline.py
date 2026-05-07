from __future__ import annotations

from anthropic import Anthropic
from langfuse import observe, get_client

SUMMARIZER_SYSTEM = """You are a specialist document summarizer.

When given a document, produce a structured summary with these sections:

## Overview
2-3 sentence executive summary.

## Key Points
The 5-7 most important points as a bullet list.

## Topics Covered
Main themes and subjects.

## Document Structure
Format, length, and organization.

## Audience & Purpose
Who this is written for and what it aims to accomplish.

Use only information from the document. Be concise and precise."""

FACT_CHECKER_SYSTEM = """You are a specialist fact-checker and claim analyzer.

When given a document, analyze its factual claims and produce a report with these sections:

## Key Claims
List the 5-10 most significant factual claims made in the document.

## Claim Assessment
For each claim, classify it as one of:
- **Verifiable** — can be checked against external sources
- **Self-referential** — the document supports itself
- **Opinion/Subjective** — not a factual claim
- **Potentially misleading** — seems exaggerated, unsourced, or inconsistent

## Red Flags
Note any claims that seem problematic or require scrutiny.

## Reliability Verdict
A brief overall assessment of the document's factual reliability.

Be analytical and neutral."""

COORDINATOR_SYSTEM = """You are a document analysis coordinator.

You will receive a summary and a fact-check report produced by two specialist agents.
Combine them into a final analysis report with these sections:

## Summary
(from the summarizer)

## Fact-Check Report
(from the fact checker)

## Overall Assessment
Your synthesis: key takeaways and how well-supported the document's claims are."""


class DocumentAnalysisPipeline:
    def __init__(self, client: Anthropic):
        self.client = client
        self.last_trace_url: str | None = None

    def setup(self) -> None:
        pass  # no setup needed for Messages API

    @observe(name="summarizer-agent")
    def _summarize(self, document: str) -> str:
        resp = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=SUMMARIZER_SYSTEM,
            messages=[{"role": "user", "content": f"Summarize this document:\n\n{document}"}],
        )
        return resp.content[0].text

    @observe(name="fact-checker-agent")
    def _fact_check(self, document: str) -> str:
        resp = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=FACT_CHECKER_SYSTEM,
            messages=[{"role": "user", "content": f"Fact-check this document:\n\n{document}"}],
        )
        return resp.content[0].text

    @observe(name="coordinator-agent")
    def _synthesize(self, summary: str, fact_check: str) -> str:
        prompt = (
            f"Summary from the Summarizer agent:\n{summary}\n\n"
            f"Fact-check from the Fact Checker agent:\n{fact_check}\n\n"
            "Combine these into a final analysis report."
        )
        resp = self.client.messages.create(
            model="claude-opus-4-7",
            max_tokens=2048,
            system=COORDINATOR_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text

    @observe(name="document-analysis")
    def analyze(self, document: str) -> dict:
        """Run the full pipeline. Returns summary, fact_check, and synthesis."""
        summary = self._summarize(document)
        fact_check = self._fact_check(document)
        synthesis = self._synthesize(summary, fact_check)
        trace_id = get_client().get_current_trace_id()
        self.last_trace_url = get_client().get_trace_url(trace_id=trace_id)
        return {"summary": summary, "fact_check": fact_check, "synthesis": synthesis}
