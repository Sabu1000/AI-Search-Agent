# Implementation Plan

## Solo full-time estimate
- Prototype: 3–4 weeks
- Connector MVP: 10–14 weeks
- Desktop and public beta: 6–10 additional weeks
- Production hardening: 2–4 additional months

## Sprint 1
- Monorepo
- local Docker environment
- auth skeleton
- database migrations
- file upload endpoint

## Sprint 2
- parsers
- chunking
- embeddings
- pgvector search
- source browser

## Sprint 3
- full-text search
- hybrid ranking
- chat streaming
- citations
- evaluation harness

## Sprint 4
- Google OAuth
- Drive import
- Gmail import
- initial sync UI

## Sprint 5
- incremental Google sync
- retries
- disconnect and deletion
- provider health

## Sprint 6
- GitHub App
- repository selector
- code parser
- issues and pull requests

## Sprint 7
- Tauri setup
- folder selection
- scanner
- manifest and sync protocol

## Sprint 8
- filesystem watcher
- offline queue
- signed packaging
- device management

## Sprint 9
- billing and quotas
- account export and deletion
- operational dashboards
- beta onboarding

## Definition of done
A feature is done only when it includes tests, metrics, user-visible errors, documentation, security review, and rollback instructions.
