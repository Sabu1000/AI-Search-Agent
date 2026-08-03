# Deployment and Infrastructure

## Initial production architecture
- Frontend: Vercel or CloudFront-hosted Next.js
- API and workers: AWS ECS Fargate
- Database: AWS RDS PostgreSQL Multi-AZ
- Redis: ElastiCache
- Object storage: S3
- Secrets: AWS Secrets Manager
- Encryption: AWS KMS
- DNS and TLS: Route 53 and ACM
- Logs and metrics: CloudWatch plus Sentry

## Environments
- local
- development
- staging
- production

No production credentials are shared with lower environments.

## Infrastructure as code
Use Terraform modules for:
- networking
- ECS
- RDS
- Redis
- S3
- KMS
- IAM
- monitoring

## CI/CD
1. Lint and type check.
2. Run unit and integration tests.
3. Build containers.
4. Scan dependencies and images.
5. Push immutable image tag.
6. Deploy staging.
7. Run smoke tests.
8. Require approval for production.
9. Run database migrations before traffic shift.

## Database migrations
- Alembic
- Backward-compatible expand/contract pattern
- Never drop columns in the same release that stops writing them

## Scaling
- API scales on CPU and request count
- Workers scale on queue depth
- Separate queues for sync, indexing, deletion, and embeddings
- Provider-specific concurrency limits

## Backups
- Automated RDS backups
- Point-in-time recovery
- S3 versioning where appropriate
- Quarterly restore test
