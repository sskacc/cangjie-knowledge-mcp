"""Pure-stdlib BM25 (Okapi) implementation.

Deliberately dependency-free so the knowledge base can be built and searched
with only the Python standard library. Queries support:
  - exact token / phrase match
  - camelCase & snake_case splitting (Cangjie/Java identifiers)
  - field-weighted scoring (name > signature > module > tags > description)
"""

from __future__ import annotations

import math
import os
import pickle
import re
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")  # underscores split words too
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def tokenize(text: str) -> List[str]:
    """Tokenize, including camelCase/snake_case splitting and lowercasing."""
    text = _TOKEN_SPLIT.sub(" ", text)
    toks = []
    for word in text.split():
        for part in _CAMEL_SPLIT.split(word):
            if part:
                toks.append(part.lower())
    return toks


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.doc_count = 0
        self.avgdl = 0.0
        self.doc_lens: List[int] = []
        self.doc_freqs: List[Counter] = []
        self.idf: Dict[str, float] = {}
        self.terms_in_doc: List[set] = []

    def build(self, documents: List[str]) -> None:
        """documents: one plain-text string per record (pre-joined fields)."""
        self.doc_count = len(documents)
        total_len = 0
        df: Counter = Counter()
        self.doc_lens = []
        self.doc_freqs = []
        self.terms_in_doc = []
        for doc in documents:
            toks = tokenize(doc)
            self.doc_lens.append(len(toks))
            total_len += len(toks)
            freq = Counter(toks)
            self.doc_freqs.append(freq)
            self.terms_in_doc.append(set(freq))
            for t in set(toks):
                df[t] += 1
        self.avgdl = total_len / self.doc_count if self.doc_count else 0.0
        n = self.doc_count
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def score(self, query_tokens: List[str], doc_idx: int) -> float:
        if doc_idx >= self.doc_count:
            return 0.0
        doc_len = self.doc_lens[doc_idx]
        freq = self.doc_freqs[doc_idx]
        score = 0.0
        for t in query_tokens:
            idf = self.idf.get(t)
            if idf is None:
                continue
            f = freq.get(t, 0)
            if f == 0:
                continue
            denom = f + self.k1 * (1 - self.b + self.b * doc_len / self.avgdl)
            score += idf * (f * (self.k1 + 1)) / denom
        return score

    def search(self, query: str, top_k: int = 10, min_score: float = 0.0,
               doc_weights: Optional[List[float]] = None) -> List[Tuple[int, float]]:
        """Return [(doc_idx, score)] sorted by score desc.

        doc_weights: per-document multiplier (e.g. boost manual docs lower).
        """
        qtoks = tokenize(query)
        if not qtoks:
            return []
        scored: List[Tuple[int, float]] = []
        for i in range(self.doc_count):
            s = self.score(qtoks, i)
            if doc_weights:
                s *= doc_weights[i]
            if s >= min_score:
                scored.append((i, s))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    # ---- persistence ----------------------------------------------------
    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "k1": self.k1, "b": self.b, "doc_count": self.doc_count,
                "avgdl": self.avgdl, "doc_lens": self.doc_lens,
                "doc_freqs": self.doc_freqs, "idf": self.idf,
                "terms_in_doc": self.terms_in_doc,
            }, f)

    @staticmethod
    def load(path: str) -> "BM25Index":
        with open(path, "rb") as f:
            data = pickle.load(f)
        idx = BM25Index(k1=data["k1"], b=data["b"])
        idx.doc_count = data["doc_count"]
        idx.avgdl = data["avgdl"]
        idx.doc_lens = data["doc_lens"]
        idx.doc_freqs = data["doc_freqs"]
        idx.idf = data["idf"]
        idx.terms_in_doc = data["terms_in_doc"]
        return idx
