"""Per-submission retrieval. Each criterion pulls only the chunks relevant to it,
so no prompt ever needs the whole project.

PoC: in-memory BM25 (lexical). Production: hybrid = BM25 (Postgres full-text) +
embeddings in pgvector, filtered by submission_id, fused with reciprocal rank
fusion. Retrieval is always scoped to one submission, so the corpus per query is
small (hundreds to a few thousand chunks) and exact search is fast.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from app.ingest.chunker import Chunk

TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")
STOP = {"the", "and", "for", "with", "that", "this", "are", "from", "is", "of", "to", "in", "a", "an", "be", "on"}


def _tokens(text: str) -> list[str]:
    out = []
    for t in TOKEN_RE.findall(text):
        # split camelCase / snake_case so "getUserById" matches "user"
        for part in re.split(r"_|(?<=[a-z])(?=[A-Z])", t):
            p = part.lower()
            if len(p) > 2 and p not in STOP:
                out.append(p)
    return out


class BM25Index:
    def __init__(self, chunks: list[tuple[Chunk, str]], k1: float = 1.5, b: float = 0.75):
        """chunks: (chunk, category) pairs."""
        self.items = chunks
        self.k1, self.b = k1, b
        self.docs = [Counter(_tokens(c.path + " " + c.text)) for c, _ in chunks]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 1
        df: Counter = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 8, categories: set[str] | None = None) -> list[Chunk]:
        q = _tokens(query)
        scored = []
        for i, (doc, ln) in enumerate(zip(self.docs, self.lens, strict=True)):
            chunk, cat = self.items[i]
            if categories and cat not in categories:
                continue
            s = 0.0
            for t in q:
                f = doc.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / self.avg))
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        return [self.items[i][0] for _, i in scored[:k]]


# rubric evidence source -> file categories it may draw from
SOURCE_CATEGORIES = {
    "code": {"code", "notebook"},
    "tests": {"test"},
    "docs": {"docs", "api_spec"},
    "architecture": {"docs", "config", "schema", "api_spec", "code"},
    "schema": {"schema"},
    "config": {"config"},
}


def categories_for(sources: list[str]) -> set[str] | None:
    cats: set[str] = set()
    for s in sources:
        cats |= SOURCE_CATEGORIES.get(s, set())
    return cats or None
