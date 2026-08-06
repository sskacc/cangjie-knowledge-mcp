"""Data models for the Cangjie knowledge base.

Every record in the knowledge base is one of:
  - ApiRecord:    a function / class / interface / enum / macro / property
  - ExampleRecord: a runnable (or illustrative) Cangjie snippet
  - JavaMapping:  a Java symbol -> Cangjie symbol / concept mapping
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ApiRecord:
    """One documented API entry (function, class, interface, enum, prop, macro).

    ``kind`` is one of: "class", "interface", "enum", "struct", "func",
    "macro", "prop", "init", "exception", "typealias".
    """

    name: str                      # e.g. "ArrayList", "add", "HashMap"
    kind: str                      # class / func / ...
    module: str                    # e.g. "std.collection"
    library: str                   # "std" | "stdx" | "j2cjlib" | "manual"
    signature: str = ""            # full declaration line (or code block)
    description: str = ""          # Chinese/English description from docs
    params: List[Dict[str, str]] = field(default_factory=list)
    returns: str = ""
    exceptions: List[str] = field(default_factory=list)
    parent: str = ""               # for members: owning class/interface
    source: str = ""               # source file path (relative)
    source_url: str = ""           # where it came from (git repo / docs path)
    examples: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)  # e.g. ["generics", "collection"]
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ApiRecord":
        return ApiRecord(**{k: v for k, v in d.items() if k in ApiRecord.__dataclass_fields__})


@dataclass
class ExampleRecord:
    """A code example snippet."""

    title: str
    code: str
    module: str = ""
    library: str = "std"
    source: str = ""               # file path
    description: str = ""
    tags: List[str] = field(default_factory=list)
    generated: bool = False        # True when produced by the LLM example writer

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ExampleRecord":
        return ExampleRecord(**{k: v for k, v in d.items() if k in ExampleRecord.__dataclass_fields__})


@dataclass
class JavaMapping:
    """A Java symbol mapped to its Cangjie equivalent / translation notes."""

    java_symbol: str               # e.g. "java.util.List", "Thread.sleep"
    cangjie_symbol: str            # e.g. "std.collection.ArrayList", "sleep"
    source: str = ""               # provenance: j2cjlib file, terms yaml, manual
    notes: str = ""
    library: str = "j2cjlib"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "JavaMapping":
        return JavaMapping(**{k: v for k, v in d.items() if k in JavaMapping.__dataclass_fields__})


class KnowledgeBase:
    """In-memory container for all records; serialized to JSONL."""

    def __init__(self) -> None:
        self.apis: List[ApiRecord] = []
        self.examples: List[ExampleRecord] = []
        self.mappings: List[JavaMapping] = []
        self.modules: Dict[str, Dict[str, Any]] = {}   # module -> metadata

    # -- serialization -------------------------------------------------
    def to_jsonl(self, base_path: str) -> None:
        import os
        os.makedirs(base_path, exist_ok=True)
        with open(os.path.join(base_path, "apis.jsonl"), "w", encoding="utf-8") as f:
            for r in self.apis:
                f.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
        with open(os.path.join(base_path, "examples.jsonl"), "w", encoding="utf-8") as f:
            for r in self.examples:
                f.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
        with open(os.path.join(base_path, "java_mappings.jsonl"), "w", encoding="utf-8") as f:
            for r in self.mappings:
                f.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")
        with open(os.path.join(base_path, "modules.json"), "w", encoding="utf-8") as f:
            json.dump(self.modules, f, ensure_ascii=False, indent=2)

    @staticmethod
    def from_jsonl(base_path: str) -> "KnowledgeBase":
        import os
        kb = KnowledgeBase()
        for fn, builder in (("apis.jsonl", ApiRecord.from_dict),
                            ("examples.jsonl", ExampleRecord.from_dict),
                            ("java_mappings.jsonl", JavaMapping.from_dict)):
            p = os.path.join(base_path, fn)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        kb._append(builder(json.loads(line)))
        mf = os.path.join(base_path, "modules.json")
        if os.path.exists(mf):
            with open(mf, encoding="utf-8") as f:
                kb.modules = json.load(f)
        return kb

    def _append(self, rec: Any) -> None:
        if isinstance(rec, ApiRecord):
            self.apis.append(rec)
        elif isinstance(rec, ExampleRecord):
            self.examples.append(rec)
        elif isinstance(rec, JavaMapping):
            self.mappings.append(rec)

    def stats(self) -> Dict[str, int]:
        return {
            "apis": len(self.apis),
            "examples": len(self.examples),
            "java_mappings": len(self.mappings),
            "modules": len(self.modules),
        }
