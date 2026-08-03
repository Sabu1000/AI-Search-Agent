SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_migrator') THEN
        CREATE ROLE app_migrator NOLOGIN NOSUPERUSER BYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
        CREATE ROLE app_api NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') THEN
        CREATE ROLE app_worker NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_audit_reader') THEN
        CREATE ROLE app_audit_reader NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$$;

DO $$
BEGIN
    EXECUTE format('GRANT app_migrator TO %I', current_user);
END
$$;

CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION app_migrator;
REVOKE ALL ON SCHEMA app FROM PUBLIC;

CREATE OR REPLACE FUNCTION app.current_workspace_id()
RETURNS UUID
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog
AS $$
DECLARE
    configured_value TEXT;
BEGIN
    configured_value := current_setting('app.workspace_id', true);
    IF configured_value IS NULL OR configured_value = '' THEN
        RETURN NULL;
    END IF;
    RETURN configured_value::UUID;
EXCEPTION
    WHEN invalid_text_representation THEN RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION app.current_user_id()
RETURNS UUID
LANGUAGE plpgsql
STABLE
SET search_path = pg_catalog
AS $$
DECLARE
    configured_value TEXT;
BEGIN
    configured_value := current_setting('app.user_id', true);
    IF configured_value IS NULL OR configured_value = '' THEN
        RETURN NULL;
    END IF;
    RETURN configured_value::UUID;
EXCEPTION
    WHEN invalid_text_representation THEN RETURN NULL;
END
$$;

CREATE TABLE app.users (
    id UUID PRIMARY KEY,
    email CITEXT NOT NULL,
    password_hash TEXT,
    full_name TEXT NOT NULL,
    status TEXT NOT NULL,
    email_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lock_version INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT uq_users_email UNIQUE (email),
    CONSTRAINT ck_users_full_name_length CHECK (char_length(full_name) BETWEEN 1 AND 200),
    CONSTRAINT ck_users_status CHECK (status IN ('pending_verification', 'active', 'suspended', 'deleting')),
    CONSTRAINT ck_users_lock_version CHECK (lock_version > 0)
);

CREATE TABLE app.workspaces (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    plan TEXT NOT NULL,
    status TEXT NOT NULL,
    authorization_version BIGINT NOT NULL DEFAULT 1,
    search_index_generation BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lock_version INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT ck_workspaces_name_length CHECK (char_length(name) BETWEEN 1 AND 200),
    CONSTRAINT ck_workspaces_plan CHECK (plan IN ('free', 'paid', 'internal')),
    CONSTRAINT ck_workspaces_status CHECK (status IN ('active', 'suspended', 'deleting')),
    CONSTRAINT ck_workspaces_authorization_version CHECK (authorization_version > 0),
    CONSTRAINT ck_workspaces_index_generation CHECK (search_index_generation > 0),
    CONSTRAINT ck_workspaces_lock_version CHECK (lock_version > 0)
);

CREATE TABLE app.workspace_members (
    workspace_id UUID NOT NULL,
    user_id UUID NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_workspace_members PRIMARY KEY (workspace_id, user_id),
    CONSTRAINT fk_workspace_members_workspace FOREIGN KEY (workspace_id)
        REFERENCES app.workspaces (id) ON DELETE CASCADE,
    CONSTRAINT fk_workspace_members_user FOREIGN KEY (user_id)
        REFERENCES app.users (id) ON DELETE CASCADE,
    CONSTRAINT ck_workspace_members_role CHECK (role IN ('owner', 'admin', 'member')),
    CONSTRAINT ck_workspace_members_status CHECK (status IN ('active', 'suspended', 'deleting'))
);

CREATE OR REPLACE FUNCTION app.has_active_workspace_membership(requested_workspace_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, app
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM app.workspace_members AS membership
        JOIN app.workspaces AS workspace ON workspace.id = membership.workspace_id
        WHERE membership.workspace_id = requested_workspace_id
          AND membership.user_id = app.current_user_id()
          AND membership.status = 'active'
          AND workspace.status = 'active'
    )
$$;

ALTER FUNCTION app.current_workspace_id() OWNER TO app_migrator;
ALTER FUNCTION app.current_user_id() OWNER TO app_migrator;
ALTER FUNCTION app.has_active_workspace_membership(UUID) OWNER TO app_migrator;
REVOKE ALL ON FUNCTION app.current_workspace_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION app.current_user_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION app.has_active_workspace_membership(UUID) FROM PUBLIC;

CREATE TABLE app.auth_identities (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    email_at_link_time CITEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    CONSTRAINT fk_auth_identities_user FOREIGN KEY (user_id)
        REFERENCES app.users (id) ON DELETE CASCADE,
    CONSTRAINT uq_auth_identities_issuer_subject UNIQUE (issuer, subject),
    CONSTRAINT ck_auth_identities_issuer_length CHECK (char_length(issuer) BETWEEN 1 AND 255),
    CONSTRAINT ck_auth_identities_subject_length CHECK (char_length(subject) BETWEEN 1 AND 512)
);

CREATE TABLE app.one_time_tokens (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    purpose TEXT NOT NULL,
    token_hash BYTEA NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    attempt_count SMALLINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_one_time_tokens_user FOREIGN KEY (user_id)
        REFERENCES app.users (id) ON DELETE CASCADE,
    CONSTRAINT uq_one_time_tokens_hash UNIQUE (token_hash),
    CONSTRAINT ck_one_time_tokens_purpose CHECK (purpose IN ('verify_email', 'reset_password', 'change_email')),
    CONSTRAINT ck_one_time_tokens_attempt_count CHECK (attempt_count >= 0),
    CONSTRAINT ck_one_time_tokens_expiry CHECK (expires_at > created_at),
    CONSTRAINT ck_one_time_tokens_consumed CHECK (consumed_at IS NULL OR consumed_at >= created_at)
);

CREATE TABLE app.sessions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    refresh_token_hash BYTEA NOT NULL,
    family_id UUID NOT NULL,
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    rotated_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    replaced_by_session_id UUID,
    device_metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    last_seen_at TIMESTAMPTZ,
    CONSTRAINT fk_sessions_user FOREIGN KEY (user_id)
        REFERENCES app.users (id) ON DELETE CASCADE,
    CONSTRAINT fk_sessions_replacement FOREIGN KEY (replaced_by_session_id)
        REFERENCES app.sessions (id) ON DELETE SET NULL,
    CONSTRAINT uq_sessions_refresh_token_hash UNIQUE (refresh_token_hash),
    CONSTRAINT ck_sessions_expiry CHECK (expires_at > issued_at),
    CONSTRAINT ck_sessions_device_metadata_object CHECK (jsonb_typeof(device_metadata) = 'object'),
    CONSTRAINT ck_sessions_device_metadata_size CHECK (octet_length(device_metadata::TEXT) <= 16384)
);

