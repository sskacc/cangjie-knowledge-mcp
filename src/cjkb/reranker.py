"""LLM reranker: reorder BM25 retrieval candidates by semantic relevance.

This module implements the LongCodeZip insight -- semantic relevance beats
lexical similarity -- as an OPTIONAL plug-in rerank stage on top of the BM25
search core. The search core (``cjkb.index``) stays dependency-free: reranking
only activates when an LLM is configured (``llm.api_key`` present). On any LLM
failure -- or when no key is set -- the original BM25 order is returned
unchanged, so behavior degrades gracefully to pure lexical retrieval.

Usage pattern (callers fetch a wider recall pool, then rerank down to top_k):

    recs = searcher.search_api(query, top_k=top_k * 3)
    recs = rerank(query, recs, llm_cfg, top_k)

The ``llm_cfg`` dict is the ``config.yaml`` ``llm:`` block (already resolved by
``cjkb.config.load_config``), i.e. ``{"base_url", "api_key", "model"}``.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from typing import Dict, List, Optional

# Cap the number of candidates sent to the LLM so the prompt stays small.
MAX_CANDIDATES = 40

_SYSTEM_PROMPT = (
    "You are a retrieval reranker for a Java-to-Cangjie (仓颉) code translation "
    "knowledge base. Given a search query describing what the user wants to do, "
    "rank the candidate records below by SEMANTIC relevance (the user's intent), "
    "not just lexical keyword overlap. Output ONLY a JSON array of candidate "
    "indices ordered from most to least relevant, including every index exactly "
    "once. Example output: [2, 0, 1, 3]"
)


def _enabled(llm_cfg: Optional[Dict]) -> bool:
    """Rerank only when an API key is present and not explicitly disabled."""
    cfg = llm_cfg or {}
    return bool(cfg.get("api_key")) and cfg.get("rerank", True) is not False


def _candidate_texts(records: List) -> str:
    """One compact, numbered line per record (ApiRecord or ExampleRecord)."""
    lines = []
    for i, r in enumerate(records):
        if hasattr(r, "name"):  # ApiRecord
            sig = (getattr(r, "signature", "") or "")[:100]
            desc = (getattr(r, "description", "") or "")[:60]
            lines.append(f"{i}) {r.name} [{r.kind}] @ {r.module}: {sig} - {desc}")
        else:  # ExampleRecord
            desc = (getattr(r, "description", "") or "")[:60]
            lines.append(f"{i}) {getattr(r, 'title', '')} @ {r.module}: {desc}")
    return "\n".join(lines)


def _parse_order(content: str, n_candidates: int) -> Optional[List[int]]:
    """Parse the model's JSON index array into a valid partial permutation.

    Returns a list of unique, in-range indices, or ``None`` when the model's
    output is unusable (missing array, bad JSON, too few indices). The caller
    treats ``None`` as "keep BM25 order".
    """
    m = re.search(r"\[[^\]]*\]", content)
    if not m:
        return None
    try:
        idxs = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(idxs, list):
        return None
    order: List[int] = []
    for x in idxs:
        if isinstance(x, int) and 0 <= x < n_candidates and x not in order:
            order.append(x)
    if len(order) < 2:
        return None
    return order


def _call_llm(llm_cfg: Dict, query: str, candidates_text: str,
              n_candidates: int) -> Optional[List[int]]:
    """Ask the LLM to rank candidates; return an index permutation or None."""
    base = (llm_cfg or {}).get("base_url") or "https://api.openai.com/v1"
    model = (llm_cfg or {}).get("model") or "gpt-4o-mini"
    key = (llm_cfg or {}).get("api_key")
    if not key:
        return None
    url = base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",
             "content": f"Query: {query}\n\nCandidates:\n{candidates_text}"},
        ],
        "temperature": 0.0,
        # Reasoning models (e.g. deepseek-*) spend most of their budget on
        # chain-of-thought in `reasoning_content`; `content` stays empty unless
        # max_tokens is large enough for the reasoning to finish. Keep it high.
        "max_tokens": 4096,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        msg = data["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        if not content:
            # reasoning model: final answer may be truncated into the content
            # field or left in reasoning_content; try both for a JSON array.
            content = (msg.get("reasoning_content") or "").strip()
    except Exception as exc:
        # stderr, not stdout: rerank runs inside the MCP server where stdout
        # is the JSON-RPC protocol channel. A stray print would corrupt it.
        print(f"  [reranker] LLM rerank failed: {exc}", file=sys.stderr)
        return None
    return _parse_order(content, n_candidates)


def rerank(query: str, records: List, llm_cfg: Optional[Dict] = None,
           top_k: Optional[int] = None) -> List:
    """Reorder ``records`` by LLM semantic relevance to ``query``.

    Returns at most ``top_k`` records. When the LLM is unavailable (no key),
    fails, or returns an unusable ranking, the original (BM25) order is
    preserved -- so callers can safely use this unconditionally.
    """
    records = list(records)
    if top_k is None:
        top_k = len(records)
    if len(records) < 2 or not _enabled(llm_cfg):
        return records[:top_k]

    # Rerank only the head of the recall pool; keep any remainder in BM25 order.
    head = records[:MAX_CANDIDATES]
    order = _call_llm(llm_cfg, query, _candidate_texts(head), len(head))
    if not order:
        return records[:top_k]

    ranked = [head[i] for i in order]
    # Safety: append any candidate the model dropped, preserving coverage.
    seen = set(order)
    ranked += [head[i] for i in range(len(head)) if i not in seen]
    ranked += records[MAX_CANDIDATES:]
    return ranked[:top_k]
