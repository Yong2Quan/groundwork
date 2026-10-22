"""Zero-cost analysis of the corpus and the retriever.

Runs no model and spends nothing. It answers the questions that would
otherwise be left as assertions in the write-up:

* Is the retriever any good? If RAG loses, was it retrieval or reasoning?
* Does the ticket text leak the answer for the tiers that are supposed to be
  hard? If it does, the tiers are fiction and so is every conclusion.

Run it before any sweep. If the leakage check fails, the eval is invalid and
no amount of model spend will fix it.
"""

from __future__ import annotations

from collections import Counter

from . import retrieval, store
from .configs import common


def dataset_summary() -> str:
    incidents = store.all_public_incidents()
    tiers = Counter(store.ground_truth_for(i["id"])["tier"] for i in incidents)
    causes = Counter(
        store.ground_truth_for(i["id"])["root_cause_id"] for i in incidents
    )
    services = Counter(i["service"] for i in incidents)

    lines = [
        f"{len(incidents)} incidents, {len(store.runbooks())} runbooks, "
        f"{len(store.taxonomy())} root causes, "
        f"{len(store.past_incidents())} resolved past incidents.",
        "",
        "| Answerability tier | Incidents | What it takes to solve |",
        "| --- | ---: | --- |",
        f"| `ticket_sufficient` | {tiers['ticket_sufficient']} | "
        f"The ticket names the signature. |",
        f"| `runbook_sufficient` | {tiers['runbook_sufficient']} | "
        f"The symptom is in the ticket; the mapping is in a runbook. |",
        f"| `evidence_required` | {tiers['evidence_required']} | "
        f"The ticket is ambiguous; only metrics or history decide. |",
        "",
        f"Root causes used: {len(causes)} of {len(store.taxonomy())}. "
        f"Most frequent: "
        + ", ".join(f"`{c}` ({n})" for c, n in causes.most_common(3)) + ".",
        f"Services covered: {len(services)}.",
    ]
    return "\n".join(lines)


def retrieval_quality(k: int = 3) -> str:
    """Recall@k of the gold runbook, per tier.

    This is the ceiling on what RAG can know. On `evidence_required` the
    ceiling is irrelevant - the gold runbook describes the right *procedure*
    but contains none of the numbers needed to run it.
    """
    retriever = retrieval.BM25Retriever()
    buckets: dict[str, list[bool]] = {}
    misses: list[tuple[str, str, str, list[str]]] = []

    for incident in store.all_public_incidents():
        truth = store.ground_truth_for(incident["id"])
        hits = [rid for rid, _ in retriever.search_runbooks(
            common.search_query(incident), k=k
        )]
        found = truth["gold_runbook"] in hits
        buckets.setdefault(truth["tier"], []).append(found)
        if not found:
            misses.append(
                (incident["id"], truth["tier"], truth["gold_runbook"], hits)
            )

    lines = [
        f"| Tier | Recall@{k} of the gold runbook |",
        "| --- | ---: |",
    ]
    for tier in ["ticket_sufficient", "runbook_sufficient", "evidence_required"]:
        found = buckets.get(tier, [])
        if found:
            lines.append(
                f"| `{tier}` | {sum(found)}/{len(found)} "
                f"({sum(found) / len(found):.0%}) |"
            )
    overall = [v for vals in buckets.values() for v in vals]
    lines.append(
        f"| **all** | {sum(overall)}/{len(overall)} "
        f"({sum(overall) / len(overall):.0%}) |"
    )
    if misses:
        lines += ["", "Retrieval misses:", ""]
        lines += [
            f"- `{i}` ({t}): wanted `{g}`, got {', '.join(f'`{h}`' for h in got)}"
            for i, t, g, got in misses
        ]
    return "\n".join(lines)


# A lexical lead smaller than this is a tie between candidates, not a
# ticket that gives its answer away.
LEAK_MARGIN = 0.15


