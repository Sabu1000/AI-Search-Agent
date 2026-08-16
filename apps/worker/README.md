# Worker deployment entry points

The worker deployment unit uses the backend package in `apps/api` and the same
immutable backend image as the API. Its current loop gives Gmail sync work
priority, processes one provider page per durable job, and otherwise consumes
index jobs. Deletion and other provider queues will be added in later stages.

The foundation configuration check is:

```sh
universal-ai-search-worker --check
```

This directory owns deployment-level worker configuration only. Worker domain
logic remains in the backend package so API and worker processes share explicit
application contracts without copying code.
