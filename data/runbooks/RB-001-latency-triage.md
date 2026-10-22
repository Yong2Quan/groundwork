---
id: RB-001
title: Triaging elevated p99 latency on request-serving services
covers: [connection_pool_exhaustion, db_slow_query_missing_index, upstream_dependency_outage]
services: [checkout-api, auth-gateway, search-indexer, report-builder]
---

# Triaging elevated p99 latency on request-serving services

A latency spike on a request-serving service is a *symptom*, not a cause. Three
causes account for most of them, and they are only distinguishable from
metrics - the ticket text alone will not tell you which one you have.

## Decision procedure

Pull the service metric window covering the 30 minutes before the alert fired,
then walk this table in order:

| Observation | Conclusion |
| --- | --- |
| `db_active_connections` flat at the configured pool maximum, `db_query_time_p99` roughly unchanged | Connection pool exhaustion. See RB-003. |
| `db_query_time_p99` rising steadily, `db_active_connections` well below maximum | Slow query, usually a missing or unused index. See the index section below. |
| `upstream_5xx_rate` rising *before* this service's own latency, `db_*` series flat | Upstream dependency degradation. See RB-006. |

The ordering matters. Connection saturation and a slow query both end in high
latency and both raise error rates once callers start timing out, so an
engineer reading only the alert text will frequently pick the wrong one.

## Missing or unused index

Symptoms are a steadily climbing `db_query_time_p99` with normal connection
counts and normal upstream health. Query time climbs with table growth rather
than with traffic, so the regression often appears days after the change that
caused it.

Remediation: identify the slow statement from the statement digest, confirm the
plan performs a sequential scan, and add the covering index during a
low-traffic window.

## What not to do

Do not restart the service. Restarting clears the connection pool and masks
pool exhaustion for several minutes, which destroys the evidence you need to
tell these three causes apart.
