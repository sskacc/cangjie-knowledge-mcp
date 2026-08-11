"""Java type extractor: pull type references out of Java code.

Feeds the type-locking step of `resolve_java_code`. Extracts:
  - variable declarations      `HashMap<K,V> map = ...`     -> HashMap
  - method-call receivers      `map.put(k, v)`              -> map (needs var type)
  - generic type arguments     `ArrayList<String>`          -> ArrayList, String
  - return/param/field types   `public Map<String,Integer> f(...)` -> Map, String, Integer
  - casts                      `(List) x`                   -> List
  - fully-qualified names      `java.util.HashMap`          -> java.util.HashMap, HashMap

Heuristic (regex-based) on purpose: fast, deterministic, no parser dependency.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# regexes
# ---------------------------------------------------------------------------

# declaration: [modifiers] Type<...> name [= ...];
_DECL_RE = re.compile(
    r"(?:\b(?:public|private|protected|static|final|abstract|volatile|transient|synchronized)\s+)*"
    r"(?P<type>[A-Z][A-Za-z0-9_$]*(?:\.[A-Z][A-Za-z0-9_$]*)*"
    r"(?:<[^;=]+>)?)\s+"
    r"(?P<name>[a-z_][A-Za-z0-9_$]*)\s*(?:=|;|,)"
)

# method signature: [modifiers] ReturnType name(args) [throws ...]
_METHOD_RE = re.compile(
    r"(?:\b(?:public|private|protected|static|final|abstract|synchronized|native)\s+)*"
    r"(?P<ret>[A-Z][A-Za-z0-9_$]*(?:\.[A-Z][A-Za-z0-9_$]*)*"
    r"(?:<[^()]+>)?)\s+"
    r"(?P<name>[a-zA-Z_][A-Za-z0-9_$]*)\s*\("
)

# method call: receiver.method(args)  (receiver is a lower-case identifier)
_CALL_RE = re.compile(r"(?P<recv>[a-z_][A-Za-z0-9_$]*)\s*\.\s*(?P<method>[A-Za-z_][A-Za-z0-9_$]*)\s*\(")

# assignment:  name = expr ;  (to resolve receivers that were declared elsewhere)
_ASSIGN_RE = re.compile(r"\b([a-z_][A-Za-z0-9_$]*)\s*=")

# generic args: <A, B<C>>
_GENERIC_ARGS_RE = re.compile(r"<([^<>]*)>")

# cast: (Type) expr
_CAST_RE = re.compile(r"\(\s*([A-Z][A-Za-z0-9_$]*(?:\.[A-Z][A-Za-z0-9_$]*)*)\s*\)")

# fully qualified: java.util.HashMap
_FQN_RE = re.compile(r"\b(?:java|javax|java\.util|java\.io|java\.lang|java\.net|java\.nio"
                     r")\.[A-Z][A-Za-z0-9_$]*")

# type-level token (for generics inner types)
_TYPE_TOKEN_RE = re.compile(r"[A-Z][A-Za-z0-9_$]*")

# Java primitives (String and wrapper classes are real types, keep them)
_PRIMITIVES = {"int", "long", "short", "byte", "char", "float", "double",
               "boolean", "void"}


def _strip_generics(t: str) -> str:
    return re.sub(r"<.*>", "", t).strip()


def _simple_name(t: str) -> str:
    return _strip_generics(t).split(".")[-1].strip()


def _generic_inners(type_str: str) -> List[str]:
    """'HashMap<String, ArrayList<Integer>>' -> ['String', 'ArrayList', 'Integer']"""
    out = []
    for m in _GENERIC_ARGS_RE.finditer(type_str):
        for tok in _TYPE_TOKEN_RE.findall(m.group(1)):
            if tok not in _PRIMITIVES:
                out.append(tok)
    return out


def _add_with_generics(types: List[str], seen: set, t: str) -> None:
    """Add a type and all its generic inner types to the unique list."""
    for part in (t, _simple_name(t)):
        if part and part not in seen:
            seen.add(part)
            types.append(part)
    for inner in _generic_inners(t):
        if inner not in seen:
            seen.add(inner)
            types.append(inner)


def extract_types(java_code: str) -> Dict[str, object]:
    """Extract type references from Java code.

    Returns:
        {
          "declared": [{"type": "HashMap<K,V>", "name": "map", "simple": "HashMap"}],
          "calls":    [{"receiver": "map", "method": "put"}],
          "types":    ["java.util.HashMap", "HashMap", "String", ...],  # unique, order kept
          "var_types": {"map": "HashMap<K,V>"},  # receiver -> declared type
        }
    """
    declared: List[Dict[str, str]] = []
    calls: List[Dict[str, str]] = []
    var_types: Dict[str, str] = {}

    # declarations
    for m in _DECL_RE.finditer(java_code):
        t = m.group("type").strip()
        name = m.group("name")
        declared.append({"type": t, "name": name, "simple": _simple_name(t)})
        var_types.setdefault(name, t)

    # method return types (also catches `public HashMap<K,V> getMap()`)
    for m in _METHOD_RE.finditer(java_code):
        ret = m.group("ret").strip()
        simple = _simple_name(ret)
        if simple not in _PRIMITIVES:
            declared.append({"type": ret, "name": "", "simple": simple})

    # calls
    for m in _CALL_RE.finditer(java_code):
        calls.append({"receiver": m.group("recv"), "method": m.group("method")})

    # assignment type resolution: `List<String> x = new ArrayList<>();` already
    # handled by _DECL_RE. `map = ...` where map declared elsewhere -> keep var_types.

    # collect all type tokens
    types: List[str] = []
    seen = set()

    for d in declared:
        _add_with_generics(types, seen, d["type"])
    for m in _FQN_RE.finditer(java_code):
        _add_with_generics(types, seen, m.group(0))
    for m in _CAST_RE.finditer(java_code):
        _add_with_generics(types, seen, m.group(1))

    # calls on declared receivers: enrich each call with the declared type
    for c in calls:
        vt = var_types.get(c["receiver"], "")
        c["declared_type"] = vt
        c["declared_simple"] = _simple_name(vt) if vt else ""

    return {
        "declared": declared,
        "calls": calls,
        "types": types,
        "var_types": var_types,
    }
