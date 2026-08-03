# MASTER_TASK_LIST.md

**Project:** Universal AI Search

**Version:** 1.0

**Status:** In progress — documentation stages 00 through 03 and the runnable
project foundation are complete; repository policy and licensing remain owner
decisions

---

# Overview

This document is the master implementation roadmap for Universal AI Search.

Every feature in the system is broken into small, verifiable tasks.

Rules:

- Never skip dependencies.
- Complete every task before starting dependent tasks.
- Every completed task must compile, pass tests, and be documented.
- Mark completed tasks with `[x]`.
- Create a pull request for every major feature branch.
- If architecture changes, update the documentation first.

---

# Phase 1 — Project Foundation

## Repository

- [x] P1-001 Create GitHub repository
- [ ] P1-002 Configure branch protection
- [x] P1-003 Create README
- [ ] P1-004 Add LICENSE
- [x] P1-005 Create CONTRIBUTING guide
- [x] P1-006 Create CODEOWNERS
- [x] P1-007 Configure issue templates
- [x] P1-008 Configure pull request template

## Monorepo

- [x] P1-009 Initialize monorepo
- [x] P1-010 Configure pnpm workspace
- [x] P1-011 Create backend application
- [x] P1-012 Create frontend application
- [x] P1-013 Create desktop application
- [x] P1-014 Create shared packages

## Development Environment

- [x] P1-015 Configure Docker Compose
- [x] P1-016 Configure PostgreSQL
- [x] P1-017 Configure Redis
- [x] P1-018 Configure MinIO (S3-compatible storage)
- [x] P1-019 Create .env.example
- [x] P1-020 Configure local environment

## Code Quality

- [x] P1-021 Configure Ruff
- [x] P1-022 Configure Black
- [x] P1-023 Configure ESLint
- [x] P1-024 Configure Prettier
- [x] P1-025 Configure pre-commit hooks

## Testing

- [x] P1-026 Configure pytest
- [x] P1-027 Configure frontend testing
- [x] P1-028 Configure GitHub Actions
- [x] P1-029 Verify Docker build
- [ ] P1-030 Phase review

**Milestone:** A new developer can clone the repository and run the project locally with a single documented setup process.

---

# Phase 2 — Authentication

- [ ] P2-001 User model
- [ ] P2-002 Password hashing
- [ ] P2-003 JWT authentication
- [ ] P2-004 Refresh tokens
- [ ] P2-005 Register endpoint
- [ ] P2-006 Login endpoint
- [ ] P2-007 Logout endpoint
- [ ] P2-008 Forgot password
- [ ] P2-009 Reset password
- [ ] P2-010 Email verification
- [ ] P2-011 Google OAuth
- [ ] P2-012 GitHub OAuth (login)
- [ ] P2-013 Session management
- [ ] P2-014 User profile API
- [ ] P2-015 Authentication middleware
- [ ] P2-016 Authorization middleware
- [ ] P2-017 Security review
- [ ] P2-018 Phase review

**Milestone:** Users can securely create accounts and log in.

---

# Phase 3 — Database

- [ ] P3-001 PostgreSQL migrations
- [ ] P3-002 Users table
- [ ] P3-003 Connections table
- [ ] P3-004 Sources table
- [ ] P3-005 Documents table
- [ ] P3-006 Chunks table
- [ ] P3-007 Conversations table
- [ ] P3-008 Messages table
- [ ] P3-009 Citations table
- [ ] P3-010 Sync jobs table
- [ ] P3-011 Audit logs
- [ ] P3-012 Database indexes
- [ ] P3-013 Seed data
- [ ] P3-014 Backup strategy
- [ ] P3-015 Phase review

**Milestone:** Database schema is complete and versioned.

---

# Phase 4 — Connector SDK

- [ ] P4-001 BaseConnector interface
- [ ] P4-002 OAuth manager
- [ ] P4-003 Token storage
- [ ] P4-004 Connector registry
- [ ] P4-005 Job scheduler
- [ ] P4-006 Retry framework
- [ ] P4-007 Sync framework
- [ ] P4-008 Health checks
- [ ] P4-009 Logging
- [ ] P4-010 Metrics
- [ ] P4-011 Phase review

**Milestone:** New connectors can be added using one shared framework.

---

# Phase 5 — Gmail Connector

- [ ] P5-001 Google OAuth scopes
- [ ] P5-002 Gmail API client
- [ ] P5-003 Initial sync
- [ ] P5-004 Incremental sync
- [ ] P5-005 Email parser
- [ ] P5-006 Attachment extraction
- [ ] P5-007 Metadata extraction
- [ ] P5-008 Normalization
- [ ] P5-009 Delete handling
- [ ] P5-010 Error recovery
- [ ] P5-011 Phase review

**Milestone:** Gmail emails are searchable.

---

# Phase 6 — Google Drive

- [ ] P6-001 Drive API client
- [ ] P6-002 OAuth scopes
- [ ] P6-003 Folder sync
- [ ] P6-004 PDF support
- [ ] P6-005 DOCX support
- [ ] P6-006 Google Docs support
- [ ] P6-007 Google Sheets support
- [ ] P6-008 Google Slides support
- [ ] P6-009 Delete handling
- [ ] P6-010 Phase review

