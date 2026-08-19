# Worker deployment entry points

The worker deployment unit uses the backend package in `apps/api` and the same
immutable backend image as the API. Its current loop processes at most one
bounded Gmail full/incremental page and one index job per iteration. Gmail
history deletions immediately tombstone sources so search excludes them; other
provider and retention/deletion queues will be added in later stages.

The foundation configuration check is:

```sh
universal-ai-search-worker --check
```

This directory owns deployment-level worker configuration only. Worker domain
logic remains in the backend package so API and worker processes share explicit
application contracts without copying code.