def _coverage(query: set[str], terms: set[str]) -> float:
    """Share of the ticket's vocabulary a cause accounts for."""
    return len(query & terms) / (len(query) or 1)


def _jaccard(query: set[str], terms: set[str]) -> float:
    return len(query & terms) / (len(query | terms) or 1)


# Both measures are biased by how much runbook text documents a cause, in
# opposite directions: Jaccard divides by the union so a well-documented
# cause is penalised, coverage divides by the query so it is rewarded. Used
# alone, each one invents leaks that are really artifacts of corpus size -
# both did, on different incidents, while this was being built. Requiring
# them to agree cancels the bias, and is the test below.
_MEASURES = {"jaccard": _jaccard, "coverage": _coverage}


def leakage_check() -> tuple[bool, str]:
    """Does an `evidence_required` ticket give its own answer away?

    Two ways it could: the description contains the words of its own label,
    or term overlap picks the true cause out from the specific alternatives
    the incident was built to be confusable with.

    The comparison is against the designated distractors, not against all 14
    causes, because that is precisely what the tier claims: given these two
    or three candidates, the ticket does not tell you which. Whether some
    unrelated fourth cause scores higher is irrelevant to that claim.
    """
    from .providers.offline import OfflineRulesProvider

    rules = OfflineRulesProvider()
    problems: list[str] = []
    near_misses: list[str] = []
    total = 0

    for incident in store.all_public_incidents():
        truth = store.ground_truth_for(incident["id"])
        if truth["tier"] != "evidence_required":
            continue
        total += 1
        text = f"{incident['title']} {incident['description']}"

        words = set(text.lower().replace("-", " ").split())
        cause_words = set(truth["root_cause_id"].split("_"))
        giveaway = (cause_words & words) - {"db", "p99"}
        if len(giveaway) >= 2:
            problems.append(
                f"- `{incident['id']}` description contains "
                f"{sorted(giveaway)} from its own label"
            )

        if not truth["plausible_distractors"]:
            continue

        query = set(retrieval.tokenize(text))
        margins = {}
        for label, measure in _MEASURES.items():
            true_score = measure(query, rules.cause_terms(truth["root_cause_id"]))
            best_rival, rival = max(
                (measure(query, rules.cause_terms(d)), d)
                for d in truth["plausible_distractors"]
            )
            margins[label] = (
                (true_score - best_rival) / true_score if true_score else 0.0,
                rival,
            )

        if all(m >= LEAK_MARGIN for m, _ in margins.values()):
            detail = ", ".join(
                f"{label} +{m:.0%} over `{rival}`"
                for label, (m, rival) in margins.items()
            )
            problems.append(
                f"- `{incident['id']}`: `{truth['root_cause_id']}` beats its "
                f"distractors on both measures ({detail})"
            )
        else:
            detail = ", ".join(
                f"{label} {m:+.0%}" for label, (m, _) in margins.items()
            )
            near_misses.append(f"- `{incident['id']}`: {detail}")

    clean = not problems
    lines = [
        f"Checked {total} `evidence_required` incidents. A ticket leaks if "
        f"its true cause out-scores every cause it was built to be confused "
        f"with by {LEAK_MARGIN:.0%} or more on *both* term-overlap measures.",
        "",
    ]
    if clean:
        lines.append(
            "**No leakage.** No `evidence_required` ticket singles out its "
            "own answer lexically, which is what the tier claims."
        )
    else:
        lines.append(f"**{len(problems)} problem(s) found.**")
        lines += ["", *problems]
    if near_misses:
        lines += ["", "Chance hits, within the tie band:", "", *near_misses]
    return clean, "\n".join(lines)


def render() -> str:
    clean, leak = leakage_check()
    return "\n".join([
        "## Corpus",
        "",
        dataset_summary(),
        "",
        "## Retrieval quality",
        "",
        retrieval_quality(),
        "",
        "## Answer-leakage check",
        "",
        leak,
        "",
        f"_Status: {'PASS' if clean else 'FAIL'}_",
    ])