CREATE TABLE app.oauth_transactions (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    user_id UUID NOT NULL,
    provider TEXT NOT NULL,
    state_hash BYTEA NOT NULL,
    nonce_hash BYTEA NOT NULL,
    pkce_verifier_ciphertext BYTEA,
    encrypted_data_key BYTEA,
    key_version INTEGER,
    redirect_path TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_oauth_transactions_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_oauth_transactions_membership FOREIGN KEY (workspace_id, user_id)
        REFERENCES app.workspace_members (workspace_id, user_id) ON DELETE CASCADE,
    CONSTRAINT uq_oauth_transactions_state_hash UNIQUE (state_hash),
    CONSTRAINT uq_oauth_transactions_nonce_hash UNIQUE (nonce_hash),
    CONSTRAINT ck_oauth_transactions_provider CHECK (provider IN ('google', 'github_login')),
    CONSTRAINT ck_oauth_transactions_envelope CHECK (
        (pkce_verifier_ciphertext IS NULL AND encrypted_data_key IS NULL AND key_version IS NULL)
        OR (pkce_verifier_ciphertext IS NOT NULL AND encrypted_data_key IS NOT NULL AND key_version > 0)
    ),
    CONSTRAINT ck_oauth_transactions_redirect CHECK (
        redirect_path ~ '^/[A-Za-z0-9/_-]*$' AND redirect_path !~ '^//'
    ),
    CONSTRAINT ck_oauth_transactions_expiry CHECK (expires_at > created_at)
);

CREATE TABLE app.connections (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    owner_user_id UUID NOT NULL,
    provider TEXT NOT NULL,
    external_account_id_hash BYTEA,
    display_label TEXT NOT NULL,
    status TEXT NOT NULL,
    credential_ciphertext BYTEA,
    encrypted_data_key BYTEA,
    key_version INTEGER,
    last_successful_sync_at TIMESTAMPTZ,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    lock_version INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT uq_connections_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_connections_workspace FOREIGN KEY (workspace_id)
        REFERENCES app.workspaces (id) ON DELETE CASCADE,
    CONSTRAINT fk_connections_owner FOREIGN KEY (workspace_id, owner_user_id)
        REFERENCES app.workspace_members (workspace_id, user_id)
        ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT ck_connections_provider CHECK (provider IN ('google', 'github', 'local_files')),
    CONSTRAINT ck_connections_label_length CHECK (char_length(display_label) BETWEEN 1 AND 200),
    CONSTRAINT ck_connections_status CHECK (status IN ('pending', 'active', 'reauthorization_required', 'error', 'deleting', 'deleted')),
    CONSTRAINT ck_connections_envelope CHECK (
        (credential_ciphertext IS NULL AND encrypted_data_key IS NULL AND key_version IS NULL)
        OR (credential_ciphertext IS NOT NULL AND encrypted_data_key IS NOT NULL AND key_version > 0)
    ),
    CONSTRAINT ck_connections_error_code_length CHECK (last_error_code IS NULL OR char_length(last_error_code) <= 100),
    CONSTRAINT ck_connections_lock_version CHECK (lock_version > 0)
);

CREATE UNIQUE INDEX uq_connections_active_external_account
    ON app.connections (workspace_id, provider, external_account_id_hash)
    WHERE external_account_id_hash IS NOT NULL AND status <> 'deleted';

