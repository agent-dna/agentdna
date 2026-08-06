-- Automatically run on first database creation by docker-entrypoint-initdb.d.
-- Enables the required Postgres extensions for the CBAC service.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_textsearch;
