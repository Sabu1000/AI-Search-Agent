# Database migrations

Alembic is the only application-schema migration mechanism. Run migrations
from `apps/api` with a privileged migration URL:

```sh
UAS_DATABASE_MIGRATION_URL=postgresql+psycopg://... alembic upgrade head
```

Application API and worker credentials must never be used for schema changes.
The initial privileged migration creates the NOLOGIN `app_migrator` owner role
and grants it to the deployment identity so later revisions can manage owned
objects without making that role directly connectable.

Initial-schema downgrade is supported only for empty development/test
databases; production recovery rolls application code forward or restores a
tested backup instead of discarding accepted user data.
