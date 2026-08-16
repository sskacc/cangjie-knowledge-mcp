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
from cjkb.reranker import rerank

LEVELS = ["api", "statement", "function"]

# type names that are too generic to lock onto
_GENERIC_TYPES = {"Object", "String", "Integer", "Long", "Double", "Float",
                  "Boolean", "Byte", "Short", "Char", "List", "Map", "Set",
                  "Collection", "Iterable", "Optional", "Exception", "Error"}

# record kinds that represent actual members (vs. the type declaration itself)
_MEMBER_KINDS = {"init", "func", "prop", "macro", "operator", "getter", "setter"}


def _clean_nl_query(nl: Dict[str, str]) -> str:
    """Join en+zh descriptions into one search query, dedup tokens."""
    parts = [nl.get("en", ""), nl.get("zh", "")]
    return " ".join(p for p in parts if p)


def _search_level(searcher: Searcher, nl: Dict[str, str],
                  module: Optional[str], top_k: int,
                  cfg: Optional[Dict] = None) -> Dict:
    """Retrieve APIs + examples for one level's NL description.

    Fetches a wider recall pool (top_k*3) then reranks down to top_k when an
    LLM is configured; otherwise the result is the plain BM25 order.
    """
    query = _clean_nl_query(nl)
    apis = searcher.search_api(query, module=module, top_k=top_k * 3)
    examples = searcher.find_examples(query, module=module, top_k=top_k * 3)
    apis = rerank(query, apis, cfg, top_k=top_k)
    examples = rerank(query, examples, cfg, top_k=top_k)
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
        # Strip generics (e.g. "HashMap<String,Integer>" -> "HashMap") and take
        # the simple name; otherwise the mapping table lookup and the BM25
        # class search both run on a raw generic string and miss.
        stripped = re.sub(r"<.*>", "", jt).strip()
        simple = stripped.split(".")[-1] if "." in stripped else stripped
        if not simple or simple in _GENERIC_TYPES:
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
                       levels: Dict[str, Dict], best_level: str,
                       top_k: int, cfg: Optional[Dict] = None) -> Optional[Dict]:
    """Pick the best candidate type and return its members + examples.

    Prefers a candidate whose class actually shows up in the retrieval hits
    (i.e. the NL search and the type lock agree); falls back to the first
    confident candidate.

    Granularity-aware member selection:
      - best_level == "api"  (fine-grained): the caller already knows exactly
        which API it wants, so return only the members most relevant to the
        api-level NL query (ranked by BM25), not the whole class.
      - best_level == "statement"/"function" (coarse): the caller needs the
        full picture to choose, so keep returning all members (current behavior).
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

    examples = searcher.find_examples(simple, top_k=top_k * 3)
    examples = rerank(levels.get("api", {}).get("query", simple), examples, cfg, top_k=top_k)
    members = searcher.get_class_members(simple, module=best.get("module") or None)
    # keep only member-level records (init/prop/func), drop the class itself
    member_recs = [r for r in members if r.kind in _MEMBER_KINDS]

    if best_level == "api" and member_recs:
        # fine-grained: rank the class's members against the api-level NL query
        # and return only the top matches, so the caller gets a precise list of
        # "the methods that do what I asked" instead of the whole class.
        api_query = levels["api"]["query"]
        member_recs = searcher.rank_records(member_recs, api_query,
                                            top_k=max(top_k * 3, len(member_recs)))
        member_recs = rerank(api_query, member_recs, cfg, top_k=top_k)

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
        res = _search_level(searcher, nls[lvl], module, top_k, cfg)
        res["score"] = _level_score(nls[lvl], res["apis"])
        res["nl"] = nls[lvl]
        levels[lvl] = res
    _cross_validate(levels, cangjie_types)

    def _key(lvl: str) -> float:
        return (levels[lvl]["score"], -LEVELS.index(lvl))

    best_level = max(LEVELS, key=_key)
    best_apis = levels[best_level]["apis"]
    best_hit = best_apis[0] if best_apis else None

    suggested = _best_type_methods(searcher, candidates, levels, best_level, top_k, cfg)

    return {
        "java_code": java_code,
        "java_types": java_types,
        "type_candidates": candidates,
        "levels": levels,
        "best_level": best_level,
        "best_hit": best_hit,
        "suggested": suggested,
    }


# ---------------------------------------------------------------------------
# per-call resolution: one suggest per API call, escalating granularity
# ---------------------------------------------------------------------------

def _call_api_suggest(searcher: Searcher, call: Dict, java_code: str,
                      cfg: Optional[Dict], top_k: int) -> Optional[Dict]:
    """Level-1 (api) suggest for a single method call.

    Locks the receiver's declared type, ranks that class's members against a
    natural-language description of the call, and returns a precise suggest
    (only the top-k members). Returns None when the type can't be locked
    (unknown receiver) or the class has no members, so the caller escalates
    to the statement / block level.
    """
    recv_type = call.get("declared_type") or call.get("declared_simple") or ""
    recv_simple = call.get("declared_simple") or ""
    if not recv_type or not recv_simple or recv_simple in _GENERIC_TYPES:
        return None

    # lock receiver type -> candidate Cangjie types.
    # Use the SIMPLE name (generics already stripped by extract_types), not the
    # raw declared_type: a generic string like "HashMap<String,Integer>" or
    # "HashMap<>" would miss the exact mapping-table lookup and fall into the
    # noisy BM25 class search (which can land on ConcurrentHashMap instead).
    candidates = _lock_types(searcher, [recv_simple], top_k=top_k)
    if not candidates:
        return None
    # pick the first candidate that is a clean type name, has a known module
    # AND actually has members. Skip noisy ones: generic-form mappings
    # ("HashMap<String, Int64>") with empty module, the too-generic "Any"
    # fallback, and unrelated class_search hits with no members. Prefer
    # candidates with a module so the returned suggestion carries provenance.
    def _cand_type(c: Dict) -> str:
        return re.sub(r"<.*>", "", c["cangjie_type"]).strip()

    best = None
    member_recs: List = []
    # pass 1: clean name + known module + members
    for c in candidates:
        s = _cand_type(c)
        if s == "Any" or not c.get("module"):
            continue
        mrecs = [r for r in searcher.get_class_members(s, module=c.get("module"))
                 if r.kind in _MEMBER_KINDS]
        if mrecs:
            best = {**c, "cangjie_type": s}
            member_recs = mrecs
            break
    # pass 2: fall back to any clean candidate with members (module unknown)
    if best is None:
        for c in candidates:
            s = _cand_type(c)
            if s == "Any":
                continue
            mrecs = [r for r in searcher.get_class_members(s, module=None)
                     if r.kind in _MEMBER_KINDS]
            if mrecs:
                best = {**c, "cangjie_type": s}
                member_recs = mrecs
                break
    if best is None or not member_recs:
        return None
    cj = best["cangjie_type"]
    simple = cj.split(".")[-1]

    # NL description of this call -> rank the class's members.
    # Constructor calls bias the query toward init members (construct/create/init).
    call_expr = _call_expr(java_code, call)
    if call.get("is_ctor"):
        nls = {
            "api": {"en": f"construct create init an instance of {simple}",
                    "zh": f"创建 {simple} 的实例初始化"},
            "statement": {"en": f"construct create init an instance of {simple}",
                          "zh": f"创建 {simple} 的实例初始化"},
        }
    else:
        nls = generate_layered(call_expr or call.get("method", ""), cfg)
    query = _clean_nl_query(nls["api"])
    # Rerank the class's members by semantic relevance (falls back to BM25
    # order when no LLM is configured). Members lists are small, so rerank
    # works directly on the recall pool instead of a wider fetch.
    ranked = searcher.rank_records(member_recs, query, top_k=max(top_k * 3, len(member_recs)))
    ranked = rerank(query, ranked, cfg, top_k=top_k)
    if not ranked:
        return None

    examples = searcher.find_examples(simple, top_k=top_k * 3)
    examples = rerank(query, examples, cfg, top_k=top_k)
    return {
        "level": "api",
        "java_expr": call_expr or f"{call.get('receiver','')}.{call.get('method','')}()",
        "cangjie_type": simple,
        "module": best.get("module", ""),
        "java_type": best.get("java_type", ""),
        "confidence": best.get("confidence", ""),
        "source": best.get("source", ""),
        "members": ranked[:top_k],
        "examples": examples[:top_k],
    }


def _call_expr(java_code: str, call: Dict) -> str:
    """Find the source text of a method call (receiver.method(...)) or a
    constructor call (new Xxx(...)) in the code."""
    if call.get("is_ctor"):
        ctype = call.get("ctor_simple", "")
        # balanced-paren extraction so nested ctors (new A(new B(...))) match fully
        m2 = re.search(r"\bnew\s+" + re.escape(ctype) + r"\s*\(", java_code)
        if m2:
            start = m2.start()
            depth = 0
            i = m2.end() - 1
            while i < len(java_code):
                if java_code[i] == "(":
                    depth += 1
                elif java_code[i] == ")":
                    depth -= 1
                    if depth == 0:
                        return java_code[start:i + 1]
                i += 1
        return f"new {ctype}()"
    recv, method = call.get("receiver", ""), call.get("method", "")
    if not recv or not method:
        return ""
    m = re.search(re.escape(recv) + r"\s*\.\s*" + re.escape(method) + r"\s*\([^)]*\)", java_code)
    return m.group(0) if m else f"{recv}.{method}()"


def resolve_java_code(searcher: Searcher, java_code: str, cfg: Optional[Dict] = None,
                      module: Optional[str] = None, top_k: int = 5) -> Dict:
    """Per-call progressive-disclosure resolution for a Java code block.

    Unlike `layered_search` (which treats the whole block as one unit and
    produces a single `suggested`), this produces **one suggest per API call**
    in the block:

        Level 1 (api):      per call - lock the receiver type, rank that
                            class's members against the call's intent.
        Level 2 (statement): per call - fall back to the whole statement's NL
                            when the receiver type can't be locked.
        Level 3 (function): one suggest for the whole block (the existing
                            layered_search path), only when calls can't be
                            resolved individually.

    Returns:
        {
          "java_code": ...,
          "java_types": [...],
          "calls": [{receiver, method, declared_type}...],
          "suggestions": [ {level, java_expr, cangjie_type, module,
                            java_type, confidence, members, examples} ... ],
          "block_suggest": {...}   # L3 fallback, or None
        }
    """
    extracted = extract_types(java_code)
    calls = extracted["calls"]
    suggestions: List[Dict] = []

    for call in calls:
        # L1: api-level per call
        sg = _call_api_suggest(searcher, call, java_code, cfg, top_k)
        if sg:
            suggestions.append(sg)
            continue

        # L2: statement-level per call (whole line's NL, no type lock needed)
        call_expr = _call_expr(java_code, call)
        nls = generate_layered(call_expr or call.get("method", ""), cfg)
        query = _clean_nl_query(nls["statement"])
        apis = searcher.search_api(query, module=module, top_k=top_k * 3)
        apis = rerank(query, apis, cfg, top_k=top_k)
        if apis:
            suggestions.append({
                "level": "statement",
                "java_expr": call_expr or f"{call.get('receiver','')}.{call.get('method','')}()",
                "apis": [_simple_api(a) for a in apis[:top_k]],
                "query": query,
            })
            continue

        # L3: block-level (whole block, generated once per block)
        block = layered_search(searcher, java_code, cfg, module=module, top_k=top_k)
        suggestions.append({
            "level": "function",
            "java_expr": java_code.strip()[:80],
            "block": True,
            **{k: block[k] for k in ("java_types", "type_candidates", "levels", "best_level", "best_hit", "suggested")},
        })

    # If no call could be resolved individually, produce one block-level suggest
    # (L3 fallback) so the caller always gets something actionable.
    if not suggestions:
        block = layered_search(searcher, java_code, cfg, module=module, top_k=top_k)
        suggestions.append({
            "level": "function",
            "java_expr": java_code.strip()[:80],
            "block": True,
            **{k: block[k] for k in ("java_types", "type_candidates", "levels", "best_level", "best_hit", "suggested")},
        })

    return {
        "java_code": java_code,
        "java_types": extracted["types"],
        "calls": calls,
        "suggestions": suggestions,
    }


def _simple_api(a) -> Dict:
    """Compact dict for a search hit (used in statement-level fallback)."""
    return {
        "name": a.name,
        "kind": a.kind,
        "module": a.module,
        "signature": a.signature[:200],
        "parent": a.parent,
    }
