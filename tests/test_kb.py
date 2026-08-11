"""Unit tests for the knowledge base core (tokenizer, BM25, parser, MCP)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cjkb.index.bm25 import BM25Index, tokenize            # noqa: E402
from cjkb.collector.corpus_parser import _parse_api_doc, _parse_sample_doc, split_code_blocks  # noqa: E402
from cjkb.models import KnowledgeBase, JavaMapping             # noqa: E402
from cjkb.index.searcher import Searcher                    # noqa: E402


class TestTokenize(unittest.TestCase):
    def test_camel_split(self):
        self.assertIn("array", tokenize("ArrayList"))
        self.assertIn("list", tokenize("ArrayList"))

    def test_snake_and_space(self):
        toks = tokenize("read_file_bytes and 123x")
        self.assertIn("read", toks)
        self.assertIn("file", toks)
        self.assertIn("bytes", toks)

    def test_identifier(self):
        toks = tokenize("getOrThrow")
        self.assertIn("get", toks)
        self.assertIn("throw", toks)
        self.assertNotIn("getor", toks)


class TestBM25(unittest.TestCase):
    def test_rank(self):
        idx = BM25Index()
        idx.build([
            "ArrayList add element collection",
            "HashMap put key value map",
            "File read bytes from disk",
        ])
        hits = idx.search("add element list")
        self.assertTrue(hits)
        top = hits[0][0]
        self.assertEqual(top, 0)


class TestParser(unittest.TestCase):
    def test_api_doc_class(self):
        md = """# x

## class ArrayDeque<T>

```cangjie
public class ArrayDeque<T> <: Deque<T> {
    public init()
}
```

功能：双端队列实现类。

### prop capacity

```cangjie
public prop capacity: Int64
```

功能：获取容量。
"""
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(md)
            path = f.name
        try:
            recs = _parse_api_doc(path, "std.collection", "std", "")
            self.assertEqual(len(recs), 2)
            cls = recs[0]
            self.assertEqual(cls.kind, "class")
            self.assertEqual(cls.name, "ArrayDeque")
            prop = recs[1]
            self.assertEqual(prop.kind, "prop")
            self.assertEqual(prop.parent, "ArrayDeque")
        finally:
            os.unlink(path)

    def test_sample_doc(self):
        md = """# ArrayList 的 add 函数

说明文字。

