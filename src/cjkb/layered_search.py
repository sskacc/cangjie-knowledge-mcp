"""Layered search: progressive-disclosure retrieval over the knowledge base.

Implements the layered-retrieval idea:

    Level 1 (api)      : fine-grained NL  -> exact 1:1 API match (may fail)
    Level 2 (statement): coarser NL       -> one or several Cangjie APIs
    Level 3 (function) : coarsest NL      -> whole feature / module

Two-stage pipeline (type locking -> method matching):

    Stage 1 (type lock): extract Java types from the code, map them to
        candidate Cangjie types via java_to_cangjie (exact) or search_api
        (similarity). This narrows the search space from the whole KB
        (3537 records) to a handful of candidate classes.
    Stage 2 (method match): the layered NL retrieval runs as before, but each
        level's hits are cross-validated against the candidate types: an API
        whose `parent` (or name) matches a candidate type is far more credible.
    Output: `suggested` bundles {cangjie_type, module, methods, examples} so
        the caller gets one actionable recommendation instead of raw lists.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from cjkb.index.searcher import Searcher
from cjkb.java_types import extract_types
from cjkb.nl_generator import generate_layered

LEVELS = ["api", "statement", "function"]

# type names that are too generic to lock onto
_GENERIC_TYPES = {"Object", "String", "Integer", "Long", "Double", "Float",
                  "Boolean", "Byte", "Short", "Char", "List", "Map", "Set",
                  "Collection", "Iterable", "Optional", "Exception", "Error"}


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
    return {"query": query, "apis": apis, "examples": examples}


def _level_score(nl: Dict[str, str], apis: List) -> float:
    """Estimate how well this level landed on real Cangjie docs."""
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


# ---------------------------------------------------------------------------
# Stage 1: type locking
# ---------------------------------------------------------------------------

def _lock_types(searcher: Searcher, java_types: List[str],
                top_k: int = 3) -> List[Dict]:
    """Map Java type tokens to candidate Cangjie types.

    Returns sorted list of:
        {"java_type", "cangjie_type", "module", "source", "confidence"}
    where cangjie_type comes from java_to_cangjie (exact) or a KB class search.
    """
    candidates: List[Dict] = []
    seen = set()
    for jt in java_types:
        simple = jt.split(".")[-1] if "." in jt else jt
        if simple in _GENERIC_TYPES:
            continue
        # 1) exact mapping table (j2cjlib shims + terms)
        exact = searcher.java_to_cangjie(jt) or searcher.java_to_cangjie(simple)
        for m in exact[:2]:
            key = m.cangjie_symbol
            if key in seen:
                continue
            seen.add(key)
            candidates.append({
                "java_type": jt,
                "cangjie_type": m.cangjie_symbol,
                "module": _module_of(searcher, m.cangjie_symbol),
                "source": m.source,
                "confidence": "exact" if m.library == "j2cjlib" else "mapping",
            })
        # 2) KB class-name similarity search (covers std.* types)
        if simple not in _GENERIC_TYPES:
            hits = searcher.search_api(simple, top_k=top_k)
            for r in hits:
                if r.kind not in ("class", "interface", "enum", "struct"):
                    continue
                key = (r.name, r.module)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append({
                    "java_type": jt,
                    "cangjie_type": r.name,
                    "module": r.module,
                    "source": r.source,
                    "confidence": "class_search",
                })
    return candidates[:6]


def _module_of(searcher: Searcher, cangjie_type: str) -> str:
    """Best-effort: find the module a Cangjie type lives in."""
    for r in searcher.kb.apis:
        if r.kind in ("class", "interface", "enum", "struct") and r.name == cangjie_type:
            return r.module
    return ""


# ---------------------------------------------------------------------------
# Stage 2: cross-validation
# ---------------------------------------------------------------------------

def _matches_type(api, cangjie_types: List[str]) -> bool:
    """Does this API belong to one of the locked candidate types?"""
    if not cangjie_types:
        return False
    parent = api.parent or ""
    for ct in cangjie_types:
        ct_simple = ct.split(".")[-1]
        if parent == ct or parent == ct_simple or api.name == ct_simple:
            return True
    return False


def _cross_validate(levels: Dict[str, Dict], cangjie_types: List[str]) -> None:
    """Boost scores / tag hits that belong to locked candidate types."""
    for lvl in LEVELS:
        apis = levels[lvl]["apis"]
        tagged = 0
        for a in apis:
            if _matches_type(a, cangjie_types):
                a._kb_type_match = True  # type: ignore[attr-defined]
                tagged += 1
        levels[lvl]["type_matched"] = tagged
        if tagged:
            levels[lvl]["score"] *= 1.5  # type-matched hits are more credible


def _best_type_methods(searcher: Searcher, candidates: List[Dict],
                       levels: Dict[str, Dict], top_k: int) -> Optional[Dict]:
    """Pick the best candidate type and return its members + examples.

    Prefers a candidate whose class actually shows up in the retrieval hits
    (i.e. the NL search and the type lock agree); falls back to the first
    confident candidate.
    """
    if not candidates:
        return None
    hit_modules = {r.module for lvl in LEVELS for r in levels[lvl]["apis"]}

    # score candidates: class_search confidence is weak, exact mapping strong
    def _score(c: Dict) -> float:
        s = 0.0
        s += 2.0 if c.get("confidence") == "exact" else 1.0
        if c.get("module") in hit_modules:
            s += 3.0
        return s

    ordered = sorted(candidates, key=_score, reverse=True)
    best = ordered[0]
    cj = best["cangjie_type"]
    simple = cj.split(".")[-1]

    members = searcher.get_class_members(simple, module=best.get("module") or None)
    examples = searcher.find_examples(simple, top_k=top_k)
    # keep only member-level records (init/prop/func), drop the class itself
    member_recs = [r for r in members if r.kind != "class"][:top_k * 2]
    return {
        "cangjie_type": simple,
        "module": best.get("module", ""),
        "java_type": best.get("java_type", ""),
        "confidence": best.get("confidence", ""),
        "source": best.get("source", ""),
        "members": member_recs,
        "examples": examples[:top_k],
    }


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------

def layered_search(searcher: Searcher, java_code: str, cfg: Optional[Dict] = None,
                   module: Optional[str] = None, top_k: int = 5) -> Dict:
    """Two-stage progressive-disclosure retrieval for Java code.

    Returns:
        {
          "java_code": ...,
          "java_types": [...],          # extracted Java types
          "type_candidates": [...],     # locked Cangjie type candidates
          "levels": { api/statement/function: {query, apis, examples, score, type_matched} },
          "best_level": ...,
          "best_hit": ...,
          "suggested": {                # actionable recommendation
              "cangjie_type", "module", "java_type", "confidence",
              "members": [...], "examples": [...]
          }
        }
    """
    # ---- stage 1: type locking ----
    extracted = extract_types(java_code)
    java_types = extracted["types"]
    candidates = _lock_types(searcher, java_types, top_k=top_k)
    cangjie_types = [c["cangjie_type"] for c in candidates]

    # ---- stage 2: layered NL retrieval + cross-validation ----
    nls = generate_layered(java_code, cfg)
    levels: Dict[str, Dict] = {}
    for lvl in LEVELS:
        res = _search_level(searcher, nls[lvl], module, top_k)
        res["score"] = _level_score(nls[lvl], res["apis"])
        res["nl"] = nls[lvl]
        levels[lvl] = res
    _cross_validate(levels, cangjie_types)

    def _key(lvl: str) -> float:
        return (levels[lvl]["score"], -LEVELS.index(lvl))

    best_level = max(LEVELS, key=_key)
    best_apis = levels[best_level]["apis"]
    best_hit = best_apis[0] if best_apis else None

    suggested = _best_type_methods(searcher, candidates, levels, top_k)

    return {
        "java_code": java_code,
        "java_types": java_types,
        "type_candidates": candidates,
        "levels": levels,
        "best_level": best_level,
        "best_hit": best_hit,
        "suggested": suggested,
    }
