# Observability and Operations

## Correlation IDs
Every HTTP request, job, provider call, model call, and citation pipeline event carries a request or trace ID.

## Metrics
- API latency and error rate
- search latency by stage
- model latency and token usage
- retrieval candidate counts
- sync lag by provider
- queue depth and age
- parser failure rate
- embedding failure rate
- deletion backlog
- OAuth refresh failures
- desktop active devices

## Logs
Structured JSON logs with:
- timestamp
- service
- environment
- trace_id
- workspace_id hash
- user_id hash
- event
- duration
- status
- sanitized error code

## Alerts
- API 5xx above threshold
- queue oldest age above 10 minutes
- database connections above 80%
- sync failure spike
- OAuth refresh failure spike
- deletion job older than 24 hours
- cross-tenant authorization anomaly

## Dashboards
- Product usage
- Search quality
- Connector health
- Infrastructure health
- Cost and model usage
- Security events

## SLOs
- API availability 99.9% production
- P95 non-LLM API latency under 500 ms
- P95 end-to-end search under 10 seconds
- 99% incremental sync completion within 15 minutes
