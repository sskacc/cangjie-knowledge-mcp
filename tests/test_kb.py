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
from cjkb.models import KnowledgeBase                       # noqa: E402
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
               "output": {"data_dir": tempfile.mkdtemp()}}
        s = Searcher(kb, cfg).build()
        server = McpServer(s)

        resp = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "cangjie-knowledge-mcp")

        resp = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertIn("search_api", names)
        self.assertIn("error_fix_hint", names)
        self.assertIn("java_to_cangjie", names)

        resp = server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                      "params": {"name": "search_api",
                                                 "arguments": {"query": "put map key value"}}})
        self.assertFalse(resp["result"]["isError"])
        data = json.loads(resp["result"]["content"][0]["text"])
        self.assertGreaterEqual(len(data["results"]), 1)
        self.assertEqual(data["results"][0]["name"], "put")


if __name__ == "__main__":
    unittest.main()
