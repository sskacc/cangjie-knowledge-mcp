"""Build the knowledge base: collect -> index -> write data/."""

from __future__ import annotations

import argparse
import os
import sys

# allow running from anywhere without install
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cjkb.config import load_config                      # noqa: E402
from cjkb.collector.corpus_parser import collect_corpus  # noqa: E402
from cjkb.collector.j2cj_parser import collect_j2c       # noqa: E402
from cjkb.index.searcher import Searcher                 # noqa: E402
from cjkb.models import KnowledgeBase                    # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the Cangjie knowledge base")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "..", "config.yaml"))
    ap.add_argument("--corpus", default="", help="override CangjieCorpus dir")
    ap.add_argument("--j2cjlib", default="", help="override j2cjlib dir")
    ap.add_argument("--terms", default="", help="override java_cangjie_terms.yaml")
    ap.add_argument("--data-dir", default="", help="override output data dir")
    ap.add_argument("--write-examples", action="store_true",
                    help="use LLM to generate examples for APIs missing them")
    ap.add_argument("--example-limit", type=int, default=20)
    args = ap.parse_args()

    cfg = load_config(args.config)
    corpus = args.corpus or cfg["corpus"].get("cangjie_corpus", "")
    j2cjlib = args.j2cjlib or cfg["corpus"].get("j2cjlib", "")
    terms = args.terms or cfg["corpus"].get("java_terms", "")
    data_dir = args.data_dir or cfg["output"].get("data_dir", "data")

    if not corpus or not os.path.isdir(corpus):
        print(f"[build_kb] ERROR: corpus dir not found: {corpus!r}")
        print("  Set corpus.cangjie_corpus in config.yaml or pass --corpus.")
        return 1

    print(f"[build_kb] collecting from corpus: {corpus}")
    kb = collect_corpus(corpus, data_dir)
    print(f"[build_kb] corpus parsed: {kb.stats()}")

    mappings = collect_j2c(j2cjlib if os.path.isdir(j2cjlib) else "", terms)
    kb.mappings = mappings
    print(f"[build_kb] java->cangjie mappings: {len(mappings)}")

    if args.write_examples:
        from cjkb.collector.example_writer import write_examples
        # skip already-generated examples so rebuilds are incremental
        skip_titles = set()
        ex_path = os.path.join(data_dir, "examples.jsonl")
        if os.path.exists(ex_path):
            import json as _json
            with open(ex_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = _json.loads(line)
                    if rec.get("generated"):
                        skip_titles.add(rec.get("title", ""))
        gen = write_examples(kb.apis, cfg["llm"], limit=args.example_limit,
                             skip_titles=skip_titles)
        kb.examples.extend(gen)
        print(f"[build_kb] generated examples: {len(gen)} (new) / {len(skip_titles)} (already existing)")

    searcher = Searcher(kb, cfg, data_dir=data_dir)
    searcher.build()
    searcher.save()
    print(f"[build_kb] knowledge base saved to {data_dir}")
    print(f"[build_kb] DONE: {kb.stats()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
