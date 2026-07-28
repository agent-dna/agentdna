# CLAUDE.md — cbac_service

Guidance for `cbac_service/`. The repo-root `CLAUDE.md` covers the `agent-dna`
library; this file covers only what's specific to the service.

## What this is

`cbac_service` is the **reference CBAC decision service** — a standalone FastAPI
app (`cbac_service/cbac.py`, `app = FastAPI()`) that the library calls over HTTP.
It is the server-side successor to the library's old in-process CBAC: `agentdna/`
imports **none** of the ML deps; all of it lives here.

- **Not published as a wheel** (`[tool.uv] package = false`). Deployed from a
  checkout: `uvicorn cbac_service.cbac:app`.
- One endpoint: **`POST /authorize-cbac`**.
- Depends on `agent-dna` (for `Provenance`, `AgentCard`, `IntentWorkflow`, `id`).

## Environment — separate from the library

This package has its **own `.venv` and its own `uv.lock`**. It is *not* a uv
workspace member, so `uv sync --all-packages` from the repo root does **not**
install it. Work from inside `cbac_service/`:

- `uv sync` — dev install. `[tool.uv.sources]` resolves `agent-dna` from the
  sibling checkout (`path = "..", editable`), so library edits are picked up live.
- `uv sync --no-sources` — deploy install, resolving `agent-dna` from PyPI.
- `uv run pytest` — runs `tests/` (its own `[tool.pytest.ini_options]`,
  `pythonpath = [".."]`). The root `pytest` does not reach these tests.
- `transformers` is pinned `<5` on purpose — HHEM-2.1's remote code
  (`hallucination_score`) breaks on transformers 5.x. Don't loosen it.

## The decision pipeline (`cbac.py`)

One file, class `CBAC`. On each request it fetches the agent's latest policy from
the Provenance Layer, flattens it to chunks (structure-aware: paragraphs / list
items, split past ~120 words), NLI-classifies chunks into allowed/forbidden, and
runs three tiers:

1. **Tier 1** — cosine gap `max_allowed − max_forbidden` (allow > +0.12,
   deny < −0.08, else escalate), with an optional Check-1 NLI drift test vs the
   root user intent.
2. **Tier 2** — NLI entailment vs the top allowed chunk (entail ≥ 0.55 allow,
   contradiction ≥ 0.60 deny).
3. **Tier 3** — optional LLM backend; absent → `"advise"`.

Decisions are `"allow" | "deny" | "advise"` and the pipeline is **fail-closed**
(any error → `deny`). Policy embeddings are cached as pickles under
`~/.agentdna/embeddings_cache/`, keyed by policy hash.

## Two things beyond the old library CBAC

- **Hallucination score (HHEM):** when a decision is reached with a user intent
  present, `CBAC.hallucination_score` (vectara HHEM model, 1 = grounded,
  0 = hallucinated) is attached to the result.
- **LHI trust:** `compute_lhi` combines four component scores
  (intent, policy, hallucination, output) as a weighted geometric mean
  (`_LHI_WEIGHTS = (0.3, 0.3, 0.2, 0.2)`), then folds it into a stored trust value
  via an **asymmetric EMA — slow to build (`λ_up = 0.95`), fast to lose
  (`λ_down = 0.70`)**. Any zero component zeroes the instantaneous score. Trust is
  tracked **per caller→callee edge** and persisted in `trust_scores.json` under
  the config dir. The LHI math is covered by `tests/test_cbac_lhi.py`; score
  attachment by `tests/test_cbac_verify.py` — keep both green when touching it.
