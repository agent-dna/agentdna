from fastapi import Request
from fastapi import FastAPI
import uvicorn
from fastapi.responses import PlainTextResponse
import asyncio
import base64
import hashlib
import json
import pickle
import re
import requests
import yaml
import os

from typing import Optional, Callable, Dict, Any, List, Tuple
from pathlib import Path
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from agentdna.id import get_id
from agentdna.provenance import Provenance
from agentdna.types import AgentCard, MODEL_EMBEDDINGS_DIR, IntentWorkflow


from sentence_transformers import SentenceTransformer

_DEFAULT_ENCODER = "BAAI/bge-small-en-v1.5"  # bi-encoder for Tier 1 cosine
_DEFAULT_NLI_MODEL = "cross-encoder/nli-deberta-v3-small"  # NLI for classify + Tier 2

# Gap thresholds: allow when gap > +0.12, deny when gap < -0.08, else escalate.
# These are model-agnostic (relative difference, not absolute scores).
_ALLOW_GAP_DEFAULT: float = 0.12
_DENY_GAP_DEFAULT: float = 0.08

# NLI thresholds for Check 1 drift and Tier 2 entailment.
_ENTAILMENT_THRESHOLD: float = 0.55
_CONTRADICTION_THRESHOLD: float = 0.60

# HHEM cross-encoder for hallucination scoring (1 = grounded, 0 = hallucinated).
_DEFAULT_HHEM_MODEL = "vectara/hallucination_evaluation_model"

# LHI: weighted geometric mean of (intent, policy, hallucination, output) scores,
# then an asymmetric EMA against the stored trust — slow to build, fast to lose.
_LHI_WEIGHTS: Tuple[float, float, float, float] = (0.3, 0.3, 0.2, 0.2)
_LHI_LAMBDA_UP: float = 0.95
_LHI_LAMBDA_DOWN: float = 0.70

_TRUST_STORE_FILE = "trust_scores.json"

# Structure-aware chunking: paragraphs / list items are the primary unit,
# split further only past this word-count budget (~1.3 tokens/word for English).
_CHUNK_MAX_WORDS: int = 120
_NLI_BATCH_SIZE: int = 64

_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

REQUIRED_FRONTMATTER_KEYS = (
    "agent-did",
    "issued-by",
    "issued-at",
    "expires-at",
    "allowed-actions",
)


@dataclass
class SkillsCard:
    """Parsed skill.md card. Frontmatter fields + the markdown body."""

    agent_did: str
    agent_name: str
    issued_by: str
    issued_at: datetime
    expires_at: datetime
    allowed_actions: List[str]
    forbidden_actions: List[str] = field(default_factory=list)
    constraints: Dict[str, Any] = field(default_factory=dict)
    can_delegate_to: List[str] = field(default_factory=list)
    requires: Dict[str, Any] = field(default_factory=dict)
    body: str = ""
    raw_frontmatter: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CardCheck:
    """Result of CBAC check for one layer in the chain."""

    layer_did: str
    card_id: Optional[str]
    card: Optional[SkillsCard]
    action: Optional[str]
    passed: bool
    reasons: List[str] = field(default_factory=list)


@dataclass
class CBACResult:
    """Overall CBAC decision after walking the full chain."""

    decision: str  # "allow" | "deny" | "advise"
    reason: str = ""
    trace: List[CardCheck] = field(default_factory=list)
    hallucination_score: Optional[float] = None
    intent_score: Optional[float] = None
    policy_score: Optional[float] = None


def parse_skill_md(text: str) -> SkillsCard:
    """
    Parse a skill.md (YAML frontmatter + markdown body) into a Card.

    Raises ValueError for any structural problem.
    """
    if not text.startswith("---"):
        raise ValueError("skill.md must start with YAML frontmatter delimited by ---")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("skill.md must have a closing --- line after frontmatter")
    frontmatter_text = parts[1]
    body = parts[2].lstrip("\n")

    fm = yaml.safe_load(frontmatter_text)
    if not isinstance(fm, dict):
        raise ValueError("skill.md frontmatter must be a YAML object")

    missing = [k for k in REQUIRED_FRONTMATTER_KEYS if k not in fm]
    if missing:
        raise ValueError(f"skill.md missing required field(s): {', '.join(missing)}")

    return SkillsCard(
        agent_did=fm["agent-did"],
        agent_name=fm.get("agent-name", ""),
        issued_by=fm["issued-by"],
        issued_at=_parse_dt(fm["issued-at"]),
        expires_at=_parse_dt(fm["expires-at"]),
        allowed_actions=list(fm.get("allowed-actions") or []),
        forbidden_actions=list(fm.get("forbidden-actions") or []),
        constraints=dict(fm.get("constraints") or {}),
        can_delegate_to=list(fm.get("can-delegate-to") or []),
        requires=dict(fm.get("requires") or {}),
        body=body,
        raw_frontmatter=fm,
    )


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    raise ValueError(f"unparseable timestamp: {value!r}")


