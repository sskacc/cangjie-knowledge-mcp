"""Generate examples with an LLM for every API missing one.

A standalone sibling of build_kb.py --write-examples that works directly on
the committed knowledge base (data/), WITHOUT re-collecting the corpus:

  - reads the existing data/*.jsonl (never overwrites manual edits)
  - finds APIs that have no official example and no generated one yet
  - calls the configured LLM to write a Cangjie example for each
  - appends them to data/examples.jsonl with "generated": true
  - rebuilds the BM25 index so find_examples() sees the new examples

LLM config comes from config.yaml `llm:` or the env vars:
    OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL

Usage:
    python scripts/generate_examples.py --limit 50
    python scripts/generate_examples.py --dry-run          # list missing only
    python scripts/generate_examples.py --limit 0          # generate ALL missing
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cjkb.config import load_config                      # noqa: E402
from cjkb.collector.example_writer import write_examples  # noqa: E402
from cjkb.index.searcher import Searcher                  # noqa: E402
from cjkb.models import KnowledgeBase                     # noqa: E402


def _load_generated_titles(data_dir: str) -> set:
    """Titles of examples already marked generated=True (for incremental runs)."""
    titles = set()
    path = os.path.join(data_dir, "examples.jsonl")
    if not os.path.exists(path):
        return titles
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("generated"):
                titles.add(rec.get("title", ""))
    return titles


def _missing_count(kb: KnowledgeBase, generated_titles: set) -> int:
    """APIs lacking both an official example and a generated one (deduped)."""
    seen = set()
    n = 0
    for api in kb.apis:
        if api.examples:
            continue
        key = (api.module, api.name)
        if key in seen:
            continue
        seen.add(key)
        title = f"{api.module}.{api.name} (generated)"
        if title not in generated_titles:
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate LLM examples for missing APIs")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "..", "config.yaml"))
    ap.add_argument("--data-dir", default="", help="KB data dir (default: config)")
    ap.add_argument("--limit", type=int, default=20,
                    help="max examples to generate this run (0 = all missing)")
    ap.add_argument("--dry-run", action="store_true",
                    help="only report how many APIs are missing examples, don't generate")
    ap.add_argument("--skip-rebuild", action="store_true",
                    help="don't rebuild the BM25 index after appending")
    args = ap.parse_args()

    cfg = load_config(args.config)
    data_dir = args.data_dir or cfg["output"].get("data_dir", "data")
    if not os.path.exists(os.path.join(data_dir, "apis.jsonl")):
        print(f"[generate] ERROR: knowledge base not found at {data_dir}")
        return 1

    kb = KnowledgeBase.from_jsonl(data_dir)
    generated_titles = _load_generated_titles(data_dir)
    missing = _missing_count(kb, generated_titles)
    print(f"[generate] apis={len(kb.apis)} examples={len(kb.examples)} "
          f"(generated so far: {len(generated_titles)})")
    print(f"[generate] APIs missing an example: {missing}")

    if args.dry_run:
        print("[generate] dry run: nothing generated. "
              f"Use `--limit {missing}` or `--limit 0` to fill them all.")
        return 0

    limit = args.limit
    if limit == 0:
        limit = missing
    if limit <= 0:
        print("[generate] nothing to do.")
        return 0

    if not cfg.get("llm", {}).get("api_key"):
        print("[generate] ERROR: no LLM configured. Set OPENAI_API_KEY (and "
              "OPENAI_BASE_URL / OPENAI_MODEL) or config.yaml llm: section.")
        return 1

    print(f"[generate] generating up to {limit} examples with LLM "
          f"({cfg['llm'].get('model', 'default')})...")
    new_examples = write_examples(kb.apis, cfg["llm"], limit=limit,
                                  skip_titles=generated_titles)
    if not new_examples:
        print("[generate] no examples generated (LLM failures or all skipped).")
        return 1

    kb.examples.extend(new_examples)
    kb.to_jsonl(data_dir)  # rewrites all jsonl, preserving manual edits
    print(f"[generate] appended {len(new_examples)} generated examples; "
          f"examples now {len(kb.examples)}")

    if not args.skip_rebuild:
        searcher = Searcher(kb, cfg, data_dir=data_dir)
        searcher.build()
        searcher.save()
        print(f"[generate] BM25 index rebuilt in {data_dir}")

    print(f"[generate] done. Next run will skip these "
          f"({len(generated_titles) + len(new_examples)} generated total).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
