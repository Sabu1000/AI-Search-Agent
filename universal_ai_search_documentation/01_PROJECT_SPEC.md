# Project Specification

## Product name
Universal AI Search

## Problem
Knowledge is fragmented across local folders, email, cloud drives, and source-control systems. Users waste time remembering where information lives and manually searching each system.

## Product promise
A user connects approved data sources and asks a natural-language question. The system returns a grounded answer with direct citations to the original sources.

## Primary users
- Individual professionals
- Software engineers
- Researchers
- Founders and operators
- Small teams in a later release

## MVP user stories
- I can create an account and sign in.
- I can install the desktop agent and select folders.
- I can connect Gmail and Drive through Google OAuth.
- I can install a GitHub App on selected repositories.
- I can see sync status and errors.
- I can search by keyword, person, date, repository, folder, or source.
- I can ask a question and receive cited answers.
- I can open each citation in the original provider.
- I can disconnect a provider and delete its indexed data.
- I can delete my account and all retained data.

## Non-goals for MVP
- Sending email
- Editing files
- Autonomous agents
- Slack, Notion, Microsoft 365, Jira, or Dropbox
- Mobile applications
- Enterprise SSO
- Organization-wide permissions
- OCR for scanned documents

## Success metrics
- Retrieval Recall@10 >= 0.85 on the evaluation set
- Citation correctness >= 0.95
- Unsupported material claim rate <= 0.03
- Median query latency <= 6 seconds
- Initial connector sync success >= 0.98
- Incremental sync success >= 0.995
- Account deletion completes within 24 hours

## MVP limits
- 25,000 indexed items per user
- 10 GB extracted content per user
- 100 MB maximum individual file size
- 30 requests per minute per free user
- Read-only connectors only

## Core screens
- Marketing and pricing
- Sign up and login
- Onboarding
- Connections
- Search and chat
- Sources browser
- Sync status
- Privacy and deletion settings
- Billing
