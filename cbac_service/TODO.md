# TODO — cbac_service

Open work for the CBAC decision service. Grouped by priority; the LHI items
carry the reasoning from the design discussion so it isn't re-litigated.

## Correctness — LHI

- [ ] **Renormalize over observed scores instead of skipping the record.**
      Today a record is written only when *all* of intent/policy/hallucination
      are present ([guard.py](../agentdna/cbac/guard.py) `_report_lhi`), which
      is complete-case analysis over data that is **not** missing at random:
      `policy_score` is `None` exactly on Tier-3 gray-zone decisions, and
      intent/hallucination are `None` whenever no `user_intent` was supplied.
      Net effect: the interactions trust is meant to arbitrate never build
      trust, and a deployment without `user_intent` accumulates **no records
      at all**, silently.
      Fix: `s = Σ_{observed} wᵢxᵢ / Σ_{observed} wᵢ`, store the missing
      component as `null` (never a substituted 0.5 — the on-chain record must
      stay honest about what was measured). Guard rail: require ≥1 *semantic*
      score; skip when only `output_score` survives, or trust silently becomes
      a pure reliability tracker.
      Touches: `compute_lhi` (Optional params + renormalize), `_report_lhi`
      skip condition, `tests/test_cbac_lhi.py`.

- [x] **Render actions as natural language before scoring them.** Measured on
      14 realistic agent cases (2026-08-03): both Check-1 NLI and HHEM degrade
      badly on flattened `tool k=v` intent text. NLI missed direct opposites
      hidden in snake_case tool names (`close_pull_request` vs "do not close
      anything" → contradiction 0.01, caught at 0.95 as prose), and HHEM
      scores every tool-syntax string as ungrounded.
      **Implemented (2026-08-03):** the guard's `_default_intent` renders
      "The agent wants to <verb phrase>, with k = v, …" — verb phrase = the
      wrapped function's docstring first line, else the de-snaked tool name.
      All three scorers receive this one full-prose string; no wire change.
      **Decision:** HHEM scores the params too (they are the LLM-generated
      content). Accepted tradeoff, measured: with params in view, legitimate
      params_added calls (HHEM 0.09) are inseparable from hijacks (0.05), so
      hallucination thresholds must stay advisory. The verb-only view
      (faithful 0.53 / params_added 0.62 vs hijack 0.04) remains a one-line
      switch in `verify_cbac` — `intent_text.split(", with ", 1)[0]` — via
      the `_action_summary` helper kept in guard.py, if separation matters
      later.

- [ ] **Record violation / denial evidence.** A denied call writes nothing
      (correct — nothing executed), so an agent probing forbidden actions 50
      times keeps pristine trust. Needs a separate per-edge negative-evidence
      stream (counter, or beta-style `α/β`) that feeds the EMA without
      pretending a denial was an interaction. Biggest blind spot today.

- [ ] **Continuous `output_score` instead of binary.** Currently 1.0/0.0 from
      "did it raise / return an error dict". Post-execution the tool's actual
      output can be scored: HHEM grounding of output vs. request (model already
      loaded), schema validity, empty/error-shaped payloads. Upgrades the
      weakest component from {0,1} to [0,1].

- [ ] **Time decay + evidence volume in the update rule.** The EMA is
      per-interaction, so an edge idle for months keeps frozen trust — decay
      toward a neutral prior on wall-clock time. And expose the interaction
      count (or beta `α+β`) alongside `T`, refusing to let trust arbitrate the
      gray zone below a minimum-evidence threshold (cold start: one good call
      ≠ trusted edge).

## Deferred LHI candidates

Evaluated and parked — each adds a noisy estimator needing its own tuning, and
dilutes the weights of the four components already justified. Revisit only
after the core is validated against real workflows.

- [ ] Score variance / dispersion penalty — an erratic agent is riskier than a
      steadily mediocre one.
- [ ] Operational reliability detail (latency, timeouts, retries).
- [ ] Least-privilege adherence; cross-agent reputation rollup (EigenTrust-style,
      the per-edge store is already shaped for it); human feedback labels;
      behavioral anomaly scoring.

## Engineering

- [x] **Replace the trust JSON file with a light DB** (done 2026-08-07). LHI
      now lives in the `lhi_records` Postgres table — append-only, one row per
      interaction, so the full trust history per edge is kept (no more
      last-value-wins) and current trust = the edge's latest row. Migration
      `7b21c9e4d3a8`. Remaining narrower race: two concurrent updates for the
      *same edge* can read the same prev trust before both insert — fix with
      `SELECT ... FOR UPDATE` on the latest row or a serializable transaction
      if concurrent per-edge traffic becomes real.
- [ ] **Fix return types in the `CBAC` class** (`cbac.py:42`).
- [ ] **Remove `authorise_agent_app_interaction`** (`cbac.py:592`) — superseded
      by the guard + `/authorize-cbac`; it also makes the engine an HTTP
      *client*, which the rest of the class isn't.
- [ ] Score-transport hardening: `/compute-lhi` trusts caller-supplied
      intent/policy/hallucination scores, so a malicious guard can inflate its
      own reputation. Documented tradeoff (the guard is already trusted to make
      the authorize call at all); revisit with server-side correlation
      (`interaction_id` → cached scores) if the threat model tightens.
- [ ] No CI for `cbac_service/tests/` — the root workflow only runs the
      library's `tests/`.
- [ ] Add Readme