"""Comprehensive end-to-end tests for cangjie-knowledge-mcp.

Covers all 9 tools, per-call resolution, constructor extraction, level
degradation, rerank, error handling, and edge cases. Runs against the real
committed knowledge base (data/). LLM-dependent behavior (semantic rerank
ordering) is NOT asserted here — those paths degrade gracefully to BM25 when
no API key is configured, and are verified separately.
"""

from __future__ import annotations

import json

from cjkb.config import load_config
from cjkb.index.searcher import Searcher
from cjkb.mcp_server import McpServer

cfg = load_config()
data_dir = cfg["output"]["data_dir"]
searcher = Searcher.load(data_dir, cfg)
server = McpServer(searcher, cfg)


def call(name, args):
    resp = server.handle_message({"jsonrpc": "2.0", "id": 99, "method": "tools/call",
                                  "params": {"name": name, "arguments": args}})
    assert not resp["result"]["isError"], resp["result"]
    return json.loads(resp["result"]["content"][0]["text"])


def test_list_modules():
    d = call("list_modules", {})
    assert d["modules"], "no modules"
    assert len(d["modules"]) == 52, f"expected 52 modules, got {len(d['modules'])}"


def test_search_api_map_related():
    d = call("search_api", {"query": "map put key value", "top_k": 5})
    names = [r["name"] for r in d["results"]]
    assert len(names) == 5, names
    # pure BM25 (no LLM key): must land on map-related APIs
    map_related = [n for n in names if any(k in n for k in ("add", "get", "replace", "put", "Map", "map", "contains"))]
    assert map_related, f"no map-related results: {names}"


def test_search_api_chinese_query():
    d = call("search_api", {"query": "读取文件所有行", "top_k": 3})
    assert d["results"], "no results for Chinese query (tokenizer regression)"


def test_get_api_details():
    d = call("get_api_details", {"name": "ArrayList"})
    assert d["results"], "ArrayList not found"
    assert any(r["kind"] == "class" for r in d["results"])


def test_get_class_members():
    d = call("get_class_members", {"class_name": "ArrayList"})
    assert d["member_count"] > 0
    names = [m["name"] for m in d["members"]]
    assert any("add" in n for n in names), names[:10]


def test_java_to_cangjie():
    d = call("java_to_cangjie", {"java_symbol": "java.util.List"})
    assert d["results"], "no mapping"
    assert any(r["cangjie"] == "ArrayList" for r in d["results"]), d["results"][:3]


def test_resolve_java_code_multi_call():
    d = call("resolve_java_code", {
        "java_code": "HashMap<String,Integer> map = new HashMap<>();\nmap.put(k, v);\nmap.get(k);",
        "top_k": 3})
    assert d["suggestions"], "no suggestions"
    exprs = [sg.get("java_expr", "") for sg in d["suggestions"]]
    assert len(d["suggestions"]) >= 3, f"expected >=3 (incl. diamond ctor), got {len(d['suggestions'])}: {exprs}"
    put_sugg = [sg for sg in d["suggestions"] if "put" in sg.get("java_expr", "")]
    assert put_sugg, f"no put suggestion: {exprs}"
    assert put_sugg[0]["cangjie_type"] == "HashMap", put_sugg[0]
    ctor_sugg = [sg for sg in d["suggestions"] if sg.get("java_expr", "").startswith("new")]
    assert ctor_sugg, f"no ctor suggestion: {exprs}"
    assert ctor_sugg[0]["cangjie_type"] == "HashMap", ctor_sugg[0]


def test_resolve_java_code_nested_ctor():
    d = call("resolve_java_code", {
        "java_code": "BufferedReader reader = new BufferedReader(new InputStreamReader(System.in));",
        "top_k": 3})
    exprs = [sg.get("java_expr", "") for sg in d["suggestions"]]
    assert any("new BufferedReader" in e for e in exprs), exprs
    assert any("new InputStreamReader" in e for e in exprs), exprs


def test_resolve_java_code_degrade():
    # System.out.println: receiver cannot be traced to a declared type -> degrade
    d = call("resolve_java_code", {"java_code": "System.out.println(x);", "top_k": 3})
    assert d["suggestions"], "no suggestions"
    levels = {sg.get("level") for sg in d["suggestions"]}
    assert "api" not in levels or len(levels) > 1, f"unexpected api lock: {levels}"


def test_error_fix_hint():
    d = call("error_fix_hint", {"error_text": "cannot find symbol println in std.io", "top_k": 3})
    assert d["apis"] or d["examples"], "no results"


def test_find_examples():
    d = call("find_examples", {"query": "read file lines", "top_k": 3})
    assert d["results"], "no examples"


def test_describe_java_code():
    d = call("describe_java_code", {"java_code": "reader.readLine()"})
    assert d["nl"], "no nl"
    assert d["detected_level"] in ("api", "statement", "function")


def test_unknown_tool_error():
    resp = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "nonexistent", "arguments": {}}})
    assert resp["result"]["isError"]


def test_missing_arg_error():
    resp = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "search_api", "arguments": {}}})
    assert resp["result"]["isError"]


def test_rerank_disabled_keeps_bm25_order():
    from cjkb.reranker import rerank, _enabled
    recs = searcher.search_api("map put key value", top_k=10)
    llm_off = dict(cfg["llm"], rerank=False)
    assert not _enabled(llm_off)
    ordered = rerank("map put key value", recs, llm_off, top_k=10)
    assert [r.name for r in ordered] == [r.name for r in recs]


def test_rerank_parse_order_robustness():
    from cjkb.reranker import _parse_order
    assert _parse_order("[2,0,1]", 3) == [2, 0, 1]
    assert _parse_order("garbage", 3) is None
    assert _parse_order("[0,99,-1,0]", 3) is None  # filtered to <2 valid -> None
    assert _parse_order("[0]", 3) is None
    assert _parse_order("[0,1]", 3) == [0, 1]


def test_tokenize_chinese_unigram():
    from cjkb.index.bm25 import tokenize
    assert "读" in tokenize("读取文件所有行")
    assert "read" in tokenize("read_file_bytes")
    assert tokenize("ArrayList") == ["array", "list"]
