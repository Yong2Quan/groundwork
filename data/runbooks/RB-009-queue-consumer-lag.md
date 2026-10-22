---
id: RB-009
title: Queue consumer lag and backlog growth
covers: [queue_consumer_lag]
services: [payments-worker, notify-dispatch, ledger-sync, media-transcode]
---

# Queue consumer lag and backlog growth

## Signature

`queue_depth` grows without bound while `consumer_lag_seconds` rises with it.
Production rate exceeds consumption rate. The user-visible symptom is delay,
not failure - messages are eventually processed, just far too late, so error
rate often stays flat and the service looks healthy on a dashboard.

## Which side is at fault

Compare `queue_depth` against `request_rate`:

- `request_rate` flat and `queue_depth` rising: the consumer slowed down. Look
  for a deploy, a dependency that got slower, or consumer replicas that were
  scaled down or are crash-looping.
- `request_rate` up proportionally with `queue_depth`: producers got busier
  and consumer capacity never followed. This is a capacity problem.

A consumer stuck on a single poison message shows a third shape: `queue_depth`
rising while `consumer_lag_seconds` rises on one partition only.

## Remediation

Scale consumers for a capacity problem. For a slowdown, roll back the consumer
change. Never purge the queue to make the number go down - that is data loss,
and the backlog is the only record that the work was requested.
