"""Searcher: high-level search over the KnowledgeBase.

Combines:
  - exact name lookup (get_api_details)
  - BM25 similarity search over weighted record text
  - Java -> Cangjie term expansion (so a Java query like "Thread" also matches
    Cangjie docs mentioning 线程 / concurrent)
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

from cjkb.index.bm25 import BM25Index, tokenize
from cjkb.models import ApiRecord, ExampleRecord, JavaMapping, KnowledgeBase


class Searcher:
    def __init__(self, kb: KnowledgeBase, cfg: Dict, data_dir: str = "") -> None:
        self.kb = kb
        self.cfg = cfg
        self.data_dir = data_dir or cfg.get("output", {}).get("data_dir", "data")
        self.weights = cfg.get("index", {}).get("field_weights",
                                                {"name": 4.0, "module": 2.0,
                                                 "signature": 3.0, "description": 1.0, "tags": 1.5})
        self.top_k = cfg.get("index", {}).get("top_k", 10)
        self.min_score = cfg.get("index", {}).get("min_score", 0.01)
        self._api_index: Optional[BM25Index] = None
        self._example_index: Optional[BM25Index] = None
        self._api_docs: List[str] = []
        self._example_docs: List[str] = []
        self._name_index: Dict[str, List[int]] = {}
        self._java_map: Dict[str, List[JavaMapping]] = {}

    # ------------------------------------------------------------------ build
    def build(self) -> "Searcher":
        api_docs = []
        self._name_index = {}
        for i, rec in enumerate(self.kb.apis):
            api_docs.append(self._api_text(rec))
            key = rec.name.lower()
            self._name_index.setdefault(key, []).append(i)
            # also index without params e.g. "add(Int64)" -> "add"
            base = rec.name.split("(")[0].lower()
            if base != key:
                self._name_index.setdefault(base, []).append(i)
        self._api_index = BM25Index()
        self._api_index.build(api_docs)
        self._api_docs = api_docs

        ex_docs = []
        for rec in self.kb.examples:
            ex_docs.append(f"{rec.title} {rec.module} {rec.description} {rec.code[:2000]}")
        self._example_index = BM25Index()
        self._example_index.build(ex_docs)
        self._example_docs = ex_docs

        self._java_map = {}
        for m in self.kb.mappings:
            self._java_map.setdefault(m.java_symbol.lower(), []).append(m)
            self._java_map.setdefault(m.cangjie_symbol.lower(), []).append(m)
        return self

    def _api_text(self, rec: ApiRecord) -> str:
        parts = [
            rec.name * int(self.weights.get("name", 4)),
            rec.module * int(self.weights.get("module", 2)),
            rec.signature * int(self.weights.get("signature", 3)),
            rec.description,
            rec.returns,
            " ".join(rec.tags) * int(self.weights.get("tags", 1.5)),
            rec.parent,
        ]
        return " ".join(parts)

    def _expand_query(self, query: str) -> str:
        """Expand Java-ish tokens with Cangjie equivalents from the mapping."""
        expanded = query
        for tok in tokenize(query):
            matches = self._java_map.get(tok.lower())
            if matches:
                for m in matches[:3]:
                    expanded += f" {m.cangjie_symbol}"
        return expanded

    # ------------------------------------------------------------------ search
    def search_api(self, query: str, module: Optional[str] = None,
                   top_k: Optional[int] = None, min_score: Optional[float] = None) -> List[ApiRecord]:
        top_k = top_k or self.top_k
        min_score = self.min_score if min_score is None else min_score
        q = self._expand_query(query)
        results = self._api_index.search(q, top_k=top_k * 3, min_score=min_score)
        out: List[ApiRecord] = []
        for idx, _score in results:
            rec = self.kb.apis[idx]
            if module and not (rec.module == module or rec.module.startswith(module)):
                continue
            out.append(rec)
            if len(out) >= top_k:
                break
        return out

    def search_examples(self, query: str, module: Optional[str] = None,
                        top_k: Optional[int] = None) -> List[ExampleRecord]:
        top_k = top_k or self.top_k
        q = self._expand_query(query)
        results = self._example_index.search(q, top_k=top_k * 3)
        out = []
        for idx, _score in results:
            rec = self.kb.examples[idx]
            if module and not (rec.module == module or rec.module.startswith(module)):
                continue
            out.append(rec)
            if len(out) >= top_k:
                break
        return out

    def get_api_details(self, name: str, module: Optional[str] = None) -> List[ApiRecord]:
        """Exact (or prefix) lookup by API name."""
        key = name.lower()
        idxs = self._name_index.get(key, [])
        if not idxs:
            # try base name (strip parens) and fuzzy starts-with
            base = key.split("(")[0]
            idxs = self._name_index.get(base, [])
            if not idxs:
                idxs = [i for k, v in self._name_index.items()
                        for i in v if k.startswith(base)][:50]
        out = []
        seen = set()
        for i in idxs:
            rec = self.kb.apis[i]
            if module and not (rec.module == module or rec.module.startswith(module)):
                continue
            if rec.name.lower() != key and not rec.name.lower().startswith(key.split("(")[0]):
                continue
            uid = (rec.name, rec.signature, rec.module)
            if uid in seen:
                continue
            seen.add(uid)
            out.append(rec)
            if len(out) >= 30:
                break
        return out

    def get_class_members(self, class_name: str, module: Optional[str] = None) -> List[ApiRecord]:
        """All members (init/prop/func) belonging to a class/interface."""
        out = []
        for rec in self.kb.apis:
            if rec.parent == class_name or (rec.kind in ("class", "interface", "enum", "struct")
                                            and rec.name == class_name):
                if module and not (rec.module == module or rec.module.startswith(module)):
                    continue
                out.append(rec)
        return out

    def java_to_cangjie(self, java_symbol: str) -> List[JavaMapping]:
        key = java_symbol.lower()
        out = list(self._java_map.get(key, []))
        if not out:
            # fuzzy: contains
            out = [m for k, v in self._java_map.items() if key in k for m in v][:20]
        return out

    def find_examples(self, query: str, module: Optional[str] = None,
                      top_k: Optional[int] = None) -> List[ExampleRecord]:
        return self.search_examples(query, module=module, top_k=top_k)

    def list_modules(self) -> List[str]:
        return sorted(self.kb.modules.keys())

    # ------------------------------------------------------------------ persist
    def save(self, path: str = "") -> None:
        path = path or self.data_dir
        os.makedirs(path, exist_ok=True)
        self.kb.to_jsonl(path)
        self._api_index.save(os.path.join(path, "bm25_apis.pkl"))
        self._example_index.save(os.path.join(path, "bm25_examples.pkl"))

    @staticmethod
    def load(data_dir: str, cfg: Dict, auto_rebuild: bool = True) -> "Searcher":
        """Load a Searcher from JSONL sources + pkl index.

        If `auto_rebuild` and the BM25 pkl files are missing or older than the
        JSONL sources (e.g. right after a fresh clone where only JSONL was
        committed), rebuild the index in-memory and persist it.
        """
        kb = KnowledgeBase.from_jsonl(data_dir)
        s = Searcher(kb, cfg, data_dir=data_dir)
        api_pkl = os.path.join(data_dir, "bm25_apis.pkl")
        ex_pkl = os.path.join(data_dir, "bm25_examples.pkl")
        need_rebuild = (not os.path.exists(api_pkl) or not os.path.exists(ex_pkl))
        if not need_rebuild and auto_rebuild:
            # stale if any source jsonl is newer than the pkl
            for src in ("apis.jsonl", "examples.jsonl", "java_mappings.jsonl"):
                sp = os.path.join(data_dir, src)
                if os.path.exists(sp) and os.path.getmtime(sp) > os.path.getmtime(api_pkl):
                    need_rebuild = True
                    break
        if need_rebuild and auto_rebuild:
            print(f"[searcher] rebuilding BM25 index in {data_dir} "
                  f"(missing or stale pkl; sources are committed, index is derived)")
            return Searcher(kb, cfg, data_dir=data_dir).build_and_save()

        s._api_index = BM25Index.load(api_pkl)
        s._example_index = BM25Index.load(ex_pkl)
        s._name_index = {}
        for i, rec in enumerate(kb.apis):
            s._name_index.setdefault(rec.name.lower(), []).append(i)
            base = rec.name.split("(")[0].lower()
            if base != rec.name.lower():
                s._name_index.setdefault(base, []).append(i)
        s._java_map = {}
        for m in kb.mappings:
            s._java_map.setdefault(m.java_symbol.lower(), []).append(m)
            s._java_map.setdefault(m.cangjie_symbol.lower(), []).append(m)
        s._api_docs = [s._api_text(r) for r in kb.apis]
        s._example_docs = [f"{r.title} {r.module} {r.description} {r.code[:2000]}" for r in kb.examples]
        return s

    def build_and_save(self) -> "Searcher":
        """Build indexes from the current KB and persist, returning self."""
        self.build()
        self.save()
        return self
