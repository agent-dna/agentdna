# CLAUDE.md — cbac_service

Guidance for `cbac_service/`. The repo-root `CLAUDE.md` covers the `agent-dna`
library; this file covers only what's specific to the service.

## What this is

`cbac_service` is the **reference CBAC decision service** — a standalone FastAPI
app that the library's guard calls over HTTP. All the ML deps live here;
`agentdna/` imports **none** of them.

- `main.py` — the HTTP boundary: `app = FastAPI()`, the lazy `CBAC` singleton,
  DB lifecycle via lifespan, and the `__main__` uvicorn runner.
- `cbac.py` — the decision engine (class `CBAC`), no HTTP.
- `config.py` — pipeline tunables as module-level constants (`ALLOW_GAP`,
  `ENCODER_MODEL`, `LHI_WEIGHTS`, `DATABASE_URL`, …). Change a value here and
  redeploy.
- `chunking.py` — structure-aware policy-text chunking (`chunk_body_text`).
- `skills.py` — `skill.md` parsing + the CBAC result dataclasses.
- **Not published as a wheel** (`[tool.uv] package = false`). Deployed from a
  checkout: `uvicorn cbac_service.main:app`.
- Endpoints:
  - **`POST /authorize-cbac`** — main decision gate.
  - **`POST /compute-lhi`** — fold interaction scores into trust.
  - **`POST /precompute-policy`** — explicitly trigger embedding precomputation.
  - **`GET /health`** — DB connectivity check.
- Depends on `agent-dna` (for `Provenance`, `AgentCard`, `IntentWorkflow`, `id`).

## Architecture

```
Guard (agentdna/cbac) --HTTP--> cbac_service (FastAPI)
                                     |
                                PostgreSQL 18
                                ├── pgvector 0.8.6 (semantic search)
                                ├── pg_textsearch 1.4.0 (BM25 keyword search)
                                ├── policy_chunks table (embeddings + text)
                                └── policy_meta table (cache invalidation)
```

## Database

The service uses **PostgreSQL 18** with two extensions:
- **pgvector** — stores 384-dim embeddings, HNSW index for cosine similarity.
- **pg_textsearch** — BM25 ranked keyword search on chunk text.

### Tables

**`policy_chunks`** — one row per chunk per agent:
- `agent_id` — who this belongs to
- `chunk_text` — the original text (needed for Tier 2 NLI)
- `chunk_type` — `allowed` or `forbidden`
- `embedding` — vector(384), the searchable embedding
- `policy_hash` — for cache invalidation
- `section` / `chunk_index` — ordering and provenance

**`policy_meta`** — one row per agent, lightweight cache check:
- `policy_hash` — compared against on-chain hash at runtime
- `encoder_model` / `nli_model` — detect if models changed
- `chunk_count` / `cached_at` — operational metadata

### Indexes
- `policy_chunks_embedding_idx` — HNSW (vector_cosine_ops)
- `policy_chunks_bm25_idx` — BM25 (text_config='english')
- `ix_policy_chunks_agent_hash` — B-tree (agent_id, policy_hash)
- `ix_policy_chunks_agent_id` — B-tree (agent_id)

### Schema management
Schema is managed via **Alembic** (async). Models are defined in
`db/models.py`; migrations live in `db/migrations/versions/`. Run:
```bash
cd cbac_service
alembic upgrade head
```

## Environment

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

### Required environment variables

See `.env.sample` for the full list. Key ones:
- `DATABASE_URL` — async Postgres connection string
- `AGENTDNA_API_KEY` — Provenance Layer access
- `CBAC_SERVICE_HOST` / `CBAC_SERVICE_PORT` — binding
- `HYBRID_SEARCH_ENABLED` — toggle BM25 fusion (default: true)

## The decision pipeline (`cbac.py`)

Class `CBAC`. On each request:

1. **Check 1 — NLI drift** (when `user_intent` supplied): NLI cross-encoder
   checks if the agent's intended action contradicts the user intent.
   Contradiction ≥ 0.60 → immediate deny.

2. **Policy fetch + cache check**: Fetches the agent's latest policy from the
   Provenance Layer. Compares the policy hash against `policy_meta` in Postgres.
   If stale or missing → `precompute_policy` (chunk, classify, encode, store).

3. **Tier 1 — Cosine gap** (via pgvector): Encodes the intent, runs
   `vector_search(allowed)` and `vector_search(forbidden)`. If
   `max_allowed − max_forbidden > ALLOW_GAP` → allow. If gap < −`DENY_GAP` → deny.
   Otherwise escalate.

4. **Tier 2 — NLI entailment**: Uses `hybrid_search` (pgvector + pg_textsearch
   RRF fusion) to find the best allowed chunk candidate, then runs the NLI
   cross-encoder. Entailment ≥ 0.55 → allow, contradiction ≥ 0.60 → deny.

5. **Tier 3 — LLM backend** (optional): If configured, sends intent + full
   policy text to an LLM for judgment. Otherwise returns `"advise"`.

6. **Hallucination score** (HHEM): Attached after the decision, never gates it.

Decisions are `"allow" | "deny" | "advise"` and the pipeline is **fail-closed**
(any error → `deny`).

## Scoring attached to a decision

- **Hallucination score (HHEM):** when a decision is reached with a user intent
  present, `CBAC.hallucination_score` (vectara HHEM model, 1 = grounded,
  0 = hallucinated) is attached to the result.
- **LHI trust:** `compute_lhi` combines four component scores
  (intent, policy, hallucination, output) as a **weighted arithmetic mean**
  (`LHI_WEIGHTS`) — deliberately compensatory, since the allow/deny gates
  already enforce the hard constraints *before* execution — then folds it into a
  stored trust value via an **asymmetric EMA — slow to build (`LHI_LAMBDA_UP`),
  fast to lose (`LHI_LAMBDA_DOWN`)**. A zero component costs only its weight, not
  the whole score (a transient tool failure must not annihilate an
  otherwise-compliant interaction). Trust is tracked **per caller→callee edge**
  and persisted in `TRUST_STORE_FILE` (`trust_scores.json`) under the config dir
  — not in Postgres, which holds only policy chunks/embeddings. The LHI math is
  covered by `tests/test_cbac_lhi.py`; score attachment by
  `tests/test_cbac_verify.py` — keep both green when touching it.
