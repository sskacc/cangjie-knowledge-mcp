"""One-command knowledge-base setup for a fresh clone.

The committed repo ships the JSONL sources (apis.jsonl, examples.jsonl,
java_mappings.jsonl, modules.json) but NOT the binary BM25 index (.pkl) --
the index is a derived artifact that is regenerated locally (platform/Python
version safe). This script:

  1. verifies the JSONL sources are present,
  2. rebuilds the BM25 index if missing or stale (Searcher.load auto-rebuild),
  3. prints KB stats.

Usage:
    python scripts/install_kb.py [--data-dir data]
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cjkb.config import load_config                      # noqa: E402
from cjkb.index.searcher import Searcher                 # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Prepare the knowledge base for use")
    ap.add_argument("--data-dir", default="", help="KB data dir (default: config)")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "..", "config.yaml"))
    args = ap.parse_args()

    cfg = load_config(args.config)
    data_dir = args.data_dir or cfg["output"].get("data_dir", "data")

    missing = [f for f in ("apis.jsonl", "examples.jsonl", "java_mappings.jsonl")
               if not os.path.exists(os.path.join(data_dir, f))]
    if missing:
        print(f"[install] ERROR: knowledge base sources missing in {data_dir}: {missing}")
        print("  The repo ships the JSONL data. If you cloned without it, check")
        print("  that data/*.jsonl were not excluded by your checkout.")
        return 1

    # Searcher.load() rebuilds pkl automatically when missing/stale.
    s = Searcher.load(data_dir, cfg, auto_rebuild=True)
    print(f"[install] knowledge base ready: {s.kb.stats()}")
    print(f"[install] index files: bm25_apis.pkl, bm25_examples.pkl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
