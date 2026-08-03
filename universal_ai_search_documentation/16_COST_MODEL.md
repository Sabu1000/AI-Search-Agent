# Cost Model

## Cost drivers
- Embedding tokens during indexing
- Answer-generation tokens
- PostgreSQL storage and vector indexes
- Object storage
- Connector API traffic
- Worker CPU
- Monitoring retention

## Beta planning range
For 50–200 active testers:
- application compute: $100–$500/month
- managed PostgreSQL: $100–$600/month
- Redis: $30–$150/month
- object storage and transfer: $20–$200/month
- monitoring: $0–$300/month
- AI usage: $100–$2,000/month

Expected broad total: $350–$3,750/month depending on usage and architecture.

## Unit economics to track
- cost per indexed 1,000 documents
- cost per active user
- cost per search
- cost per cited answer
- storage per user
- sync API calls per connector

## Cost controls
- batch embeddings
- skip unchanged files
- deduplicate content
- use smaller models for query planning
- cache embeddings and repeated searches
- cap context size
- enforce plan quotas
- archive inactive content when appropriate
