# Indexing Pipeline

> **Implementation status:** Specification complete. B5 implements the
> provider-neutral normalized-text path from the deterministic fake connector
> through PostgreSQL queueing, chunking, deterministic test embeddings, and
> atomic searchable-version promotion. Provider parsers and production
> embedding services remain in their owning phases. See
> [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md).

## Goals and ownership

The indexing pipeline turns one authorized, normalized connector document into
immutable searchable chunks and embeddings without exposing a partial
replacement. It owns extraction boundaries, normalization, language tagging,
chunking, deduplication, embedding batches, durable index jobs, version
promotion, retry behavior, and reindex triggers.

Provider API access and normalization belong to
[`06_CONNECTOR_FRAMEWORK.md`](06_CONNECTOR_FRAMEWORK.md). Relational invariants
belong to [`04_DATABASE_SCHEMA.md`](04_DATABASE_SCHEMA.md), retrieval semantics
to [`03_SEARCH_ENGINE_DESIGN.md`](03_SEARCH_ENGINE_DESIGN.md), untrusted-content
controls to [`08_SECURITY_AND_PRIVACY.md`](08_SECURITY_AND_PRIVACY.md), and
provider-specific parsers to their connector phases. Indexing never grants
access, follows instructions found in content, or calls provider write APIs.

## Pipeline state machine

The durable lifecycle is:

1. A trusted connector sync validates the active connection, workspace, source
   permission snapshot, size, and supported media type.
2. Ingestion computes content, permission, parser, chunker, and embedding
   profile fingerprints.
3. An unchanged source whose current ready version has the same fingerprints is
   skipped without creating another job.
4. A changed source receives an immutable `pending` document version and one
   idempotent `index` job in the same transaction.
5. A worker atomically leases one runnable job without accepting a
   caller-selected workspace. The claim returns the authoritative workspace and
   identifiers only.
6. In a new transaction under that workspace, the worker loads the pending
   version, extracts and normalizes text, detects language/structure, chunks,
   deduplicates, and generates embeddings in bounded batches.
7. The worker writes every chunk and embedding, marks the version `ready`,
   points the source to it, supersedes the old ready version, updates usage, and
   completes the job in one transaction.
8. On failure, the pending version is marked `failed` with a bounded code and
   the prior current version remains searchable.

States are monotonic except a retryable job may move from `leased` to
`retry_wait` and be leased again. A stale lease can be recovered only after its
expiry. A source is searchable only through `sources.current_document_version_id`.

## Input and extraction contract

The provider-neutral input is `NormalizedDocument` from the Connector SDK. It
contains bounded text, title, MIME type, canonical URL, timestamps, access
metadata, and sanitized provider metadata. The pipeline verifies the provider
matches the durable connection and treats every text and metadata field as
untrusted data.

Extractors return a bounded immutable result containing UTF-8 text, structural
blocks, source offsets, optional page/line coordinates, detected media type,
and an extractor version. They cannot perform network requests, execute
macros/scripts, resolve external entities, open arbitrary local paths, or trust
file extensions over validated content type.

The initial B5 implementation accepts already-normalized `text/plain` and
`text/markdown`. Later provider phases add isolated, resource-limited parsers:

| Format | Required preserved structure |
| --- | --- |
| PDF | Page number, reading order, headings where reliable |
| DOCX | Headings, paragraphs, lists, tables |
| PPTX | Slide title/body/notes and slide number |
| XLSX/CSV | Sheet, table range, headers, bounded row groups |
| Markdown | Heading tree, prose, fenced-code boundary |
| Code | Language, symbol, line range, enclosing path |
| Email | Subject, participants, sent time, cleaned body, attachment boundary |

Unsupported, encrypted, malformed, or oversized input fails with a stable
sanitized code. Raw content and parser exceptions never enter job payloads,
logs, metrics labels, or user-visible errors.

## Normalization and language

Text is normalized with Unicode NFC, line-ending normalization, removal of NUL
and unsafe control characters, bounded whitespace folding, and stable blank
lines. Meaningful code indentation, Markdown fences, table cells, and source
coordinates are preserved by format-aware extractors. Normalization is
deterministic and versioned; it never interprets content as commands.

Language detection runs on bounded normalized samples and returns a BCP 47-like
allowlisted tag or `und` when evidence is insufficient. The selected PostgreSQL
text-search configuration is mapped by server policy, never supplied as raw SQL
by a connector. B5 uses conservative `en`/`und` detection and the `english` or
`simple` configuration.

