# Coding Standards

## Python
- Python 3.12+
- Ruff for linting and formatting
- mypy or pyright strict mode
- Pydantic models at boundaries
- SQLAlchemy 2.x
- pytest
- no untyped public functions

## TypeScript
- strict mode
- ESLint
- Prettier
- no implicit `any`
- runtime validation for API responses
- React components kept presentation-focused

## Architecture rules
- Provider-specific code stays inside connector packages
- API handlers contain no indexing logic
- Workers are idempotent
- Database access goes through repositories or service modules
- Authorization is checked before resource loading
- External API calls have timeouts and retries

## Pull-request requirements
- tests
- migration review when schema changes
- security impact note
- observability impact note
- screenshots for UI changes
- rollback plan for risky changes

## Secrets
Never commit credentials, private keys, real user data, or production exports.
