"""Unit tests for the LLM reranker (semantic reorder over BM25).

Covers: fallback-to-BM25-order when no key / disabled / LLM fails, the JSON
order parser, and candidate text formatting. No real LLM is contacted.
"""

from __future__ import annotations

import unittest
from unittest import mock

from cjkb.reranker import _parse_order, _candidate_texts, rerank
from cjkb.models import ApiRecord, ExampleRecord


def _apis():
    return [
        ApiRecord(name="add", kind="func", module="std.collection", library="std",
                  signature="public func add(element: T): Unit",
                  description="Adds an element."),
        ApiRecord(name="put", kind="func", module="std.collection", library="std",
                  signature="public func add(key: K, value: V): ?V",
                  description="Inserts a key-value pair."),
        ApiRecord(name="HashMap", kind="class", module="std.collection", library="std",
                  signature="public class HashMap<K,V>",
                  description="Hash table map."),
    ]


class TestParseOrder(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(_parse_order("[2, 0, 1]", 3), [2, 0, 1])

    def test_out_of_range_and_dupes_filtered(self):
        self.assertEqual(_parse_order("[0, 99, 1, 1, -1]", 3), [0, 1])

    def test_non_int_ignored(self):
        self.assertEqual(_parse_order('[0, "x", 1, null]', 3), [0, 1])

    def test_too_few_indices_returns_none(self):
        self.assertIsNone(_parse_order("[0]", 3))

    def test_bad_json_returns_none(self):
        self.assertIsNone(_parse_order("[0, 1", 3))
        self.assertIsNone(_parse_order("no array here", 3))

    def test_json_wrapped_in_text(self):
        # models often emit prose around the array
        self.assertEqual(_parse_order('Sure: [1, 2, 0] done.', 3), [1, 2, 0])


class TestRerankFallback(unittest.TestCase):
    def test_no_key_returns_original_order(self):
        apis = _apis()
        out = rerank("put key value", apis, {"base_url": "", "api_key": ""}, top_k=2)
        self.assertEqual([a.name for a in out], ["add", "put"])

    def test_disabled_returns_original_order(self):
        apis = _apis()
        out = rerank("put key value", apis,
                     {"api_key": "k", "rerank": False}, top_k=2)
        self.assertEqual([a.name for a in out], ["add", "put"])

    def test_llm_failure_returns_original_order(self):
        apis = _apis()
        cfg = {"base_url": "http://127.0.0.1:1", "api_key": "k", "model": "m"}
        out = rerank("put key value", apis, cfg, top_k=2)
        self.assertEqual([a.name for a in out], ["add", "put"])

    def test_single_record_no_llm_call(self):
        apis = _apis()[:1]
        with mock.patch("cjkb.reranker._call_llm") as m:
            out = rerank("x", apis, {"api_key": "k"}, top_k=1)
        m.assert_not_called()
        self.assertEqual(len(out), 1)


class TestRerankSuccess(unittest.TestCase):
    def test_reorders_by_llm_permutation(self):
        apis = _apis()
        cfg = {"api_key": "k", "model": "m", "base_url": "https://x/v1"}
        with mock.patch("cjkb.reranker._call_llm", return_value=[1, 2, 0]) as m:
            out = rerank("put key value", apis, cfg, top_k=3)
        m.assert_called_once()
        self.assertEqual([a.name for a in out], ["put", "HashMap", "add"])

    def test_top_k_truncates_after_rerank(self):
        apis = _apis()
        cfg = {"api_key": "k"}
        with mock.patch("cjkb.reranker._call_llm", return_value=[2, 1, 0]):
            out = rerank("map", apis, cfg, top_k=2)
        self.assertEqual([a.name for a in out], ["HashMap", "put"])

    def test_dropped_indices_are_reappended(self):
        # model returns only [2]; index 0,1 must still appear after
        apis = _apis()
        cfg = {"api_key": "k"}
        with mock.patch("cjkb.reranker._call_llm", return_value=[2]):
            out = rerank("map", apis, cfg, top_k=3)
        self.assertEqual([a.name for a in out], ["HashMap", "add", "put"])


class TestCandidateTexts(unittest.TestCase):
    def test_api_formatting(self):
        txt = _candidate_texts(_apis())
        self.assertIn("0)", txt)
        self.assertIn("put", txt)
        self.assertIn("std.collection", txt)

    def test_example_formatting(self):
        exs = [ExampleRecord(title="Read lines", code="let f = File(...)",
                             module="std.io", description="Reads a file.")]
        txt = _candidate_texts(exs)
        self.assertIn("Read lines", txt)
        self.assertIn("std.io", txt)


if __name__ == "__main__":
    unittest.main()