def _collect_text(obj: Any, *, include_keys: bool = True, _depth: int = 0) -> str:
    """Recursively gather every string found in an arbitrary blob.

    Format-agnostic: handles raw text, dicts, lists, and dataclasses/objects
    (e.g. :class:`Card`) alike.

    ``include_keys`` controls whether mapping *keys* are gathered alongside
    values. For a large policy blob, keys are useful searchable vocabulary
    (default). For a short intent, structural keys like ``action``/``params``
    are pure scaffolding that would dilute the coverage score, so the intent
    side passes ``include_keys=False`` to flatten values only.
    """
    if _depth > 6 or obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, (int, float, bool)):
        return str(obj)
    if isinstance(obj, dict):
        parts: List[str] = []
        for k, v in obj.items():
            if include_keys:
                parts.append(str(k))
            parts.append(_collect_text(v, include_keys=include_keys, _depth=_depth + 1))
        return " ".join(parts)
    if isinstance(obj, (list, tuple, set)):
        return " ".join(_collect_text(v, include_keys=include_keys, _depth=_depth + 1) for v in obj)
    # Dataclass / arbitrary object → walk its __dict__.
    data = getattr(obj, "__dict__", None)
    if isinstance(data, dict):
        return _collect_text(data, include_keys=include_keys, _depth=_depth + 1)
    return str(obj)


def _intended_action_text(intended_action: Any) -> str:
    """Flatten an intended-action of *any* shape (str / dict / list / object)
    into one string for tokenizing.

    Format-agnostic by design — it reuses the same recursive flattener as the
    policy side (:func:`_collect_text`), so no fixed key schema is assumed.
    Common keys like ``action``/``description``/``params`` are picked up
    automatically because their string values are gathered. Field *names* are
    skipped (``include_keys=False``) — for a short intent they are structural
    scaffolding that would only dilute the coverage score.
    """
    return _collect_text(intended_action, include_keys=False)


