"""MCP server exposing the Cangjie knowledge base over stdio.

Implements the Model Context Protocol (JSON-RPC 2.0 over stdin/stdout)
natively with zero third-party dependencies, so it runs in any environment
with Python >= 3.9.

Tools exposed:
  - search_api          similarity search over Cangjie API records
  - get_api_details     exact lookup of a function/class by name
  - get_class_members   members (init/prop/func) of a class/interface
  - find_examples       example snippets for a concept/API
  - java_to_cangjie     Java symbol -> Cangjie equivalent
  - error_fix_hint      compile-error text -> relevant APIs/examples
  - list_modules        available stdlib/stdx modules
  - resolve_java_code   progressive-disclosure layered retrieval (API/statement/function NL)
  - describe_java_code  NL description of Java code without searching
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional

from cjkb.config import load_config
from cjkb.index.searcher import Searcher
from cjkb.layered_search import layered_search
from cjkb.nl_generator import generate_nl, detect_level

PROTOCOL_VERSION = "2024-11-05"


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_api",
        "description": "Similarity search over Cangjie standard-library APIs "
                       "(functions, classes, interfaces, enums). Returns signature, "
                       "source module/library and description. Java terms are "
                       "auto-expanded to Cangjie equivalents.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "API name or natural-language description, e.g. 'HashMap put', 'file read bytes'"},
                "module": {"type": "string", "description": "optional filter, e.g. std.collection"},
                "top_k": {"type": "integer", "description": "max results (default 10)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_api_details",
        "description": "Exact lookup of a Cangjie API by name (function, class, "
                       "interface, enum, macro). Returns full signature, params, "
                       "returns, exceptions, source file and any inline examples.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "exact API name, e.g. 'add' or 'ArrayList'"},
                "module": {"type": "string", "description": "optional module filter"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_class_members",
        "description": "All members (init/prop/func) of a Cangjie class or interface.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "class_name": {"type": "string", "description": "class name, e.g. 'ArrayList'"},
                "module": {"type": "string", "description": "optional module filter"},
            },
            "required": ["class_name"],
        },
    },
    {
        "name": "find_examples",
        "description": "Find runnable Cangjie example snippets for a concept or API "
                       "(e.g. 'read file lines', 'thread', 'JSON parse').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "concept or API"},
                "module": {"type": "string", "description": "optional module filter"},
                "top_k": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "java_to_cangjie",
        "description": "Map a Java symbol (class, package or method) to its Cangjie "
                       "equivalent using the j2cjlib shims and terminology glossary. "
                       "Use BEFORE translating a fragment to know which stdlib type "
                       "or j2cjlib shim corresponds to the Java API.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "java_symbol": {"type": "string",
                                "description": "e.g. 'java.util.List', 'Thread', 'System.out.println'"},
            },
            "required": ["java_symbol"],
        },
    },
    {
        "name": "error_fix_hint",
        "description": "Given a Cangjie compile error message, find the most relevant "
                       "API docs and examples to help fix it. Use inside the error-"
                       "fixing loop after a failed cjpm build.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "error_text": {"type": "string", "description": "compiler error text"},
                "top_k": {"type": "integer"},
            },
            "required": ["error_text"],
        },
    },
    {
        "name": "list_modules",
        "description": "List all modules available in the knowledge base "
                       "(std.* and stdx.*) with API/example counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "resolve_java_code",
        "description": "PROGRESSIVE-DISCLOSURE layered retrieval. "
                       "Given a Java code fragment, generate natural-language "
                       "descriptions at three granularities -- API call (finest, "
                       "may have no 1:1 Cangjie match), statement/code segment "
                       "(medium, maps to one or several Cangjie APIs), whole "
                       "function (coarsest, maps to a whole feature) -- and search "
                       "the Cangjie knowledge base at each layer. Use when "
                       "translating a Java fragment: start from `best_level` and "
                       "fall back to coarser levels when the fine-grained search "
                       "finds nothing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "java_code": {"type": "string",
                              "description": "Java code fragment: a single method call, a statement, or a whole method body"},
                "module": {"type": "string", "description": "optional module filter, e.g. std.io"},
                "top_k": {"type": "integer", "description": "results per level (default 5)"},
            },
            "required": ["java_code"],
        },
    },
    {
        "name": "describe_java_code",
        "description": "Generate natural-language descriptions (English + Chinese) "
                       "of a Java code fragment at API/statement/function "
                       "granularity, without searching. Useful when you want the "
                       "NL description itself (e.g. to build a prompt or explain "
                       "code) rather than retrieval results.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "java_code": {"type": "string", "description": "Java code fragment"},
            },
            "required": ["java_code"],
        },
    },
]


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------

class MCPError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class McpServer:
    def __init__(self, searcher: Searcher, cfg: Optional[Dict[str, Any]] = None) -> None:
        self.searcher = searcher
        self.cfg = cfg or {}
        self.llm_cfg = (cfg or {}).get("llm", {})

    # ---- tool implementations ------------------------------------------
    def _run_tool(self, name: str, args: Dict[str, Any]) -> Any:
        handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {
            "search_api": self._search_api,
            "get_api_details": self._get_api_details,
            "get_class_members": self._get_class_members,
            "find_examples": self._find_examples,
            "java_to_cangjie": self._java_to_cangjie,
            "error_fix_hint": self._error_fix_hint,
            "list_modules": self._list_modules,
            "resolve_java_code": self._resolve_java_code,
            "describe_java_code": self._describe_java_code,
        }
        if name not in handlers:
            raise MCPError(-32601, f"unknown tool: {name}")
        return handlers[name](args)

    def _search_api(self, a: Dict[str, Any]) -> Any:
        q = a.get("query", "")
        module = a.get("module")
        top_k = a.get("top_k")
        if not q:
            raise MCPError(-32602, "query is required")
        recs = self.searcher.search_api(q, module=module, top_k=top_k)
        return {"results": [_api_dict(r) for r in recs]}

    def _get_api_details(self, a: Dict[str, Any]) -> Any:
        name = a.get("name", "")
        module = a.get("module")
        if not name:
            raise MCPError(-32602, "name is required")
        recs = self.searcher.get_api_details(name, module=module)
        return {"results": [_api_dict(r) for r in recs]}

    def _get_class_members(self, a: Dict[str, Any]) -> Any:
        cls = a.get("class_name", "")
        module = a.get("module")
        if not cls:
            raise MCPError(-32602, "class_name is required")
        members = self.searcher.get_class_members(cls, module=module)
        return {"class": cls, "member_count": len(members),
                "members": [_api_dict(r) for r in members]}

    def _find_examples(self, a: Dict[str, Any]) -> Any:
        q = a.get("query", "")
        module = a.get("module")
        top_k = a.get("top_k")
        if not q:
            raise MCPError(-32602, "query is required")
        exs = self.searcher.find_examples(q, module=module, top_k=top_k)
        return {"results": [_example_dict(e) for e in exs]}

    def _java_to_cangjie(self, a: Dict[str, Any]) -> Any:
        sym = a.get("java_symbol", "")
        if not sym:
            raise MCPError(-32602, "java_symbol is required")
        maps = self.searcher.java_to_cangjie(sym)
        return {"java_symbol": sym,
                "results": [{"java": m.java_symbol, "cangjie": m.cangjie_symbol,
                             "source": m.source, "notes": m.notes, "library": m.library}
                            for m in maps]}

    def _error_fix_hint(self, a: Dict[str, Any]) -> Any:
        err = a.get("error_text", "")
        top_k = a.get("top_k")
        if not err:
            raise MCPError(-32602, "error_text is required")
        # 1) strip noise, 2) search API docs + examples
        clean = _clean_error(err)
        apis = self.searcher.search_api(clean, top_k=top_k)
        exs = self.searcher.find_examples(clean, top_k=top_k)
        return {"error": err[:1000],
                "apis": [_api_dict(r) for r in apis],
                "examples": [_example_dict(e) for e in exs]}

    def _list_modules(self, _a: Dict[str, Any]) -> Any:
        return {"modules": [{"module": m, **_mod_counts(m, self.searcher.kb.modules)}
                            for m in self.searcher.list_modules()]}

    def _resolve_java_code(self, a: Dict[str, Any]) -> Any:
        code = a.get("java_code", "")
        module = a.get("module")
        top_k = a.get("top_k") or 5
        if not code:
            raise MCPError(-32602, "java_code is required")
        res = layered_search(self.searcher, code, self.llm_cfg, module=module, top_k=top_k)

        def _lvl_dict(lvl: Dict) -> Dict:
            return {
                "nl": lvl["nl"],
                "query": lvl["query"],
                "score": round(lvl["score"], 3),
                "type_matched": lvl.get("type_matched", 0),
                "apis": [_api_dict(r) for r in lvl["apis"]],
                "examples": [_example_dict(e) for e in lvl["examples"]],
            }

        return {
            "java_code": code,
            "java_types": res["java_types"],
            "type_candidates": res["type_candidates"],
            "best_level": res["best_level"],
            "levels": {lvl: _lvl_dict(res["levels"][lvl]) for lvl in ("api", "statement", "function")},
            "best_hit": _api_dict(res["best_hit"]) if res["best_hit"] else None,
            "suggested": _suggested_dict(res["suggested"]) if res["suggested"] else None,
        }

    def _describe_java_code(self, a: Dict[str, Any]) -> Any:
        code = a.get("java_code", "")
        if not code:
            raise MCPError(-32602, "java_code is required")
        from cjkb.nl_generator import generate_layered
        nls = generate_layered(code, self.llm_cfg)
        return {"java_code": code,
                "detected_level": detect_level(code),
                "nl": nls}

    # ---- protocol --------------------------------------------------------
    def handle_message(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "cangjie-knowledge-mcp", "version": "0.1.0"},
            }}
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            try:
                result = self._run_tool(name, args)
                return {"jsonrpc": "2.0", "id": msg_id,
                        "result": {"content": [{"type": "text",
                                                "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                                   "isError": False}}
            except MCPError as e:
                return {"jsonrpc": "2.0", "id": msg_id,
                        "result": {"content": [{"type": "text", "text": f"error: {e}"}],
                                   "isError": True}}
            except Exception as e:  # defensive: never crash the loop
                return {"jsonrpc": "2.0", "id": msg_id,
                        "result": {"content": [{"type": "text", "text": f"internal error: {e!r}"}],
                                   "isError": True}}
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}}
        if method == "notifications/cancelled":
            return None
        if method is None and msg_id is not None:
            return None  # response, ignore
        # unknown method
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"method not found: {method}"}}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _api_dict(r) -> Dict[str, Any]:
    return {
        "name": r.name,
        "kind": r.kind,
        "module": r.module,
        "library": r.library,
        "signature": r.signature,
        "description": r.description[:500],
        "params": r.params[:10],
        "returns": r.returns[:300],
        "exceptions": r.exceptions[:5],
        "parent": r.parent,
        "source": r.source,
        "examples": r.examples[:2],
    }


def _example_dict(e) -> Dict[str, Any]:
    return {
        "title": e.title,
        "module": e.module,
        "library": e.library,
        "code": e.code,
        "description": e.description[:200],
        "source": e.source,
        "generated": e.generated,
    }


def _suggested_dict(s) -> Dict[str, Any]:
    return {
        "cangjie_type": s["cangjie_type"],
        "module": s["module"],
        "java_type": s["java_type"],
        "confidence": s["confidence"],
        "source": s["source"],
        "members": [_api_dict(r) for r in s["members"]],
        "examples": [_example_dict(e) for e in s["examples"]],
    }


def _mod_counts(module: str, modules: Dict[str, Any]) -> Dict[str, int]:
    info = modules.get(module, {})
    return {"apis": info.get("apis", 0), "examples": info.get("examples", 0)}


def _clean_error(err: str) -> str:
    """Extract the most query-relevant tokens from a compile error."""
    import re
    # keep identifiers, symbols and Chinese; drop paths/line numbers
    err = re.sub(r"\b[A-Za-z]:[\\/][\w\\/.\-]+", " ", err)      # windows paths
    err = re.sub(r"at\s+[\w.$]+\(.*?\)", " ", err)               # stack frames
    err = re.sub(r"\d+:\d+", " ", err)                           # line:col
    err = re.sub(r"[{}()\[\];,]", " ", err)
    words = re.findall(r"[A-Za-z_]\w*|[\u4e00-\u9fff]+", err)
    # drop common noise
    stop = {"error", "cannot", "find", "the", "of", "to", "in", "for", "expected",
            "but", "found", "at", "line", "error", "cangjie", "compiler", "编译",
            "错误", "找不到", "期望", "但", "是"}
    keep = [w for w in words if w.lower() not in stop and len(w) > 1]
    return " ".join(keep[:20])


# ---------------------------------------------------------------------------
# stdio loop
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Cangjie knowledge base MCP server")
    parser.add_argument("--config", default="", help="path to config.yaml")
    parser.add_argument("--data-dir", default="", help="path to built KB data dir")
    args = parser.parse_args(argv)

    cfg = load_config(args.config or os.path.join(
        os.path.dirname(__file__), "..", "..", "config.yaml")) if args.config or os.path.exists(
        os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")) else {
        "output": {"data_dir": "data"}, "index": {}}
    data_dir = args.data_dir or cfg["output"].get("data_dir", "data")
    if not os.path.exists(os.path.join(data_dir, "apis.jsonl")):
        sys.stderr.write(
            f"[cjkb] knowledge base not found at {data_dir}. "
            f"Run `python scripts/build_kb.py` first.\n")
        return 1

    searcher = Searcher.load(data_dir, cfg)
    server = McpServer(searcher, cfg)
    sys.stderr.write(f"[cjkb] MCP server ready ({server.searcher.kb.stats()})\n")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = server.handle_message(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
