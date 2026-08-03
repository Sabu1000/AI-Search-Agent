# Indexing Pipeline

## Pipeline stages
1. Validate ownership and provider permission.
2. Compute source fingerprint.
3. Skip unchanged sources.
4. Extract text.
5. Normalize Unicode and whitespace.
6. Remove boilerplate and repeated quoted content.
7. Detect structure.
8. Chunk.
9. Deduplicate.
10. Generate embeddings in batches.
11. Write keyword and vector indexes transactionally.
12. Mark source searchable.

## File parsers
- PDF: page-aware extraction; preserve page numbers
- DOCX: headings, paragraphs, lists, and tables
- PPTX: slide title, body, notes, slide number
- XLSX/CSV: sheet name, table ranges, row headers
- Markdown: heading tree
- Code: tree-sitter or language-aware symbol parser
- Email: subject, sender, recipients, timestamp, cleaned body

## Chunk targets
- General documents: 400–800 tokens
- Emails: one logical message or compact thread segment
- Code: one symbol or tightly related group of symbols
- Tables: header plus 20–50 rows depending on token count
- Overlap: 50–100 tokens only when structure requires it

## Deduplication
- Source-level SHA-256 content hash
- Near-duplicate chunk detection using SimHash or MinHash
- Do not index quoted email history multiple times

## Idempotency
The same source version processed repeatedly must produce the same chunk IDs. Use deterministic IDs from `document_id + parser_version + chunk_index + chunk_hash`.

## Reindexing
Trigger when:
- parser version changes
- embedding model changes
- chunking strategy changes
- source content hash changes
- permissions hash changes

## Large files
- Reject unsupported or oversized files before extraction
- Stream extraction where possible
- Store parser failure reason
- Allow user-visible retry

## Failure handling
A source is either fully indexed at one version or remains on the previous searchable version. Never expose a partially replaced document.
