import asyncio
import base64
import hashlib
import json
from collections.abc import Callable
from typing import Any

import structlog
from sentence_transformers import SentenceTransformer
from sqlalchemy.ext.asyncio import AsyncSession

from agentdna.id import get_id
from agentdna.provenance import Provenance
from agentdna.types import AgentCard
from cbac_service.chunking import flatten_policy_chunks
from cbac_service.config import (
    ALLOW_GAP,
    CONTRADICTION_THRESHOLD,
    DENY_GAP,
    ENCODER_MODEL,
    ENTAILMENT_THRESHOLD,
    HHEM_MODEL,
    HYBRID_SEARCH_ENABLED,
    LHI_LAMBDA_DOWN,
    LHI_LAMBDA_UP,
    LHI_WEIGHTS,
    NLI_BATCH_SIZE,
    NLI_MODEL,
)
from cbac_service.db.repository import (
    agent_has_lhi_records,
    get_latest_trust,
    get_policy_chunks,
    insert_lhi_record,
    policy_hash_matches,
    save_policy_chunks,
)
from cbac_service.db.search import hybrid_search, vector_search
from cbac_service.skills import (
    CBACResult,
    _intended_action_text,
)

# TODO:- Fix return types in CBAC class

logger = structlog.get_logger("cbac_service.cbac")