Email extraction removes repeated quoted history and signatures only when the
provider-specific parser has high-confidence boundaries. Ambiguous text is
retained rather than silently discarded.

## Chunking contract

Chunking is structure-first and deterministic. Targets are:

- general prose: 400–800 model tokens;
- email: one message or compact thread segment;
- code: one symbol or a tightly related symbol group;
- tables: headers plus 20–50 rows within the token ceiling; and
- overlap: 50–100 tokens only across a necessary structural boundary.

Every chunk records its zero-based index, exact content hash, token count,
heading path, character offsets, optional page/line coordinates, narrow-section
key, and bounded metadata. Empty chunks are forbidden. Oversized indivisible
blocks split at deterministic sentence, line, then hard token boundaries.

B5 uses a deterministic Unicode word/punctuation tokenizer for local operation.
Production embedding adapters must provide their model tokenizer and bump the
chunker version when token boundaries change.

## Deterministic identity and deduplication

SHA-256 fingerprints cover canonical normalized content and canonical access
metadata. A document `version_key` binds:

- source ID;
- content hash and permission hash;
- extractor/parser version;
- normalization/chunker version; and
- embedding profile ID/model version.

The document-version UUID is UUIDv5 from source ID plus `version_key`. A chunk
UUID is UUIDv5 from document-version ID, chunker version, chunk index, and chunk
hash. Reprocessing the same version produces exactly the same identifiers.

Source-level exact duplicates are skipped when the active source already points
to the same ready version. Within a document, exact normalized chunk duplicates
are stored once. Near-duplicate detection uses deterministic 64-bit SimHash over
normalized tokens; chunks within the configured Hamming threshold are skipped
only when they share the same structural scope. Cross-source deduplication may
reuse embeddings later, but it must not merge authorization, lineage, or
citation identity.

## Embedding contract

An embedding adapter declares provider, model, dimensions, distance metric,
maximum input tokens, and batch limit. Output count must equal input count;
every vector must have exactly 1536 finite values and non-zero norm. Inputs are
sent in bounded batches, retain chunk ordering, and never include credentials
or unrelated tenant content.

Production adapters implement timeouts, retry classification, rate-limit
handling, cost accounting, and model/version pinning. B5 deliberately uses a
deterministic local SHA-256-derived adapter so CI and development require no
external model or secret. Its vectors prove storage and lifecycle behavior but
are not semantic search quality and cannot be activated in production.

## Durable queue and worker authority

PostgreSQL `jobs` is the source of truth. Redis may later wake workers but never
owns job state. Job payloads contain identifiers and version/config labels only,
not document text, provider credentials, tokens, arbitrary URLs, or paths.

Enqueue is idempotent on workspace, job type, and version key. Claiming uses a
narrow `SECURITY DEFINER` function with fixed `search_path`, revoked `PUBLIC`
access, an atomic `FOR UPDATE SKIP LOCKED` lease, bounded return columns, and no
caller-selected workspace. The worker then sets transaction-local
`app.workspace_id` and operates through the NOBYPASSRLS `app_worker` role.

Each attempt records worker ID, attempt number, start/finish state, and a stable
error code. Retryable failures use capped exponential backoff with jitter;
malformed/unsupported input is permanent. Exhausted jobs become `dead_letter`.
Lease recovery and cancellation are idempotent.

## Atomic persistence and promotion

Chunk and embedding writes occur in one promotion transaction for the pending
version. Before the pointer changes, the transaction verifies:

- the job still owns a valid lease;
- the source and pending version belong to the claimed workspace;
- source content/permission fingerprints still match the pending version;
- the connection/source are active;
- all chunks and embeddings pass count, dimension, finite, and lineage checks;
  and
- the embedding profile is the expected active local/deployment profile.

The transaction inserts chunks and embeddings, marks the pending version ready,
sets `ready_at`, updates the source pointer, supersedes the previous version,
increments search-index generation and usage, emits an identifier-only outbox
event, and completes the job. Any error rolls back all of these changes.

## Reindexing and permission changes

Reindex when content, permissions, extractor, parser, normalizer, chunker,
tokenizer, or embedding profile changes. A permission change immediately makes
the source unavailable if the current authorization snapshot is stale; a new
version restores searchability only after promotion. Model migration builds a
new profile beside the old profile and switches retrieval only after coverage
and evaluation gates pass.

