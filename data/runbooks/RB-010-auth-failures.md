---
id: RB-010
title: Authentication failures - clock skew, DNS and quota
covers: [clock_skew_auth_failure, dns_misconfiguration, resource_quota_exceeded]
services: [auth-gateway, session-store, notify-dispatch, inventory-cache]
---

# Authentication failures: clock skew, DNS and quota

Three unrelated causes produce authentication-shaped failures. They are
separated by the error text and by which hosts are affected.

## Clock skew

Signed tokens carry `nbf` (not before) and `exp` (expiry) claims validated
against the verifier local clock. If that clock drifts past the allowed skew,
valid tokens are rejected as `token used before issued` or `token is expired`
while the issuer believes everything is fine.

The signature is `auth_failure_rate` high on *some* hosts and zero on others -
skew is per host, so a partial failure that correlates with the host serving
the request is close to conclusive. `clock_offset_ms` exceeding the allowed
skew confirms it. Check that time synchronisation is actually running on the
affected hosts; a failed sync daemon is the usual cause.

## DNS misconfiguration

Errors are `NXDOMAIN`, `SERVFAIL`, or `no such host`, and they appear at
connection time rather than after a response. `dns_resolution_failures` rises
while the dependency itself is healthy when addressed directly by IP - that
contrast is the diagnostic.

Causes are a record that expired or was edited, a stale negative cache entry
still being served for its TTL, or a resolver change that altered the search
path. Resolution failures hit everything using that name at once, regardless
of which host is asking.

## Resource quota exceeded

The provider returns an explicit quota error - `quota exceeded`,
`RESOURCE_EXHAUSTED`, `LimitExceeded` - naming the limit. `quota_used_percent`
sits at 100%. Unlike throttling, this does not recover when your request rate
drops, because the limit is on a provisioned resource rather than on a rate.
Only raising the quota or releasing the resource clears it.
