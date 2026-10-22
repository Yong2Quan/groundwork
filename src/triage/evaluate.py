"""The eval harness: run every configuration over every incident and score it.

Scoring is exact match of `root_cause_id` against the known label. No LLM
judge: the answer space is a closed list of 14 causes, so a judge would add
cost, latency and a second source of error to a comparison that a string
equality already settles.

Two properties worth keeping:

* Runs are resumable. A sweep writes each cell as it completes, and `--resume`
  skips cells already present. A sweep that dies at incident 22 does not cost
  you the first 21.
* Failures are recorded, never retried silently. A provider error or an
  unparseable verdict is a real difference between architectures - the agent
  arm has more ways to fail than a single call does, and hiding that would
  flatter it.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import schema, store
from .configs import ARMS
from .providers import get_provider

RESULTS_DIR = Path(__file__).resolve().parents[2] / "results" / "runs"


@dataclass
class Cell:
    """One (configuration, incident) result."""

    arm: str
    incident_id: str
    tier: str
    service: str
    predicted: str
    truth: str
    correct: bool
    abstained: bool
    confidence: float
    reasoning: str
    evidence_used: list[str] = field(default_factory=list)
    tool_calls: int = 0
    tool_call_names: list[str] = field(default_factory=list)
    latency_s: float = 0.0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    num_turns: int = 1
    error: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class Sweep:
    provider: str
    model: str
    started_at: str
    cells: list[Cell] = field(default_factory=list)
    finished_at: str | None = None
    notes: dict = field(default_factory=dict)

    def key(self, arm: str, incident_id: str) -> bool:
        return any(c.arm == arm and c.incident_id == incident_id for c in self.cells)

    def to_json(self) -> str:
        payload = {
            "provider": self.provider,
            "model": self.model,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "notes": self.notes,
            "cells": [asdict(c) for c in self.cells],
        }
        return json.dumps(payload, indent=2)

    @classmethod
    def load(cls, path: Path) -> "Sweep":
        data = json.loads(path.read_text(encoding="utf-8"))
        sweep = cls(
            provider=data["provider"],
            model=data["model"],
            started_at=data["started_at"],
            finished_at=data.get("finished_at"),
            notes=data.get("notes", {}),
        )
        sweep.cells = [Cell(**c) for c in data["cells"]]
        return sweep


def score(arm: str, incident: dict, output) -> Cell:
    truth = store.ground_truth_for(incident["id"])
    predicted = output.verdict.root_cause_id
    detail = dict(output.result.raw or {})
    detail.pop("model_usage", None)
    return Cell(
        arm=arm,
        incident_id=incident["id"],
        tier=truth["tier"],
        service=incident["service"],
        predicted=predicted,
        truth=truth["root_cause_id"],
        correct=predicted == truth["root_cause_id"],
        abstained=predicted == schema.UNKNOWN,
        confidence=output.verdict.confidence,
        reasoning=output.verdict.reasoning,
        evidence_used=output.verdict.evidence_used,
        tool_calls=output.result.tool_calls,
        tool_call_names=output.result.tool_call_names,
        latency_s=output.result.latency_s,
        cost_usd=output.result.cost_usd,
        input_tokens=output.result.input_tokens,
        output_tokens=output.result.output_tokens,
        num_turns=output.result.num_turns,
        error=output.result.error,
        detail=detail,
    )


def run_sweep(provider_name: str, model: str | None = None,
              arms: list[str] | None = None,
              incident_ids: list[str] | None = None,
              effort: str | None = None,
              out_path: Path | None = None,
              resume: bool = False,
              verbose: bool = True) -> tuple[Sweep, Path]:
    arms = arms or list(ARMS)
    incident_ids = incident_ids or store.incident_ids()
    provider = get_provider(provider_name, model=model, effort=effort)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if out_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = RESULTS_DIR / f"{stamp}-{provider_name}-{provider.model}.json"

    if resume and out_path.exists():
        sweep = Sweep.load(out_path)
        if verbose:
            print(f"resuming {out_path.name} with {len(sweep.cells)} cells done")
    else:
        sweep = Sweep(
            provider=provider_name,
            model=provider.model,
            started_at=datetime.now(timezone.utc).isoformat(),
            notes={
                "cost_basis": (
                    "Claude Code reports list API prices; on a subscription "
                    "you are not billed this. Comparable across arms, not a "
                    "quote for running it."
                    if provider_name == "claude_cli" else
                    "First-party list price computed from reported tokens."
                ),
                "data": "Synthetic. See data/ and scripts/generate_data.py.",
            },
        )

    total = len(arms) * len(incident_ids)
    done = 0
    started = time.perf_counter()

    for arm in arms:
        for incident_id in incident_ids:
            done += 1
            if resume and sweep.key(arm, incident_id):
                continue
            incident = store.public_incident(incident_id)
            try:
                output = ARMS[arm](provider, incident)
                cell = score(arm, incident, output)
            except Exception as exc:  # noqa: BLE001 - a crash is a result
                truth = store.ground_truth_for(incident_id)
                cell = Cell(
                    arm=arm, incident_id=incident_id, tier=truth["tier"],
                    service=incident["service"], predicted=schema.UNKNOWN,
                    truth=truth["root_cause_id"], correct=False, abstained=True,
                    confidence=0.0,
                    reasoning=f"[harness] {type(exc).__name__}: {exc}",
                    error=f"{type(exc).__name__}: {exc}",
                )
            sweep.cells.append(cell)
            out_path.write_text(sweep.to_json(), encoding="utf-8")

            if verbose:
                mark = "ok " if cell.correct else ("ABS" if cell.abstained else "X  ")
                note = f" !{cell.error[:40]}" if cell.error else ""
                print(
                    f"[{done:>3}/{total}] {arm:<8} {incident_id} {cell.tier:<18} "
                    f"{mark} {cell.predicted:<28} "
                    f"{cell.tool_calls}t {cell.latency_s:>6.1f}s "
                    f"${cell.cost_usd:.4f}{note}"
                )

    sweep.finished_at = datetime.now(timezone.utc).isoformat()
    out_path.write_text(sweep.to_json(), encoding="utf-8")
    if verbose:
        print(f"\nsweep finished in {time.perf_counter() - started:.0f}s "
              f"-> {out_path}")
    return sweep, out_path
