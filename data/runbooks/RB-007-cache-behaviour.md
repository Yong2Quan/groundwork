---
id: RB-007
title: Cache behaviour - stampede, cold start and eviction
covers: [cache_stampede]
services: [inventory-cache, session-store, search-indexer, checkout-api]
---

# Cache behaviour: stampede, cold start and eviction

## Cache stampede

When a hot key expires, every concurrent request for that key misses at the
same instant and goes to the origin together. The signature is a sharp drop in
`cache_hit_ratio` paired with a simultaneous spike in `origin_request_rate`,
both recovering within a few minutes without intervention.

Two details separate a stampede from a cold start. A stampede is periodic - it
recurs on the key TTL boundary, so look for the same shape at a regular
interval. A cold start happens once, after a deploy or a cache flush, and does
not recur.

Latency during a stampede rises at the origin, not in the cache tier, so
`cache_latency_p99` stays flat while the backing service suffers. Teams paged
for the backing service routinely diagnose it as a backing-service problem.

## Remediation

Add jitter to the TTL so keys do not expire in lockstep, and serve stale while
revalidating in the background. A request-coalescing lock in front of the
origin fetch caps concurrent regeneration at one per key.