CREATE TABLE app.connection_scopes (
    workspace_id UUID NOT NULL,
    connection_id UUID NOT NULL,
    scope TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_connection_scopes PRIMARY KEY (connection_id, scope),
    CONSTRAINT fk_connection_scopes_connection FOREIGN KEY (workspace_id, connection_id)
        REFERENCES app.connections (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_connection_scopes_scope_length CHECK (char_length(scope) BETWEEN 1 AND 255)
);

CREATE TABLE app.connection_cursors (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    connection_id UUID NOT NULL,
    stream TEXT NOT NULL,
    cursor JSONB NOT NULL,
    cursor_version BIGINT NOT NULL DEFAULT 1,
    committed_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_connection_cursors_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_connection_cursors_connection FOREIGN KEY (workspace_id, connection_id)
        REFERENCES app.connections (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_connection_cursors_connection_stream UNIQUE (connection_id, stream),
    CONSTRAINT ck_connection_cursors_stream_length CHECK (char_length(stream) BETWEEN 1 AND 100),
    CONSTRAINT ck_connection_cursors_object CHECK (jsonb_typeof(cursor) = 'object'),
    CONSTRAINT ck_connection_cursors_size CHECK (octet_length(cursor::TEXT) <= 16384),
    CONSTRAINT ck_connection_cursors_version CHECK (cursor_version > 0)
);

CREATE TABLE app.source_collections (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    connection_id UUID NOT NULL,
    provider_external_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    parent_collection_id UUID,
    path_display TEXT,
    selected BOOLEAN NOT NULL DEFAULT false,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_source_collections_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT uq_source_collections_workspace_connection_id UNIQUE (workspace_id, connection_id, id),
    CONSTRAINT fk_source_collections_connection FOREIGN KEY (workspace_id, connection_id)
        REFERENCES app.connections (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_source_collections_parent FOREIGN KEY (workspace_id, connection_id, parent_collection_id)
        REFERENCES app.source_collections (workspace_id, connection_id, id) ON DELETE SET NULL (parent_collection_id),
    CONSTRAINT uq_source_collections_external UNIQUE (connection_id, kind, provider_external_id),
    CONSTRAINT ck_source_collections_kind CHECK (kind IN ('repository', 'folder', 'mailbox')),
    CONSTRAINT ck_source_collections_name_length CHECK (char_length(name) BETWEEN 1 AND 500),
    CONSTRAINT ck_source_collections_path_length CHECK (path_display IS NULL OR char_length(path_display) <= 2000),
    CONSTRAINT ck_source_collections_status CHECK (status IN ('active', 'removed', 'deleting'))
);

CREATE TABLE app.devices (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    user_id UUID NOT NULL,
    connection_id UUID NOT NULL,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    app_version TEXT NOT NULL,
    public_key BYTEA NOT NULL,
    credential_hash BYTEA NOT NULL,
    status TEXT NOT NULL,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    lock_version INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT uq_devices_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_devices_membership FOREIGN KEY (workspace_id, user_id)
        REFERENCES app.workspace_members (workspace_id, user_id) ON DELETE CASCADE,
    CONSTRAINT fk_devices_connection FOREIGN KEY (workspace_id, connection_id)
        REFERENCES app.connections (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_devices_public_key UNIQUE (public_key),
    CONSTRAINT uq_devices_credential_hash UNIQUE (credential_hash),
    CONSTRAINT ck_devices_name_length CHECK (char_length(name) BETWEEN 1 AND 200),
    CONSTRAINT ck_devices_platform CHECK (platform IN ('macos', 'windows')),
    CONSTRAINT ck_devices_app_version_length CHECK (char_length(app_version) BETWEEN 1 AND 50),
    CONSTRAINT ck_devices_status CHECK (status IN ('pending', 'active', 'revoked', 'deleting')),
    CONSTRAINT ck_devices_lock_version CHECK (lock_version > 0)
);

CREATE TABLE app.device_folders (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    device_id UUID NOT NULL,
    external_root_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    absolute_path_hash BYTEA NOT NULL,
    ignore_rules_hash BYTEA NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_device_folders_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_device_folders_device FOREIGN KEY (workspace_id, device_id)
        REFERENCES app.devices (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_device_folders_external_root UNIQUE (device_id, external_root_id),
    CONSTRAINT ck_device_folders_root_length CHECK (char_length(external_root_id) BETWEEN 1 AND 512),
    CONSTRAINT ck_device_folders_name_length CHECK (char_length(display_name) BETWEEN 1 AND 500),
    CONSTRAINT ck_device_folders_status CHECK (status IN ('active', 'removed', 'deleting'))
);

CREATE TABLE app.provider_events (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    connection_id UUID NOT NULL,
    provider_event_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_hash BYTEA NOT NULL,
    signature_verified_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    CONSTRAINT uq_provider_events_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_provider_events_connection FOREIGN KEY (workspace_id, connection_id)
        REFERENCES app.connections (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_provider_events_delivery UNIQUE (connection_id, provider_event_id),
    CONSTRAINT ck_provider_events_id_length CHECK (char_length(provider_event_id) BETWEEN 1 AND 512),
    CONSTRAINT ck_provider_events_type_length CHECK (char_length(event_type) BETWEEN 1 AND 100),
    CONSTRAINT ck_provider_events_status CHECK (status IN ('accepted', 'processed', 'ignored', 'failed'))
);

CREATE TABLE app.sources (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    connection_id UUID NOT NULL,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    mime_type TEXT,
    file_extension TEXT,
    canonical_url TEXT,
    author_display TEXT,
    source_timestamp TIMESTAMPTZ,
    source_timestamp_kind TEXT,
    content_hash BYTEA NOT NULL,
    permissions_hash BYTEA NOT NULL,
    current_document_version_id UUID,
    state TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    lock_version INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT uq_sources_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT uq_sources_workspace_connection_id UNIQUE (workspace_id, connection_id, id),
    CONSTRAINT fk_sources_connection FOREIGN KEY (workspace_id, connection_id)
        REFERENCES app.connections (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_sources_connection_external UNIQUE (connection_id, external_id),
    CONSTRAINT ck_sources_provider CHECK (provider IN ('gmail', 'google_drive', 'github', 'local_files')),
    CONSTRAINT ck_sources_type CHECK (source_type IN ('email', 'attachment', 'file', 'issue', 'pull_request', 'review', 'commit', 'code')),
    CONSTRAINT ck_sources_title_length CHECK (char_length(title) BETWEEN 1 AND 2000),
    CONSTRAINT ck_sources_mime_length CHECK (mime_type IS NULL OR char_length(mime_type) <= 255),
    CONSTRAINT ck_sources_extension_length CHECK (file_extension IS NULL OR char_length(file_extension) <= 50),
    CONSTRAINT ck_sources_url CHECK (canonical_url IS NULL OR canonical_url ~ '^https://[^[:space:]@]+'),
    CONSTRAINT ck_sources_timestamp_pair CHECK ((source_timestamp IS NULL) = (source_timestamp_kind IS NULL)),
    CONSTRAINT ck_sources_timestamp_kind CHECK (source_timestamp_kind IS NULL OR source_timestamp_kind IN ('sent', 'modified', 'authored', 'created')),
    CONSTRAINT ck_sources_state CHECK (state IN ('active', 'permission_blocked', 'deleting', 'deleted')),
    CONSTRAINT ck_sources_metadata_object CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT ck_sources_metadata_size CHECK (octet_length(metadata::TEXT) <= 65536),
    CONSTRAINT ck_sources_lock_version CHECK (lock_version > 0)
);

CREATE TABLE app.source_people (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    source_id UUID NOT NULL,
    relationship TEXT NOT NULL,
    identity_kind TEXT NOT NULL,
    normalized_identifier CITEXT NOT NULL,
    display_name TEXT,
    CONSTRAINT uq_source_people_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_source_people_source FOREIGN KEY (workspace_id, source_id)
        REFERENCES app.sources (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_source_people_identity UNIQUE (source_id, relationship, identity_kind, normalized_identifier),
    CONSTRAINT ck_source_people_relationship CHECK (relationship IN ('author', 'sender', 'recipient', 'owner', 'participant', 'reviewer')),
    CONSTRAINT ck_source_people_identity_kind CHECK (identity_kind IN ('email', 'provider_user', 'display_name')),
    CONSTRAINT ck_source_people_identifier_length CHECK (char_length(normalized_identifier) BETWEEN 1 AND 512),
    CONSTRAINT ck_source_people_display_length CHECK (display_name IS NULL OR char_length(display_name) <= 500)
);

CREATE TABLE app.document_versions (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    source_id UUID NOT NULL,
    version_key BYTEA NOT NULL,
    state TEXT NOT NULL,
    normalized_text TEXT,
    language TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    content_hash BYTEA NOT NULL,
    permissions_hash BYTEA NOT NULL,
    token_count BIGINT NOT NULL,
    extracted_bytes BIGINT NOT NULL,
    object_storage_key TEXT,
    failure_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ready_at TIMESTAMPTZ,
    superseded_at TIMESTAMPTZ,
    CONSTRAINT uq_document_versions_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT uq_document_versions_workspace_source_id UNIQUE (workspace_id, source_id, id),
    CONSTRAINT uq_document_versions_source_id_id UNIQUE (source_id, id),
    CONSTRAINT fk_document_versions_source FOREIGN KEY (workspace_id, source_id)
        REFERENCES app.sources (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_document_versions_source_key UNIQUE (source_id, version_key),
    CONSTRAINT ck_document_versions_state CHECK (state IN ('pending', 'ready', 'failed', 'superseded', 'deleting')),
    CONSTRAINT ck_document_versions_ready_text CHECK (state <> 'ready' OR normalized_text IS NOT NULL),
    CONSTRAINT ck_document_versions_language_length CHECK (char_length(language) BETWEEN 1 AND 50),
    CONSTRAINT ck_document_versions_parser_length CHECK (char_length(parser_version) BETWEEN 1 AND 100),
    CONSTRAINT ck_document_versions_chunker_length CHECK (char_length(chunker_version) BETWEEN 1 AND 100),
    CONSTRAINT ck_document_versions_token_count CHECK (token_count >= 0),
    CONSTRAINT ck_document_versions_extracted_bytes CHECK (extracted_bytes >= 0),
    CONSTRAINT ck_document_versions_object_key CHECK (object_storage_key IS NULL OR (char_length(object_storage_key) BETWEEN 1 AND 1024 AND object_storage_key !~ '://')),
    CONSTRAINT ck_document_versions_failure_code CHECK (failure_code IS NULL OR char_length(failure_code) <= 100)
);

ALTER TABLE app.sources
    ADD CONSTRAINT fk_sources_current_document_version
    FOREIGN KEY (id, current_document_version_id)
    REFERENCES app.document_versions (source_id, id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE app.chunks (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_hash BYTEA NOT NULL,
    heading_path TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    content TEXT NOT NULL,
    search_config REGCONFIG NOT NULL DEFAULT 'english'::REGCONFIG,
    search_vector TSVECTOR GENERATED ALWAYS AS (to_tsvector(search_config, content)) STORED,
    token_count INTEGER NOT NULL,
    start_offset INTEGER,
    end_offset INTEGER,
    page_number INTEGER,
    line_start INTEGER,
    line_end INTEGER,
    narrow_section_key TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    CONSTRAINT uq_chunks_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT uq_chunks_workspace_document_id UNIQUE (workspace_id, document_version_id, id),
    CONSTRAINT fk_chunks_document_version FOREIGN KEY (workspace_id, document_version_id)
        REFERENCES app.document_versions (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_chunks_document_index UNIQUE (document_version_id, chunk_index),
    CONSTRAINT uq_chunks_document_hash UNIQUE (document_version_id, chunk_hash),
    CONSTRAINT ck_chunks_index CHECK (chunk_index >= 0),
    CONSTRAINT ck_chunks_content CHECK (char_length(content) > 0),
    CONSTRAINT ck_chunks_token_count CHECK (token_count > 0),
    CONSTRAINT ck_chunks_offsets CHECK (
        (start_offset IS NULL AND end_offset IS NULL)
        OR (start_offset >= 0 AND end_offset > start_offset)
    ),
    CONSTRAINT ck_chunks_page CHECK (page_number IS NULL OR page_number > 0),
    CONSTRAINT ck_chunks_lines CHECK (
        (line_start IS NULL AND line_end IS NULL)
        OR (line_start > 0 AND line_end >= line_start)
    ),
    CONSTRAINT ck_chunks_metadata_object CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT ck_chunks_metadata_size CHECK (octet_length(metadata::TEXT) <= 65536)
);

CREATE TABLE app.embedding_profiles (
    id SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    distance_metric TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ,
    CONSTRAINT uq_embedding_profiles_provider_model UNIQUE (provider, model),
    CONSTRAINT ck_embedding_profiles_provider_length CHECK (char_length(provider) BETWEEN 1 AND 100),
    CONSTRAINT ck_embedding_profiles_model_length CHECK (char_length(model) BETWEEN 1 AND 255),
    CONSTRAINT ck_embedding_profiles_dimensions CHECK (dimensions = 1536),
    CONSTRAINT ck_embedding_profiles_distance CHECK (distance_metric = 'cosine'),
    CONSTRAINT ck_embedding_profiles_status CHECK (status IN ('building', 'active', 'retired'))
);

CREATE UNIQUE INDEX uq_embedding_profiles_one_active
    ON app.embedding_profiles ((status)) WHERE status = 'active';

CREATE TABLE app.chunk_embeddings (
    workspace_id UUID NOT NULL,
    chunk_id UUID NOT NULL,
    embedding_profile_id SMALLINT NOT NULL,
    embedding VECTOR(1536) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_chunk_embeddings PRIMARY KEY (chunk_id, embedding_profile_id),
    CONSTRAINT fk_chunk_embeddings_chunk FOREIGN KEY (workspace_id, chunk_id)
        REFERENCES app.chunks (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_chunk_embeddings_profile FOREIGN KEY (embedding_profile_id)
        REFERENCES app.embedding_profiles (id) ON DELETE RESTRICT,
    CONSTRAINT ck_chunk_embeddings_nonzero_finite CHECK (
        vector_norm(embedding) > 0 AND vector_norm(embedding) < 'Infinity'::DOUBLE PRECISION
    )
);

CREATE TABLE app.source_collection_memberships (
    workspace_id UUID NOT NULL,
    connection_id UUID NOT NULL,
    source_id UUID NOT NULL,
    collection_id UUID NOT NULL,
    relationship TEXT NOT NULL,
    CONSTRAINT pk_source_collection_memberships PRIMARY KEY (source_id, collection_id),
    CONSTRAINT fk_source_collection_memberships_source FOREIGN KEY (workspace_id, connection_id, source_id)
        REFERENCES app.sources (workspace_id, connection_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_source_collection_memberships_collection FOREIGN KEY (workspace_id, connection_id, collection_id)
        REFERENCES app.source_collections (workspace_id, connection_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_source_collection_memberships_relationship CHECK (relationship IN ('direct', 'ancestor'))
);

CREATE TABLE app.conversations (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    user_id UUID NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    CONSTRAINT uq_conversations_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_conversations_membership FOREIGN KEY (workspace_id, user_id)
        REFERENCES app.workspace_members (workspace_id, user_id) ON DELETE CASCADE,
    CONSTRAINT ck_conversations_title_length CHECK (char_length(title) BETWEEN 1 AND 500),
    CONSTRAINT ck_conversations_status CHECK (status IN ('active', 'deleted'))
);

CREATE TABLE app.search_requests (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    user_id UUID NOT NULL,
    conversation_id UUID,
    mode TEXT NOT NULL,
    query_text TEXT NOT NULL,
    normalized_plan JSONB NOT NULL,
    planner_version TEXT NOT NULL,
    ranker_version TEXT NOT NULL,
    embedding_profile_id SMALLINT,
    index_generation BIGINT NOT NULL,
    authorization_version BIGINT NOT NULL,
    status TEXT NOT NULL,
    insufficient_reason TEXT,
    error_code TEXT,
    latency_ms INTEGER,
    result_count INTEGER,
    context_tokens INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    purge_after TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_search_requests_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_search_requests_membership FOREIGN KEY (workspace_id, user_id)
        REFERENCES app.workspace_members (workspace_id, user_id) ON DELETE CASCADE,
    CONSTRAINT fk_search_requests_conversation FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES app.conversations (workspace_id, id) ON DELETE SET NULL (conversation_id),
    CONSTRAINT fk_search_requests_profile FOREIGN KEY (embedding_profile_id)
        REFERENCES app.embedding_profiles (id) ON DELETE RESTRICT,
    CONSTRAINT ck_search_requests_mode CHECK (mode IN ('results', 'answer')),
    CONSTRAINT ck_search_requests_query_length CHECK (char_length(query_text) BETWEEN 1 AND 10000),
    CONSTRAINT ck_search_requests_plan_object CHECK (jsonb_typeof(normalized_plan) = 'object'),
    CONSTRAINT ck_search_requests_plan_size CHECK (octet_length(normalized_plan::TEXT) <= 65536),
    CONSTRAINT ck_search_requests_versions CHECK (char_length(planner_version) BETWEEN 1 AND 100 AND char_length(ranker_version) BETWEEN 1 AND 100),
    CONSTRAINT ck_search_requests_generations CHECK (index_generation > 0 AND authorization_version > 0),
    CONSTRAINT ck_search_requests_status CHECK (status IN ('running', 'completed', 'insufficient_evidence', 'failed', 'cancelled')),
    CONSTRAINT ck_search_requests_metrics CHECK (
        (latency_ms IS NULL OR latency_ms >= 0)
        AND (result_count IS NULL OR result_count >= 0)
        AND (context_tokens IS NULL OR context_tokens >= 0)
    ),
    CONSTRAINT ck_search_requests_purge CHECK (purge_after >= created_at)
);

CREATE TABLE app.messages (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    conversation_id UUID NOT NULL,
    search_request_id UUID,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    content TEXT NOT NULL,
    model TEXT,
    model_response_id TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    latency_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_messages_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_messages_conversation FOREIGN KEY (workspace_id, conversation_id)
        REFERENCES app.conversations (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_messages_search_request FOREIGN KEY (workspace_id, search_request_id)
        REFERENCES app.search_requests (workspace_id, id) ON DELETE SET NULL (search_request_id),
    CONSTRAINT ck_messages_role CHECK (role IN ('user', 'assistant')),
    CONSTRAINT ck_messages_status CHECK (status IN ('pending', 'complete', 'insufficient_evidence', 'interrupted', 'failed')),
    CONSTRAINT ck_messages_content_length CHECK (char_length(content) <= 200000),
    CONSTRAINT ck_messages_metrics CHECK (
        (input_tokens IS NULL OR input_tokens >= 0)
        AND (output_tokens IS NULL OR output_tokens >= 0)
        AND (latency_ms IS NULL OR latency_ms >= 0)
    )
);

CREATE TABLE app.message_claims (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    message_id UUID NOT NULL,
    claim_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    material BOOLEAN NOT NULL,
    CONSTRAINT uq_message_claims_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT uq_message_claims_workspace_message_id UNIQUE (workspace_id, message_id, id),
    CONSTRAINT fk_message_claims_message FOREIGN KEY (workspace_id, message_id)
        REFERENCES app.messages (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_message_claims_order UNIQUE (message_id, claim_index),
    CONSTRAINT ck_message_claims_index CHECK (claim_index >= 0),
    CONSTRAINT ck_message_claims_text CHECK (char_length(text) BETWEEN 1 AND 10000)
);

CREATE TABLE app.citations (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    message_id UUID NOT NULL,
    claim_id UUID NOT NULL,
    source_id UUID NOT NULL,
    document_version_id UUID NOT NULL,
    chunk_id UUID NOT NULL,
    citation_index INTEGER NOT NULL,
    excerpt TEXT NOT NULL,
    rank_score DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_citations_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_citations_claim_lineage FOREIGN KEY (workspace_id, message_id, claim_id)
        REFERENCES app.message_claims (workspace_id, message_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_citations_source FOREIGN KEY (workspace_id, source_id)
        REFERENCES app.sources (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_citations_document FOREIGN KEY (workspace_id, source_id, document_version_id)
        REFERENCES app.document_versions (workspace_id, source_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_citations_chunk FOREIGN KEY (workspace_id, document_version_id, chunk_id)
        REFERENCES app.chunks (workspace_id, document_version_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_citations_message_order UNIQUE (message_id, citation_index),
    CONSTRAINT uq_citations_claim_chunk UNIQUE (claim_id, chunk_id),
    CONSTRAINT ck_citations_index CHECK (citation_index >= 0),
    CONSTRAINT ck_citations_excerpt CHECK (char_length(excerpt) BETWEEN 1 AND 4000),
    CONSTRAINT ck_citations_rank_finite CHECK (rank_score IS NULL OR rank_score BETWEEN -1.0e308 AND 1.0e308)
);

CREATE TABLE app.api_idempotency_keys (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    user_id UUID NOT NULL,
    key_hash BYTEA NOT NULL,
    method TEXT NOT NULL,
    route_template TEXT NOT NULL,
    request_hash BYTEA NOT NULL,
    status TEXT NOT NULL,
    response_status SMALLINT,
    response_body JSONB,
    resource_type TEXT,
    resource_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT uq_api_idempotency_keys_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_api_idempotency_keys_membership FOREIGN KEY (workspace_id, user_id)
        REFERENCES app.workspace_members (workspace_id, user_id) ON DELETE CASCADE,
    CONSTRAINT uq_api_idempotency_keys_reservation UNIQUE (workspace_id, user_id, method, route_template, key_hash),
    CONSTRAINT ck_api_idempotency_keys_method CHECK (method IN ('POST', 'PUT', 'PATCH', 'DELETE')),
    CONSTRAINT ck_api_idempotency_keys_route CHECK (char_length(route_template) BETWEEN 1 AND 255),
    CONSTRAINT ck_api_idempotency_keys_status CHECK (status IN ('processing', 'completed', 'failed')),
    CONSTRAINT ck_api_idempotency_keys_response_status CHECK (response_status IS NULL OR response_status BETWEEN 100 AND 599),
    CONSTRAINT ck_api_idempotency_keys_response_object CHECK (response_body IS NULL OR jsonb_typeof(response_body) = 'object'),
    CONSTRAINT ck_api_idempotency_keys_response_size CHECK (response_body IS NULL OR octet_length(response_body::TEXT) <= 65536),
    CONSTRAINT ck_api_idempotency_keys_resource_pair CHECK ((resource_type IS NULL) = (resource_id IS NULL)),
    CONSTRAINT ck_api_idempotency_keys_expiry CHECK (expires_at > created_at)
);

CREATE TABLE app.deletion_requests (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    requested_by_user_id UUID,
    target_type TEXT NOT NULL,
    target_id UUID NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    deadline_at TIMESTAMPTZ NOT NULL,
    receipt_token_hash BYTEA,
    remaining_counts JSONB NOT NULL DEFAULT '{}'::JSONB,
    failure_codes JSONB NOT NULL DEFAULT '{}'::JSONB,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_deletion_requests_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_deletion_requests_workspace FOREIGN KEY (workspace_id)
        REFERENCES app.workspaces (id) ON DELETE CASCADE,
    CONSTRAINT fk_deletion_requests_requester FOREIGN KEY (workspace_id, requested_by_user_id)
        REFERENCES app.workspace_members (workspace_id, user_id) ON DELETE SET NULL (requested_by_user_id),
    CONSTRAINT uq_deletion_requests_receipt UNIQUE (receipt_token_hash),
    CONSTRAINT uq_deletion_requests_idempotency UNIQUE (workspace_id, target_type, target_id, idempotency_key),
    CONSTRAINT ck_deletion_requests_target CHECK (target_type IN ('account', 'connection', 'source', 'conversation', 'device')),
    CONSTRAINT ck_deletion_requests_status CHECK (status IN ('pending', 'running', 'blocked', 'completed', 'failed')),
    CONSTRAINT ck_deletion_requests_key_length CHECK (char_length(idempotency_key) BETWEEN 1 AND 255),
    CONSTRAINT ck_deletion_requests_deadline CHECK (
        deadline_at >= requested_at
        AND (target_type NOT IN ('account', 'connection') OR deadline_at <= requested_at + INTERVAL '24 hours')
    ),
    CONSTRAINT ck_deletion_requests_account_receipt CHECK (target_type <> 'account' OR receipt_token_hash IS NOT NULL),
    CONSTRAINT ck_deletion_requests_remaining_object CHECK (jsonb_typeof(remaining_counts) = 'object'),
    CONSTRAINT ck_deletion_requests_failures_object CHECK (jsonb_typeof(failure_codes) = 'object'),
    CONSTRAINT ck_deletion_requests_metadata_size CHECK (octet_length(remaining_counts::TEXT) + octet_length(failure_codes::TEXT) <= 65536)
);

CREATE TABLE app.jobs (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    connection_id UUID,
    source_id UUID,
    deletion_request_id UUID,
    job_type TEXT NOT NULL,
    queue TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at TIMESTAMPTZ,
    lease_owner TEXT,
    error_code TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_jobs_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_jobs_workspace FOREIGN KEY (workspace_id)
        REFERENCES app.workspaces (id) ON DELETE CASCADE,
    CONSTRAINT fk_jobs_connection FOREIGN KEY (workspace_id, connection_id)
        REFERENCES app.connections (workspace_id, id) ON DELETE SET NULL (connection_id),
    CONSTRAINT fk_jobs_source FOREIGN KEY (workspace_id, source_id)
        REFERENCES app.sources (workspace_id, id) ON DELETE SET NULL (source_id),
    CONSTRAINT fk_jobs_deletion_request FOREIGN KEY (workspace_id, deletion_request_id)
        REFERENCES app.deletion_requests (workspace_id, id) ON DELETE SET NULL (deletion_request_id),
    CONSTRAINT uq_jobs_idempotency UNIQUE (workspace_id, job_type, idempotency_key),
    CONSTRAINT ck_jobs_type CHECK (job_type IN ('sync', 'index', 'embed', 'delete', 'export', 'reconcile')),
    CONSTRAINT ck_jobs_queue CHECK (queue IN ('sync', 'index', 'embedding', 'deletion', 'privacy')),
    CONSTRAINT ck_jobs_idempotency_length CHECK (char_length(idempotency_key) BETWEEN 1 AND 255),
    CONSTRAINT ck_jobs_status CHECK (status IN ('pending', 'leased', 'retry_wait', 'completed', 'failed', 'dead_letter')),
    CONSTRAINT ck_jobs_priority CHECK (priority BETWEEN -1000 AND 1000),
    CONSTRAINT ck_jobs_attempts CHECK (attempt_count >= 0 AND max_attempts BETWEEN 1 AND 100 AND attempt_count <= max_attempts),
    CONSTRAINT ck_jobs_lease_pair CHECK ((lease_expires_at IS NULL) = (lease_owner IS NULL)),
    CONSTRAINT ck_jobs_error_length CHECK (error_code IS NULL OR char_length(error_code) <= 100),
    CONSTRAINT ck_jobs_payload_object CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT ck_jobs_payload_size CHECK (octet_length(payload::TEXT) <= 65536)
);

CREATE TABLE app.job_attempts (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    job_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL,
    worker_id TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    diagnostic_ref TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    CONSTRAINT uq_job_attempts_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_job_attempts_job FOREIGN KEY (workspace_id, job_id)
        REFERENCES app.jobs (workspace_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_job_attempts_number UNIQUE (job_id, attempt_number),
    CONSTRAINT ck_job_attempts_number CHECK (attempt_number > 0),
    CONSTRAINT ck_job_attempts_worker_length CHECK (char_length(worker_id) BETWEEN 1 AND 255),
    CONSTRAINT ck_job_attempts_status CHECK (status IN ('running', 'succeeded', 'retryable_failure', 'permanent_failure', 'lease_expired')),
    CONSTRAINT ck_job_attempts_error_length CHECK (error_code IS NULL OR char_length(error_code) <= 100),
    CONSTRAINT ck_job_attempts_diagnostic_length CHECK (diagnostic_ref IS NULL OR char_length(diagnostic_ref) <= 500)
);

CREATE TABLE app.outbox_events (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    publish_attempts INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT uq_outbox_events_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT fk_outbox_events_workspace FOREIGN KEY (workspace_id)
        REFERENCES app.workspaces (id) ON DELETE CASCADE,
    CONSTRAINT ck_outbox_events_aggregate_type CHECK (char_length(aggregate_type) BETWEEN 1 AND 100),
    CONSTRAINT ck_outbox_events_event_type CHECK (char_length(event_type) BETWEEN 1 AND 150),
    CONSTRAINT ck_outbox_events_payload_object CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT ck_outbox_events_payload_size CHECK (octet_length(payload::TEXT) <= 65536),
    CONSTRAINT ck_outbox_events_attempts CHECK (publish_attempts >= 0)
);

CREATE TABLE app.workspace_usage (
    workspace_id UUID PRIMARY KEY,
    indexed_source_count BIGINT NOT NULL DEFAULT 0,
    extracted_bytes BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lock_version INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT fk_workspace_usage_workspace FOREIGN KEY (workspace_id)
        REFERENCES app.workspaces (id) ON DELETE CASCADE,
    CONSTRAINT ck_workspace_usage_sources CHECK (indexed_source_count BETWEEN 0 AND 25000),
    CONSTRAINT ck_workspace_usage_bytes CHECK (extracted_bytes BETWEEN 0 AND 10737418240),
    CONSTRAINT ck_workspace_usage_lock_version CHECK (lock_version > 0)
);

CREATE TABLE app.audit_events (
    id UUID PRIMARY KEY,
    workspace_id UUID,
    workspace_ref_hash BYTEA NOT NULL,
    actor_user_id UUID,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    target_ref_hash BYTEA,
    request_id UUID,
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fk_audit_events_workspace FOREIGN KEY (workspace_id)
        REFERENCES app.workspaces (id) ON DELETE SET NULL,
    CONSTRAINT fk_audit_events_actor FOREIGN KEY (actor_user_id)
        REFERENCES app.users (id) ON DELETE SET NULL,
    CONSTRAINT ck_audit_events_action_length CHECK (char_length(action) BETWEEN 1 AND 150),
    CONSTRAINT ck_audit_events_target_length CHECK (char_length(target_type) BETWEEN 1 AND 100),
    CONSTRAINT ck_audit_events_outcome CHECK (outcome IN ('succeeded', 'failed', 'denied')),
    CONSTRAINT ck_audit_events_metadata_object CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT ck_audit_events_metadata_size CHECK (octet_length(metadata::TEXT) <= 16384)
);

CREATE INDEX ix_auth_identities_user ON app.auth_identities (user_id);
CREATE INDEX ix_one_time_tokens_user_expiry ON app.one_time_tokens (user_id, expires_at);
CREATE INDEX ix_sessions_user_expiry ON app.sessions (user_id, expires_at);
CREATE INDEX ix_sessions_active_family ON app.sessions (family_id, expires_at)
    WHERE revoked_at IS NULL AND rotated_at IS NULL;
CREATE INDEX ix_sessions_replacement ON app.sessions (replaced_by_session_id);
CREATE INDEX ix_workspace_members_user_status ON app.workspace_members (user_id, status);
CREATE INDEX ix_workspace_members_workspace_role_status ON app.workspace_members (workspace_id, role, status);
CREATE INDEX ix_oauth_transactions_workspace_expiry ON app.oauth_transactions (workspace_id, expires_at);
CREATE INDEX ix_oauth_transactions_membership ON app.oauth_transactions (workspace_id, user_id);
CREATE INDEX ix_connections_workspace_status ON app.connections (workspace_id, status);
CREATE INDEX ix_connections_owner ON app.connections (workspace_id, owner_user_id);
CREATE INDEX ix_connection_scopes_workspace_connection ON app.connection_scopes (workspace_id, connection_id);
CREATE INDEX ix_connection_cursors_workspace_connection ON app.connection_cursors (workspace_id, connection_id);
CREATE INDEX ix_source_collections_workspace_connection ON app.source_collections (workspace_id, connection_id, status);
CREATE INDEX ix_source_collections_parent ON app.source_collections (workspace_id, connection_id, parent_collection_id);
CREATE INDEX ix_devices_workspace_user_status ON app.devices (workspace_id, user_id, status);
CREATE INDEX ix_devices_workspace_connection ON app.devices (workspace_id, connection_id);
CREATE INDEX ix_device_folders_workspace_device ON app.device_folders (workspace_id, device_id, status);
CREATE INDEX ix_provider_events_workspace_connection_status ON app.provider_events (workspace_id, connection_id, status);
CREATE INDEX ix_sources_workspace_connection_state ON app.sources (workspace_id, connection_id, state);
CREATE INDEX ix_sources_workspace_filters ON app.sources (workspace_id, provider, source_type, source_timestamp DESC);
CREATE INDEX ix_sources_title_trgm ON app.sources USING GIN (title gin_trgm_ops);
CREATE INDEX ix_source_people_workspace_identifier ON app.source_people (workspace_id, normalized_identifier, relationship);
CREATE INDEX ix_source_people_source ON app.source_people (workspace_id, source_id);
CREATE INDEX ix_source_collection_memberships_collection ON app.source_collection_memberships (workspace_id, collection_id, source_id);
CREATE INDEX ix_source_collection_memberships_source_fk ON app.source_collection_memberships (workspace_id, connection_id, source_id);
CREATE INDEX ix_source_collection_memberships_collection_fk ON app.source_collection_memberships (workspace_id, connection_id, collection_id);
CREATE INDEX ix_document_versions_workspace_source_state ON app.document_versions (workspace_id, source_id, state, created_at DESC);
CREATE INDEX ix_chunks_search_vector ON app.chunks USING GIN (search_vector);
CREATE INDEX ix_chunks_workspace_document_index ON app.chunks (workspace_id, document_version_id, chunk_index);
CREATE INDEX ix_chunk_embeddings_workspace_profile_chunk ON app.chunk_embeddings (workspace_id, embedding_profile_id, chunk_id);
CREATE INDEX ix_chunk_embeddings_chunk_fk ON app.chunk_embeddings (workspace_id, chunk_id);
CREATE INDEX ix_chunk_embeddings_hnsw ON app.chunk_embeddings USING HNSW (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX ix_search_requests_workspace_user_created ON app.search_requests (workspace_id, user_id, created_at DESC)
    WHERE purge_after > created_at;
CREATE INDEX ix_search_requests_workspace_conversation ON app.search_requests (workspace_id, conversation_id);
CREATE INDEX ix_search_requests_profile ON app.search_requests (embedding_profile_id);
CREATE INDEX ix_conversations_workspace_user_status ON app.conversations (workspace_id, user_id, status);
CREATE INDEX ix_messages_workspace_conversation_created ON app.messages (workspace_id, conversation_id, created_at);
CREATE INDEX ix_messages_workspace_search_request ON app.messages (workspace_id, search_request_id);
CREATE INDEX ix_message_claims_workspace_message ON app.message_claims (workspace_id, message_id);
CREATE INDEX ix_citations_claim_lineage ON app.citations (workspace_id, message_id, claim_id);
CREATE INDEX ix_citations_workspace_source ON app.citations (workspace_id, source_id, document_version_id, chunk_id);
CREATE INDEX ix_api_idempotency_keys_workspace_expiry ON app.api_idempotency_keys (workspace_id, expires_at);
CREATE INDEX ix_deletion_requests_incomplete_deadline ON app.deletion_requests (status, deadline_at)
    WHERE status IN ('pending', 'running', 'blocked', 'failed');
CREATE INDEX ix_deletion_requests_requester ON app.deletion_requests (workspace_id, requested_by_user_id);
CREATE INDEX ix_jobs_runnable ON app.jobs (queue, priority DESC, available_at, created_at)
    WHERE status IN ('pending', 'retry_wait');
CREATE INDEX ix_jobs_workspace_connection_status ON app.jobs (workspace_id, connection_id, status);
CREATE INDEX ix_jobs_workspace_source_status ON app.jobs (workspace_id, source_id, status);
CREATE INDEX ix_jobs_workspace_deletion_request ON app.jobs (workspace_id, deletion_request_id);
CREATE INDEX ix_job_attempts_workspace_job ON app.job_attempts (workspace_id, job_id, attempt_number);
CREATE INDEX ix_job_attempts_started_brin ON app.job_attempts USING BRIN (started_at);
CREATE INDEX ix_outbox_events_unpublished ON app.outbox_events (created_at) WHERE published_at IS NULL;
CREATE INDEX ix_outbox_events_workspace ON app.outbox_events (workspace_id, created_at);
CREATE INDEX ix_audit_events_workspace_created ON app.audit_events (workspace_id, created_at DESC);
CREATE INDEX ix_audit_events_actor ON app.audit_events (actor_user_id);
CREATE INDEX ix_audit_events_created_brin ON app.audit_events USING BRIN (created_at);

DO $$
DECLARE
    tenant_table TEXT;
BEGIN
    FOREACH tenant_table IN ARRAY ARRAY[
        'workspace_members', 'oauth_transactions', 'connections',
        'connection_scopes', 'connection_cursors', 'source_collections',
        'devices', 'device_folders', 'provider_events', 'sources',
        'source_people', 'source_collection_memberships', 'document_versions',
        'chunks', 'chunk_embeddings', 'search_requests', 'conversations',
        'messages', 'message_claims', 'citations', 'api_idempotency_keys',
        'jobs', 'job_attempts', 'outbox_events', 'deletion_requests',
        'workspace_usage'
    ]
    LOOP
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY', tenant_table);
        EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY', tenant_table);
        EXECUTE format(
            'CREATE POLICY api_workspace_isolation ON app.%I TO app_api '
            'USING (workspace_id = app.current_workspace_id() '
            'AND app.has_active_workspace_membership(workspace_id)) '
            'WITH CHECK (workspace_id = app.current_workspace_id() '
            'AND app.has_active_workspace_membership(workspace_id))',
            tenant_table
        );
        EXECUTE format(
            'CREATE POLICY worker_workspace_isolation ON app.%I TO app_worker '
            'USING (workspace_id = app.current_workspace_id()) '
            'WITH CHECK (workspace_id = app.current_workspace_id())',
            tenant_table
        );
    END LOOP;
END
$$;

ALTER TABLE app.workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.workspaces FORCE ROW LEVEL SECURITY;
CREATE POLICY api_workspace_isolation ON app.workspaces TO app_api
    USING (id = app.current_workspace_id() AND app.has_active_workspace_membership(id))
    WITH CHECK (id = app.current_workspace_id() AND app.has_active_workspace_membership(id));
CREATE POLICY worker_workspace_isolation ON app.workspaces TO app_worker
    USING (id = app.current_workspace_id())
    WITH CHECK (id = app.current_workspace_id());

ALTER TABLE app.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.users FORCE ROW LEVEL SECURITY;
CREATE POLICY api_user_isolation ON app.users TO app_api
    USING (id = app.current_user_id()) WITH CHECK (id = app.current_user_id());
CREATE POLICY worker_user_isolation ON app.users TO app_worker
    USING (id = app.current_user_id()) WITH CHECK (id = app.current_user_id());

DO $$
DECLARE
    user_table TEXT;
BEGIN
    FOREACH user_table IN ARRAY ARRAY['auth_identities', 'one_time_tokens', 'sessions']
    LOOP
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY', user_table);
        EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY', user_table);
        EXECUTE format(
            'CREATE POLICY api_user_isolation ON app.%I TO app_api '
            'USING (user_id = app.current_user_id()) '
            'WITH CHECK (user_id = app.current_user_id())',
            user_table
        );
        EXECUTE format(
            'CREATE POLICY worker_user_isolation ON app.%I TO app_worker '
            'USING (user_id = app.current_user_id()) '
            'WITH CHECK (user_id = app.current_user_id())',
            user_table
        );
    END LOOP;
END
$$;

ALTER TABLE app.audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.audit_events FORCE ROW LEVEL SECURITY;
CREATE POLICY api_audit_workspace_isolation ON app.audit_events TO app_api
    USING (
        workspace_id = app.current_workspace_id()
        AND app.has_active_workspace_membership(workspace_id)
    )
    WITH CHECK (
        workspace_id = app.current_workspace_id()
        AND app.has_active_workspace_membership(workspace_id)
    );
CREATE POLICY worker_audit_workspace_isolation ON app.audit_events TO app_worker
    USING (workspace_id = app.current_workspace_id())
    WITH CHECK (workspace_id = app.current_workspace_id());
CREATE POLICY audit_reader_all ON app.audit_events TO app_audit_reader USING (true);

DO $$
DECLARE
    object_name TEXT;
BEGIN
    FOR object_name IN
        SELECT tablename FROM pg_tables WHERE schemaname = 'app'
    LOOP
        EXECUTE format('ALTER TABLE app.%I OWNER TO app_migrator', object_name);
    END LOOP;
    FOR object_name IN
        SELECT sequencename FROM pg_sequences WHERE schemaname = 'app'
    LOOP
        EXECUTE format('ALTER SEQUENCE app.%I OWNER TO app_migrator', object_name);
    END LOOP;
END
$$;

GRANT USAGE ON SCHEMA app TO app_api, app_worker, app_audit_reader;
GRANT EXECUTE ON FUNCTION app.current_workspace_id() TO app_api, app_worker;
GRANT EXECUTE ON FUNCTION app.current_user_id() TO app_api, app_worker;
GRANT EXECUTE ON FUNCTION app.has_active_workspace_membership(UUID) TO app_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO app_api, app_worker;
REVOKE INSERT, UPDATE, DELETE ON app.embedding_profiles FROM app_api, app_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app TO app_worker;
GRANT SELECT ON app.embedding_profiles TO app_api, app_worker;
GRANT SELECT ON app.audit_events TO app_audit_reader;
REVOKE INSERT, UPDATE, DELETE ON app.audit_events FROM app_audit_reader;
GRANT SELECT ON TABLE public.alembic_version TO app_api, app_worker;
REVOKE ALL ON ALL TABLES IN SCHEMA app FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA app FROM PUBLIC;
