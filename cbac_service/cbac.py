import asyncio
import base64
import hashlib
import json
import os
import pickle
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests
from sentence_transformers import SentenceTransformer

from agentdna.id import get_id
from agentdna.provenance import Provenance
from agentdna.types import MODEL_EMBEDDINGS_DIR, AgentCard, IntentWorkflow
from cbac_service.chunking import chunk_body_text
from cbac_service.config import (
    ALLOW_GAP,
    CONTRADICTION_THRESHOLD,
    DENY_GAP,
    ENCODER_MODEL,
    ENTAILMENT_THRESHOLD,
    HHEM_MODEL,
    LHI_LAMBDA_DOWN,
    LHI_LAMBDA_UP,
    LHI_WEIGHTS,
    NLI_BATCH_SIZE,
    NLI_MODEL,
    TRUST_STORE_FILE,
)
from cbac_service.skills import (
    CBACResult,
    SkillsCard,
    _intended_action_text,
    parse_skill_md,
)

# TODO:- Fix return types in CBAC class


def _policy_hash(policy: str) -> str:
    """Content hash keying the embedding cache. The precompute side (writes it)
    and the read side (compares it) must derive it identically, or cache
    invalidation breaks — so both go through here."""
    return hashlib.sha256(policy.encode()).hexdigest()


def _flatten_mapping(mapping: dict[str, Any]) -> list[str]:
    """Flatten a key→value mapping to "key: value" chunks — one per list item
    for list-valued keys, one per scalar otherwise. ``None`` values are skipped."""
    chunks: list[str] = []
    for key, value in mapping.items():
        if isinstance(value, list):
            chunks.extend(f"{key}: {item}" for item in value)
        elif value is not None:
            chunks.append(f"{key}: {value}")
    return chunks