def _policy_hash(policy: str) -> str:
    """Content hash keying the embedding cache. The precompute side (writes it)
    and the read side (compares it) must derive it identically, or cache
    invalidation breaks — so both go through here."""
    return hashlib.sha256(policy.encode()).hexdigest()


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

    # ── Model accessors (lazy-loaded) ─────────────────────────────────────────

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
                    raise ValueError("NLI model is not initialised")
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

    # ── Scoring helpers ───────────────────────────────────────────────────────

    def hallucination_score(self, source_text: str, generated_text: str) -> float:
        """Score how well ``generated_text`` is grounded in ``source_text``.

        Returns a score in [0, 1]: 1 = fully supported, 0 = hallucinated.
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

    # ── Policy processing ─────────────────────────────────────────────────────

    def _classify_chunks(self, chunks: list[str]) -> tuple[list[str], list[str]]:
        """NLI-classify each chunk as allowed or forbidden."""
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

    def _get_latest_agent_policy(self, agent_id: str) -> str:
        """Returns the latest decoded policy associated with an agent."""
        actor_card_dict = self.provenance.get_latest_provenance_record(actor_id=agent_id)
        actor_card = AgentCard(**actor_card_dict)
        try:
            return base64.b64decode(actor_card.policy).decode("utf-8")
        except Exception as exc:
            raise RuntimeError(f"failed to decode policy for agent {agent_id}: {exc}") from exc

    # ── Precompute (DB-backed) ────────────────────────────────────────────────

    async def index_policy(
        self,
        session: AsyncSession,
        agent_id: str,
        policy: str | None = None,
    ) -> int:
        """Precompute and cache policy vectors for an agent into Postgres.

        Steps:
        1. Take ``policy`` when supplied, else fetch it from the Provenance Layer.
        2. Flatten it to text chunks.
        3. NLI-classify each chunk into allowed / forbidden buckets.
        4. Encode all chunks with the bi-encoder.
        5. Persist to the policy_chunks table via the repository layer.

        Always recomputes — callers that want a cache check do it themselves
        (``verify_cbac`` compares the on-chain hash before calling).

        Passing ``policy`` warms the cache for a policy the chain does not carry
        yet. ``verify_cbac`` still re-reads the chain on every request, so a
        cached policy that disagrees with the chain is replaced on the next
        authorization — this is a cache warm, never a way to override the policy
        a decision is made against.

        Returns the number of chunks stored.
        """
        if policy is None:
            policy = self._get_latest_agent_policy(agent_id=agent_id)
        if not policy:
            raise RuntimeError(f"No policy found for agent {agent_id}")

        current_hash = _policy_hash(policy)

        # Flatten and classify.
        chunks = flatten_policy_chunks(policy)
        if not chunks:
            raise RuntimeError(f"Policy for agent {agent_id} produced no chunks")

        allowed_chunks, forbidden_chunks = await asyncio.to_thread(self._classify_chunks, chunks)

        # Encode all chunks.
        all_chunks = allowed_chunks + forbidden_chunks
        chunk_types = (["allowed"] * len(allowed_chunks)) + (["forbidden"] * len(forbidden_chunks))

        encoder = self._get_encoder()
        embeddings = await asyncio.to_thread(
            lambda: encoder.encode(all_chunks, normalize_embeddings=True)
        )

        # Persist to DB.
        count = await save_policy_chunks(
            session=session,
            agent_id=agent_id,
            chunks=all_chunks,
            chunk_types=chunk_types,
            embeddings=embeddings,
            policy_hash=current_hash,
        )

        return count

    # ── Check 1: NLI drift ────────────────────────────────────────────────────

    async def _check1_drift(
        self,
        user_intent: str,
        agent_action: str,
    ) -> tuple[tuple[str, str] | None, float]:
        """NLI drift check: does the agent's action contradict the user's intent?

        Returns ``((decision, reason), intent_score)`` if contradiction is
        strong enough to deny, else ``(None, intent_score)``.
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

    # ── Tiered decision (DB-backed search) ────────────────────────────────────

    async def _tiered_decision(
        self,
        session: AsyncSession,
        agent_id: str,
        intent_text: str,
    ) -> tuple[str, str, float | None]:
        """Tier 1 (cosine gap) → Tier 2 (NLI entailment) → Tier 3 (LLM).

        Uses pgvector search for Tier 1 instead of in-memory numpy operations.
        Returns ``(decision, reason, policy_score)``.
        """

        # Encode only the intent at runtime (~5 ms on CPU).
        encoder = self._get_encoder()
        intent_vec = await asyncio.to_thread(
            lambda: encoder.encode([intent_text], normalize_embeddings=True)[0]
        )

        # Tier 1: cosine gap via pgvector.
        allowed_results = await vector_search(
            session, agent_id, intent_vec, top_k=1, chunk_type="allowed"
        )
        forbidden_results = await vector_search(
            session, agent_id, intent_vec, top_k=1, chunk_type="forbidden"
        )

        allowed_score = allowed_results[0].score if allowed_results else 0.0
        forbidden_score = forbidden_results[0].score if forbidden_results else 0.0
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
        # Use hybrid search if enabled for better candidate selection,
        # otherwise fall back to the vector search result we already have.
        if HYBRID_SEARCH_ENABLED and allowed_results:
            top_results = await hybrid_search(
                session, agent_id, intent_vec, intent_text, top_k=1, chunk_type="allowed"
            )
            top_chunk = top_results[0].chunk_text if top_results else None
        elif allowed_results:
            top_chunk = allowed_results[0].chunk_text
        else:
            top_chunk = None

        if not top_chunk:
            return ("deny", "Tier 2: no allowed policy chunks found", None)

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

        # Tier 3: LLM judgment (optional).
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

        # Gather policy text for LLM context.
        all_chunks = await get_policy_chunks(session, agent_id)
        policy_text = "\n".join(all_chunks)

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

    # ── Main entry point ──────────────────────────────────────────────────────

    async def verify_cbac(
        self,
        session: AsyncSession,
        agent_id: str,
        intended_action: Any,
        user_intent: str | None = None,
    ) -> CBACResult:
        """Semantic intent verification against the agent's on-chain policy.

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
        session:
            Async DB session for repository/search calls.
        agent_id:
            Whose policy to fetch (via Provenance Layer).
        intended_action:
            The action the agent wants to perform (any shape, flattened to text).
        user_intent:
            The root user request. Enables Check-1 drift + hallucination score.

        Returns
        -------
        CBACResult with ``decision`` in ``{"allow", "deny", "advise"}``.
        Fail-closed: any unrecoverable error resolves to ``deny``.
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
            current_policy = await asyncio.to_thread(self._get_latest_agent_policy, agent_id)
        except Exception as e:
            return CBACResult(
                decision="deny", reason=f"Policy lookup failed for agent {agent_id}: {e}"
            )
        if not current_policy:
            return CBACResult(decision="deny", reason=f"No policy available for agent {agent_id}")

        current_hash = _policy_hash(current_policy)

        # Check DB cache — recompute if stale.
        cache_valid = await policy_hash_matches(session, agent_id, current_hash)
        if not cache_valid:
            try:
                await self.index_policy(session, agent_id, policy=current_policy)
            except Exception as e:
                return CBACResult(
                    decision="deny", reason=f"Policy unavailable for agent {agent_id}: {e}"
                )

        # Check that chunks actually exist.
        all_chunks = await get_policy_chunks(session, agent_id)
        if not all_chunks:
            return CBACResult(decision="deny", reason="Policy carries no analysable content")

        # Run the tiered decision pipeline (DB-backed search).
        decision, reason, policy_score = await self._tiered_decision(session, agent_id, intent_text)

        # Hallucination score — informational, never gates the decision.
        hallucination = None
        if user_intent:
            try:
                hallucination = await asyncio.to_thread(
                    self.hallucination_score, user_intent, intent_text
                )
            except Exception:
                logger.warning("hallucination scoring failed", decision=decision, exc_info=True)
                hallucination = None

        return CBACResult(
            decision=decision,
            reason=reason,
            hallucination_score=hallucination,
            intent_score=intent_score,
            policy_score=policy_score,
        )

    # TODO:- Verify writing provenance card.
    # TODO:- Remove output_score? In that case we can compute lhi score at the time of decision or it can be computed asynchornously\
    async def compute_lhi(
        self,
        session: AsyncSession,
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

        Storage: one ``lhi_records`` row per interaction — the table is the
        full trust history for every edge ((agent, callee_name, callee_type)),
        and the current trust is simply the edge's latest row (see
        ``repository.get_latest_trust`` / ``get_trust_history``). The record
        is also appended to a dedicated per-agent CBAC card
        (``{agent_id}:cbac``) on the Provenance Layer (created on the agent's
        first record), so the history is independently verifiable on-chain.
        Trust never overrides the per-interaction allow/deny gates — it is
        read pre-execution only to arbitrate the inconclusive ("advise") band.
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

        prev = await get_latest_trust(session, agent_id, callee_name, callee_type)
        if prev is None:
            trust = s
        else:
            lam = self._lhi_lambda_up if s >= prev else self._lhi_lambda_down
            trust = lam * prev + (1 - lam) * s

        # First record for this agent (any edge) -> its CBAC card doesn't
        # exist yet. Checked before the insert commits.
        is_new_agent = not await agent_has_lhi_records(session, agent_id)

        # DB is the working copy; the chain is the audit log. Commit first so
        # a chain failure never loses the trust update.
        db_record = await insert_lhi_record(
            session,
            agent_id=agent_id,
            callee_name=callee_name,
            callee_type=callee_type,
            intent_score=intent_score,
            policy_score=policy_score,
            hallucination_score=hallucination_score,
            output_score=output_score,
            trust=trust,
        )

        record = {
            "type": "lhi_record",
            "agent_id": agent_id,
            "callee": {"name": callee_name, "type": callee_type},
            "scores": scores,
            "trust": trust,
            "updated_at": db_record.created_at.isoformat(),
        }
        card_id = get_id(f"{agent_id}:cbac")

        try:
            # Chain writes are sync network calls — off the event loop.
            if is_new_agent:
                await asyncio.to_thread(
                    self.provenance.create_new_provenance_card,
                    card_id=card_id,
                    card_info=json.dumps(record),
                )
            else:
                await asyncio.to_thread(
                    self.provenance.append_to_provenance_card,
                    card_id=card_id,
                    card_info=json.dumps(record),
                )
        except Exception as exc:
            raise RuntimeError(
                f"LHI record for agent {agent_id} saved locally but failed to "
                f"write to provenance card {card_id}: {exc}"
            ) from exc

        return trust
