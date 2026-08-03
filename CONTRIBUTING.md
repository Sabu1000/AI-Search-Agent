# Contributing to Universal AI Search

## Development workflow

1. Create a focused branch from `main`.
2. Keep changes within the current documented stage or foundation milestone.
3. Add tests for new behavior and regression tests for fixes.
4. Run `pnpm check` and the relevant container checks.
5. Open a pull request using the repository template.

## Prerequisites

- Node.js 22 LTS
- pnpm 11
- Python 3.12
- Docker Desktop with Docker Compose
- Rust stable and the platform-specific Tauri prerequisites for desktop work

Copy `.env.example` to `.env`; never place real credentials in a committed file.

## Commit messages

Use an imperative, scoped message that explains the completed outcome, such as:

```text
feat(search): add workspace-scoped keyword retrieval
fix(sync): preserve cursor when provider fetch fails
docs: define local development workflow
```

Keep generated files, formatting changes, and feature behavior in the same
commit only when they are part of one coherent outcome.

## Pull-request requirements

- Explain the user-visible or operational impact.
- List automated and manual tests performed.
- Include a security and privacy impact note.
- Include an observability impact note.
- Include migration and rollback notes when applicable.
- Add screenshots for visual changes.

Never commit secrets, private user data, provider exports, or production logs.