Reindex jobs are idempotent and lower priority than user-visible sync work.
Removing a source or connection prevents new leases, cancels safe pending work,
excludes it from retrieval immediately, and delegates hard deletion to the
documented deletion workflow.

## Limits and failure behavior

Configured limits cover raw bytes, extracted characters, structural blocks,
chunks per document, tokens per chunk/document, parser CPU/wall time, memory,
embedding batch size, and retry count. B5 accepts Connector SDK content up to
its five-million-character bound and creates at most 10,000 chunks; deployment
may lower these limits.

Failures expose stable codes such as `UNSUPPORTED_MEDIA_TYPE`,
`CONTENT_TOO_LARGE`, `EXTRACTION_FAILED`, `NO_INDEXABLE_TEXT`,
`CHUNK_LIMIT_EXCEEDED`, `EMBEDDING_INVALID`, `EMBEDDING_UNAVAILABLE`,
`AUTHORIZATION_CHANGED`, and `INDEX_PROMOTION_CONFLICT`. Diagnostics live in
restricted logs under an opaque reference. The old ready version remains the
only searchable version.

## Observability and privacy

Metrics include job queue age, lease/retry/dead-letter counts, extraction and
embedding duration, bytes/tokens/chunks, unchanged-skip rate, promotion
conflicts, and per-profile embedding usage. Labels use provider, media class,
version, outcome, and bounded error code—never workspace/user IDs, filenames,
URLs, titles, content, queries, credentials, or raw exceptions.

Traces propagate request/sync/job IDs and record only opaque entity IDs.
Normalized text remains in the tenant database only as required for search and
deletion; temporary parser files are private, bounded, and removed after the
attempt.

## Test matrix and release gates

Required automated coverage includes:

1. normalization determinism, Unicode/control handling, and empty/limit cases;
2. heading-aware chunk boundaries, token ceilings, offsets, stable UUIDs, exact
   and near-duplicate behavior;
3. embedding batch order, dimensions, finite/non-zero validation, timeout and
   retry classification;
4. repeated ingestion skip, changed-content reindex, changed-permission reindex,
   and concurrent idempotent enqueue;
5. lease exclusivity, stale-lease recovery, attempt accounting, backoff, dead
   letter, and payload-secret rejection;
6. atomic promotion, rollback on chunk/vector failure, old-version visibility,
   usage/outbox updates, and generation increment;
7. cross-workspace RLS and a claim-function test proving the workspace cannot be
   caller selected; and
8. an end-to-end fake connector → durable job → worker → ready source/chunks/
   embeddings test against PostgreSQL/pgvector.

Production release additionally requires parser corpus/fuzz tests, malware and
resource-exhaustion fixtures, real embedding-provider sandbox tests, quality
evaluation, cost/load targets, dashboards, and rollback drills.

## Requirement coverage

| Requirement | Indexing contribution |
| --- | --- |
| `GOOGLE-001` | Defines the normalized authorized handoff for Gmail and Drive content. |
| `GITHUB-001` | Defines the normalized authorized handoff for selected repository content. |
| `DESKTOP-001` | Defines the bounded authorized handoff for selected local files. |
| `SYNC-001` | Provides durable idempotent jobs, incremental fingerprints, retries, and observable failures. |
| `SEARCH-001` | Produces authorized immutable text chunks and versioned embeddings for hybrid retrieval. |
| `ANSWER-001` | Preserves bounded structure and lineage needed for grounded context. |
| `CITATION-001` | Preserves source/version/chunk IDs and page/line/offset coordinates. |
| `CONNECTION-001` | Stops work and excludes sources immediately when connection authority ends. |
| `ACCOUNT-001` | Keeps derived artifacts tenant-linked for complete deletion. |
| `SAFETY-001` | Treats content as inert untrusted data and forbids content-triggered actions. |

## Stage 07 completion criteria

The specification is complete when the state machine, deterministic identities,
extraction/chunk/embedding boundaries, queue authority, atomic promotion,
reindex/failure behavior, observability, test matrix, and requirement mappings
are explicit and automatically validated. B5 implementation completion means
the normalized text/Markdown fake-connector path passes the PostgreSQL end-to-
end gate; it does not claim provider parsers or production semantic embeddings.
