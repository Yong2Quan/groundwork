"""Turn a sweep into the tables that go in the README."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from pathlib import Path

from .configs import ARM_LABELS
from .evaluate import Cell, Sweep

TIERS = ["ticket_sufficient", "runbook_sufficient", "evidence_required"]
TIER_LABELS = {
    "ticket_sufficient": "Ticket is enough",
    "runbook_sufficient": "Needs a runbook",
    "evidence_required": "Needs evidence",
}


def arm_label(sweep: Sweep, arm: str) -> str:
    """Name the row for what actually ran.

    The `offline` provider is a keyword matcher, not a language model.
    Printing "LLM alone" over its numbers would misreport the control arm as
    a model result, which is the one thing the control exists to prevent.
    """
    base = ARM_LABELS.get(arm, arm)
    if sweep.provider == "offline":
        return "Rules only (no LLM)"
    return base


def _pct(numerator: int, denominator: int) -> str:
    if not denominator:
        return "-"
    return f"{numerator / denominator:.0%}"


def _by_arm(cells: list[Cell]) -> dict[str, list[Cell]]:
    out: dict[str, list[Cell]] = defaultdict(list)
    for cell in cells:
        out[cell.arm].append(cell)
    return out


def headline_table(sweep: Sweep) -> str:
    rows = [
        "| Configuration | Accuracy | Abstained | Tool calls / incident "
        "| Median latency | p95 latency | Cost / incident | Errors |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for arm, cells in _by_arm(sweep.cells).items():
        n = len(cells)
        correct = sum(c.correct for c in cells)
        latencies = sorted(c.latency_s for c in cells)
        p95 = latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))]
        rows.append(
            f"| {arm_label(sweep, arm)} "
            f"| **{_pct(correct, n)}** ({correct}/{n}) "
            f"| {_pct(sum(c.abstained for c in cells), n)} "
            f"| {statistics.mean(c.tool_calls for c in cells):.1f} "
            f"| {statistics.median(latencies):.1f}s "
            f"| {p95:.1f}s "
            f"| ${statistics.mean(c.cost_usd for c in cells):.4f} "
            f"| {sum(1 for c in cells if c.error)} |"
        )
    return "\n".join(rows)


def tier_table(sweep: Sweep) -> str:
    by_arm = _by_arm(sweep.cells)
    header = "| Configuration | " + " | ".join(
        f"{TIER_LABELS[t]}" for t in TIERS
    ) + " |"
    rows = [header, "| --- | ---: | ---: | ---: |"]
    for arm, cells in by_arm.items():
        buckets: dict[str, list[Cell]] = defaultdict(list)
        for cell in cells:
            buckets[cell.tier].append(cell)
        cols = []
        for tier in TIERS:
            group = buckets.get(tier, [])
            correct = sum(c.correct for c in group)
            cols.append(f"{_pct(correct, len(group))} ({correct}/{len(group)})")
        rows.append(f"| {arm_label(sweep, arm)} | " + " | ".join(cols) + " |")
    return "\n".join(rows)


def confusion_table(sweep: Sweep, limit: int = 6) -> str:
    rows = [
        "| Configuration | Predicted instead of truth | Count |",
        "| --- | --- | ---: |",
    ]
    for arm, cells in _by_arm(sweep.cells).items():
        wrong = Counter(
            f"`{c.predicted}` instead of `{c.truth}`"
            for c in cells if not c.correct and not c.abstained
        )
        if not wrong:
            rows.append(f"| {arm_label(sweep, arm)} | (no confident errors) | 0 |")
            continue
        for label, count in wrong.most_common(limit):
            rows.append(f"| {arm_label(sweep, arm)} | {label} | {count} |")
    return "\n".join(rows)


def tool_usage_table(sweep: Sweep) -> str:
    agent_cells = [c for c in sweep.cells if c.arm == "agent"]
    if not agent_cells:
        return "_No agent cells in this sweep._"
    counts = Counter(
        name for cell in agent_cells for name in cell.tool_call_names
    )
    total = sum(counts.values()) or 1
    rows = ["| Tool | Calls | Share |", "| --- | ---: | ---: |"]
    for name, count in counts.most_common():
        rows.append(f"| `{name}` | {count} | {count / total:.0%} |")
    return "\n".join(rows)


def calibration_table(sweep: Sweep) -> str:
    """Is a confident answer more likely to be right?

    A configuration whose confidence does not separate its right answers from
    its wrong ones cannot be used to route incidents to a human, which is the
    main thing you would actually want from it.
    """
    rows = [
        "| Configuration | Mean confidence when right | when wrong | Separation |",
        "| --- | ---: | ---: | ---: |",
    ]
    for arm, cells in _by_arm(sweep.cells).items():
        right = [c.confidence for c in cells if c.correct]
        wrong = [c.confidence for c in cells if not c.correct and not c.abstained]
        if not right or not wrong:
            rows.append(f"| {arm_label(sweep, arm)} | - | - | - |")
            continue
        mr, mw = statistics.mean(right), statistics.mean(wrong)
        rows.append(
            f"| {arm_label(sweep, arm)} | {mr:.2f} | {mw:.2f} | {mr - mw:+.2f} |"
        )
    return "\n".join(rows)


def failures(sweep: Sweep, arm: str | None = None) -> list[Cell]:
    return [
        c for c in sweep.cells
        if not c.correct and (arm is None or c.arm == arm)
    ]


def render(sweep: Sweep, source: Path | None = None) -> str:
    totals = {
        "cells": len(sweep.cells),
        "cost": sum(c.cost_usd for c in sweep.cells),
        "wall": sum(c.latency_s for c in sweep.cells),
    }
    parts = [
        f"Provider `{sweep.provider}`, model `{sweep.model}`, "
        f"{totals['cells']} runs, "
        f"{totals['cost']:.2f} USD list-equivalent, "
        f"{totals['wall'] / 60:.0f} minutes of model time.",
    ]
    if source:
        parts.append(f"Source: `{source.as_posix()}`")
    parts += [
        "",
        "### Headline",
        "",
        headline_table(sweep),
        "",
        "### Accuracy by answerability tier",
        "",
        tier_table(sweep),
        "",
        "### Where each configuration went wrong",
        "",
        confusion_table(sweep),
        "",
        "### Confidence calibration",
        "",
        calibration_table(sweep),
        "",
        "### Agent tool usage",
        "",
        tool_usage_table(sweep),
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Cross-model comparison
# --------------------------------------------------------------------------

def compare(sweeps: list[tuple[Path, Sweep]]) -> str:
    """Put two or more sweeps side by side with the model as an axis.

    One model cannot tell you whether "the agent architecture wins" or
    "this particular model wins". A second model is the cheapest way to
    separate those two claims, and the tier breakdown is where the
    difference shows up if there is one.
    """
    if not sweeps:
        return "_No sweeps to compare._"

    order = ["baseline", "rag", "agent"]
    lines = [
        "### Accuracy by configuration and model",
        "",
        "| Configuration | " + " | ".join(s.model for _, s in sweeps) + " |",
        "| --- | " + " | ".join("---:" for _ in sweeps) + " |",
    ]
    for arm in order:
        cells = []
        for _, sweep in sweeps:
            group = [c for c in sweep.cells if c.arm == arm]
            if not group:
                cells.append("-")
                continue
            correct = sum(c.correct for c in group)
            cells.append(f"{_pct(correct, len(group))} ({correct}/{len(group)})")
        if any(c != "-" for c in cells):
            label = ARM_LABELS.get(arm, arm)
            lines.append(f"| {label} | " + " | ".join(cells) + " |")

    lines += ["", "### The tier that decides it: `evidence_required`", ""]
    lines += [
        "| Configuration | " + " | ".join(s.model for _, s in sweeps) + " |",
        "| --- | " + " | ".join("---:" for _ in sweeps) + " |",
    ]
    for arm in order:
        cells = []
        for _, sweep in sweeps:
            group = [c for c in sweep.cells
                     if c.arm == arm and c.tier == "evidence_required"]
            if not group:
                cells.append("-")
                continue
            correct = sum(c.correct for c in group)
            cells.append(f"{_pct(correct, len(group))} ({correct}/{len(group)})")
        if any(c != "-" for c in cells):
            lines.append(f"| {ARM_LABELS.get(arm, arm)} | " + " | ".join(cells) + " |")

    lines += ["", "### Cost and abstention", ""]
    lines += [
        "| Model | Configuration | Cost / incident | Abstained | Confidently wrong |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for _, sweep in sweeps:
        for arm in order:
            group = [c for c in sweep.cells if c.arm == arm]
            if not group:
                continue
            wrong = sum(1 for c in group if not c.correct and not c.abstained)
            lines.append(
                f"| {sweep.model} | {ARM_LABELS.get(arm, arm)} "
                f"| ${statistics.mean(c.cost_usd for c in group):.4f} "
                f"| {_pct(sum(c.abstained for c in group), len(group))} "
                f"| {wrong} |"
            )
    return "\n".join(lines)
