"""Interactive demo / smoke-test of the knowledge base search.

Usage:
    python scripts/query_demo.py "HashMap put"
    python scripts/query_demo.py --details ArrayList
    python scripts/query_demo.py --examples "read file lines"
    python scripts/query_demo.py --java "java.util.List"
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cjkb.config import load_config          # noqa: E402
from cjkb.index.searcher import Searcher     # noqa: E402


def pprint_api(r) -> None:
    print(f"\n  [{r.kind}] {r.name}   ({r.module}, lib={r.library})")
    if r.signature:
        print(f"    signature: {r.signature}")
    if r.description:
        print(f"    desc: {r.description[:180]}")
    if r.parent:
        print(f"    parent: {r.parent}")
    if r.examples:
        print(f"    example: {r.examples[0][:200]}...")
    print(f"    source: {r.source}")


def pprint_ex(e) -> None:
    print(f"\n  * {e.title}  ({e.module})")
    print(f"    {e.code[:400]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", help="search query")
    ap.add_argument("--details", help="exact API lookup by name")
    ap.add_argument("--class-members", help="list members of a class")
    ap.add_argument("--examples", help="find examples")
    ap.add_argument("--java", help="Java symbol -> Cangjie")
    ap.add_argument("--modules", action="store_true")
    ap.add_argument("--data-dir", default="", help="built KB data dir")
    args = ap.parse_args()

    cfg = load_config()
    data_dir = args.data_dir or cfg["output"].get("data_dir", "data")
    if not os.path.exists(os.path.join(data_dir, "apis.jsonl")):
        print(f"KB not found at {data_dir}; run scripts/build_kb.py first.")
        return 1
    s = Searcher.load(data_dir, cfg)

    if args.modules:
        for m in s.list_modules():
            print(m)
        return 0
    if args.details:
        for r in s.get_api_details(args.details):
            pprint_api(r)
        return 0
    if args.class_members:
        for r in s.get_class_members(args.class_members):
            pprint_api(r)
        return 0
    if args.examples:
        for e in s.find_examples(args.examples):
            pprint_ex(e)
        return 0
    if args.java:
        for m in s.java_to_cangjie(args.java):
            print(f"  {m.java_symbol} -> {m.cangjie_symbol}  [{m.source}]")
        return 0
    if not args.query:
        ap.print_help()
        return 0

    print(f"=== search_api: {args.query} ===")
    for r in s.search_api(args.query):
        pprint_api(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
