"""Retriever quality and verdict parsing."""

from __future__ import annotations

import json

import pytest

from triage import analyze, retrieval, schema, store
from triage.configs import common


# -- retrieval -------------------------------------------------------------

def test_snake_case_identifiers_are_split_for_matching():
    tokens = retrieval.tokenize("db_active_connections pinned")
    assert "db_active_connections" in tokens
    assert "connections" in tokens and "active" in tokens


def test_stopwords_are_dropped():
    assert "the" not in retrieval.tokenize("the database is the problem")


def test_every_runbook_contributes_chunks():
    chunks = retrieval.chunk_runbooks()
    assert len({c.runbook_id for c in chunks}) == len(store.runbooks())
    assert all(c.text.strip() for c in chunks)


def test_retriever_is_not_a_strawman():
    """RAG has to be a competent baseline or the comparison is worthless.

    The bar is recall of the gold runbook on the tier that retrieval is
    actually supposed to solve.
    """
    retriever = retrieval.BM25Retriever()
    hits, total = 0, 0
    for incident in store.all_public_incidents():
        truth = store.ground_truth_for(incident["id"])
        if truth["tier"] != "runbook_sufficient":
            continue
        total += 1
        found = [r for r, _ in retriever.search_runbooks(
            common.search_query(incident), k=3)]
        hits += truth["gold_runbook"] in found
    assert hits / total >= 0.80, f"recall@3 on runbook_sufficient is {hits}/{total}"


def test_search_returns_scores_in_descending_order():
    hits = retrieval.BM25Retriever().search("pool exhausted timeouts", k=5)
    assert hits
    assert [s for _, s in hits] == sorted((s for _, s in hits), reverse=True)


def test_empty_query_returns_nothing_rather_than_everything():
    assert retrieval.BM25Retriever().search("", k=5) == []


# -- verdict schema --------------------------------------------------------

def test_schema_enumerates_the_closed_answer_set():
    enum = schema.json_schema()["properties"]["root_cause_id"]["enum"]
    assert set(enum) == set(store.taxonomy()) | {schema.UNKNOWN}


def test_parses_a_clean_verdict():
    verdict = schema.parse_verdict(json.dumps({
        "root_cause_id": "disk_full", "reasoning": "ENOSPC",
        "confidence": 0.9, "evidence_used": ["disk_used_percent"],
    }))
    assert verdict.root_cause_id == "disk_full"
    assert verdict.confidence == 0.9


def test_parses_a_fenced_verdict():
    verdict = schema.parse_verdict(
        'Here you go:\n```json\n{"root_cause_id": "disk_full", '
        '"reasoning": "x", "confidence": 0.5, "evidence_used": []}\n```'
    )
    assert verdict.root_cause_id == "disk_full"


@pytest.mark.parametrize("text", [
    "", "not json at all", "{}", '{"root_cause_id": "made_up_cause"}',
])
def test_unparseable_output_becomes_an_honest_unknown(text):
    """A parse failure must count as a failure, never a silent retry."""
    verdict = schema.parse_verdict(text)
    assert verdict.root_cause_id == schema.UNKNOWN
    assert verdict.confidence == 0.0
    assert "[harness]" in verdict.reasoning


def test_confidence_is_clamped():
    verdict = schema.parse_verdict(json.dumps({
        "root_cause_id": "disk_full", "reasoning": "",
        "confidence": 9.5, "evidence_used": [],
    }))
    assert verdict.confidence == 1.0


# -- prompt construction ---------------------------------------------------

def test_the_three_arms_share_everything_but_the_access_clause():
    """Otherwise the eval compares prompts, not architectures."""
    prompts = {arm: common.system_prompt(arm)
               for arm in ["baseline", "rag", "agent"]}
    for arm, text in prompts.items():
        remainder = text.replace(common.ACCESS_CLAUSES[arm], "")
        others = [
            p.replace(common.ACCESS_CLAUSES[a], "")
            for a, p in prompts.items() if a != arm
        ]
        assert all(remainder == o for o in others)


def test_timeline_is_withheld_from_the_non_agent_arms():
    """It is what get_ticket returns; handing it over free would remove one
    of the things the agent has to go and fetch."""
    incident = store.public_incident("INC-014")
    assert "Timeline" not in common.render_ticket(incident)
    assert "Timeline" in common.render_ticket(incident, include_timeline=True)


def test_the_rules_provider_is_reported_as_rules_not_as_a_model():
    from triage.providers.offline import OfflineRulesProvider
    assert OfflineRulesProvider().name == "rules"
