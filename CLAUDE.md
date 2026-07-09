# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`agent-dna` is a Python library (published to PyPI as `agent-dna`) providing a security, governance, and audit framework for multi-agent AI systems. It lets every participant in a workflow (human, agent, or app) cryptographically sign the action they perform, so a completed workflow can be independently verified and audited back to the original requester.

The `agentdna/` package is the shippable library. `examples/github_agent_workflow/` is a standalone LangGraph + MCP demo app with its own deps and README — not part of the published package.

## Environment & commands

- Python `>=3.10,<3.13`. There is **no test suite, Makefile, linter config, or CI** in this repo — do not assume `pytest`/`make`/`ruff` targets exist.
- Install for development: `pip install -e .`
- CBAC (`agentdna/cbac.py`) requires optional ML deps that are **not** in the base install: `pip install -e ".[semantic]"` (pulls `sentence-transformers`, `scipy`, `numpy`). The rest of the library works without them; only import `cbac` when these are present.
- Build: `python -m build` (setuptools backend).
- Bump the version in `pyproject.toml` (`[project].version`) when releasing.

## The Provenance Layer is a Rubix blockchain node

The single most important external dependency to understand: `agentdna/provenance.py` wraps `rubix-py` (`RubixClient`, `Signer`, `Querier`). The "Provenance Layer" is a Rubix node (default `https://chain-connector-2.rubix.net`), and **Cards are NFTs**:
- `create_new_provenance_card` → `deploy_nft`
- `append_to_provenance_card` → `execute_nft` (Cards are append-only logs; the newest NFT state wins)
- `create_new_child_provenance_card` → `create_child_nft` (workflow provenance is a child of the user's card)
- `provenance_card_history` → `get_nft_states` (full append history — used to read policy versions)

Actor identity (`provenance_id`) is the Rubix DID from the `Signer`. Signing keys are managed by `rubix-py` under the config dir (`~/.agentdna` by default) keyed by the actor `name` (the `Signer` alias).

## Core architecture

Public API lives in `agentdna/core.py` as the `AgentDNA` class. Note `agentdna/__init__.py` is **empty** — despite the README's `from agentdna import AgentDNA`, code imports from `agentdna.core`.

The workflow lifecycle every participant follows: **`handle()` (verify inbound) → do work → `build()` (append signed envelope) → forward**. Only a `human` actor calls `create_workflow_provenance()` to commit the finished workflow.

Data model (`agentdna/types.py`, all dataclasses):
- `Actor` — id / name / type (`human` | `agent` | `app`).
- `Envelope` — one signed interaction. `parent_envelope` points to the previous envelope, so a workflow is a **nested chain**, not a list. The field is `from_` with dataclass metadata alias `"from"`.
- `IntentWorkflow` — holds only the *latest* `Envelope`; the whole chain is reconstructed by walking `parent_envelope`.
- `AgentCard` / `UserCard` — on-chain identity records; `AgentCard.policy` is a base64-encoded policy document.

Module map:
- `core.py` — `AgentDNA`: registration, card creation, `build`/`handle`/`create_workflow_provenance`, policy updates.
- `provenance.py` — `Provenance`: all Rubix/NFT operations + envelope sign/verify.
- `verifier.py` — `verify_light` (latest envelope only, `verification_mode="light"`) vs `verify_heavy` (entire chain, `"heavy"`). Both return `VerificationResult` with a list of `Issue`s.
- `helpers.py` — envelope canonicalization + chain traversal (`unwrap_workflow`, `parse_workflow`, `parse_envelope`).
- `id.py` — deterministic Card IDs: `CIDv0` over `sha256(actor_id)`.
- `card.py` — card payload builders.
- `config.py` — local actor registry at `~/.agentdna/actor_info.json` (maps actor DID → cached card id, so cards aren't re-created).
- `cbac.py` — Context-Based Access Control (see below), an independent optional subsystem.

## Critical invariant: envelope canonicalization

`canonicalize_envelope` / `_envelope_to_dict` in `agentdna/helpers.py` defines the exact bytes that get signed and verified. Signatures cover `from_`, `to`, `payload`, `metadata`, the current signature (when present), **and recursively every parent envelope's signature** — this is what chains the attestations together. Any change to this serialization (field order, which fields are included, JSON separators, `sort_keys`) **invalidates every existing signature on-chain**. Treat it as a wire format; do not modify casually.

Verification (`Provenance.verify_envelope`) calls `rubix.did.online_signature_verify` against the Rubix node — it is a **network call**, and it verifies each envelope against the DID in that envelope's `from_.id`.

## CBAC (agentdna/cbac.py)

A separate three-tier semantic authorization pipeline that decides whether an agent's intended action is permitted by its on-chain policy. Fetches the latest policy from the Provenance Layer, flattens it to text chunks (YAML frontmatter of a `skill.md` + body), and runs:
1. **Tier 1** — cosine gap between allowed vs forbidden policy chunks (with an optional Check-1 NLI drift test against the root user intent).
2. **Tier 2** — NLI entailment against the top allowed chunk.
3. **Tier 3** — optional LLM backend; if none is configured the result is `"advise"` and the caller decides.

Decisions are `"allow" | "deny" | "advise"` and the pipeline is **fail-closed** (any error → `deny`). Policy embeddings are precomputed and cached as pickles under `~/.agentdna/embeddings_cache/`, keyed by policy hash so an on-chain policy update triggers recompute. `authorise_agent_app_interaction` is a different path that delegates the decision to a remote CBAC service (`cbac_url`, default `https://cbac-admin.agentdna.io`).

