# CLAUDE.md — cbac_service

Guidance for `cbac_service/`. The repo-root `CLAUDE.md` covers the `agent-dna`
library; this file covers only what's specific to the service.

## What this is

`cbac_service` is the **reference CBAC decision service** — a standalone FastAPI
app that the library's guard calls over HTTP. All the ML deps live here;
`agentdna/` imports **none** of them.

- `main.py` — the HTTP boundary: `app = FastAPI()`, the lazy `CBAC` singleton,
  and the `__main__` uvicorn runner (`CBAC_SERVICE_HOST` / `CBAC_SERVICE_PORT`).
- `cbac.py` — the decision engine (class `CBAC`), no HTTP.
- `config.py` — pipeline tunables as module-level constants (`ALLOW_GAP`,
  `ENCODER_MODEL`, `LHI_WEIGHTS`, …). Change a value here and redeploy.
- `chunking.py` — structure-aware policy-text chunking (`chunk_body_text`).
- `skills.py` — `skill.md` parsing + the CBAC result dataclasses.
- **Not published as a wheel** (`[tool.uv] package = false`). Deployed from a
  checkout: `uvicorn cbac_service.main:app`.
- One endpoint: **`POST /authorize-cbac`**. Returns the reason as the body and
  the decision in the `X-CBAC-Decision` header.
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

Class `CBAC`. On each request it fetches the agent's latest policy from the
Provenance Layer, flattens it to chunks (structure-aware: paragraphs / list
items, split past `chunk_max_words`), NLI-classifies chunks into
allowed/forbidden, and runs three tiers:

1. **Tier 1** — cosine gap `max_allowed − max_forbidden` (allow > `allow_gap`,
   deny < −`deny_gap`, else escalate), with an optional Check-1 NLI drift test
   vs the root user intent.
2. **Tier 2** — NLI entailment vs the top allowed chunk (`entailment_threshold`
   → allow, `contradiction_threshold` → deny).
3. **Tier 3** — optional LLM backend; absent → `"advise"`.

Every threshold and model name above is a constant in `config.py` — read the
values from there, not from this file.

Decisions are `"allow" | "deny" | "advise"` and the pipeline is **fail-closed**
(any error → `deny`). Policy embeddings are cached as pickles under
`~/.agentdna/embeddings_cache/`, keyed by policy hash.

## Scoring attached to a decision

- **Hallucination score (HHEM):** when a decision is reached with a user intent
  present, `CBAC.hallucination_score` (vectara HHEM model, 1 = grounded,
  0 = hallucinated) is attached to the result.
- **LHI trust (Local Heuristic Intelligence):** `compute_lhi` combines four
  component scores (intent, policy, hallucination, output) as a **weighted
  arithmetic mean** (`lhi_weights`) — expected interaction quality, deliberately
  compensatory because the allow/deny gates already enforce the hard constraints
  pre-execution — then folds it into a stored trust value via an **asymmetric
  EMA — slow to build (`lhi_lambda_up`), fast to lose (`lhi_lambda_down`)**.
  (Not a geometric mean: the binary output score would zero the whole
  interaction on any transient tool failure.) Trust is tracked **per
  caller→callee edge** and persisted in `trust_store_file` under the config dir. The LHI math is covered by `tests/test_cbac_lhi.py`; score
  attachment by `tests/test_cbac_verify.py` — keep both green when touching it.
