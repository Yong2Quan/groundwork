"""Checks that the corpus supports the claims the eval makes about it.

If any of these fail, the results table is measuring something other than
what the README says it measures.
"""

from __future__ import annotations

import pytest

from triage import analyze, store


def test_corpus_shape():
    assert len(store.incident_ids()) == 30
    assert len(store.runbooks()) == 10
    assert len(store.taxonomy()) == 14


def test_every_cause_is_exercised():
    """An unused cause is a distractor nobody ever has to rule in."""
    used = {
        store.ground_truth_for(i)["root_cause_id"] for i in store.incident_ids()
    }
    assert used == set(store.taxonomy())


def test_tier_distribution():
    tiers: dict[str, int] = {}
    for i in store.incident_ids():
        t = store.ground_truth_for(i)["tier"]
        tiers[t] = tiers.get(t, 0) + 1
    assert tiers == {
        "ticket_sufficient": 8,
        "runbook_sufficient": 11,
        "evidence_required": 11,
    }


def test_public_incident_never_carries_the_label():
    for incident_id in store.incident_ids():
        public = store.public_incident(incident_id)
        assert "ground_truth" not in public
        blob = str(public)
        assert store.ground_truth_for(incident_id)["root_cause_id"] not in blob


def test_gold_runbook_actually_covers_the_cause():
    for incident_id in store.incident_ids():
        truth = store.ground_truth_for(incident_id)
        runbook = store.runbook(truth["gold_runbook"])
        assert truth["root_cause_id"] in runbook.covers, (
            f"{incident_id}: {truth['gold_runbook']} does not document "
            f"{truth['root_cause_id']}"
        )


def test_evidence_required_incidents_have_distractors():
    for incident_id in store.incident_ids():
        truth = store.ground_truth_for(incident_id)
        if truth["tier"] != "evidence_required":
            continue
        distractors = truth["plausible_distractors"]
        assert len(distractors) >= 2, f"{incident_id} needs >=2 distractors"
        assert truth["root_cause_id"] not in distractors


def test_no_answer_leakage_in_evidence_required_tickets():
    """The headline validity check. See analyze.leakage_check."""
    clean, detail = analyze.leakage_check()
    assert clean, detail


# -- the discriminating signatures ----------------------------------------
# These are the numbers the agent arm has to read to beat RAG. If a profile
# stops matching its runbook, the incident becomes unanswerable by anyone and
# the evidence_required tier silently turns into noise.

def _summary(incident_id: str) -> dict:
    return store.summarise_series(store.metrics_for(incident_id))


@pytest.mark.parametrize("incident_id", ["INC-020", "INC-023"])
def test_pool_exhaustion_pins_connections_and_leaves_query_time_flat(incident_id):
    s = _summary(incident_id)
    assert s["db_active_connections"]["incident_avg"] > 95
    query = s["db_query_time_p99_ms"]
    assert query["incident_avg"] < query["baseline_avg"] * 1.5


@pytest.mark.parametrize("incident_id", ["INC-021", "INC-024"])
def test_slow_query_raises_query_time_without_pinning_connections(incident_id):
    s = _summary(incident_id)
    query = s["db_query_time_p99_ms"]
    assert query["incident_avg"] > query["baseline_avg"] * 5
    assert s["db_active_connections"]["incident_avg"] < 70


@pytest.mark.parametrize("incident_id", ["INC-022", "INC-025", "INC-027"])
def test_upstream_outage_shows_5xx_not_4xx(incident_id):
    s = _summary(incident_id)
    assert s["upstream_5xx_rate"]["incident_avg"] > 20
    assert s["upstream_4xx_rate"]["incident_avg"] < 5
    assert s["db_active_connections"]["incident_avg"] < 70


def test_throttling_shows_4xx_not_5xx_and_tracks_request_rate():
    s = _summary("INC-026")
    assert s["upstream_4xx_rate"]["incident_avg"] > 20
    assert s["upstream_5xx_rate"]["incident_avg"] < 5
    rate = s["request_rate"]
    assert rate["incident_avg"] > rate["baseline_avg"]


def test_clock_skew_shows_offset_and_no_dns_failures():
    s = _summary("INC-028")
    assert s["clock_offset_ms"]["incident_avg"] > 10000
    assert s["dns_resolution_failures"]["peak"] == 0


def test_dns_failure_shows_resolution_errors_and_normal_clock():
    s = _summary("INC-029")
    assert s["dns_resolution_failures"]["incident_avg"] > 100
    assert s["clock_offset_ms"]["incident_avg"] < 1000


def test_quota_does_not_recover_when_request_rate_falls():
    """The one thing that separates a quota from a rate limit."""
    s = _summary("INC-030")
    assert s["quota_used_percent"]["incident_avg"] >= 99.9
    rate = s["request_rate"]
    assert rate["incident_avg"] < rate["baseline_avg"]
    assert s["error_rate"]["incident_avg"] > 20