class CBAC:
    def __init__(
        self,
        provenance: Provenance,
        cbac_url: str = "https://cbac-admin.agentdna.io",
        encoder_name: str = _DEFAULT_ENCODER,
        nli_model_name: str = _DEFAULT_NLI_MODEL,
        llm_backend: Optional[Callable] = None,
        allow_gap: float = _ALLOW_GAP_DEFAULT,
        deny_gap: float = _DENY_GAP_DEFAULT,
        hhem_model_name: str = _DEFAULT_HHEM_MODEL,
        lhi_weights: Tuple[float, float, float, float] = _LHI_WEIGHTS,
        lhi_lambda_up: float = _LHI_LAMBDA_UP,
        lhi_lambda_down: float = _LHI_LAMBDA_DOWN,
    ):
        self.provenance = provenance
        self.cbac_url = cbac_url
        self._encoder_name = encoder_name
        self._nli_model_name = nli_model_name
        self._llm_backend = llm_backend
        self._allow_gap = allow_gap
        self._deny_gap = deny_gap
        self._hhem_model_name = hhem_model_name
        self._lhi_weights = lhi_weights
        self._lhi_lambda_up = lhi_lambda_up
        self._lhi_lambda_down = lhi_lambda_down

        self._encoder = None
        self._nli = None
        self._hhem = None
        self._nli_labels: Dict[int, str] = {}

        # Make embeddings config dir
        self.embeddings_dir = os.path.join(self.provenance.config_dir, MODEL_EMBEDDINGS_DIR)
        os.makedirs(self.embeddings_dir, exist_ok=True)

    def _get_encoder(self):
        if self._encoder is None:
            self._encoder = SentenceTransformer(self._encoder_name)
        return self._encoder

    def _get_nli(self):
        if self._nli is None:
            from sentence_transformers.cross_encoder import CrossEncoder

            self._nli = CrossEncoder(self._nli_model_name)
            try:
                if not self._nli.model:
                    raise ValueError("NLI model is not initialsed")
                id2label = self._nli.model.config.id2label

                if id2label is None:
                    raise ValueError("id2label is not initialised")

                self._nli_labels = {i: lbl.lower() for i, lbl in id2label.items()}
            except AttributeError:
                # Fallback for deberta NLI label order (contradiction/entailment/neutral)
                self._nli_labels = {0: "contradiction", 1: "entailment", 2: "neutral"}
        return self._nli

    def _get_hhem(self):
        if self._hhem is None:
            from transformers import AutoModelForSequenceClassification

            # trust_remote_code is required by HHEM-2.1 (custom predict() head).
            self._hhem = AutoModelForSequenceClassification.from_pretrained(
                self._hhem_model_name, trust_remote_code=True
            )
        return self._hhem

    def hallucination_score(self, source_text: str, generated_text: str) -> float:
        """Score how well ``generated_text`` is grounded in ``source_text``.

        Runs the HHEM cross-encoder on the (premise, hypothesis) pair and
        returns a score in [0, 1]: 1 = fully supported by the source,
        0 = hallucinated. Higher is better.
        """
        model = self._get_hhem()
        return float(model.predict([(source_text, generated_text)])[0])

    def _nli_scores(self, premise: str, hypothesis: str) -> Dict[str, float]:
        """Run NLI cross-encoder on a (premise, hypothesis) pair.

        Returns a dict like {'entailment': 0.82, 'contradiction': 0.05, 'neutral': 0.13}.
        Scores are softmax-normalised probabilities.
        """
        from scipy.special import softmax as sp_softmax

        nli = self._get_nli()
        raw = nli.predict([(premise, hypothesis)], apply_softmax=False)
        probs = sp_softmax(raw[0])
        return {self._nli_labels.get(i, str(i)): float(probs[i]) for i in range(len(probs))}

    def _nli_scores_batch(self, pairs: List[Tuple[str, str]]) -> List[Dict[str, float]]:
        """Batched NLI over many (premise, hypothesis) pairs in one predict() call.

        Same per-pair result shape as calling :meth:`_nli_scores` in a loop,
        but the cross-encoder processes ``pairs`` in mini-batches instead of
        one sample at a time — the loop form never lets the model's internal
        batching do anything useful.
        """
        from scipy.special import softmax as sp_softmax

        if not pairs:
            return []
        nli = self._get_nli()
        raw = nli.predict(
            pairs,
            apply_softmax=False,
            batch_size=_NLI_BATCH_SIZE,
            show_progress_bar=False,
        )
        probs = sp_softmax(raw, axis=1)
        return [
            {self._nli_labels.get(i, str(i)): float(row[i]) for i in range(len(row))}
            for row in probs
        ]

    def _split_list_items(self, block: str) -> List[str]:
        """Split a paragraph block into its list items, if any.

        A bulleted/numbered line starts a new item; any following line that
        isn't itself a bullet is a wrapped continuation of that item, not a
        new unit. Blocks with no bullets at all collapse into a single item
        (this is what re-joins a hard-wrapped prose paragraph).
        """
        lines = block.splitlines()
        if not any(_BULLET_RE.match(ln) for ln in lines):
            return [" ".join(ln.strip() for ln in lines if ln.strip())]
        items: List[str] = []
        current: List[str] = []
        for line in lines:
            if _BULLET_RE.match(line):
                if current:
                    items.append(" ".join(current))
                current = [line.strip()]
            else:
                stripped = line.strip()
                if stripped:
                    current.append(stripped)
        if current:
            items.append(" ".join(current))
        return items

    def _split_by_word_budget(self, unit: str, max_words: int) -> List[str]:
        """Split ``unit`` only if it exceeds ``max_words``, preferring sentence
        boundaries so a chunk never straddles unrelated sentences unnecessarily."""
        words = unit.split()
        if len(words) <= max_words:
            return [unit]
        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(unit) if s.strip()]
        if len(sentences) <= 1:
            return [" ".join(words[i : i + max_words]) for i in range(0, len(words), max_words)]
        out: List[str] = []
        current: List[str] = []
        current_len = 0
        for sent in sentences:
            n = len(sent.split())
            if current and current_len + n > max_words:
                out.append(" ".join(current))
                current, current_len = [], 0
            current.append(sent)
            current_len += n
        if current:
            out.append(" ".join(current))
        return out

    def _chunk_body_text(self, text: str, max_words: int = _CHUNK_MAX_WORDS) -> List[str]:
        """Structure-aware, budget-capped chunking for free-form policy text.

        Paragraphs (blank-line separated) and list items are the primary
        chunk boundary — not individual lines — so a hard-wrapped sentence or
        a bullet's wrapped continuation stays in one chunk. A unit is only
        split further, on sentence boundaries, if it exceeds ``max_words``.
        Lines starting with ``#`` are dropped, matching the previous
        comment-stripping behaviour.
        """
        filtered = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
        chunks: List[str] = []
        for block in re.split(r"\n\s*\n", filtered):
            block = block.strip()
            if not block:
                continue
            for unit in self._split_list_items(block):
                unit = unit.strip()
                if not unit:
                    continue
                chunks.extend(self._split_by_word_budget(unit, max_words))
        return chunks

    def _flatten_policy_chunks(self, policy: Any) -> List[str]:
        """Flatten a policy of any shape into a list of text chunks.

        Each YAML frontmatter entry becomes "key: value" (one chunk per list item
        for list-valued keys). The body is chunked by paragraph/list-item
        structure via :meth:`_chunk_body_text`, not by raw line. This unified
        path works for any skill.md schema — no hardcoded key names.
        """
        chunks: List[str] = []
        if isinstance(policy, SkillsCard):
            for key, value in policy.raw_frontmatter.items():
                if isinstance(value, list):
                    for item in value:
                        chunks.append(f"{key}: {item}")
                elif value is not None:
                    chunks.append(f"{key}: {value}")
            chunks.extend(self._chunk_body_text(policy.body))
            return chunks
        if isinstance(policy, str):
            stripped = policy.strip()
            if stripped.startswith("---"):
                try:
                    return self._flatten_policy_chunks(parse_skill_md(policy))
                except (ValueError, Exception):
                    pass
            return self._chunk_body_text(policy)
        if isinstance(policy, dict):
            for key, value in policy.items():
                if isinstance(value, list):
                    for item in value:
                        chunks.append(f"{key}: {item}")
                elif isinstance(value, str):
                    chunks.append(f"{key}: {value}")
            return chunks
        return []

    def _classify_chunks(self, chunks: List[str]) -> Tuple[List[str], List[str]]:
        """NLI-classify each chunk as allowed or forbidden.

        For every chunk we run two NLI queries:
          premise = chunk, hypothesis = "This capability is permitted"
          premise = chunk, hypothesis = "This capability is prohibited"
        The chunk goes into the forbidden bucket only when the prohibition
        entailment clearly beats the permission entailment. All 2*len(chunks)
        queries are sent to the cross-encoder in one batched call instead of
        one pair at a time.
        """
        if not chunks:
            return [], []
        pairs: List[Tuple[str, str]] = []
        for chunk in chunks:
            pairs.append((chunk, "This capability is permitted and allowed"))
            pairs.append((chunk, "This capability is prohibited and forbidden"))
        scores = self._nli_scores_batch(pairs)

        allowed: List[str] = []
        forbidden: List[str] = []
        for i, chunk in enumerate(chunks):
            allow_e = scores[2 * i].get("entailment", 0.0)
            forbid_e = scores[2 * i + 1].get("entailment", 0.0)
            if forbid_e > allow_e and forbid_e > 0.40:
                forbidden.append(chunk)
            else:
                allowed.append(chunk)
        return allowed, forbidden

    def __get_latest_agent_policy(
        self,
        agent_id: str,
    ) -> str:
        """
        Returns the latest decoded policy
        associated with an agent.
        """

        actor_card_dict = self.provenance.get_latest_provenance_record(actor_id=agent_id)

        actor_card = AgentCard(**actor_card_dict)

        try:
            return base64.b64decode(actor_card.policy).decode("utf-8")
        except Exception as exc:
            raise RuntimeError(f"failed to decode policy for agent {agent_id}: {exc}") from exc

    def __get_embedding_file_path(self, agent_id: str) -> Path:
        """Return the .pkl path for this agent's precomputed policy vectors."""
        digest = hashlib.sha256(agent_id.encode()).hexdigest()[:32]
        return Path(self.embeddings_dir) / f"{digest}.pkl"

    def __save_to_embeddings_dir(self, agent_id: str, data: Dict[str, Any]):
        path = self.__get_embedding_file_path(agent_id)
        with path.open("wb") as f:
            pickle.dump(data, f)

    def __load_from_embeddings_dir(self, agent_id: str) -> Optional[Dict[str, Any]]:
        path = self.__get_embedding_file_path(agent_id)
        if not path.exists():
            return None
        try:
            with path.open("rb") as f:
                return pickle.load(f)
        except Exception:
            return None

    def check_policy_embedding_exists(self, agent_id: str) -> Dict[str, Any] | None:
        """
        Checks if the embedding file for an agent is present or not

        Returns None if its not present
        """
        embedding = self.__load_from_embeddings_dir(agent_id=agent_id)
        return embedding

    async def precompute_policy(self, agent_id: str, skip_compute=True) -> Dict[str, Any]:
        """
        Precompute and cache policy vectors for an agent.  Call this once
        after deploying or updating the agent's policy card — not on every
        inbound request.

        What it does
        ------------
        1. Fetches the policy from the Provenance Layer.
        2. Flattens it to text chunks (unified key:value + body path).
        3. NLI-classifies each chunk into allowed / forbidden buckets.
        4. Encodes both buckets with the bi-encoder.
        5. Persists everything to ``~/.agentdna/embedding_cache/<hash>.pkl``.

        Returns the cached payload so callers can inspect it.
        """
        # Checks if embedding is present for a model locally.
        # If it exists, then check if skip_compute is True.
        # If its true, proceed to fetch the embeddings from local
        local_embedding = self.check_policy_embedding_exists(agent_id)
        if local_embedding and skip_compute:
            return local_embedding

        policy = self.__get_latest_agent_policy(agent_id=agent_id)
        if not policy:
            raise RuntimeError(f"No policy found for agent {agent_id}")

        chunks = self._flatten_policy_chunks(policy)
        if not chunks:
            raise RuntimeError(f"Policy for agent {agent_id} produced no chunks")

        allowed_chunks, forbidden_chunks = self._classify_chunks(chunks)
        encoder = self._get_encoder()
        to_encode = allowed_chunks + forbidden_chunks

        vecs = encoder.encode(to_encode, normalize_embeddings=True)

        n_allowed = len(allowed_chunks)
        allowed_vecs = vecs[:n_allowed]
        forbidden_vecs = vecs[n_allowed:]

        policy_text = "\n".join(chunks)
        payload: Dict[str, Any] = {
            "agent_id": agent_id,
            "allowed_chunks": allowed_chunks,
            "forbidden_chunks": forbidden_chunks,
            "allowed_vecs": allowed_vecs,
            "forbidden_vecs": forbidden_vecs,
            "policy_text": policy_text,
            "policy_hash": hashlib.sha256(policy.encode()).hexdigest(),
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "encoder": self._encoder_name,
            "nli_model": self._nli_model_name,
        }

        await asyncio.to_thread(self.__save_to_embeddings_dir, agent_id, payload)
        return payload

    async def __check1_drift(
        self,
        user_intent: str,
        agent_action: str,
    ) -> Tuple[Optional[Tuple[str, str]], float]:
        """NLI drift check: does the agent's action contradict the user's intent?

        Returns ``((decision, reason), intent_score)`` if contradiction is
        strong enough to deny, else ``(None, intent_score)``. ``intent_score``
        (``1 - contradiction``, in [0, 1]) is always returned — it's the same
        NLI call regardless of outcome, so the caller gets it for free to feed
        into :meth:`compute_lhi` later.
        """
        scores = await asyncio.to_thread(self._nli_scores, user_intent, agent_action)
        contradiction = scores.get("contradiction", 0.0)
        intent_score = 1.0 - contradiction
        if contradiction >= _CONTRADICTION_THRESHOLD:
            return (
                (
                    "deny",
                    f"Check 1 drift: user intent {user_intent!r} contradicts agent action "
                    f"{agent_action!r} (NLI contradiction={contradiction:.2f})",
                ),
                intent_score,
            )
        return None, intent_score

    def _max_cosine(self, query_vec, chunk_vecs) -> float:
        """Maximum cosine similarity from query_vec to any row in chunk_vecs."""
        import numpy as np

        if chunk_vecs.shape[0] == 0:
            return 0.0
        sims = chunk_vecs @ query_vec  # both already L2-normalised by SentenceTransformer
        return float(np.max(sims))

    async def __tiered_decision(
        self,
        intent_text: str,
        intent_vec,
        allowed_chunks: List[str],
        forbidden_chunks: List[str],
        allowed_vecs,
        forbidden_vecs,
        policy_text: str,
    ) -> Tuple[str, str, Optional[float]]:
        """Tier 1 (cosine gap) → Tier 2 (NLI entailment) → Tier 3 (LLM).

        Returns ``(decision, reason, policy_score)``. ``policy_score`` is a
        [0, 1] normalization of whichever signal decided — the Tier 1 cosine
        gap rescaled against its own allow/deny thresholds (so it lands at
        exactly 1.0/0.0 on a clean Tier 1 allow/deny), or the Tier 2 NLI
        entailment as-is. Tier 3 (LLM judgment or no-backend "advise") has no
        numeric signal, so ``policy_score`` is ``None`` there — the caller
        must supply their own if they want to feed :meth:`compute_lhi`.

        Pure decision logic, extracted from :meth:`verify_agent_app_interaction` so
        the caller can attach a hallucination score to the result exactly once,
        after a decision is reached, without duplicating it at every tier's exit.
        """
        import numpy as np

        # Tier 1: cosine gap.
        allowed_score = self._max_cosine(intent_vec, allowed_vecs)
        forbidden_score = self._max_cosine(intent_vec, forbidden_vecs)
        gap = allowed_score - forbidden_score

        span = self._allow_gap + self._deny_gap
        gap_score = max(0.0, min(1.0, (gap + self._deny_gap) / span)) if span > 0 else None

        if gap > self._allow_gap:
            return (
                "allow",
                f"Tier 1 cosine gap {gap:+.3f} > +{self._allow_gap} "
                f"(allowed={allowed_score:.3f}, forbidden={forbidden_score:.3f})",
                gap_score,
            )
        if gap < -self._deny_gap:
            return (
                "deny",
                f"Tier 1 cosine gap {gap:+.3f} < -{self._deny_gap} "
                f"(intent closer to forbidden than allowed policy)",
                gap_score,
            )

        # Tier 2: NLI entailment vs top allowed chunk.
        if not allowed_chunks:
            return ("deny", "Tier 2: no allowed policy chunks found", None)

        top_idx = int(np.argmax(allowed_vecs @ intent_vec))
        top_chunk = allowed_chunks[top_idx]
        t2_scores = await asyncio.to_thread(self._nli_scores, intent_text, top_chunk)
        entailment = t2_scores.get("entailment", 0.0)
        contradiction = t2_scores.get("contradiction", 0.0)

        if entailment >= _ENTAILMENT_THRESHOLD:
            return ("allow", f"Tier 2 NLI entailment={entailment:.2f} vs {top_chunk!r}", entailment)
        if contradiction >= _CONTRADICTION_THRESHOLD:
            return (
                "deny",
                f"Tier 2 NLI contradiction={contradiction:.2f} vs {top_chunk!r}",
                entailment,
            )

        # Tier 3: LLM judgment (optional). No numeric signal — policy_score is None.
        if self._llm_backend is None:
            return (
                "advise",
                f"Tier 1/2 inconclusive (gap={gap:+.3f}, "
                f"entailment={entailment:.2f}, contradiction={contradiction:.2f}); "
                "no LLM backend configured — caller must decide",
                None,
            )

        try:
            llm_decision = await self._llm_backend(intent_text, policy_text)
        except Exception as e:
            return ("advise", f"Tier 3 LLM error: {e}", None)

        verdict = str(llm_decision).lower()
        if any(w in verdict for w in ("deny", "reject", "not allow", "prohibited")):
            return ("deny", f"Tier 3 LLM: {llm_decision}", None)
        if any(w in verdict for w in ("allow", "permit", "approve", "authorise", "authorize")):
            return ("allow", f"Tier 3 LLM: {llm_decision}", None)
        return ("advise", f"Tier 3 LLM inconclusive: {llm_decision}", None)

    async def verify_agent_app_interaction(
        self,
        agent_id: str,
        intended_action: Any,
        user_intent: Optional[str] = None,
    ) -> CBACResult:
        """
        Three-tier semantic intent verification against the agent's policy.

        The policy is fetched from the Provenance Layer and flattened to
        text chunks via a unified path — YAML key:value pairs plus markdown
        body lines — so any skill.md schema works without hardcoded key names.

        Pipeline
        --------
        Check 1 (NLI drift)
            If ``intended_action`` carries both a user-side field
            (``user_intent`` / ``user_request``) and an agent-side field
            (``action`` / ``description``), we run NLI to verify the agent
            hasn't drifted from the user's original request.  A contradiction
            score ≥ 0.60 → deny immediately.

        Tier 1 (cosine gap)
            All policy chunks are NLI-classified into allowed / forbidden
            buckets at query time.  Both buckets are encoded with the
            bi-encoder.  ``gap = max_allowed_cosine - max_forbidden_cosine``.
            gap > +allow_gap → allow;  gap < -deny_gap → deny;  else → Tier 2.

        Tier 2 (NLI entailment)
            The intent is compared against the top-scoring allowed chunk via
            NLI cross-encoder.  Entailment ≥ 0.55 → allow;
            contradiction ≥ 0.60 → deny;  else → Tier 3.

        Tier 3 (LLM judgment)
            Delegated to ``llm_backend`` if configured.  If ``llm_backend``
            is ``None`` the result is ``"advise"`` — the caller decides.

        Parameters
        ----------
        agent_id:
            Used to fetch the agent's policy via :meth:`_get_policy`.
        intended_action:
            The action the agent wants to perform.  Any shape is accepted:
            a plain string, a dict with ``action``/``description``/``params``,
            or an object — the same recursive flattener used by the lexical
            path gathers the text.

        Returns
        -------
        CBACResult with ``decision`` in ``{"allow", "deny", "advise"}``.
        Fail-closed: any unrecoverable error (no policy, empty content,
        model failure) resolves to ``deny``. When ``user_intent`` is supplied,
        ``intent_score`` is set from the Check-1 NLI comparison (``1 -
        contradiction``). When Tier 1 or 2 decide, ``policy_score`` is also
        set (normalized cosine gap or NLI entailment respectively; ``None``
        on a Tier 3 decision, which has no numeric signal). When Tier 1/2/3
        reach a decision, ``hallucination_score`` is set (HHEM score of
        ``intended_action`` grounded in ``user_intent``, 1 = grounded).
        ``intent_score``/``policy_score``/``hallucination_score`` are all
        informational only — none of them ever changes ``decision``.
        """
        import numpy as np

        intent_text = _intended_action_text(intended_action)
        if not intent_text.strip():
            return CBACResult(
                decision="deny", reason="Intended action carries no analysable content"
            )

        # Check 1: NLI drift — only runs when caller supplies the root user intent.
        intent_score: Optional[float] = None
        if user_intent and intent_text:
            drift, intent_score = await self.__check1_drift(user_intent, intent_text)
            if drift is not None:
                decision, reason = drift
                return CBACResult(decision=decision, reason=reason, intent_score=intent_score)

        # Fetch current policy from chain — always, so we can detect updates.
        try:
            current_policy = self.__get_latest_agent_policy(agent_id)
        except Exception as e:
            return CBACResult(
                decision="deny", reason=f"Policy lookup failed for agent {agent_id}: {e}"
            )
        if not current_policy:
            return CBACResult(decision="deny", reason=f"No policy available for agent {agent_id}")

        current_hash = hashlib.sha256(current_policy.encode()).hexdigest()

        # Load local cache and validate against current policy hash.
        cached = await asyncio.to_thread(self.__load_from_embeddings_dir, agent_id)

        if cached is None or cached.get("policy_hash") != current_hash:
            # Cache miss or policy updated on chain — recompute and persist.
            try:
                cached = await self.precompute_policy(agent_id)
            except Exception as e:
                return CBACResult(
                    decision="deny", reason=f"Policy unavailable for agent {agent_id}: {e}"
                )

        allowed_chunks: List[str] = cached["allowed_chunks"]
        forbidden_chunks: List[str] = cached["forbidden_chunks"]
        allowed_vecs: np.ndarray = cached["allowed_vecs"]
        forbidden_vecs: np.ndarray = cached["forbidden_vecs"]
        policy_text: str = cached["policy_text"]

        if not allowed_chunks and not forbidden_chunks:
            return CBACResult(decision="deny", reason="Policy carries no analysable content")

        # Encode only the intent at runtime (~5 ms on CPU).
        encoder = self._get_encoder()
        intent_vec = await asyncio.to_thread(
            lambda: encoder.encode([intent_text], normalize_embeddings=True)[0]
        )

        decision, reason, policy_score = await self.__tiered_decision(
            intent_text,
            intent_vec,
            allowed_chunks,
            forbidden_chunks,
            allowed_vecs,
            forbidden_vecs,
            policy_text,
        )

        # Hallucination score rides along with the decision but never gates it —
        # a scoring failure must not flip an already-decided allow into a deny.
        hallucination = None
        if user_intent:
            try:
                hallucination = await asyncio.to_thread(
                    self.hallucination_score, user_intent, intent_text
                )
            except Exception:
                hallucination = None

        return CBACResult(
            decision=decision,
            reason=reason,
            hallucination_score=hallucination,
            intent_score=intent_score,
            policy_score=policy_score,
        )

    def authorise_agent_app_interaction(
        self,
        agent_id: str,
        action_intent: str,
        envelope: IntentWorkflow,
        app_url: str,
        app_method: str = "POST",
        app_headers: dict | None = None,
        app_body: str | dict | None = None,
        app_timeout: float = 100.0,
    ) -> requests.Response:
        if isinstance(app_body, dict):
            app_body = json.dumps(app_body)
            app_headers = {"Content-Type": "application/json", **(app_headers or {})}

        envelope_dict = asdict(envelope)
        payload = {
            "agent_id": agent_id,
            "action_intent": action_intent,
            "envelope": envelope_dict,
            "app_request": {
                "url": app_url,
                "method": app_method,
                "headers": app_headers or {},
                "body": app_body or "",
            },
        }

        resp = requests.post(
            f"{self.cbac_url.rstrip('/')}/agent-admin/v1/authorize-action",
            json=payload,
            timeout=app_timeout,
        )

        decision = resp.headers.get("X-CBAC-Decision")

        if decision == "deny":
            raise PermissionError(resp.text)
        if decision == "error":
            raise RuntimeError(resp.text)

        return resp

    # TODO:- Replace trust file by light db.
    @property
    def __trust_store_path(self) -> Path:
        return Path(self.provenance.config_dir) / _TRUST_STORE_FILE

    def __load_trust_store(self) -> Dict[str, Any]:
        try:
            with self.__trust_store_path.open("r") as f:
                return json.load(f)
        except Exception:
            return {}

    def __save_trust_store(self, store: Dict[str, Any]):
        with self.__trust_store_path.open("w") as f:
            json.dump(store, f, indent=2)

    def compute_lhi(
        self,
        agent_id: str,
        callee_name: str,
        callee_type: str,
        intent_score: float,
        policy_score: float,
        hallucination_score: float,
        output_score: float,
    ) -> float:
        """Update and return the LHI trust score for one (agent → callee) edge.

        Called *after* the interaction executed, once all four per-interaction
        scores (each in [0, 1], higher is better) are known:

        1. Instantaneous quality ``s`` = weighted geometric mean of the four
           scores — non-compensatory, so a single near-zero component drags
           ``s`` toward zero regardless of the others.
        2. Asymmetric EMA against the stored trust for this edge:
           ``T = λ·T_prev + (1−λ)·s`` with λ = ``lhi_lambda_up`` when improving
           (trust builds slowly) and ``lhi_lambda_down`` when degrading (trust
           drops fast). First interaction with a callee: ``T = s``.

        The full record (callee, all four scores, trust) is persisted locally
        under the config dir and appended to a dedicated per-agent LHI card on
        the Provenance Layer (created on first write), so the trust history is
        independently verifiable on-chain. Trust never overrides the per-
        interaction allow/deny gates — it is read pre-execution only to
        arbitrate the inconclusive ("advise") band.
        """
        scores = {
            "intent": intent_score,
            "policy": policy_score,
            "hallucination": hallucination_score,
            "output": output_score,
        }
        for key, value in scores.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{key}_score must be in [0, 1], got {value}")

        # TODO: should this be a geometric mean?
        s = 1.0
        for value, weight in zip(scores.values(), self._lhi_weights):
            s *= value**weight

        store = self.__load_trust_store()
        agent_entry = store.setdefault(agent_id, {"callees": {}})
        prev_entry = agent_entry["callees"].get(callee_name)

        if prev_entry is None:
            trust = s
        else:
            prev = prev_entry["trust"]
            lam = self._lhi_lambda_up if s >= prev else self._lhi_lambda_down
            trust = lam * prev + (1 - lam) * s

        record = {
            "type": "lhi_record",
            "agent_id": agent_id,
            "callee": {"name": callee_name, "type": callee_type},
            "scores": scores,
            "trust": trust,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        agent_entry["callees"][callee_name] = {
            "type": callee_type,
            "scores": scores,
            "trust": trust,
            "updated_at": record["updated_at"],
        }

        # Local store is the working copy; the chain is the audit log. Persist
        # locally first so a chain failure never loses the trust update.
        lhi_card_id = agent_entry.get("lhi_card_id")
        is_new_card = lhi_card_id is None
        if is_new_card:
            lhi_card_id = get_id(f"{agent_id}:lhi")
            agent_entry["lhi_card_id"] = lhi_card_id
        self.__save_trust_store(store)

        try:
            if is_new_card:
                self.provenance.create_new_provenance_card(
                    card_id=lhi_card_id, card_info=json.dumps(record)
                )
            else:
                self.provenance.append_to_provenance_card(
                    card_id=lhi_card_id, card_info=json.dumps(record)
                )
        except Exception as exc:
            raise RuntimeError(
                f"LHI record for agent {agent_id} saved locally but failed to "
                f"write to provenance card {lhi_card_id}: {exc}"
            ) from exc

        return trust


# ── HTTP boundary ──────────────────────────────────────────────────────────────

_cbac: CBAC | None = None


def _get_cbac() -> CBAC:
    global _cbac
    if _cbac is None:
        provenance = Provenance(
            name="cbac-service",
            api_key=os.environ.get("AGENTDNA_API_KEY", ""),
        )
        _cbac = CBAC(provenance=provenance)
    return _cbac


app = FastAPI()


@app.post("/authorize-cbac")
async def authorize_cbac(request: Request) -> PlainTextResponse:
    body = await request.json()

    try:
        result = await _get_cbac().verify_agent_app_interaction(
            agent_id=body.get("agent_id", ""),
            intended_action=body.get("intended_action"),
            user_intent=body.get("user_intent"),
        )
        decision, reason = result.decision, result.reason
    except Exception as exc:
        decision, reason = "error", str(exc)

    return PlainTextResponse(reason, headers={"X-CBAC-Decision": decision})


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("CBAC_SERVICE_HOST", "127.0.0.1"),
        port=int(os.environ.get("CBAC_SERVICE_PORT", "8767")),
    )
