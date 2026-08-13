# Tunables for the CBAC decision pipeline (cbac_service/cbac.py).

import os

# ── Database ──────────────────────────────────────────────────────────────────
# Async Postgres connection string (asyncpg driver).
# Override via environment variable for deployment.
DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://madhavbaidya@localhost:5432/cbac",
)

# pgvector index type: "hnsw" (low-latency, moderate data) or "ivfflat" (large scale).
VECTOR_INDEX_TYPE: str = os.environ.get("VECTOR_INDEX_TYPE", "hnsw")

# Hybrid search: enable BM25 fusion alongside vector cosine.
HYBRID_SEARCH_ENABLED: bool = os.environ.get("HYBRID_SEARCH_ENABLED", "true").lower() == "true"

# Reciprocal Rank Fusion constant (higher = less aggressive re-ranking).
RRF_K: int = int(os.environ.get("RRF_K", "60"))

# Models
ENCODER_MODEL = "BAAI/bge-small-en-v1.5"  # bi-encoder for Tier 1 cosine
NLI_MODEL = "cross-encoder/nli-deberta-v3-small"  # NLI for classify + Tier 2
HHEM_MODEL = "vectara/hallucination_evaluation_model"  # hallucination scoring (1 = grounded)

# Tier 1 gap thresholds: allow when gap > +allow_gap, deny when gap < -deny_gap,
# else escalate. Model-agnostic (relative difference, not absolute scores).
ALLOW_GAP = 0.12
DENY_GAP = 0.08

# NLI thresholds for Check 1 drift and Tier 2 entailment.
ENTAILMENT_THRESHOLD = 0.55
CONTRADICTION_THRESHOLD = 0.60

# LHI (Local Heuristic Intelligence): weighted arithmetic mean of the
# (intent, policy, hallucination, output) scores — expected interaction quality;
# the allow/deny gates already enforce the hard constraints pre-execution —
# then an asymmetric EMA against the stored trust: slow to build, fast to lose.
LHI_WEIGHTS: tuple[float, float, float, float] = (0.3, 0.3, 0.2, 0.2)
LHI_LAMBDA_UP = 0.95
LHI_LAMBDA_DOWN = 0.70


# Structure-aware chunking: paragraphs / list items are the primary unit,
# split further only past this word-count budget (~1.3 tokens/word for English).
CHUNK_MAX_WORDS = 120
NLI_BATCH_SIZE = 64
