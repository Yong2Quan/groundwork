---
id: RB-006
title: Upstream dependency degradation and rate-limit throttling
covers: [upstream_dependency_outage, rate_limit_throttling]
services: [checkout-api, payments-worker, notify-dispatch, ledger-sync]
---

# Upstream dependency degradation and rate-limit throttling

Both present as failures that originate outside the service you are paged for.
They are told apart by status code and by whether the failure correlates with
your own request volume.

## Upstream dependency outage

`upstream_5xx_rate` rises *before* your service own `error_rate` and
`latency_p99`. Your own resource metrics - CPU, memory, connections - stay
normal throughout, because your service is healthy and merely waiting.

The causal ordering is what identifies this. If your latency moved first, the
upstream errors are a consequence of your own retry storm, not the cause.

## Rate-limit throttling

The upstream returns 429, not 5xx, so `upstream_4xx_rate` carries the signal
while `upstream_5xx_rate` stays flat. Throttling correlates tightly with your
own request rate: it begins when `request_rate` crosses the quota and stops
when it falls back under.

Check the quota documented for the integration against measured request rate
over the window. A retry policy without jitter will hold you above the quota
indefinitely once you cross it.

## Remediation

For an outage, fail open or shed load, and stop retrying - retries extend the
outage for everyone. For throttling, add jitter and respect `Retry-After`.