class CBAC:
    def __init__(
        self,
        provenance: Provenance,
        cbac_url: str = "https://cbac-admin.agentdna.io",
        encoder_name: str = ENCODER_MODEL,
        nli_model_name: str = NLI_MODEL,
        llm_backend: Callable | None = None,
        allow_gap: float = ALLOW_GAP,
        deny_gap: float = DENY_GAP,
        hhem_model_name: str = HHEM_MODEL,
        lhi_weights: tuple[float, float, float, float] = LHI_WEIGHTS,
        lhi_lambda_up: float = LHI_LAMBDA_UP,
        lhi_lambda_down: float = LHI_LAMBDA_DOWN,
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
        self._nli_labels: dict[int, str] = {}

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

    def _nli_scores(self, premise: str, hypothesis: str) -> dict[str, float]:
        """Run NLI cross-encoder on a (premise, hypothesis) pair.

        Returns a dict like {'entailment': 0.82, 'contradiction': 0.05, 'neutral': 0.13}.
        Scores are softmax-normalised probabilities. Thin wrapper over the
        batched path — a single pair is just a batch of one.
        """
        return self._nli_scores_batch([(premise, hypothesis)])[0]

    def _nli_scores_batch(self, pairs: list[tuple[str, str]]) -> list[dict[str, float]]:
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
            batch_size=NLI_BATCH_SIZE,
            show_progress_bar=False,
        )
        probs = sp_softmax(raw, axis=1)
        return [
            {self._nli_labels.get(i, str(i)): float(row[i]) for i in range(len(row))}
            for row in probs
        ]

    def _flatten_policy_chunks(self, policy: Any) -> list[str]:
        """Flatten a policy of any shape into a list of text chunks.

        Each YAML frontmatter (or dict) entry becomes "key: value" (one chunk
        per list item for list-valued keys); the body is chunked by
        paragraph/list-item structure via :func:`chunk_body_text`, not by raw
        line. This unified path works for any skill.md schema — no hardcoded
        key names.
        """
        if isinstance(policy, SkillsCard):
            return _flatten_mapping(policy.raw_frontmatter) + chunk_body_text(policy.body)
        if isinstance(policy, str):
            stripped = policy.strip()
            if stripped.startswith("---"):
                try:
                    return self._flatten_policy_chunks(parse_skill_md(policy))
                except Exception:
                    pass
            return chunk_body_text(policy)
        if isinstance(policy, dict):
            return _flatten_mapping(policy)
        return []

    def _classify_chunks(self, chunks: list[str]) -> tuple[list[str], list[str]]:
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
        pairs: list[tuple[str, str]] = []
        for chunk in chunks:
            pairs.append((chunk, "This capability is permitted and allowed"))
            pairs.append((chunk, "This capability is prohibited and forbidden"))
        scores = self._nli_scores_batch(pairs)

        allowed: list[str] = []
        forbidden: list[str] = []
        for i, chunk in enumerate(chunks):
            allow_e = scores[2 * i].get("entailment", 0.0)
            forbid_e = scores[2 * i + 1].get("entailment", 0.0)
            if forbid_e > allow_e and forbid_e > 0.40:
                forbidden.append(chunk)
            else:
                allowed.append(chunk)
        return allowed, forbidden

    def _get_latest_agent_policy(
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

    def _get_embedding_file_path(self, agent_id: str) -> Path:
        """Return the .pkl path for this agent's precomputed policy vectors."""
        digest = hashlib.sha256(agent_id.encode()).hexdigest()[:32]
        return Path(self.embeddings_dir) / f"{digest}.pkl"

    def _save_to_embeddings_dir(self, agent_id: str, data: dict[str, Any]):
        path = self._get_embedding_file_path(agent_id)
        with path.open("wb") as f:
            pickle.dump(data, f)

    def _load_from_embeddings_dir(self, agent_id: str) -> dict[str, Any] | None:
        path = self._get_embedding_file_path(agent_id)
        if not path.exists():
            return None
        try:
            with path.open("rb") as f:
                return pickle.load(f)
        except Exception:
            return None

    def check_policy_embedding_exists(self, agent_id: str) -> dict[str, Any] | None:
        """
        Checks if the embedding file for an agent is present or not

        Returns None if its not present
        """
        embedding = self._load_from_embeddings_dir(agent_id=agent_id)
        return embedding

    async def precompute_policy(self, agent_id: str, skip_compute=True) -> dict[str, Any]:
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

        policy = self._get_latest_agent_policy(agent_id=agent_id)
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
        payload: dict[str, Any] = {
            "agent_id": agent_id,
            "allowed_chunks": allowed_chunks,
            "forbidden_chunks": forbidden_chunks,
            "allowed_vecs": allowed_vecs,
            "forbidden_vecs": forbidden_vecs,
            "policy_text": policy_text,
            "policy_hash": _policy_hash(policy),
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "encoder": self._encoder_name,
            "nli_model": self._nli_model_name,
        }

        await asyncio.to_thread(self._save_to_embeddings_dir, agent_id, payload)
        return payload

    async def _check1_drift(
        self,
        user_intent: str,
        agent_action: str,
    ) -> tuple[tuple[str, str] | None, float]:
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
        if contradiction >= CONTRADICTION_THRESHOLD:
            return (
                (
                    "deny",
                    (
                        f"Check 1 drift: user intent {user_intent!r} contradicts agent action "
                        f"{agent_action!r} (NLI contradiction={contradiction:.2f})"
                    ),
                ),
                intent_score,
            )
        return None, intent_score

    def _max_cosine(self, query_vec, chunk_vecs) -> float:
        """Maximum cosine similarity from query_vec to any row in chunk_vecs."""
        if chunk_vecs.shape[0] == 0:
            return 0.0
        sims = chunk_vecs @ query_vec  # both already L2-normalised by SentenceTransformer
        return float(np.max(sims))

    async def _tiered_decision(
        self,
        intent_text: str,
        intent_vec,
        allowed_chunks: list[str],
        forbidden_chunks: list[str],
        allowed_vecs,
        forbidden_vecs,
        policy_text: str,
    ) -> tuple[str, str, float | None]:
        """Tier 1 (cosine gap) → Tier 2 (NLI entailment) → Tier 3 (LLM).

        Returns ``(decision, reason, policy_score)``. ``policy_score`` is a
        [0, 1] normalization of whichever signal decided — the Tier 1 cosine
        gap rescaled against its own allow/deny thresholds (so it lands at
        exactly 1.0/0.0 on a clean Tier 1 allow/deny), or the Tier 2 NLI
        entailment as-is. Tier 3 (LLM judgment or no-backend "advise") has no
        numeric signal, so ``policy_score`` is ``None`` there — the caller
        must supply their own if they want to feed :meth:`compute_lhi`.

        Pure decision logic, extracted from :meth:`verify_cbac` so
        the caller can attach a hallucination score to the result exactly once,
        after a decision is reached, without duplicating it at every tier's exit.
        """
        # Tier 1: cosine gap.
        allowed_score = self._max_cosine(intent_vec, allowed_vecs)
        forbidden_score = self._max_cosine(intent_vec, forbidden_vecs)
        gap = allowed_score - forbidden_score

        span = self._allow_gap + self._deny_gap
        gap_score = max(0.0, min(1.0, (gap + self._deny_gap) / span)) if span > 0 else None

        if gap > self._allow_gap:
            return (
                "allow",
                (
                    f"Tier 1 cosine gap {gap:+.3f} > +{self._allow_gap} "
                    f"(allowed={allowed_score:.3f}, forbidden={forbidden_score:.3f})"
                ),
                gap_score,
            )
        if gap < -self._deny_gap:
            return (
                "deny",
                (
                    f"Tier 1 cosine gap {gap:+.3f} < -{self._deny_gap} "
                    f"(intent closer to forbidden than allowed policy)"
                ),
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

        if entailment >= ENTAILMENT_THRESHOLD:
            return ("allow", f"Tier 2 NLI entailment={entailment:.2f} vs {top_chunk!r}", entailment)
        if contradiction >= CONTRADICTION_THRESHOLD:
            return (
                "deny",
                f"Tier 2 NLI contradiction={contradiction:.2f} vs {top_chunk!r}",
                entailment,
            )

        # Tier 3: LLM judgment (optional). No numeric signal — policy_score is None.
        if self._llm_backend is None:
            return (
                "advise",
                (
                    f"Tier 1/2 inconclusive (gap={gap:+.3f}, "
                    f"entailment={entailment:.2f}, contradiction={contradiction:.2f}); "
                    "no LLM backend configured — caller must decide"
                ),
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

    async def verify_cbac(
        self,
        agent_id: str,
        intended_action: Any,
        user_intent: str | None = None,
    ) -> CBACResult:
        """
        Semantic intent verification against the agent's on-chain policy.

        Fetches the policy, flattens it into allowed/forbidden chunks (cached
        per policy hash), then runs the tiered decision — Tier 1 cosine gap →
        Tier 2 NLI entailment → Tier 3 optional LLM — in
        :meth:`_tiered_decision`; see that method for the per-tier thresholds
        and the ``policy_score`` normalization.

        A Check-1 NLI drift test runs first, but only when ``user_intent`` is
        supplied: it compares ``user_intent`` against the flattened
        ``intended_action`` text and denies immediately on a strong
        contradiction (``CONTRADICTION_THRESHOLD``).

        Parameters
        ----------
        agent_id:
            Whose policy to fetch (via :meth:`_get_latest_agent_policy`).
        intended_action:
            The action the agent wants to perform. Any shape — string, dict,
            or object — is flattened to text by :func:`_intended_action_text`.
        user_intent:
            The root user request. When given, enables the Check-1 drift test
            and the hallucination score; when omitted, both are skipped.

        Returns
        -------
        CBACResult with ``decision`` in ``{"allow", "deny", "advise"}``.
        Fail-closed: any unrecoverable error (no policy, empty content, model
        failure) resolves to ``deny``. The score fields are informational only
        — none of them ever changes ``decision``:

        - ``intent_score`` — from Check-1 (``1 - contradiction``), set whenever
          ``user_intent`` is supplied.
        - ``policy_score`` — set when Tier 1 or 2 decides; ``None`` on Tier 3.
        - ``hallucination_score`` — HHEM grounding of ``intended_action`` in
          ``user_intent`` (1 = grounded), set once a decision is reached and
          ``user_intent`` is present.
        """
        intent_text = _intended_action_text(intended_action)
        if not intent_text.strip():
            return CBACResult(
                decision="deny", reason="Intended action carries no analysable content"
            )

        # Check 1: NLI drift — only runs when caller supplies the root user intent.
        intent_score: float | None = None
        if user_intent:
            drift, intent_score = await self._check1_drift(user_intent, intent_text)
            if drift is not None:
                decision, reason = drift
                return CBACResult(decision=decision, reason=reason, intent_score=intent_score)

        # Fetch current policy from chain — always, so we can detect updates.
        try:
            current_policy = self._get_latest_agent_policy(agent_id)
        except Exception as e:
            return CBACResult(
                decision="deny", reason=f"Policy lookup failed for agent {agent_id}: {e}"
            )
        if not current_policy:
            return CBACResult(decision="deny", reason=f"No policy available for agent {agent_id}")

        current_hash = _policy_hash(current_policy)

        # Load local cache and validate against current policy hash.
        cached = await asyncio.to_thread(self._load_from_embeddings_dir, agent_id)

        if cached is None or cached.get("policy_hash") != current_hash:
            # Cache miss or policy updated on chain — recompute and persist.
            try:
                cached = await self.precompute_policy(agent_id)
            except Exception as e:
                return CBACResult(
                    decision="deny", reason=f"Policy unavailable for agent {agent_id}: {e}"
                )

        allowed_chunks: list[str] = cached["allowed_chunks"]
        forbidden_chunks: list[str] = cached["forbidden_chunks"]
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

        decision, reason, policy_score = await self._tiered_decision(
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

    # TODO:- Remove
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

    # The LHI trust store — one JSON object per agent, keyed by agent DID.
    # `callees` holds one entry per (agent → callee) edge: the four component
    # scores of the *latest* interaction, plus the EMA trust carried across all
    # of them. The append-only history lives on the agent's LHI provenance card
    # (`lhi_card_id`); this file is only the working copy of the current state.
    #
    #   {
    #     "<agent_did>": {
    #       "lhi_card_id": "Qm...",                    # set on first write
    #       "callees": {
    #         "<callee_name>": {
    #           "type": "tool" | "agent" | "mcp",
    #           "scores": {"intent": 0.9, "policy": 0.8,
    #                      "hallucination": 0.95, "output": 1.0},
    #           "trust": 0.87,                         # EMA over interactions
    #           "updated_at": "<iso8601>"
    #         }
    #       }
    #     }
    #   }
    #
    # TODO:- Replace trust file by light db.
    # Fix LHI store structure.
    @property
    def _trust_store_path(self) -> Path:
        return Path(self.provenance.config_dir) / TRUST_STORE_FILE

    def _load_trust_store(self) -> dict[str, Any]:
        try:
            with self._trust_store_path.open("r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_trust_store(self, store: dict[str, Any]):
        with self._trust_store_path.open("w") as f:
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
        """Update and return the LHI (Local Heuristic Intelligence) trust
        score for one (agent → callee) edge.

        Called *after* the interaction executed, once all four per-interaction
        scores (each in [0, 1], higher is better) are known:

        1. Instantaneous quality ``s`` = weighted **arithmetic** mean of the
           four scores — the expected quality of the interaction. Deliberately
           compensatory: hard constraints are already enforced by the
           allow/deny gates *before* execution, and this update only ever sees
           interactions that passed them, so the reputation's job is unbiased
           longitudinal estimation, not constraint enforcement.
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

        s = sum(
            value * weight for value, weight in zip(scores.values(), self._lhi_weights, strict=True)
        )

        store = self._load_trust_store()
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
        self._save_trust_store(store)

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
