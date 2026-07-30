SELECT chunk_text,
       1 - (embedding <=> %s::vector) AS similarity
FROM policy_chunks
WHERE agent_id = %s AND chunk_type = %s
ORDER BY embedding <=> %s::vector
LIMIT 1
