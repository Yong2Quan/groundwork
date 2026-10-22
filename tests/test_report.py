"""Report rendering, including the cross-model comparison."""

from __future__ import annotations

from pathlib import Path

from triage import report
from triage.evaluate import Cell, Sweep


def cell(arm: str, incident: str, tier: str, *, correct: bool,
         abstained: bool = False, cost: float = 0.01) -> Cell:
    return Cell(
        arm=arm, incident_id=incident, tier=tier, service="checkout-api",
        predicted="unknown" if abstained else "disk_full",
        truth="disk_full", correct=correct, abstained=abstained,
        confidence=0.0 if abstained else 0.9, reasoning="", cost_usd=cost,
    )


def sweep(model: str, cells: list[Cell]) -> Sweep:
    s = Sweep(provider="claude_cli", model=model, started_at="now")
    s.cells = cells
    return s


def test_compare_puts_models_side_by_side():
    a = sweep("sonnet", [
        cell("agent", "INC-020", "evidence_required", correct=True),
        cell("rag", "INC-020", "evidence_required", correct=False, abstained=True),
    ])
    b = sweep("haiku", [
        cell("agent", "INC-020", "evidence_required", correct=False, abstained=True),
        cell("rag", "INC-020", "evidence_required", correct=False, abstained=True),
    ])
    text = report.compare([(Path("a.json"), a), (Path("b.json"), b)])
    assert "| sonnet | haiku |" in text
    # The agent row must show the two models disagreeing, which is the
    # entire reason for running a second model.
    assert "100% (1/1)" in text and "0% (0/1)" in text


def test_compare_handles_a_single_sweep():
    a = sweep("sonnet", [cell("agent", "INC-001", "ticket_sufficient", correct=True)])
    text = report.compare([(Path("a.json"), a)])
    assert "sonnet" in text
    assert "Agent over MCP tools" in text


def test_compare_skips_arms_absent_from_every_sweep():
    a = sweep("sonnet", [cell("agent", "INC-001", "ticket_sufficient", correct=True)])
    text = report.compare([(Path("a.json"), a)])
    assert "LLM alone" not in text


def test_compare_on_an_empty_list_does_not_crash():
    assert "No sweeps" in report.compare([])
