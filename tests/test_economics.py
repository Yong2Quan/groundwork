"""Cost model arithmetic, on a hand-built sweep with known answers."""

from __future__ import annotations

import pytest

from triage import economics
from triage.economics import Assumptions, build_hybrid, summarise_arm
from triage.evaluate import Cell, Sweep


def cell(arm: str, incident: str, *, correct: bool, abstained: bool,
         cost: float = 0.01) -> Cell:
    return Cell(
        arm=arm, incident_id=incident, tier="evidence_required",
        service="checkout-api",
        predicted="unknown" if abstained else ("disk_full" if correct else "dns_misconfiguration"),
        truth="disk_full", correct=correct, abstained=abstained,
        confidence=0.0 if abstained else 0.8, reasoning="",
        cost_usd=cost,
    )


def make_sweep(cells: list[Cell]) -> Sweep:
    s = Sweep(provider="test", model="test", started_at="now")
    s.cells = cells
    return s


A = Assumptions(escalation_minutes=20, misdirection_minutes=45, hourly_rate=75)


def test_assumptions_convert_minutes_to_money():
    assert A.escalation_cost == pytest.approx(25.0)
    assert A.misdirection_cost == pytest.approx(56.25)


def test_outcomes_are_partitioned_not_double_counted():
    cells = [
        cell("rag", "A", correct=True, abstained=False),
        cell("rag", "B", correct=False, abstained=True),
        cell("rag", "C", correct=False, abstained=False),
    ]
    e = summarise_arm(cells, make_sweep(cells))
    assert (e.resolved, e.escalated, e.misdirected) == (1, 1, 1)
    assert e.resolved + e.escalated + e.misdirected == e.n


def test_total_cost_is_model_plus_human():
    cells = [
        cell("rag", "A", correct=True, abstained=False, cost=0.02),
        cell("rag", "B", correct=False, abstained=True, cost=0.02),
        cell("rag", "C", correct=False, abstained=False, cost=0.02),
    ]
    e = summarise_arm(cells, make_sweep(cells))
    assert e.total_cost(A) == pytest.approx(0.06 + 25.0 + 56.25)


def test_hybrid_uses_the_agent_result_for_the_incident_handed_over():
    """Not an average - the specific incident's actual agent outcome."""
    cells = [
        cell("rag", "A", correct=True, abstained=False, cost=0.01),
        cell("rag", "B", correct=False, abstained=True, cost=0.01),
        # The agent solved B but would have been wrong on A. Routing never
        # sends A, so A's agent result must not affect the total.
        cell("agent", "A", correct=False, abstained=False, cost=0.05),
        cell("agent", "B", correct=True, abstained=False, cost=0.05),
    ]
    h = build_hybrid(make_sweep(cells), "rag")
    assert h.handed_off == 1
    assert h.resolved == 2          # A from RAG, B from the agent
    assert h.misdirected == 0
    assert h.model_cost == pytest.approx(0.02 + 0.05)


def test_hybrid_inherits_a_misdirection_from_the_cheap_arm():
    """Routing on abstention is only safe if the cheap arm is not
    confidently wrong. When it is, the cost lands on the hybrid."""
    cells = [
        cell("rag", "A", correct=False, abstained=False, cost=0.01),
        cell("agent", "A", correct=True, abstained=False, cost=0.05),
    ]
    h = build_hybrid(make_sweep(cells), "rag")
    assert h.handed_off == 0
    assert h.misdirected == 1
    assert h.model_cost == pytest.approx(0.01)


def test_hybrid_counts_an_agent_abstention_as_an_escalation():
    cells = [
        cell("rag", "A", correct=False, abstained=True, cost=0.01),
        cell("agent", "A", correct=False, abstained=True, cost=0.05),
    ]
    h = build_hybrid(make_sweep(cells), "rag")
    assert (h.resolved, h.escalated, h.misdirected) == (0, 1, 0)


def test_breakeven_is_where_the_two_options_cost_the_same():
    cheap = economics.ArmEconomics("rag", "RAG", n=10, resolved=6,
                                   escalated=4, misdirected=0, model_cost=0.10)
    pricey = economics.ArmEconomics("agent", "Agent", n=10, resolved=10,
                                    escalated=0, misdirected=0, model_cost=0.60)
    be = economics.escalation_breakeven(cheap, pricey, A)
    assert be == pytest.approx(0.125)
    at = Assumptions(be / A.hourly_rate * 60, A.misdirection_minutes, A.hourly_rate)
    assert cheap.total_cost(at) == pytest.approx(pricey.total_cost(at))


def test_breakeven_is_undefined_when_escalations_match():
    same = dict(n=10, resolved=10, escalated=0, misdirected=0)
    a = economics.ArmEconomics("rag", "RAG", model_cost=0.10, **same)
    b = economics.ArmEconomics("agent", "Agent", model_cost=0.60, **same)
    assert economics.escalation_breakeven(a, b, A) is None


def test_control_arm_is_not_labelled_as_a_model():
    """The rules control must never print as "LLM alone" - that reports a
    keyword matcher's numbers as a model result."""
    cells = [cell("baseline", "A", correct=True, abstained=False)]
    sweep = Sweep(provider="offline", model="bm25-no-llm", started_at="now")
    sweep.cells = cells
    assert summarise_arm(cells, sweep).label == "Rules only (no LLM)"


def test_render_runs_on_the_real_sweep():
    from pathlib import Path
    run = Path(__file__).resolve().parents[1] / "results/runs/sweep-sonnet.json"
    if not run.exists():
        pytest.skip("no sweep on disk")
    text = economics.render(Sweep.load(run), A)
    assert "Cost per 100 incidents" in text
    assert "Routing on abstention" in text


def test_misdirection_sensitivity_appears_only_when_it_matters():
    """Varying only escalation cost would hide a ranking that turns on the
    misdirection price - which is exactly what the rules control does."""
    clean = make_sweep([cell("rag", "A", correct=True, abstained=False)])
    assert "misdirection costs" not in economics.render(clean, A)

    wrong = make_sweep([cell("rag", "A", correct=False, abstained=False)])
    assert "misdirection costs" in economics.render(wrong, A)


def test_a_misdirection_costs_strictly_more_than_an_escalation_by_default():
    """The defaults have to respect the argument the README makes: a
    misdirection is an escalation *plus* the wasted work before it, so it
    is strictly the larger of the two, never merely equal."""
    assert (economics.DEFAULT_MISDIRECTION_MINUTES
            > economics.DEFAULT_ESCALATION_MINUTES)