**Milestone:** Google Drive documents are searchable.

---

# Phase 7 — GitHub

- [ ] P7-001 GitHub App
- [ ] P7-002 Repository sync
- [ ] P7-003 Code indexing
- [ ] P7-004 README indexing
- [ ] P7-005 Issue indexing
- [ ] P7-006 Pull request indexing
- [ ] P7-007 Webhooks
- [ ] P7-008 Delete handling
- [ ] P7-009 Phase review

**Milestone:** GitHub repositories are searchable.

---

# Phase 8 — Indexing Pipeline

- [ ] P8-001 Text extraction
- [ ] P8-002 Metadata extraction
- [ ] P8-003 Language detection
- [ ] P8-004 Chunking
- [ ] P8-005 Embedding generation
- [ ] P8-006 Duplicate detection
- [ ] P8-007 Queue processing
- [ ] P8-008 Incremental indexing
- [ ] P8-009 Re-indexing
- [ ] P8-010 Phase review

**Milestone:** All supported content becomes searchable.

---

# Phase 9 — Search Engine

- [ ] P9-001 Keyword search
- [ ] P9-002 Vector search
- [ ] P9-003 Metadata filters
- [ ] P9-004 Query rewriting
- [ ] P9-005 Result ranking
- [ ] P9-006 Context builder
- [ ] P9-007 Citation builder
- [ ] P9-008 Search API
- [ ] P9-009 Phase review

**Milestone:** Hybrid search returns accurate results with citations.

---

# Phase 10 — AI Chat

- [ ] P10-001 Conversation model
- [ ] P10-002 Streaming responses
- [ ] P10-003 Prompt builder
- [ ] P10-004 Conversation memory
- [ ] P10-005 Citation rendering
- [ ] P10-006 Suggested follow-up questions
- [ ] P10-007 Phase review

**Milestone:** Users can ask natural-language questions and receive grounded answers.

---

# Phase 11 — Frontend

- [ ] P11-001 Landing page
- [ ] P11-002 Dashboard
- [ ] P11-003 Search interface
- [ ] P11-004 Chat interface
- [ ] P11-005 Connections page
- [ ] P11-006 Source browser
- [ ] P11-007 Settings
- [ ] P11-008 User profile
- [ ] P11-009 Error states
- [ ] P11-010 Phase review

**Milestone:** Web application is fully usable.

---

# Phase 12 — Desktop Agent

- [ ] P12-001 Tauri setup
- [ ] P12-002 Folder selection
- [ ] P12-003 File watcher
- [ ] P12-004 Local scanner
- [ ] P12-005 Sync client
- [ ] P12-006 Offline queue
- [ ] P12-007 Device registration
- [ ] P12-008 Phase review

**Milestone:** Local files become searchable.

---

# Phase 13 — Infrastructure

- [ ] P13-001 AWS infrastructure
- [ ] P13-002 PostgreSQL deployment
- [ ] P13-003 Redis deployment
- [ ] P13-004 Object storage
- [ ] P13-005 Secrets management
- [ ] P13-006 Monitoring
- [ ] P13-007 Logging
- [ ] P13-008 Backups
- [ ] P13-009 Phase review

**Milestone:** Production infrastructure is operational.

---

# Phase 14 — Security

- [ ] P14-001 Encryption at rest
- [ ] P14-002 HTTPS
- [ ] P14-003 Rate limiting
- [ ] P14-004 Audit logging
- [ ] P14-005 Threat mitigation
- [ ] P14-006 Security review
- [ ] P14-007 Phase review

**Milestone:** Core security requirements are implemented.

---

# Phase 15 — Testing

- [ ] P15-001 Backend unit tests
- [ ] P15-002 Frontend tests
- [ ] P15-003 Integration tests
- [ ] P15-004 End-to-end tests
- [ ] P15-005 Load testing
- [ ] P15-006 Retrieval evaluation
- [ ] P15-007 Phase review

**Milestone:** The application is tested and stable.

---

# Phase 16 — Billing

- [ ] P16-001 Stripe integration
- [ ] P16-002 Subscription plans
- [ ] P16-003 Usage tracking
- [ ] P16-004 Customer portal
- [ ] P16-005 Phase review

**Milestone:** Paid subscriptions are supported.

---

# Phase 17 — Public Beta

- [ ] P17-001 Monitoring dashboards
- [ ] P17-002 Feedback collection
- [ ] P17-003 Crash reporting
- [ ] P17-004 Feature flags
- [ ] P17-005 Launch checklist
- [ ] P17-006 Beta launch
- [ ] P17-007 Post-launch review

**Milestone:** Public beta is live.

---

# Future (Post-MVP)

- Slack connector
- Notion connector
- OneDrive connector
- Dropbox connector
- Outlook connector
- Teams connector
- Calendar search
- OCR
- Image search
- Audio transcription
- Browser extension
- Team workspaces
- Enterprise SSO
- Self-hosted deployment
- Mobile application
