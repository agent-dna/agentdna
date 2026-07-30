-- pgvector schema for CBAC policy embeddings

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS policy_chunks (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    agent_id TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    chunk_type TEXT NOT NULL CHECK (chunk_type IN ('allowed', 'forbidden')),
    embedding vector(384) NOT NULL,
    policy_hash TEXT NOT NULL,
    section TEXT DEFAULT 'body',
    chunk_index INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS policy_meta (
    agent_id TEXT PRIMARY KEY,
    policy_hash TEXT NOT NULL,
    encoder TEXT NOT NULL,
    nli_model TEXT NOT NULL,
    chunk_count INTEGER DEFAULT 0,
    cached_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON policy_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_chunks_agent_type
    ON policy_chunks (agent_id, chunk_type);

CREATE INDEX IF NOT EXISTS idx_chunks_agent_hash
    ON policy_chunks (agent_id, policy_hash);
