"""Import x2cangjie type-translation results into the knowledge base.

Reads `data/java/type_resolution/*.json` (produced by translate_type_rag.py)
and appends every Java->Cangjie mapping to `data/java_mappings.jsonl` with
source=type_resolution, then rebuilds the BM25 index.

Sources consumed:
  - java_base_type_map.json      {fqn: {category, mapping, reasoning}}
  - java_generic_type_map.json   {typeName: {cangjie, arity, raw_args}}
  - universal_type_map_final.json {javaType: cangjieType}   (may contain dup keys)
  - fixed_type_map.json          {javaType: cangjieType}

Usage:
    python scripts/import_type_mappings.py \
        --type-resolution <x2cangjie路径>/data/java/type_resolution \
        --data-dir data
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cjkb.config import load_config                      # noqa: E402
from cjkb.index.searcher import Searcher                 # noqa: E402
from cjkb.models import JavaMapping, KnowledgeBase       # noqa: E402


def _load_json_lenient(path: str):
    """Load JSON allowing duplicate keys (last wins)."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass  # fall through to duplicate-key-tolerant loader
    import re
    out = {}
    for m in re.finditer(r'"([^"]+)"\s*:\s*("(?:[^"\\]|\\.)*"|\[[^\]]*\]|\{[^{}]*\}|-?\d+(?:\.\d+)?|true|false|null)',
                         text):
        key, raw = m.group(1), m.group(2)
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw.strip('"')
    return out


def collect_mappings(type_res_dir: str) -> list:
    """Extract JavaMapping records from all type_resolution json files."""
    mappings = []
    seen = set()

    def _add(java_type: str, cangjie_type: str, source: str, notes: str = "") -> None:
        if not java_type or not cangjie_type:
            return
        # normalize: strip generics for the lookup key, keep raw as java_symbol
        key = (java_type.strip(), cangjie_type.strip())
        if key in seen:
            return
        seen.add(key)
        mappings.append(JavaMapping(
            java_symbol=java_type.strip(),
            cangjie_symbol=cangjie_type.strip(),
            source=source,
            notes=notes[:200],
            library="type_resolution",
        ))

    # 1) java_base_type_map.json: fqn -> {category, mapping, reasoning}
    p = os.path.join(type_res_dir, "java_base_type_map.json")
    if os.path.exists(p):
        for fqn, info in _load_json_lenient(p).items():
            if isinstance(info, dict) and info.get("mapping"):
                _add(fqn, info["mapping"], "java_base_type_map.json",
                     info.get("reasoning", "") or "")
                # also add the simple name for lookups that drop the package
                if "." in fqn:
                    _add(fqn.split(".")[-1], info["mapping"], "java_base_type_map.json",
                         "short name of " + fqn)

    # 2) java_generic_type_map.json: typeName -> {cangjie, arity, raw_args}
    p = os.path.join(type_res_dir, "java_generic_type_map.json")
    if os.path.exists(p):
        for tname, info in _load_json_lenient(p).items():
            if isinstance(info, dict) and info.get("cangjie"):
                _add(tname, info["cangjie"], "java_generic_type_map.json")

    # 3) universal_type_map_final.json: javaType -> cangjieType
    p = os.path.join(type_res_dir, "universal_type_map_final.json")
    if os.path.exists(p):
        for jt, ct in _load_json_lenient(p).items():
            if isinstance(ct, str):
                _add(jt, ct, "universal_type_map_final.json")

    # 4) fixed_type_map.json: javaType -> cangjieType
    p = os.path.join(type_res_dir, "fixed_type_map.json")
    if os.path.exists(p):
        for jt, ct in _load_json_lenient(p).items():
            if isinstance(ct, str):
                _add(jt, ct, "fixed_type_map.json")

    return mappings


def main() -> int:
    ap = argparse.ArgumentParser(description="Import x2cangjie type-translation results")
    ap.add_argument("--type-resolution", required=True,
                    help="path to x2cangjie data/java/type_resolution")
    ap.add_argument("--data-dir", default="", help="KB data dir (default: config)")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "..", "config.yaml"))
    args = ap.parse_args()

    if not os.path.isdir(args.type_resolution):
        print(f"[import] ERROR: type_resolution dir not found: {args.type_resolution}")
        return 1

    cfg = load_config(args.config)
    data_dir = args.data_dir or cfg["output"].get("data_dir", "data")
    if not os.path.exists(os.path.join(data_dir, "apis.jsonl")):
        print(f"[import] KB not found at {data_dir}; run scripts/build_kb.py first.")
        return 1

    kb = KnowledgeBase.from_jsonl(data_dir)
    before = len(kb.mappings)
    new_mappings = collect_mappings(args.type_resolution)
    existing_keys = {(m.java_symbol, m.cangjie_symbol) for m in kb.mappings}
    added = [m for m in new_mappings
             if (m.java_symbol, m.cangjie_symbol) not in existing_keys]
    kb.mappings.extend(added)
    print(f"[import] type_resolution mappings: {len(new_mappings)} "
          f"(added {len(added)}, skipped {len(new_mappings) - len(added)} dup)")
    print(f"[import] total java_mappings now: {len(kb.mappings)} (was {before})")

    searcher = Searcher(kb, cfg, data_dir=data_dir)
    searcher.build()
    searcher.save()
    print(f"[import] index rebuilt and saved to {data_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
