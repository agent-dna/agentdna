# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`agent-dna` is a Python library (published to PyPI as `agent-dna`) providing a security, governance, and audit framework for multi-agent AI systems. It lets every participant in a workflow (human, agent, or app) cryptographically sign the action they perform, so a completed workflow can be independently verified and audited back to the original requester.

The `agentdna/` package is the shippable library. Two other trees live in this repo and are **not** part of the published package: `cbac_service/` (the deployable CBAC decision service) and `examples/github_agent_workflow/` (a standalone LangGraph + MCP demo app with its own deps and README).

## Environment & commands

- Python `>=3.10,<3.13`. Dependency + venv management is **`uv`**. The repo holds **two independent projects**, each with its own `pyproject.toml`, `uv.lock`, and `.venv`:
  - repo root = the `agent-dna` library. `uv sync`, `uv run pytest` (collects `tests/`). `pip install -e .` also works.
  - `cbac_service/` = a standalone deployable that depends on `agent-dna` **from PyPI**. `uv --directory cbac_service sync` / `run pytest`. Its `[tool.uv.sources]` resolves `agent-dna` from the sibling checkout **for local dev only**; deploy with `uv sync --no-sources` to use the published library.
- Lint / format / type-check (dev tooling, see [Working conventions](#working-conventions)): `uv run ruff format .`, `uv run ruff check .`, `uv run pyright`.
- The heavy ML deps (`sentence-transformers`, `scipy`, `numpy`, `transformers`) belong to **`cbac_service`**; `agentdna/` imports none of them. The library's only extra is `[mcp]` (`fastmcp`, `langchain-mcp-adapters`), needed by `agentdna/cbac/mcp.py`.
- Build: `python -m build` (setuptools backend).
- Bump the version in `pyproject.toml` (`[project].version`) when releasing.
- CI (`.github/workflows/`): `tests.yml` runs the library's `tests/` on every PR; `build.yml` and `release.yml` cover packaging. `cbac_service/tests/` is not in CI — run it locally.

## The Provenance Layer is a Rubix blockchain node

The single most important external dependency to understand: `agentdna/provenance.py` wraps `rubix-py` (`RubixClient`, `Signer`, `Querier`). The "Provenance Layer" is a Rubix node (default `https://chain-connector-2.rubix.net`), and **Cards are NFTs**:
- `create_new_provenance_card` → `deploy_nft`
- `append_to_provenance_card` → `execute_nft` (Cards are append-only logs; the newest NFT state wins)
- `create_new_child_provenance_card` → `create_child_nft` (workflow provenance is a child of the user's card)
- `provenance_card_history` → `get_nft_states` (full append history — used to read policy versions)

Actor identity (`provenance_id`) is the Rubix DID from the `Signer`. Signing keys are managed by `rubix-py` under the config dir (`~/.agentdna` by default) keyed by the actor `name` (the `Signer` alias).

## Core architecture

Public API lives in `agentdna/core.py` as the `AgentDNA` class, re-exported from `agentdna/__init__.py`, so both `from agentdna import AgentDNA` (as in the README) and `from agentdna.core import AgentDNA` work.

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
- `cbac/guard.py` — the framework-agnostic CBAC guard (see below): `cbac_guard`, `cbac_context`, `configure`. Re-exported from `agentdna/cbac/__init__.py`.
- `cbac/mcp.py` — MCP glue (`CBACMiddleware`, `intent_interceptor`); needs the `[mcp]` extra, so it is imported separately and `agentdna.cbac` does not pull in fastmcp.

## Critical invariant: envelope canonicalization

`canonicalize_envelope` / `_envelope_to_dict` in `agentdna/helpers.py` defines the exact bytes that get signed and verified. Signatures cover `from_`, `to`, `payload`, `metadata`, the current signature (when present), **and recursively every parent envelope's signature** — this is what chains the attestations together. Any change to this serialization (field order, which fields are included, JSON separators, `sort_keys`) **invalidates every existing signature on-chain**. Treat it as a wire format; do not modify casually.

Verification (`Provenance.verify_envelope`) calls `rubix.did.online_signature_verify` against the Rubix node — it is a **network call**, and it verifies each envelope against the DID in that envelope's `from_.id`.

## CBAC

Context-Based Access Control decides whether an agent's intended action is permitted by its on-chain policy. It splits across the two projects:

- **`agentdna/cbac/`** (in the library, no ML deps) — the guard. `cbac_guard` decorates a tool/function, `cbac_context` carries the governance context via `contextvars`, and authorization is a `requests` POST to a CBAC service (`cbac_url`, default `https://cbac-admin.agentdna.io`).
- **`cbac_service/`** (the deployable) — the decision engine + `FastAPI` app (`POST /authorize-cbac`). Decisions are `"allow" | "deny" | "advise"`, fail-closed.

Only the guard side is part of the library. For the decision engine's internals (the semantic pipeline, hallucination/trust scoring, config), see **`cbac_service/CLAUDE.md`** — don't duplicate them here.

## Working conventions

- **Don't add excessive comments.** The code is self-documenting; comment only non-obvious *why*, not *what*. (The existing `#TODO:-` question-comments are open notes, not a pattern to imitate.)
- **Don't read `.env` files** — they hold real secrets. `.env.sample` is a safe template and may be read.
- **Match the existing style.** Source under `agentdna/` is `ruff format`ted (`line-length = 100`, config in `pyproject.toml`); keep it that way and follow the existing snake_case + dataclass idioms.
- **Don't add dependencies without asking.** The library intentionally keeps a small dependency set; ML deps belong to `cbac_service`.
- **`agentdna/` is the shippable library.** `examples/github_agent_workflow/` and `cbac_service/` are separate apps. Editing any of them is fine — just never make the library import from or depend on either.

Dev tooling lives in the `dev` dependency group (`uv sync` installs it). The group also self-references `agent-dna[mcp]` so `agentdna/cbac/mcp.py`'s imports resolve for pyright; dependency groups are never published, so the wheel is unaffected. The tools: **`ruff`** (format + lint, config `[tool.ruff]`, applied repo-wide including `examples/`) and **`pyright`** (config `[tool.pyright]`, `basic` mode, scoped to `agentdna/` — `examples/` and `cbac_service/` are outside it because their heavy runtime deps aren't in the library's dev env). Keep `agentdna/` both `ruff format`-clean and pyright-clean.

