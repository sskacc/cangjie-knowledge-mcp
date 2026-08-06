"""Collector: build Java -> Cangjie mappings.

Two sources:
  1. j2cjlib: hand-written Cangjie shims that mirror Java classes
     (misc/j2cjlib/src/mappings/{io,lang,sync,util}/*.cj and top-level misc/*.cj).
     The directory name mirrors the Java package (lang -> java.lang etc.).
  2. java_cangjie_terms.yaml: term-level glossary used for query expansion.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from cjkb.models import JavaMapping, KnowledgeBase

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

JAVA_PKG = {"io": "java.io", "lang": "java.lang", "util": "java.util", "sync": "java.util.concurrent"}

# public open class J2CjThread <: J2CjRunnable & ...  /  public interface HashableImpl
CLASS_RE = re.compile(
    r"^public\s+(?:(?P<cls>open\s+)?(class|interface|enum|struct|trait)\s+"
    r"(?P<name>[A-Za-z_]\w*))",
    re.MULTILINE,
)
FUNC_RE = re.compile(
    r"^public\s+(?:(?P<static>static)\s+)?(?:open\s+)?(?:func|prop)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*(?P<sig>\([^)]*\))?",
    re.MULTILINE,
)


def _class_decls(cj: str) -> List[str]:
    return [m.group("name") for m in CLASS_RE.finditer(cj)]


def _func_decls(cj: str) -> List[str]:
    return [m.group("name") for m in FUNC_RE.finditer(cj)]


def _package_of(cj_path: str) -> str:
    """Read the `package xxx` line from a .cj file."""
    try:
        with open(cj_path, encoding="utf-8") as f:
            head = f.read(4000)
    except OSError:
        return ""
    m = re.search(r"^package\s+([\w.]+)\s*$", head, re.M)
    return m.group(1) if m else ""


def collect_j2cjlib(j2cjlib_dir: str) -> List[JavaMapping]:
    """Parse j2cjlib .cj shims into Java -> Cangjie mappings."""
    mappings: List[JavaMapping] = []
    if not j2cjlib_dir or not os.path.isdir(j2cjlib_dir):
        return mappings

    for dirpath, _dirs, files in os.walk(j2cjlib_dir):
        for fn in sorted(files):
            if not fn.endswith(".cj"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue

            rel = os.path.relpath(dirpath, j2cjlib_dir).replace(os.sep, "/")
            pkg_parts = [p for p in rel.split("/") if p and p != "src" and p != "mappings"]
            java_pkg = JAVA_PKG.get(pkg_parts[-1], "java.util") if pkg_parts else "java.util"
            pkg = _package_of(path)

            for cls in _class_decls(content):
                java_sym = f"{java_pkg}.{cls}"
                cangjie_sym = f"{pkg}.{cls}" if pkg else cls
                mappings.append(JavaMapping(
                    java_symbol=java_sym,
                    cangjie_symbol=cangjie_sym,
                    source=os.path.relpath(path, j2cjlib_dir),
                    notes="j2cjlib shim: class",
                    library="j2cjlib",
                ))
            for fname in _func_decls(content):
                cangjie_sym = fname
                java_sym = fname
                mappings.append(JavaMapping(
                    java_symbol=java_sym,
                    cangjie_symbol=cangjie_sym,
                    source=os.path.relpath(path, j2cjlib_dir),
                    notes="j2cjlib shim: member function",
                    library="j2cjlib",
                ))
    return mappings


def collect_terms(terms_yaml: str) -> List[JavaMapping]:
    """Read java_cangjie_terms.yaml (Java term -> [Cangjie terms])."""
    mappings: List[JavaMapping] = []
    if not terms_yaml or not os.path.exists(terms_yaml):
        return mappings
    if yaml is None:
        return mappings
    with open(terms_yaml, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    for java_term, cangjie_terms in data.items():
        if isinstance(cangjie_terms, list):
            for ct in cangjie_terms:
                mappings.append(JavaMapping(
                    java_symbol=str(java_term),
                    cangjie_symbol=str(ct),
                    source=os.path.basename(terms_yaml),
                    notes="terminology glossary",
                    library="terms",
                ))
    return mappings


def collect_j2c(j2cjlib_dir: str, terms_yaml: str) -> List[JavaMapping]:
    return collect_j2cjlib(j2cjlib_dir) + collect_terms(terms_yaml)
