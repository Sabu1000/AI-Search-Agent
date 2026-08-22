# Google Drive Phase Review

**Decision:** Pass for the tested selected-root, authoritative full-scan backend
scope (`P6-001` through `P6-010`).

## Completed behavior

| Task | Evidence |
| --- | --- |
| Drive API client | Exact read-only authorization, token refresh, bounded folder pages, shared-drive parameters, strict field projection and response validation, bounded media/export streaming, and sanitized provider errors. |
| OAuth scopes | The connection flow requests and verifies the exact pinned Drive read-only scope, binds callback state and PKCE to the initiating session/workspace, and encrypts credentials at rest. |
| Folder sync | One bounded folder page per durable job, encrypted traversal state, stable Drive-ID identity, logical paths, selected/shared roots, deterministic child/page jobs, whole-tree completion accounting, and shortcut non-traversal. |
| PDF | Bounded media download plus page/text limits, deterministic normalized extraction, and explicit empty, encrypted, malformed, excessive, oversized, or truncated fallback states. |
| DOCX | Bounded media/ZIP/XML/text processing with defused XML, preserved headings, paragraphs, lists, and table cells, plus safe fallback descriptors. |
| Google Docs | Read-only DOCX export through the same bounded parser while retaining native file identity, MIME type, metadata, and canonical link. |
| Google Sheets | Read-only XLSX export with named sheets, non-empty rows, cell values, booleans, formulas, and cached results under archive, XML, sheet, cell, and text limits. |
| Google Slides | Read-only PPTX export with numeric slide order, visible paragraph text, and speaker notes under archive, XML, slide, and text limits. |
| Deletion | Per-run seen markers plus a deterministic retryable reconciliation barrier after successful whole-tree completion; absent sources are scrubbed and derived index data is purged. |

## Safety conclusions

- A partial, failed, or still-running folder traversal cannot delete unseen Drive
  files. Reconciliation is a separate durable job created only after every page
  in the scan has completed without failure.
- Stable Drive file IDs—not names or paths—define source identity. Renames and
  moves update metadata without creating a second source.
- Shortcuts remain inert searchable descriptors and are never followed, so they
  cannot escape a selected root.
- Exported Office archives have independent compressed, expanded, member, XML,
  structural, and text limits. Defused XML rejects entity expansion.
- Malformed, empty, encrypted, unsupported, excessive, and oversized files retain
  bounded descriptors and safe status codes without aborting their folder page.
- Deleted content is not merely hidden: versions cascade through chunks and
  embeddings, dependent people/citations are removed, pending index work fails
  closed, usage/generation state is updated, and only scrubbed tombstones remain.
- Credentials, provider bodies, folder IDs, paths, and page tokens do not enter
  durable error messages; traversal secrets stay inside encrypted progress.

## Automated certification

```sh
pnpm check
pnpm build
./scripts/test-backend.sh
./scripts/test-connector-sdk.sh
./scripts/test-database.sh
```

The certified local suite contains `160` backend tests at `90.97%` line
coverage, `25` Connector SDK tests at `99%`, and `20` live PostgreSQL tests.
The Drive database path proves durable root-to-subfolder traversal, delayed
whole-tree completion, searchable PDF/DOCX/Docs/Sheets/Slides text, authoritative
absence reconciliation, scrubbed tombstones, derived-version removal, migration
lifecycle, and tenant isolation.

## Explicitly outside this phase

Incremental Drive Changes API polling is a later latency/efficiency improvement;
authoritative full scans already provide the certified deletion contract. The
folder-selection/connection UI, a live Google-project smoke test, uploaded
XLSX/PPTX support, OCR, and password-protected Office extraction remain later
integrations. They do not change the completed read-only Drive backend milestone.
