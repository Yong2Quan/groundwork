---
id: RB-003
title: Database connection pool exhaustion
covers: [connection_pool_exhaustion]
services: [checkout-api, ledger-sync, report-builder, auth-gateway]
---

# Database connection pool exhaustion

## Signature

`db_active_connections` sits pinned at the configured pool maximum - a flat
ceiling, not a spike - while `db_query_time_p99` stays near its baseline.
Individual queries are still fast; requests are queuing for a connection that
never becomes free. Application logs show `TimeoutError: pool exhausted`,
`could not obtain a connection within 30s`, or similar wait-timeout messages.

The flat ceiling is the diagnostic. A genuinely slow database raises query
time; an exhausted pool raises *wait* time while query time stays flat.

## Common triggers

- A traffic increase that pushes steady-state concurrency past the pool size.
- A connection leak: a code path that takes a connection and fails to return it
  on the error branch. The ceiling is reached gradually over hours.
- A long-running transaction holding connections open - check for a batch job
  or a migration running against the same database.

## Remediation

Short term, raise the pool maximum and restart the service to clear leaked
connections. This is a mitigation, not a fix: if the cause is a leak, the
ceiling will be reached again, just more slowly.

Long term, find the leaking path, and size the pool against measured
concurrency rather than guessing.
