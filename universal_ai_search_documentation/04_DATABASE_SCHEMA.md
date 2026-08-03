# Database Schema

## Database
PostgreSQL with `pgvector`, `pg_trgm`, and `citext`.

## Core tables

### workspaces
- id UUID PK
- name TEXT
- plan TEXT
- created_at TIMESTAMPTZ

### users
- id UUID PK
- email CITEXT UNIQUE
- password_hash TEXT NULL
- full_name TEXT
- status TEXT CHECK active|suspended|deleting
- created_at TIMESTAMPTZ
- updated_at TIMESTAMPTZ

### workspace_members
- workspace_id UUID FK
- user_id UUID FK
- role TEXT CHECK owner|admin|member
- PRIMARY KEY(workspace_id,user_id)

### sessions
- id UUID PK
- user_id UUID FK
- refresh_token_hash TEXT
- expires_at TIMESTAMPTZ
- revoked_at TIMESTAMPTZ NULL
- device_metadata JSONB

### connections
- id UUID PK
- workspace_id UUID FK
- user_id UUID FK
- provider TEXT
- status TEXT
- encrypted_credentials BYTEA
- granted_scopes TEXT[]
- sync_cursor JSONB
- last_successful_sync_at TIMESTAMPTZ
- last_error_code TEXT NULL
- created_at TIMESTAMPTZ

### sources
- id UUID PK
- workspace_id UUID FK
- connection_id UUID FK
- external_id TEXT
- source_type TEXT
- title TEXT
- canonical_url TEXT
- mime_type TEXT
- author_name TEXT
- created_external_at TIMESTAMPTZ NULL
- modified_external_at TIMESTAMPTZ NULL
- content_hash TEXT
- permissions_hash TEXT
- metadata JSONB
- deleted_at TIMESTAMPTZ NULL
- UNIQUE(connection_id, external_id)

### documents
- id UUID PK
- workspace_id UUID FK
- source_id UUID FK UNIQUE
- normalized_text TEXT
- language TEXT
- parser_version TEXT
- token_count INTEGER
- object_storage_key TEXT NULL
- indexed_at TIMESTAMPTZ NULL

### chunks
- id UUID PK
- workspace_id UUID FK
- document_id UUID FK
- chunk_index INTEGER
- heading_path TEXT[]
- content TEXT
- token_count INTEGER
- start_offset INTEGER NULL
- end_offset INTEGER NULL
- page_number INTEGER NULL
- line_start INTEGER NULL
- line_end INTEGER NULL
- metadata JSONB
- embedding VECTOR(<MODEL_DIMENSION>)
- UNIQUE(document_id, chunk_index)

### conversations
- id UUID PK
- workspace_id UUID FK
- user_id UUID FK
- title TEXT
- created_at TIMESTAMPTZ

### messages
- id UUID PK
- conversation_id UUID FK
- role TEXT
- content TEXT
- model TEXT NULL
- latency_ms INTEGER NULL
- created_at TIMESTAMPTZ

### citations
- id UUID PK
- message_id UUID FK
- source_id UUID FK
- chunk_id UUID FK
- claim_index INTEGER
- excerpt TEXT
- score DOUBLE PRECISION

### sync_jobs
- id UUID PK
- workspace_id UUID FK
- connection_id UUID FK
- job_type TEXT
- status TEXT
- cursor JSONB
- attempts INTEGER DEFAULT 0
- error_code TEXT NULL
- error_detail TEXT NULL
- started_at TIMESTAMPTZ NULL
- completed_at TIMESTAMPTZ NULL

### audit_events
- id UUID PK
- workspace_id UUID FK
- actor_user_id UUID NULL
- action TEXT
- target_type TEXT
- target_id UUID NULL
- ip_hash TEXT NULL
- metadata JSONB
- created_at TIMESTAMPTZ

## Required indexes
- sources(workspace_id, connection_id, external_id)
- sources USING gin(title gin_trgm_ops)
- documents USING gin(to_tsvector('english', normalized_text))
- chunks(workspace_id, document_id)
- chunks USING hnsw(embedding vector_cosine_ops)
- messages(conversation_id, created_at)
- sync_jobs(connection_id, status, created_at)

## Row-level security
Enable RLS for all tenant-owned tables. Policy must compare `workspace_id` with a trusted request-scoped setting populated by the API after authentication.
