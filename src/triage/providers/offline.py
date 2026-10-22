"""A rule-based provider that uses no language model at all.

This started as a stub for testing the harness without spending anything, and
it earned a place in the results table instead. It is the control arm: the
cheapest possible triage system, BM25 over the taxonomy and the runbooks with
no reasoning on top.

It exists to answer a question the other three arms cannot: how much of the
accuracy is the language model actually contributing? If a keyword matcher
gets most of the ticket_sufficient incidents right - and it does - then the
headline accuracy of the LLM-alone arm is mostly measuring how obvious the
easy incidents are, not how good the model is.

It is reported as `rules`, never as a model, and it is never used to stand in
for one.
"""

from __future__ import annotations

import json
import time

from .. import retrieval, schema, store
from .base import ModelResult


class OfflineRulesProvider:
    name = "rules"
    model = "bm25-no-llm"

    def __init__(self, *_args, **_kwargs):
        self._cause_index = self._build_cause_index()

    @staticmethod
    def _build_cause_index() -> dict[str, set[str]]:
        """One bag of words per root cause, from its definition plus the text
        of every runbook section that claims to cover it."""
        index: dict[str, set[str]] = {}
        for cause, description in store.taxonomy().items():
            terms = set(retrieval.tokenize(f"{cause} {description}"))
            for rb in store.runbooks():
                if cause in rb.covers:
                    terms |= set(retrieval.tokenize(rb.text))
            index[cause] = terms
        return index

    def cause_terms(self, cause: str) -> set[str]:
        """The term bag for one cause. Used by the leakage check."""
        return self._cause_index.get(cause, set())

    def rank(self, text: str) -> list[tuple[float, str]]:
        """Every cause scored by Jaccard overlap, best first.

        Exposed because `triage analyze` needs the full ranking and the
        margin between the top two, not just the winner.
        """
        query = set(retrieval.tokenize(text))
        if not query:
            return [(0.0, cause) for cause in sorted(self._cause_index)]
        return sorted(
            (
                (len(query & terms) / (len(query | terms) or 1), cause)
                for cause, terms in self._cause_index.items()
            ),
            reverse=True,
        )

    def classify(self, text: str) -> tuple[str, float]:
        ranked = self.rank(text)
        best_score, best_cause = ranked[0]
        if best_score <= 0.0:
            return schema.UNKNOWN, 0.0
        runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
        # Margin over the runner-up, as a crude confidence.
        margin = (best_score - runner_up) / best_score if best_score else 0.0
        return best_cause, round(min(1.0, margin * 2), 3)

    def complete(self, system: str, prompt: str,
                 json_schema: dict | None = None) -> ModelResult:
        started = time.perf_counter()
        cause, confidence = self.classify(prompt)
        verdict = {
            "root_cause_id": cause,
            "reasoning": "Rule-based BM25 term overlap against the root-cause "
                         "taxonomy and the runbooks. No model, no reasoning.",
            "confidence": confidence,
            "evidence_used": ["term overlap only"],
        }
        return ModelResult(
            text=json.dumps(verdict),
            latency_s=round(time.perf_counter() - started, 4),
            cost_usd=0.0,
            num_turns=1,
            tool_calls=0,
        )

