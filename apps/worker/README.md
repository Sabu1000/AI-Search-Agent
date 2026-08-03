# Worker deployment entry points

The worker deployment units use the backend package in `apps/api` and the same
immutable backend image as the API. Separate runtime commands will be added for
sync, indexing, and deletion queues as those stages are implemented.

The foundation configuration check is:

```sh
universal-ai-search-worker --check
```

This directory owns deployment-level worker configuration only. Worker domain
logic remains in the backend package so API and worker processes share explicit
application contracts without copying code.
