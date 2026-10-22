"""Generate the synthetic incident corpus.

Everything in data/ is produced by this script. It is deterministic: the same
seed yields byte-identical output, so the eval is reproducible and the labels
can be audited by reading this file rather than trusting 30 hand-typed JSON
blobs.

Design note (this is the part that makes the eval mean something):

Each incident is assigned an *answerability tier* that states what a responder
must read in order to reach the root cause.

  ticket_sufficient   The description names the signature outright. A model
                      with no retrieval and no tools should get these right.
  runbook_sufficient  The description gives a symptom; the symptom-to-cause
                      mapping lives in a runbook. Retrieval should close it.
  evidence_required   The description is deliberately ambiguous across two or
                      three candidate causes, and the discriminating evidence
                      exists ONLY in the metric series or in past incidents.
                      No amount of runbook text resolves these.

The tiers are the independent variable. If the agent configuration does not
beat RAG on evidence_required, the agent is not earning its latency and cost,
and the write-up should say so.

Usage:  python scripts/generate_data.py
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SEED = 20260401
INTERVAL_MINUTES = 5
POINTS = 36           # 3 hours of 5-minute samples
ONSET_INDEX = 24      # incident begins 1 hour before the window ends

# --------------------------------------------------------------------------
# Closed root-cause taxonomy. Every configuration picks from exactly this
# list, which makes scoring an exact-match comparison rather than a judgement
# call about free text.
# --------------------------------------------------------------------------

TAXONOMY = {
    "connection_pool_exhaustion": "Requests queue waiting for a database connection; the pool is at its configured maximum.",
    "db_slow_query_missing_index": "A query degraded, typically from a missing or unused index, raising query execution time.",
    "upstream_dependency_outage": "A dependency this service calls started failing with server errors; this service is healthy.",
    "rate_limit_throttling": "An upstream is rejecting requests with 429 because this service crossed a published quota.",
    "expired_tls_certificate": "TLS handshakes fail because the leaf certificate passed its notAfter date.",
    "memory_leak_oom": "Memory grows without bound until the orchestrator kills the process; the cycle repeats.",
    "disk_full": "A volume reached capacity and writes fail with ENOSPC.",
    "bad_deploy_regression": "A release introduced a defect; failures start at the deploy timestamp.",
    "config_drift_feature_flag": "A feature flag or configuration value changed behaviour without a deploy.",
    "cache_stampede": "A hot key expired and concurrent misses stampeded the origin simultaneously.",
    "queue_consumer_lag": "Consumers cannot keep up with producers; the backlog and its age both grow.",
    "clock_skew_auth_failure": "A verifier clock drifted beyond the allowed skew, so valid signed tokens are rejected.",
    "dns_misconfiguration": "Name resolution for a dependency fails, so connections never get established.",
    "resource_quota_exceeded": "A provisioned resource limit is fully consumed and does not recover when load drops.",
}

SERVICES = [
    "checkout-api", "payments-worker", "search-indexer", "auth-gateway",
    "notify-dispatch", "ledger-sync", "media-transcode", "inventory-cache",
    "report-builder", "session-store",
]

# Baseline value and per-sample jitter for every series we emit.
BASELINES = {
    "request_rate":            (820.0, 40.0),
    "error_rate":              (0.4, 0.15),
    "latency_p99_ms":          (310.0, 25.0),
    "cpu_utilization":         (38.0, 5.0),
    "memory_utilization":      (46.0, 3.0),
    "db_active_connections":   (34.0, 6.0),
    "db_query_time_p99_ms":    (28.0, 4.0),
    "upstream_5xx_rate":       (0.1, 0.08),
    "upstream_4xx_rate":       (0.2, 0.1),
    "cache_hit_ratio":         (0.94, 0.01),
    "origin_request_rate":     (49.0, 8.0),
    "queue_depth":             (1200.0, 300.0),
    "consumer_lag_seconds":    (8.0, 3.0),
    "disk_used_percent":       (54.0, 1.0),
    "tls_handshake_failures":  (0.0, 0.0),
    "restart_count":           (0.0, 0.0),
    "auth_failure_rate":       (0.3, 0.12),
    "clock_offset_ms":         (12.0, 6.0),
    "dns_resolution_failures": (0.0, 0.0),
    "quota_used_percent":      (61.0, 3.0),
}

DB_POOL_MAX = 100.0


# --------------------------------------------------------------------------
# Metric shaping. Each profile writes the signature that the matching runbook
# tells a responder to look for, and leaves every other series at baseline.
# The profiles are the ground truth that `evidence_required` incidents depend
# on, so they must agree with the runbook text exactly.
# --------------------------------------------------------------------------


def _ramp(i: int, onset: int, span: int = 8) -> float:
    """0.0 before onset, rising to 1.0 over `span` samples after it."""
    if i < onset:
        return 0.0
    return min(1.0, (i - onset + 1) / span)


def _step(i: int, onset: int) -> float:
    return 1.0 if i >= onset else 0.0


def profile_connection_pool_exhaustion(s, onset, rng):
    # The signature: connections pinned flat at the ceiling, query time normal.
    for i in range(POINTS):
        r = _ramp(i, onset, span=4)
        if r:
            s["db_active_connections"][i] = DB_POOL_MAX - rng.uniform(0, 0.6)
            s["latency_p99_ms"][i] *= 1 + 7.5 * r
            s["error_rate"][i] += 5.5 * r
            # query time deliberately stays flat - this is what separates it
            # from a slow query, and the only way to see it is the series.


def profile_db_slow_query_missing_index(s, onset, rng):
    # Query time climbs; connection count stays comfortably below the ceiling.
    for i in range(POINTS):
        r = _ramp(i, onset, span=12)
        if r:
            s["db_query_time_p99_ms"][i] *= 1 + 24.0 * r
            s["latency_p99_ms"][i] *= 1 + 6.0 * r
            s["error_rate"][i] += 1.8 * r
            s["db_active_connections"][i] = min(
                DB_POOL_MAX * 0.55, s["db_active_connections"][i] * (1 + 0.4 * r)
            )


def profile_upstream_dependency_outage(s, onset, rng):
    # Upstream 5xx leads this service's own latency by three samples.
    lead = 3
    for i in range(POINTS):
        ru = _ramp(i, onset - lead, span=4)
        rs = _ramp(i, onset, span=4)
        if ru:
            s["upstream_5xx_rate"][i] += 38.0 * ru
        if rs:
            s["error_rate"][i] += 22.0 * rs
            s["latency_p99_ms"][i] *= 1 + 4.0 * rs


def profile_rate_limit_throttling(s, onset, rng):
    # 4xx, not 5xx, and it tracks our own request rate crossing the quota.
    for i in range(POINTS):
        r = _ramp(i, onset, span=3)
        s["request_rate"][i] *= 1 + 0.55 * _ramp(i, onset - 2, span=5)
        if r:
            s["upstream_4xx_rate"][i] += 31.0 * r
            s["error_rate"][i] += 14.0 * r


def profile_expired_tls_certificate(s, onset, rng):
    # A step function, not a ramp. Resource metrics stay perfectly normal.
    for i in range(POINTS):
        if _step(i, onset):
            s["tls_handshake_failures"][i] = s["request_rate"][i]
            s["error_rate"][i] = 99.2 + rng.uniform(-0.3, 0.3)


def profile_memory_leak_oom(s, onset, rng):
    # Sawtooth: climb, OOM kill, reset, climb again.
    restarts = 0.0
    level = s["memory_utilization"][0]
    for i in range(POINTS):
        level += 1.15 if i < onset else 3.4
        if level >= 97.0:
            restarts += 1
            level = 44.0 + rng.uniform(0, 2)
            s["error_rate"][i] += 9.0
        s["memory_utilization"][i] = level
        s["restart_count"][i] = restarts


def profile_disk_full(s, onset, rng):
    for i in range(POINTS):
        s["disk_used_percent"][i] = min(
            100.0, 54.0 + (i / POINTS) * 44.0 + _ramp(i, onset, span=6) * 4.0
        )
        if s["disk_used_percent"][i] >= 99.9:
            s["error_rate"][i] += 16.0


def profile_bad_deploy_regression(s, onset, rng):
    # Sharp step in errors; nothing else moves. Pairs with a deploy event.
    for i in range(POINTS):
        if _step(i, onset):
            s["error_rate"][i] += 18.5
            s["latency_p99_ms"][i] *= 1.15


def profile_config_drift_feature_flag(s, onset, rng):
    # Identical shape to a deploy regression - the absence of a deploy event
    # in the ticket timeline is the only thing that tells them apart.
    for i in range(POINTS):
        if _step(i, onset):
            s["error_rate"][i] += 12.0
            s["latency_p99_ms"][i] *= 1.08


def profile_cache_stampede(s, onset, rng):
    # Periodic, recurring on the TTL boundary, recovering unaided.
    for i in range(POINTS):
        if i >= 6 and i % 6 == 0:
            s["cache_hit_ratio"][i] = 0.21 + rng.uniform(0, 0.04)
            s["origin_request_rate"][i] *= 9.5
            s["latency_p99_ms"][i] *= 3.2
            s["cpu_utilization"][i] = min(96.0, s["cpu_utilization"][i] * 2.3)


def profile_queue_consumer_lag(s, onset, rng):
    for i in range(POINTS):
        r = _ramp(i, onset, span=14)
        if r:
            s["queue_depth"][i] += 184000.0 * r
            s["consumer_lag_seconds"][i] += 2600.0 * r


def profile_clock_skew_auth_failure(s, onset, rng):
    # Partial failure plus a measurable offset. Partial is the signal: the
    # drift is per host, so only requests served by skewed hosts fail.
    for i in range(POINTS):
        r = _ramp(i, onset, span=5)
        if r:
            s["auth_failure_rate"][i] += 34.0 * r
            s["clock_offset_ms"][i] = 12.0 + 41000.0 * r
            s["error_rate"][i] += 11.0 * r


def profile_dns_misconfiguration(s, onset, rng):
    for i in range(POINTS):
        if _step(i, onset):
            s["dns_resolution_failures"][i] = s["request_rate"][i] * 0.82
            s["error_rate"][i] += 61.0
            s["auth_failure_rate"][i] += 48.0


def profile_resource_quota_exceeded(s, onset, rng):
    # Note the tail: request rate falls but the failure does not recover.
    # That is what separates a quota from a rate limit.
    for i in range(POINTS):
        if _step(i, onset):
            s["quota_used_percent"][i] = 100.0
            s["error_rate"][i] += 27.0
        if i >= onset + 8:
            s["request_rate"][i] *= 0.45   # load drops away
            s["error_rate"][i] = 27.4      # but errors do not


PROFILES = {
    "connection_pool_exhaustion": profile_connection_pool_exhaustion,
    "db_slow_query_missing_index": profile_db_slow_query_missing_index,
    "upstream_dependency_outage": profile_upstream_dependency_outage,
    "rate_limit_throttling": profile_rate_limit_throttling,
    "expired_tls_certificate": profile_expired_tls_certificate,
    "memory_leak_oom": profile_memory_leak_oom,
    "disk_full": profile_disk_full,
    "bad_deploy_regression": profile_bad_deploy_regression,
    "config_drift_feature_flag": profile_config_drift_feature_flag,
    "cache_stampede": profile_cache_stampede,
    "queue_consumer_lag": profile_queue_consumer_lag,
    "clock_skew_auth_failure": profile_clock_skew_auth_failure,
    "dns_misconfiguration": profile_dns_misconfiguration,
    "resource_quota_exceeded": profile_resource_quota_exceeded,
}


# --------------------------------------------------------------------------
# The 30 incidents.
#
# `description` is what every configuration sees. Read it alongside `tier`:
# for evidence_required rows the description must NOT contain the
# discriminating detail, or the tier is a lie and the eval is rigged in the
# agent's favour. tests/test_dataset.py enforces that property.
# --------------------------------------------------------------------------

INCIDENTS = [
    # ---- tier A: the ticket names the signature -------------------------
    dict(id="INC-001", service="auth-gateway", cause="expired_tls_certificate",
         tier="ticket_sufficient", severity="SEV1", runbook="RB-002",
         title="All API clients failing TLS handshake",
         desc="Every client integration started failing at once. Client-side logs "
              "show 'x509: certificate has expired or is not yet valid'. Our own "
              "CPU and database dashboards look completely normal."),
    dict(id="INC-002", service="media-transcode", cause="disk_full",
         tier="ticket_sufficient", severity="SEV2", runbook="RB-008",
         title="Transcode jobs failing to write output",
         desc="Transcode workers are erroring with 'ENOSPC: No space left on device' "
              "when writing finished renditions. Jobs that only read input succeed."),
    dict(id="INC-003", service="search-indexer", cause="memory_leak_oom",
         tier="ticket_sufficient", severity="SEV2", runbook="RB-004",
         title="Indexer pods restarting every 40 minutes",
         desc="Pods are being terminated with reason OOMKilled and exit code 137, "
              "roughly every 40 minutes. Memory climbs steadily from start to kill. "
              "Request volume has been flat all week."),
    dict(id="INC-004", service="checkout-api", cause="bad_deploy_regression",
         tier="ticket_sufficient", severity="SEV1", runbook="RB-005",
         title="Checkout error rate stepped up after release v2.8.1",
         desc="Error rate jumped from 0.4% to 19% in a single scrape interval, "
              "exactly matching the 14:05 deploy of v2.8.1. Traffic, memory and "
              "database metrics are unchanged."),
    dict(id="INC-005", service="payments-worker", cause="queue_consumer_lag",
         tier="ticket_sufficient", severity="SEV2", runbook="RB-009",
         title="Settlement backlog growing, oldest message 47 minutes",
         desc="Queue depth has grown to 1.2M messages and consumer lag is at 47 "
              "minutes and climbing. Messages are processing without errors, just "
              "far too slowly. Producer rate has not changed."),
    dict(id="INC-006", service="notify-dispatch", cause="rate_limit_throttling",
         tier="ticket_sufficient", severity="SEV3", runbook="RB-006",
         title="Delivery provider returning 429",
         desc="The delivery provider is returning HTTP 429 with a Retry-After header "
              "on about a third of sends. No 5xx responses at all. Started after we "
              "increased send concurrency this morning."),
    dict(id="INC-007", service="auth-gateway", cause="dns_misconfiguration",
         tier="ticket_sufficient", severity="SEV1", runbook="RB-010",
         title="Cannot resolve token service hostname",
         desc="Connections to the token service fail immediately with NXDOMAIN. "
              "Addressing the same host by IP works fine. Every caller is affected "
              "regardless of which host serves the request."),
    dict(id="INC-008", service="inventory-cache", cause="resource_quota_exceeded",
         tier="ticket_sufficient", severity="SEV2", runbook="RB-010",
         title="Provider returning RESOURCE_EXHAUSTED on all writes",
         desc="Writes are rejected with 'RESOURCE_EXHAUSTED: quota exceeded for "
              "provisioned capacity units'. The error names the limit directly. "
              "Reducing our write rate made no difference."),

    # ---- tier B: symptom is in the ticket, the mapping is in a runbook --
    dict(id="INC-009", service="checkout-api", cause="cache_stampede",
         tier="runbook_sufficient", severity="SEV2", runbook="RB-007",
         title="Backing service CPU spiking in a regular rhythm",
         desc="Every half hour or so the backing service CPU spikes hard for about a "
              "minute, latency triples, then it recovers by itself with nobody "
              "touching it. The cache tier reports healthy and its own latency is "
              "flat throughout."),
    dict(id="INC-010", service="session-store", cause="clock_skew_auth_failure",
         tier="runbook_sufficient", severity="SEV2", runbook="RB-010",
         title="Some hosts rejecting tokens that others accept",
         desc="A third of session validations are failing with 'token used before "
              "issued'. The same token succeeds when retried and lands on a "
              "different host. The issuing service reports no problems."),
    dict(id="INC-011", service="report-builder", cause="db_slow_query_missing_index",
         tier="runbook_sufficient", severity="SEV3", runbook="RB-001",
         title="Scheduled reports getting slower week over week",
         desc="Nightly report generation has gone from 4 minutes to 26 minutes over "
              "about two weeks. The slowdown tracks our row count rather than our "
              "traffic, which has been flat. No deploys in that period."),
    dict(id="INC-012", service="ledger-sync", cause="disk_full",
         tier="runbook_sufficient", severity="SEV2", runbook="RB-008",
         title="Sync writes failing but health check still green",
         desc="Ledger writes are failing while the service health endpoint keeps "
              "returning 200 and reads work normally. The health check only "
              "performs a read."),
    dict(id="INC-013", service="media-transcode", cause="memory_leak_oom",
         tier="runbook_sufficient", severity="SEV2", runbook="RB-004",
         title="Memory sawtooth on transcode workers",
         desc="Worker memory climbs steadily to the limit, the process dies, and it "
              "starts over. The pattern repeats on a fixed period. Job volume is "
              "flat and did not fall overnight when traffic did."),
    dict(id="INC-014", service="notify-dispatch", cause="config_drift_feature_flag",
         tier="runbook_sufficient", severity="SEV2", runbook="RB-005",
         title="Digest batching behaviour changed with no release",
         desc="Notification batching switched from hourly digests to per-event sends "
              "at 09:20, sharply, in one interval. There has been no deploy to this "
              "service in six days and no infrastructure change in the window."),
    dict(id="INC-015", service="payments-worker", cause="upstream_dependency_outage",
         tier="runbook_sufficient", severity="SEV1", runbook="RB-006",
         title="Settlement failures tracking the provider status page",
         desc="Our settlement error rate started climbing a few minutes after the "
              "provider began returning server errors. Our CPU, memory and "
              "connection counts are all at normal levels throughout."),
    dict(id="INC-016", service="auth-gateway", cause="expired_tls_certificate",
         tier="runbook_sufficient", severity="SEV1", runbook="RB-002",
         title="Total connection failure with no ramp",
         desc="Went from zero errors to essentially every request failing inside a "
              "single scrape interval. There was no gradual increase at all. Load "
              "has been steady and resource usage is unremarkable."),
    dict(id="INC-017", service="search-indexer", cause="queue_consumer_lag",
         tier="runbook_sufficient", severity="SEV2", runbook="RB-009",
         title="Index falling behind writes",
         desc="Documents are taking over an hour to become searchable. The backlog "
              "is growing steadily. Write volume into the system has not changed, "
              "but two of the six indexer replicas have been crash-looping."),
    dict(id="INC-018", service="inventory-cache", cause="cache_stampede",
         tier="runbook_sufficient", severity="SEV2", runbook="RB-007",
         title="Origin hammered at regular intervals",
         desc="The origin service sees a burst of identical concurrent requests at a "
              "fixed interval, always for the same small set of popular keys, and "
              "the burst subsides within two minutes without anyone doing anything."),
    dict(id="INC-019", service="checkout-api", cause="connection_pool_exhaustion",
         tier="runbook_sufficient", severity="SEV1", runbook="RB-003",
         title="Request timeouts waiting on database handles",
         desc="Application logs are full of 'TimeoutError: pool exhausted' and "
              "'could not obtain a connection within 30s'. The database itself "
              "reports normal query execution times."),

    # ---- tier C: the ticket is ambiguous; only the evidence decides -----
    # Rows 20-25 share one deliberately generic latency complaint. The three
    # causes behind them are separated by db_active_connections,
    # db_query_time_p99_ms and the lead/lag of upstream_5xx_rate. Nothing in
    # the ticket text distinguishes them.
    dict(id="INC-020", service="checkout-api", cause="connection_pool_exhaustion",
         tier="evidence_required", severity="SEV1", runbook="RB-003",
         distractors=["db_slow_query_missing_index", "upstream_dependency_outage"],
         title="Checkout p99 latency degraded, customers reporting timeouts",
         desc="Checkout p99 has gone from around 300ms to several seconds and the "
              "error rate is up. Support is getting timeout complaints. We have not "
              "deployed today. Nothing obvious in the application logs beyond "
              "timeouts."),
    dict(id="INC-021", service="report-builder", cause="db_slow_query_missing_index",
         tier="evidence_required", severity="SEV2", runbook="RB-001",
         distractors=["connection_pool_exhaustion", "upstream_dependency_outage"],
         title="Report API latency degraded through the afternoon",
         desc="Report endpoints are slow and some callers are timing out. It "
              "started early this afternoon. No release went out today, and the "
              "team is not sure whether this is us or something we depend on."),
    dict(id="INC-022", service="auth-gateway", cause="upstream_dependency_outage",
         tier="evidence_required", severity="SEV1", runbook="RB-006",
         distractors=["connection_pool_exhaustion", "db_slow_query_missing_index"],
         title="Gateway latency and errors both elevated",
         desc="The gateway is slow and throwing errors. It started about an hour "
              "ago. We have not changed anything on our side today and the on-call "
              "engineer cannot tell from the dashboard where the time is going."),
    dict(id="INC-023", service="ledger-sync", cause="connection_pool_exhaustion",
         tier="evidence_required", severity="SEV2", runbook="RB-003",
         distractors=["db_slow_query_missing_index", "queue_consumer_lag"],
         title="Ledger sync slow and timing out intermittently",
         desc="Sync operations that normally finish in under a second are taking "
              "many seconds or timing out outright. It is intermittent. No deploy, "
              "no config change that anyone is aware of."),
    dict(id="INC-024", service="search-indexer", cause="db_slow_query_missing_index",
         tier="evidence_required", severity="SEV2", runbook="RB-001",
         distractors=["connection_pool_exhaustion", "memory_leak_oom"],
         title="Indexer throughput down, latency up",
         desc="The indexer is noticeably slower than yesterday and p99 is well above "
              "normal. Error rate is slightly elevated. Nobody has shipped anything "
              "to this service this week."),
    dict(id="INC-025", service="checkout-api", cause="upstream_dependency_outage",
         tier="evidence_required", severity="SEV1", runbook="RB-006",
         distractors=["connection_pool_exhaustion", "bad_deploy_regression"],
         title="Checkout failing for a subset of customers",
         desc="Checkout is failing for a meaningful share of customers and latency "
              "is up. We cannot reproduce it reliably in staging. The last deploy "
              "was three days ago."),

    # Rows 26-27 separate an upstream outage from throttling. Both are
    # reported as "the upstream is failing us"; only the status-code split
    # and the correlation with our own request rate tells them apart.
    dict(id="INC-026", service="notify-dispatch", cause="rate_limit_throttling",
         tier="evidence_required", severity="SEV2", runbook="RB-006",
         distractors=["upstream_dependency_outage", "config_drift_feature_flag"],
         title="Delivery failures against the downstream provider",
         desc="A large share of sends are failing at the provider. The provider "
              "status page shows no incident. We are not sure whether to fail over "
              "to the secondary provider or wait it out."),
    dict(id="INC-027", service="payments-worker", cause="upstream_dependency_outage",
         tier="evidence_required", severity="SEV1", runbook="RB-006",
         distractors=["rate_limit_throttling", "connection_pool_exhaustion"],
         title="Payment authorisations failing at the processor",
         desc="Authorisation calls to the processor are failing in volume. The "
              "processor has not posted anything publicly yet and our account "
              "manager is unreachable, so we have no confirmation either way."),

    # Rows 28-30 separate three different auth-shaped failures.
    dict(id="INC-028", service="session-store", cause="clock_skew_auth_failure",
         tier="evidence_required", severity="SEV2", runbook="RB-010",
         distractors=["dns_misconfiguration", "expired_tls_certificate"],
         title="Intermittent authentication failures",
         desc="Users are being logged out unexpectedly and some API calls are "
              "rejected as unauthenticated. It is intermittent rather than total. "
              "The identity provider says everything is fine on their side."),
    dict(id="INC-029", service="auth-gateway", cause="dns_misconfiguration",
         tier="evidence_required", severity="SEV1", runbook="RB-010",
         distractors=["clock_skew_auth_failure", "upstream_dependency_outage"],
         title="Authentication broadly failing",
         desc="Authentication is failing across the board. The team is split on "
              "whether this is a network problem, a certificate problem or a "
              "problem at the identity provider."),
    dict(id="INC-030", service="inventory-cache", cause="resource_quota_exceeded",
         tier="evidence_required", severity="SEV2", runbook="RB-010",
         distractors=["rate_limit_throttling", "disk_full"],
         title="Writes rejected by the storage provider",
         desc="The storage provider is rejecting a large share of our writes. We "
              "reduced our write rate as a precaution but it has not obviously "
              "helped. No deploys today."),
]


# --------------------------------------------------------------------------
# A small history of already-resolved incidents. find_similar_incidents
# searches these, never the 30 under evaluation.
#
# Deliberately, the nearest neighbours by symptom text do NOT all share a root
# cause. If they did, similarity search alone would answer every question and
# the agent would look good for a reason that would not survive contact with a
# real incident database.
# --------------------------------------------------------------------------

PAST_INCIDENTS = [
    ("PAST-01", "checkout-api", "connection_pool_exhaustion",
     "Checkout p99 latency degraded to several seconds with timeout errors",
     "Pool was sized at 60 against measured concurrency of 95. Raised to 150."),
    ("PAST-02", "checkout-api", "db_slow_query_missing_index",
     "Checkout latency climbing through the afternoon, callers timing out",
     "A reporting join lost its index during a schema migration. Index rebuilt."),
    ("PAST-03", "auth-gateway", "upstream_dependency_outage",
     "Gateway slow and erroring, no change on our side",
     "Token provider had a regional outage. We failed open after 20 minutes."),
    ("PAST-04", "ledger-sync", "connection_pool_exhaustion",
     "Sync operations intermittently slow and timing out",
     "A batch reconciliation job held connections open for the whole run."),
    ("PAST-05", "report-builder", "db_slow_query_missing_index",
     "Report endpoints slow, no deploy, error rate slightly up",
     "Row growth pushed a sequential scan past the statement timeout."),
    ("PAST-06", "notify-dispatch", "rate_limit_throttling",
     "Large share of sends failing at the delivery provider",
     "We crossed the documented per-minute quota after a concurrency change."),
    ("PAST-07", "notify-dispatch", "upstream_dependency_outage",
     "Delivery failures at the provider with no status page update",
     "Provider was in a partial outage they acknowledged four hours later."),
    ("PAST-08", "session-store", "clock_skew_auth_failure",
     "Intermittent authentication failures, users logged out unexpectedly",
     "Time sync daemon had died on three of eleven hosts; offset reached 41s."),
    ("PAST-09", "auth-gateway", "dns_misconfiguration",
     "Authentication failing broadly, unclear whether network or provider",
     "A DNS record was edited during a migration and cached negatively."),
    ("PAST-10", "inventory-cache", "resource_quota_exceeded",
     "Storage provider rejecting a large share of writes",
     "Provisioned capacity units were fully consumed; quota raised."),
    ("PAST-11", "search-indexer", "memory_leak_oom",
     "Indexer throughput down and latency up over several hours",
     "A caching layer added in the previous release retained every document."),
    ("PAST-12", "payments-worker", "upstream_dependency_outage",
     "Authorisation calls failing in volume at the processor",
     "Processor incident; our retry storm extended it. We added a circuit breaker."),
]


def build_series(cause: str, rng: random.Random) -> dict[str, list[float]]:
    """Baseline noise for every series, then the cause-specific signature."""
    series: dict[str, list[float]] = {}
    for name, (base, jitter) in BASELINES.items():
        series[name] = [
            max(0.0, base + rng.gauss(0, jitter)) for _ in range(POINTS)
        ]
    PROFILES[cause](series, ONSET_INDEX, rng)
    return {k: [round(v, 3) for v in vals] for k, vals in series.items()}


def build_timeline(spec: dict, opened: datetime) -> list[dict]:
    """Ticket timeline. Deploy entries matter: they are the only thing that
    separates a deploy regression from feature-flag drift."""
    cause = spec["cause"]
    events = [
        {"at": _iso(opened - timedelta(minutes=4)),
         "source": "alerting",
         "text": f"Alert fired on {spec['service']}: threshold breach"},
        {"at": _iso(opened),
         "source": "on-call",
         "text": "Incident opened and acknowledged"},
    ]
    if cause == "bad_deploy_regression":
        events.insert(0, {
            "at": _iso(opened - timedelta(minutes=5)),
            "source": "deploy",
            "text": f"Deployed {spec['service']} v2.8.1 to production",
        })
    elif cause == "config_drift_feature_flag":
        events.insert(0, {
            "at": _iso(opened - timedelta(days=6)),
            "source": "deploy",
            "text": f"Deployed {spec['service']} v4.1.0 to production "
                    f"(most recent release)",
        })
    else:
        events.insert(0, {
            "at": _iso(opened - timedelta(days=rng_days(spec["id"]))),
            "source": "deploy",
            "text": f"Deployed {spec['service']} to production (most recent release)",
        })
    events.append({
        "at": _iso(opened + timedelta(minutes=6)),
        "source": "on-call",
        "text": "Paging secondary; no root cause identified yet",
    })
    return events


def rng_days(incident_id: str) -> int:
    """Stable 2-9 day offset so 'last deploy' is not suspiciously uniform."""
    return 2 + (sum(ord(c) for c in incident_id) % 8)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    rng = random.Random(SEED)
    base_day = datetime(2026, 3, 2, 0, 0, 0, tzinfo=timezone.utc)

    DATA.mkdir(exist_ok=True)
    (DATA / "metrics").mkdir(exist_ok=True)

    incidents = []
    for n, spec in enumerate(INCIDENTS):
        opened = base_day + timedelta(days=n // 3, hours=8 + (n % 3) * 4)
        window_start = opened - timedelta(minutes=INTERVAL_MINUTES * ONSET_INDEX)
        timestamps = [
            _iso(window_start + timedelta(minutes=INTERVAL_MINUTES * i))
            for i in range(POINTS)
        ]
        series = build_series(spec["cause"], rng)

        (DATA / "metrics" / f"{spec['id']}.json").write_text(
            json.dumps({
                "incident_id": spec["id"],
                "service": spec["service"],
                "window_start": timestamps[0],
                "window_end": timestamps[-1],
                "interval_minutes": INTERVAL_MINUTES,
                "timestamps": timestamps,
                "series": series,
            }, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        incidents.append({
            "id": spec["id"],
            "service": spec["service"],
            "severity": spec["severity"],
            "opened_at": _iso(opened),
            "title": spec["title"],
            "description": spec["desc"],
            "timeline": build_timeline(spec, opened),
            "metric_window": {
                "service": spec["service"],
                "start": timestamps[0],
                "end": timestamps[-1],
            },
            # --- ground truth: never exposed by the MCP server ---
            "ground_truth": {
                "root_cause_id": spec["cause"],
                "root_cause_statement": TAXONOMY[spec["cause"]],
                "tier": spec["tier"],
                "gold_runbook": spec["runbook"],
                "plausible_distractors": spec.get("distractors", []),
            },
        })

    # newline="\n" on every write: the default rewrites "\n" to the
    # platform separator, so a corpus generated on Windows would not be
    # byte-identical to one generated on Linux and the CI reproducibility
    # check would fail for a reason unrelated to the generator.
    (DATA / "incidents.json").write_text(
        json.dumps(incidents, indent=2) + "\n",
        encoding="utf-8", newline="\n")

    (DATA / "taxonomy.json").write_text(
        json.dumps(TAXONOMY, indent=2) + "\n",
        encoding="utf-8", newline="\n")

    (DATA / "past_incidents.json").write_text(
        json.dumps([
            {"id": i, "service": s, "root_cause_id": c,
             "summary": sm, "resolution": r}
            for (i, s, c, sm, r) in PAST_INCIDENTS
        ], indent=2) + "\n", encoding="utf-8", newline="\n")

    tiers: dict[str, int] = {}
    for inc in incidents:
        t = inc["ground_truth"]["tier"]
        tiers[t] = tiers.get(t, 0) + 1

    print(f"wrote {len(incidents)} incidents to {DATA / 'incidents.json'}")
    for t, c in sorted(tiers.items()):
        print(f"  {t:<20} {c}")
    print(f"wrote {len(PAST_INCIDENTS)} past incidents")
    print(f"wrote {len(TAXONOMY)} root causes to taxonomy.json")
    print(f"wrote {len(incidents)} metric windows to {DATA / 'metrics'}")


if __name__ == "__main__":
    main()
