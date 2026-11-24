# Offline retrieval regression corpus

`search_evaluation.v1.json` contains 25 invented documents across GitHub, Gmail,
Google Drive and local files, including one document in an unrelated test
workspace. The 28 hand-labeled questions cover exact lookup, code, email,
date/person filtering, semantic phrasing, cross-source evidence, conflicting
evidence, and no-answer cases. No real account, private dataset, embedding API,
model API, or live provider integration is used.

Run the benchmark against a **disposable, local PostgreSQL test database** with
the repository's extensions and the usual `UAS_TEST_DATABASE_DSN`,
`UAS_DATABASE_MIGRATION_URL`, and `UAS_TEST_ADMIN_DSN` settings:

```sh
cd apps/api
pytest -s -o addopts='' tests_database/test_search_evaluation.py
```

It also runs in the existing database suite (`./scripts/test-database.sh`).
The benchmark migrates the test database, queues and indexes the fixture through
the real indexing service, sends requests through the actual search route, and
uses the `app_api` database role. Authentication is a synthetic fixture; tenant
visibility is enforced by PostgreSQL. The `-s` switch shows the JSON report.

- Recall@5, Recall@10 and MRR are computed from distinct source IDs among the
  first ten returned results, with labels defined after filters and authorization.
- Citation correctness requires a known eligible source and matching chunk,
  bidirectional claim references, and a literal excerpt. Citation relevance
  precision and label recall additionally compare cited sources to the hand labels.
- An unsupported extract has no validated citation matching its claim text; this
  mechanical check is not semantic entailment. Conflicting sources are labeled on
  both sides to measure evidence retrieval, not conflict resolution.
- No-answer cases must return the insufficient-evidence flag with no claim or
  citation. Unauthorized results or citations must be zero. Reported latency is
  local HTTP request wall time; it is not a production performance estimate.

Regression floors: Recall@10 ≥ 0.85, citation correctness ≥ 0.95, unsupported
extracts ≤ 0.03, all four no-answer cases abstain, no unauthorized references, and
median latency ≤ 6 seconds. Keep class-level metrics visible: a pooled score can
mask weak semantic performance. This small deterministic set is not the full
release benchmark. The 300-question dev/held-out corpus, independent claim and
conflict assessment, and production-like latency/cost evaluation remain open.
