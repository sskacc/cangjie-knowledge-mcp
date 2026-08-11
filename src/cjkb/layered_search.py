"""Layered search: progressive-disclosure retrieval over the knowledge base.

Implements the layered-retrieval idea:

    Level 1 (api)      : fine-grained NL  -> exact 1:1 API match (may fail)
    Level 2 (statement): coarser NL       -> one or several Cangjie APIs
    Level 3 (function) : coarsest NL      -> whole feature / module

Strategy: run retrieval at every level, then let the caller pick. A `best_level`
is computed by scoring how well each level's top hit matches (higher = the NL
description landed on a real doc, i.e. the Cangjie side has a counterpart).
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from cjkb.index.searcher import Searcher
from cjkb.nl_generator import generate_layered

LEVELS = ["api", "statement", "function"]


def _clean_nl_query(nl: Dict[str, str]) -> str:
    """Join en+zh descriptions into one search query, dedup tokens."""
    parts = [nl.get("en", ""), nl.get("zh", "")]
    return " ".join(p for p in parts if p)


def _search_level(searcher: Searcher, nl: Dict[str, str],
                  module: Optional[str], top_k: int) -> Dict:
    """Retrieve APIs + examples for one level's NL description."""
    query = _clean_nl_query(nl)
    apis = searcher.search_api(query, module=module, top_k=top_k)
    examples = searcher.find_examples(query, module=module, top_k=top_k)
    # top hit's score proxy: we re-score by query overlap on name/signature
    return {"query": query, "apis": apis, "examples": examples}


def _level_score(nl: Dict[str, str], apis: List) -> float:
    """Estimate how well this level landed on real Cangjie docs.

    Score = average token overlap between the NL query and the top APIs'
    names/signatures. 0.0 means the NL description found nothing relevant.
    """
    if not apis:
        return 0.0
    en = (nl.get("en") or "").lower()
    zh = nl.get("zh") or ""
    toks_en = set(re.findall(r"[a-z0-9]+", en))
    toks_zh = set(re.findall(r"[\u4e00-\u9fff]+", zh))
    total = 0.0
    for r in apis[:3]:
        doc = f"{r.name} {r.signature} {r.module}".lower()
        hit = len([t for t in toks_en if t in doc])
        zh_hit = len([t for t in toks_zh if t in (r.description or "")])
        total += (hit + zh_hit) / max(1, len(toks_en) + len(toks_zh))
    return total / len(apis[:3])


def layered_search(searcher: Searcher, java_code: str, cfg: Optional[Dict] = None,
                   module: Optional[str] = None, top_k: int = 5) -> Dict:
    """Run progressive-disclosure retrieval for Java code.

    Returns:
        {
          "java_code": ...,
          "levels": {
             "api":      {"query","apis","examples","score"},
             "statement": {...},
             "function": {...}
          },
          "best_level": "function",          # highest-scoring level
          "best_hit":   <top ApiRecord dict> # overall best API
        }
    """
    nls = generate_layered(java_code, cfg)
    levels: Dict[str, Dict] = {}
    for lvl in LEVELS:
        res = _search_level(searcher, nls[lvl], module, top_k)
        res["score"] = _level_score(nls[lvl], res["apis"])
        res["nl"] = nls[lvl]
        levels[lvl] = res

    # best level = highest score; tie-break finer granularity
    def _key(lvl: str) -> float:
        return (levels[lvl]["score"], -LEVELS.index(lvl))

    best_level = max(LEVELS, key=_key)
    best_apis = levels[best_level]["apis"]
    best_hit = best_apis[0] if best_apis else None
    return {
        "java_code": java_code,
        "levels": levels,
        "best_level": best_level,
        "best_hit": best_hit,
    }
