"""NL generator: turn Java code into natural-language descriptions.

Implements the progressive-disclosure (渐进式披露) idea:

  Level 1 (api)      : a single Java API call            -> "read a line of text"
  Level 2 (statement): a statement / short code segment  -> "loop over lines until EOF"
  Level 3 (function) : a whole Java method               -> "copy a file line by line"

Each level produces a bilingual (en/zh) NL description that is then used to
search the Cangjie documentation. Level 1 is the finest granularity and may
have no 1:1 counterpart in Cangjie; levels 2/3 are coarser abstractions that
usually map to one or several Cangjie APIs.

Two generation paths:
  - LLM (OpenAI-compatible, optional): best quality; used when configured.
  - Heuristic fallback (no LLM): camelCase splitting + keyword mapping.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Level detection
# ---------------------------------------------------------------------------

LEVEL_LABELS = {"api": "API call", "statement": "statement/code segment",
                "function": "whole function"}

# rough signals: a method body with multiple statements / braces nesting
_METHOD_BODY_RE = re.compile(
    r"(?:public|private|protected|static|\s)*[\w<>\[\],.\s]+\s+\w+\s*\([^)]*\)\s*\{[^}]*[;{}][^}]*\}"
)


def detect_level(java_code: str) -> str:
    """Guess granularity: 'api' | 'statement' | 'function'."""
    code = java_code.strip()
    if not code:
        return "api"
    # whole method with body (contains '{' and at least one ';')
    if "{" in code and "}" in code and ";" in code:
        return "function"
    # single call expression like obj.method(args)
    if re.match(r"^[\w.]+\(.*\)\s*;?$", code, re.S):
        return "api"
    # assignment / expression statement
    if ";" in code:
        return "statement"
    return "api"


# ---------------------------------------------------------------------------
# Heuristic fallback
# ---------------------------------------------------------------------------

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|_")

# verb-ish words that already read like NL
_VERBS = {
    "get", "set", "put", "add", "remove", "delete", "read", "write", "create",
    "parse", "convert", "format", "toString", "equals", "compare", "sort",
    "find", "search", "open", "close", "flush", "clear", "clone", "copy",
    "split", "join", "trim", "replace", "substring", "contains", "isEmpty",
    "iterator", "stream", "collect", "map", "filter", "forEach", "print",
    "println", "next", "hasNext", "start", "stop", "join", "sleep", "wait",
    "notify", "lock", "unlock", "enter", "exit",
}

_TERM_MAP = {
    "file": "文件", "reader": "读取器", "writer": "写入器", "stream": "流",
    "line": "行", "string": "字符串", "map": "映射", "list": "列表",
    "set": "集合", "thread": "线程", "time": "时间", "date": "日期",
    "json": "JSON", "byte": "字节", "array": "数组", "buffer": "缓冲",
}


def _split_ident(ident: str) -> List[str]:
    return [p for p in _CAMEL.split(ident) if p]


def _heuristic_api(java_code: str) -> str:
    """'BufferedReader.readLine()' -> 'read a line' (en)."""
    m = re.search(r"(\w+)\.(\w+)\s*\(", java_code)
    if not m:
        return java_code.strip()[:120]
    obj, method = m.group(1), m.group(2)
    toks = _split_ident(method)
    # drop common java-ism prefixes
    toks = [t for t in toks if t.lower() not in ("get", "is", "set") or len(toks) == 1]
    words = " ".join(t.lower() for t in toks) or method
    return f"{words} on {obj}"


def _heuristic_zh(java_code: str) -> str:
    """Best-effort Chinese description via term map."""
    m = re.search(r"(\w+)\.(\w+)\s*\(", java_code)
    parts = []
    if m:
        obj, method = m.group(1), m.group(2)
        toks = [_split_ident(t) for t in (method, obj)]
        flat = [w for grp in toks for w in grp]
        for w in flat:
            parts.append(_TERM_MAP.get(w.lower(), w))
    return " ".join(parts) if parts else java_code.strip()[:120]


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------

NL_SYSTEM_PROMPT = (
    "You translate Java code into concise natural-language descriptions used "
    "to search Cangjie (仓颉) standard-library documentation. The description "
    "must capture the FUNCTIONAL INTENT, not the syntax.\n"
    "Rules:\n"
    "1. Output exactly two lines: one English, one Chinese.\n"
    "2. First line: `EN: <english description>`\n"
    "3. Second line: `ZH: <chinese description>`\n"
    "4. Describe what the code DOES (e.g. 'read all lines of a file'), "
    "not the method name.\n"
    "5. Keep each description under 20 words."
)


def _call_llm_nl(cfg: Dict, java_code: str, level: str) -> Optional[Dict[str, str]]:
    base = cfg.get("base_url") or "https://api.openai.com/v1"
    model = cfg.get("model") or "gpt-4o-mini"
    key = cfg.get("api_key")
    if not key:
        return None
    url = base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": NL_SYSTEM_PROMPT},
            {"role": "user", "content": f"Java {LEVEL_LABELS.get(level, level)}:\n```java\n{java_code}\n```"},
        ],
        "temperature": 0.1,
        "max_tokens": 400,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = (data["choices"][0]["message"].get("content") or "").strip()
    except Exception as exc:
        # stderr, not stdout: resolve_java_code / describe_java_code run inside
        # the MCP server, where stdout is the JSON-RPC protocol channel.
        print(f"  [nl_generator] LLM call failed: {exc}", file=sys.stderr)
        return None
    if not content:
        return None
    en = re.search(r"^EN:\s*(.+)$", content, re.M)
    zh = re.search(r"^ZH:\s*(.+)$", content, re.M)
    return {"en": en.group(1).strip() if en else content[:150],
            "zh": zh.group(1).strip() if zh else ""}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_nl(java_code: str, level: str = "auto",
                cfg: Optional[Dict] = None) -> Dict[str, str]:
    """Return {'en': ..., 'zh': ...} NL description of the Java code.

    Uses LLM when configured (cfg with api_key), else heuristic fallback.
    """
    if level == "auto":
        level = detect_level(java_code)
    nl = _call_llm_nl(cfg or {}, java_code, level)
    if nl:
        return nl
    return {"en": _heuristic_api(java_code), "zh": _heuristic_zh(java_code)}


def generate_layered(java_code: str, cfg: Optional[Dict] = None) -> Dict[str, Dict[str, str]]:
    """Generate NL at all three granularities (progressive disclosure).

    Returns {"api": {"en","zh"}, "statement": {...}, "function": {...}}.
    Level 1 reuses the exact call expression; higher levels use the full code.
    """
    code = java_code.strip()
    # api level: first method call found
    api_expr = ""
    m = re.search(r"[\w.]+\([^)]*\)", code)
    if m:
        api_expr = m.group(0)
    # statement level: first line / semicolon-terminated segment
    stmt_expr = ""
    for line in code.splitlines():
        line = line.strip()
        if line and not line.startswith(("public", "private", "protected",
                                         "import", "package", "//", "/*", "*")):
            stmt_expr = line.rstrip(";")
            break
    if not stmt_expr:
        stmt_expr = code[:200]

    out = {}
    out["api"] = generate_nl(api_expr or code, "api", cfg)
    out["statement"] = generate_nl(stmt_expr, "statement", cfg)
    out["function"] = generate_nl(code, "function", cfg)
    return out
