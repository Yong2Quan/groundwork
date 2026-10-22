"""BM25 retrieval over runbook sections.

Deliberately dependency-free and ordinary. The point of the project is to
compare *architectures* - no retrieval, retrieval, and tool use - so the
retriever has to be a competent, unremarkable baseline. If RAG loses here, it
should be because retrieval cannot answer the question, not because the
retriever was a strawman. `triage analyze` reports recall@k so that claim is
checkable rather than asserted.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from . import store

K1 = 1.5
B = 0.75

_STOPWORDS = frozenset("""
a an and are as at be been but by for from has have how if in into is it its
of on or that the their then there these this to was were what when which who
will with you your
""".split())

_TOKEN = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, plus the parts of any snake_case identifier.

    Metric names carry most of the diagnostic signal in this corpus, and a
    query saying "connection pool" has to be able to reach a runbook line
    saying `db_active_connections`. Emitting both the whole identifier and its
    parts is what makes that match.
    """
    tokens: list[str] = []
    for raw in _TOKEN.findall(text.lower()):
        if raw in _STOPWORDS:
            continue
        tokens.append(raw)
        if "_" in raw:
            tokens.extend(p for p in raw.split("_") if p and p not in _STOPWORDS)
    return tokens


@dataclass(frozen=True)
class Chunk:
    id: str
    runbook_id: str
    runbook_title: str
    heading: str
    text: str

    def render(self) -> str:
        return f"[{self.id}] {self.runbook_title} - {self.heading}\n{self.text}"


def chunk_runbooks() -> list[Chunk]:
    """Split each runbook on its level-2 headings.

    Sections are the natural unit here: each one describes a signature or a
    remediation, which is the granularity a responder actually needs.
    """
    chunks: list[Chunk] = []
    for rb in store.runbooks():
        parts = re.split(r"^## ", rb.body, flags=re.MULTILINE)
        preamble = parts[0].strip()
        sections: list[tuple[str, str]] = []
        if preamble:
            # Strip the leading H1 so it does not become the heading text.
            body = re.sub(r"^#\s+.*\n?", "", preamble).strip()
            if body:
                sections.append(("Overview", body))
        for part in parts[1:]:
            heading, _, body = part.partition("\n")
            sections.append((heading.strip(), body.strip()))
        for n, (heading, body) in enumerate(sections):
            chunks.append(Chunk(
                id=f"{rb.id}#{n}",
                runbook_id=rb.id,
                runbook_title=rb.title,
                heading=heading,
                text=body,
            ))
    return chunks


class BM25Retriever:
    def __init__(self, chunks: list[Chunk] | None = None):
        self.chunks = chunks if chunks is not None else chunk_runbooks()
        self._docs = [tokenize(c.render()) for c in self.chunks]
        self._lengths = [len(d) for d in self._docs]
        self._avg_len = (sum(self._lengths) / len(self._lengths)) if self._docs else 0.0
        self._freqs: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for doc in self._docs:
            counts: dict[str, int] = {}
            for term in doc:
                counts[term] = counts.get(term, 0) + 1
            self._freqs.append(counts)
            for term in counts:
                df[term] = df.get(term, 0) + 1
        n = len(self._docs)
        self._idf = {
            term: math.log(1 + (n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    def search(self, query: str, k: int = 4) -> list[tuple[Chunk, float]]:
        terms = tokenize(query)
        scored: list[tuple[Chunk, float]] = []
        for i, counts in enumerate(self._freqs):
            score = 0.0
            length = self._lengths[i]
            for term in terms:
                tf = counts.get(term)
                if not tf:
                    continue
                denom = tf + K1 * (1 - B + B * length / (self._avg_len or 1))
                score += self._idf.get(term, 0.0) * (tf * (K1 + 1)) / denom
            if score > 0:
                scored.append((self.chunks[i], score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:k]

    def search_runbooks(self, query: str, k: int = 3) -> list[tuple[str, float]]:
        """Collapse chunk hits to runbook ids, keeping each one's best chunk."""
        best: dict[str, float] = {}
        for chunk, score in self.search(query, k=k * 4):
            if score > best.get(chunk.runbook_id, 0.0):
                best[chunk.runbook_id] = score
        ranked = sorted(best.items(), key=lambda pair: pair[1], reverse=True)
        return ranked[:k]
