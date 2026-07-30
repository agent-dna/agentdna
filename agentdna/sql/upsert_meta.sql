INSERT INTO policy_meta
    (agent_id, policy_hash, encoder, nli_model, chunk_count, cached_at)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (agent_id) DO UPDATE SET
    policy_hash = EXCLUDED.policy_hash,
    encoder = EXCLUDED.encoder,
    nli_model = EXCLUDED.nli_model,
    chunk_count = EXCLUDED.chunk_count,
    cached_at = EXCLUDED.cached_at