```cangjie
import std.collection.*
main() { let a = ArrayList<Int64>([1,2,3]); print(a) }
```
"""
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write(md)
            path = f.name
        try:
            recs = _parse_sample_doc(path, "std.collection", "std", "", "sample")
            self.assertEqual(len(recs), 1)
            self.assertIn("ArrayList", recs[0].code)
        finally:
            os.unlink(path)

    def test_code_blocks(self):
        blocks = split_code_blocks("a\n```cangjie\nx\ny\n```\nb\n```text\nout\n```\n")
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0][0], "cangjie")


class TestSearcher(unittest.TestCase):
    def _make_kb(self) -> KnowledgeBase:
        kb = KnowledgeBase()
        from cjkb.models import ApiRecord, ExampleRecord, JavaMapping
        kb.apis.append(ApiRecord(name="add", kind="func", module="std.collection", library="std",
                                 signature="public func add(element: T): Unit",
                                 description="Adds an element to the collection."))
        kb.apis.append(ApiRecord(name="ArrayList", kind="class", module="std.collection", library="std",
                                 signature="public class ArrayList<T>",
                                 description="Resizable array implementation."))
        kb.examples.append(ExampleRecord(title="ArrayList add", code="let a = ArrayList<Int64>([1,2])",
                                         module="std.collection"))
        kb.mappings.append(JavaMapping(java_symbol="java.util.List", cangjie_symbol="ArrayList",
                                       source="j2cjlib"))
        kb.modules["std.collection"] = {"apis": 2, "examples": 1}
        return kb

    def test_search_and_lookup(self):
        kb = self._make_kb()
        cfg = {"index": {"field_weights": {"name": 4.0, "module": 2.0, "signature": 3.0,
                                           "description": 1.0, "tags": 1.5},
                         "top_k": 10, "min_score": 0.01},
               "output": {"data_dir": tempfile.mkdtemp()}}
        s = Searcher(kb, cfg).build()
        self.assertEqual(len(s.search_api("ArrayList")), 1)
        self.assertTrue(s.get_api_details("ArrayList"))
        self.assertEqual(s.java_to_cangjie("java.util.list")[0].cangjie_symbol, "ArrayList")
        s.save()
        s2 = Searcher.load(cfg["output"]["data_dir"], cfg)
        self.assertTrue(s2.get_api_details("add"))


class TestMcpProtocol(unittest.TestCase):
    def test_tools_list_and_call(self):
        from cjkb.mcp_server import McpServer
        from cjkb.models import ApiRecord, KnowledgeBase
        from cjkb.index.searcher import Searcher

        kb = KnowledgeBase()
        kb.apis.append(ApiRecord(name="put", kind="func", module="std.collection", library="std",
                                 signature="public func put(key: K, value: V): ?V",
                                 description="Inserts a key-value pair into the map."))
        cfg = {"index": {"field_weights": {"name": 4.0, "module": 2.0, "signature": 3.0,
                                           "description": 1.0, "tags": 1.5},
                         "top_k": 10, "min_score": 0.01},
               "output": {"data_dir": tempfile.mkdtemp()},
               "llm": {}}
        s = Searcher(kb, cfg).build()
        server = McpServer(s, cfg)

        resp = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "cangjie-knowledge-mcp")

        resp = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertIn("search_api", names)
        self.assertIn("error_fix_hint", names)
        self.assertIn("java_to_cangjie", names)
        self.assertIn("resolve_java_code", names)
        self.assertIn("describe_java_code", names)

        resp = server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                      "params": {"name": "search_api",
                                                 "arguments": {"query": "put map key value"}}})
        self.assertFalse(resp["result"]["isError"])
        data = json.loads(resp["result"]["content"][0]["text"])
        self.assertGreaterEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["name"], "put")


class TestNlGenerator(unittest.TestCase):
    def test_detect_level(self):
        from cjkb.nl_generator import detect_level
        self.assertEqual(detect_level("reader.readLine()"), "api")
        self.assertEqual(detect_level("String s = new String(bytes);"), "statement")
        self.assertEqual(detect_level("public void f() { int x = 1; return; }"), "function")

    def test_heuristic_no_llm(self):
        from cjkb.nl_generator import generate_nl, generate_layered
        nl = generate_nl("reader.readLine()", "api", {})
        self.assertIn("read", nl["en"].lower())
        # layered always returns 3 levels
        layers = generate_layered("public void copy() { reader.readLine(); }", {})
        self.assertEqual(set(layers.keys()), {"api", "statement", "function"})
        for lvl in layers.values():
            self.assertIn("en", lvl)
            self.assertIn("zh", lvl)


class TestJavaTypes(unittest.TestCase):
    def test_extract_declaration(self):
        from cjkb.java_types import extract_types
        r = extract_types("HashMap<String, Integer> map = new HashMap<>(); map.put(k, v);")
        self.assertIn("HashMap", r["types"])
        self.assertIn("String", r["types"])
        self.assertEqual(r["var_types"].get("map"), "HashMap<String, Integer>")
        calls = [c for c in r["calls"] if c["method"] == "put"]
        self.assertEqual(calls[0]["receiver"], "map")
        self.assertEqual(calls[0]["declared_simple"], "HashMap")

    def test_extract_fqn_and_cast(self):
        from cjkb.java_types import extract_types
        r = extract_types("java.util.List<String> x = (java.util.List) y;")
        self.assertIn("java.util.List", r["types"])
        self.assertIn("List", r["types"])

    def test_extract_method_ret(self):
        from cjkb.java_types import extract_types
        r = extract_types("public Map<String,Integer> getMap() { return null; }")
        self.assertIn("Map", r["types"])


class TestLayeredSearch(unittest.TestCase):
    def test_layered_search_finds_best_level(self):
        from cjkb.layered_search import layered_search
        from cjkb.models import ApiRecord, ExampleRecord, KnowledgeBase
        from cjkb.index.searcher import Searcher

        kb = KnowledgeBase()
        kb.apis.append(ApiRecord(name="readln", kind="func", module="std.io", library="std",
                                 signature="public func readln(): Option<String>",
                                 description="按行读取流中的数据。" * 3))
        kb.apis.append(ApiRecord(name="readToEnd", kind="func", module="std.io", library="std",
                                 signature="public func readToEnd(): String",
                                 description="读取流中所有剩余数据。" * 3))
        kb.apis.append(ApiRecord(name="StringReader", kind="class", module="std.io", library="std",
                                 signature="public class StringReader<T>",
                                 description="字符输入流读取器。" * 3))
        kb.examples.append(ExampleRecord(title="read lines", code="for (line in reader.lines()) {}",
                                         module="std.io"))
        cfg = {"index": {"field_weights": {"name": 4.0, "module": 2.0, "signature": 3.0,
                                           "description": 1.0, "tags": 1.5},
                         "top_k": 10, "min_score": 0.01},
               "output": {"data_dir": tempfile.mkdtemp()}, "llm": {}}
        s = Searcher(kb, cfg).build()
        res = layered_search(s, "StringReader reader = new StringReader(in); String line = reader.readLine();",
                             cfg, module="std.io", top_k=3)
        self.assertIn("best_level", res)
        self.assertIn("levels", res)
        for lvl in ("api", "statement", "function"):
            self.assertIn(lvl, res["levels"])
        # stage 1: type locking should find StringReader as a candidate
        self.assertIn("java_types", res)
        self.assertIn("type_candidates", res)
        self.assertTrue(any(c["cangjie_type"] == "StringReader"
                            for c in res["type_candidates"]))
        # stage 2 cross-validation: StringReader members must be retrievable
        self.assertIsNotNone(res["best_hit"])

    def test_layered_search_suggested(self):
        from cjkb.layered_search import layered_search
        from cjkb.models import ApiRecord, KnowledgeBase
        from cjkb.index.searcher import Searcher

        kb = KnowledgeBase()
        kb.apis.append(ApiRecord(name="HashMap", kind="class", module="std.collection", library="std",
                                 signature="public class HashMap<K, V>",
                                 description="哈希映射。" * 3))
        kb.apis.append(ApiRecord(name="add(K, V)", kind="func", module="std.collection", library="std",
                                 signature="public func add(key: K, value: V): ?V",
                                 description="插入键值对。" * 3, parent="HashMap"))
        kb.apis.append(ApiRecord(name="replace(K, V)", kind="func", module="std.collection", library="std",
                                 signature="public func replace(key: K, value: V): ?V",
                                 description="覆盖已有键的值。" * 3, parent="HashMap"))
        kb.mappings.append(JavaMapping(java_symbol="HashMap", cangjie_symbol="HashMap",
                                       source="test", library="type_resolution"))
        cfg = {"index": {"field_weights": {"name": 4.0, "module": 2.0, "signature": 3.0,
                                           "description": 1.0, "tags": 1.5},
                         "top_k": 10, "min_score": 0.01},
               "output": {"data_dir": tempfile.mkdtemp()}, "llm": {}}
        s = Searcher(kb, cfg).build()
        res = layered_search(s, "HashMap<String,Integer> map = new HashMap<>(); map.put(k, v);",
                             cfg, top_k=3)
        self.assertIsNotNone(res["suggested"])
        sug = res["suggested"]
        self.assertEqual(sug["cangjie_type"], "HashMap")
        names = [m.name for m in sug["members"]]
        self.assertIn("add(K, V)", names)
        self.assertIn("replace(K, V)", names)


if __name__ == "__main__":
    unittest.main()
