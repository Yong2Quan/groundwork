---
id: RB-004
title: Out-of-memory restarts and memory leaks
covers: [memory_leak_oom]
services: [media-transcode, search-indexer, report-builder, payments-worker]
---

# Out-of-memory restarts and memory leaks

## Signature

`memory_utilization` climbs monotonically into a sawtooth, where each tooth
ends in a restart and resets to baseline. `restart_count` increments at the
peaks. The orchestrator records `OOMKilled` as the termination reason and the
exit code is 137.

Distinguish this from a load-driven memory rise: a leak climbs while
`request_rate` is flat, and does not fall when traffic falls overnight.

## Confirming

Correlate the start of the climb with the deploy history. A leak introduced by
a release shows a clean inflection at the deploy timestamp. A leak that has
always been present shows a climb whose period scales with the memory limit.

## Remediation

Raising the memory limit buys time proportional to the increase and nothing
else - it lengthens the sawtooth period without changing the slope. Capture a
heap profile at 80% utilisation, before the kill, and compare retained-object
counts against a profile taken shortly after start.
