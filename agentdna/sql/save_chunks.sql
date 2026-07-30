INSERT INTO policy_chunks
    (agent_id, chunk_text, chunk_type, embedding, policy_hash, section, chunk_index)
VALUES (%s, %s, %s, %s::vector, %s, %s, %s)
