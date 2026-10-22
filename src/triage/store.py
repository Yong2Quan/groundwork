"""Read-only access to the synthetic corpus.

One rule governs this module: `ground_truth` never leaves it except through
`ground_truth_for()`, which only the scorer calls. Everything the three
configurations and the MCP server can see goes through `public_incident()`.
Leaking the label into a tool response would silently turn the eval into a
measurement of nothing.
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@dataclass(frozen=True)
class Runbook:
    id: str
    title: str
    covers: tuple[str, ...]
    services: tuple[str, ...]
    path: Path
    body: str

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.body}"


def _read_json(name: str):
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=1)
def taxonomy() -> dict[str, str]:
    """root_cause_id -> one-line definition. The closed answer set."""
    return _read_json("taxonomy.json")


@functools.lru_cache(maxsize=1)
def _incidents_raw() -> list[dict]:
    return _read_json("incidents.json")


@functools.lru_cache(maxsize=1)
def past_incidents() -> list[dict]:
    return _read_json("past_incidents.json")


def incident_ids() -> list[str]:
    return [i["id"] for i in _incidents_raw()]


def _find(incident_id: str) -> dict:
    for inc in _incidents_raw():
        if inc["id"] == incident_id:
            return inc
    raise KeyError(f"unknown incident: {incident_id}")


def public_incident(incident_id: str) -> dict:
    """The incident as a responder sees it. Ground truth stripped."""
    inc = dict(_find(incident_id))
    inc.pop("ground_truth", None)
    return inc


def ground_truth_for(incident_id: str) -> dict:
    """Scorer-only. Do not call this from a configuration or an MCP tool."""
    return _find(incident_id)["ground_truth"]


def all_public_incidents() -> list[dict]:
    return [public_incident(i) for i in incident_ids()]


# --------------------------------------------------------------------------
# Runbooks
# --------------------------------------------------------------------------

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _parse_list(value: str) -> tuple[str, ...]:
    value = value.strip().strip("[]")
    return tuple(v.strip() for v in value.split(",") if v.strip())


@functools.lru_cache(maxsize=1)
def runbooks() -> tuple[Runbook, ...]:
    out = []
    for path in sorted((DATA_DIR / "runbooks").glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        match = _FRONTMATTER.match(raw)
        if not match:
            raise ValueError(f"runbook {path.name} has no frontmatter")
        meta: dict[str, str] = {}
        for line in match.group(1).splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                meta[key.strip()] = val.strip()
        out.append(Runbook(
            id=meta["id"],
            title=meta["title"],
            covers=_parse_list(meta.get("covers", "")),
            services=_parse_list(meta.get("services", "")),
            path=path,
            body=raw[match.end():].strip(),
        ))
    return tuple(out)


def runbook(runbook_id: str) -> Runbook:
    for rb in runbooks():
        if rb.id == runbook_id:
            return rb
    raise KeyError(f"unknown runbook: {runbook_id}")


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

@functools.lru_cache(maxsize=64)
def metrics_for(incident_id: str) -> dict:
    return json.loads(
        (DATA_DIR / "metrics" / f"{incident_id}.json").read_text(encoding="utf-8")
    )


class AmbiguousWindow(LookupError):
    """Raised when a service/window pair does not select exactly one window.

    This must never fall back to "pick the first one". A service has several
    incidents on different days; returning the wrong one would hand the agent
    metrics from an unrelated outage and the eval would score reasoning that
    was done over the wrong evidence.
    """

    def __init__(self, message: str, available: list[dict]):
        super().__init__(message)
        self.available = available


def windows_for_service(service: str) -> list[dict]:
    return [
        {"start": inc["metric_window"]["start"], "end": inc["metric_window"]["end"]}
        for inc in _incidents_raw()
        if inc["service"] == service
    ]


def metrics_for_service_window(service: str, start: str | None,
                               end: str | None) -> dict:
    """Back the `get_metrics(service, window)` MCP tool.

    The agent asks by service and time window, the way it would against a real
    metrics backend - it does not get to ask by incident id, which would be a
    shortcut that does not exist in production. The window it needs is on the
    ticket, so `get_ticket` first is the intended path.
    """
    available = windows_for_service(service)
    if not available:
        raise AmbiguousWindow(f"No metrics retained for service {service!r}.", [])

    if not (start and end):
        raise AmbiguousWindow(
            f"A time window is required: {service!r} has {len(available)} "
            f"retained windows and no way to choose between them. The window "
            f"for this incident is on the ticket under 'metric_window'.",
            available,
        )

    matches = [
        inc["id"] for inc in _incidents_raw()
        if inc["service"] == service
        and not (end < inc["metric_window"]["start"]
                 or start > inc["metric_window"]["end"])
    ]
    if not matches:
        raise AmbiguousWindow(
            f"No metrics retained for {service!r} between {start} and {end}.",
            available,
        )
    if len(matches) > 1:
        raise AmbiguousWindow(
            f"That window overlaps {len(matches)} retained windows for "
            f"{service!r}. Narrow it.",
            available,
        )
    return metrics_for(matches[0])


def summarise_series(metrics: dict, points: int = 6) -> dict:
    """Condense a window into pre/post/peak per series.

    Thirty-six raw samples across twenty series is roughly 15k tokens of
    numbers. Most of it is baseline noise that costs money and crowds out
    reasoning, so the tool returns this summary alongside a trimmed raw view.
    """
    out = {}
    onset = len(metrics["timestamps"]) - (points * 2)
    for name, values in metrics["series"].items():
        pre = values[max(0, onset - points):onset]
        post = values[-points:]
        avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else 0.0
        out[name] = {
            "baseline_avg": avg(pre),
            "incident_avg": avg(post),
            "peak": round(max(values), 3),
            "min": round(min(values), 3),
        }
    return out
