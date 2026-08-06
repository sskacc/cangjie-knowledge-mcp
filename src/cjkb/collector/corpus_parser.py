"""Collector: parse CangjieCorpus markdown docs into ApiRecord/ExampleRecord.

Corpus layout (https://gitcode.com/Cangjie/cangjie_runtime, mirrored locally):

    <corpus>/libs/std/<module>/<module>_package_api/*.md     API reference
    <corpus>/libs/std/<module>/<module>_package_samples/*.md runnable examples
    <corpus>/libs/std/<module>/<module>_package_overview.md  module overview
    <corpus>/libs/stdx/<module>/...                          extended stdlib
    <corpus>/extra/*.md                                      extra guides
    <corpus>/manual/source_zh_cn/...                         language manual
    <corpus>/tools/source_zh_cn/...                          tool docs
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from cjkb.models import ApiRecord, ExampleRecord, KnowledgeBase

# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------

FENCE_RE = re.compile(r"^```(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

# `## class ArrayDeque<T>` / `## func all<T>(...)` / `## interface Iterable<E>`
API_HEADING_RE = re.compile(
    r"^(?P<kind>class|interface|enum|struct|macro|func|typealias|exception|trait)"
    r"(?:\s+(?P<name>[A-Za-z_]\w*[^()]*?))?\s*(?P<sig>\(.*\))?$"
)

# `### prop capacity` / `### init()` / `### func add(...)` / `### static func ...`
MEMBER_HEADING_RE = re.compile(
    r"^(?:(?P<static>static)\s+)?(?P<kind>prop|init|func|macro|operator|getter|setter)"
    r"(?:\s+(?P<name>[A-Za-z_]\w*))?(?P<sig>\(.*\))?$"
)

DESC_LABELS = ("功能", "说明", "描述", "作用")
PARAM_LABEL = "参数"
RETURN_LABEL = "返回值"
EXCEPTION_LABELS = ("异常", "错误", "抛出")


def split_code_blocks(md: str) -> List[Tuple[str, str]]:
    """Return [(lang, content)] for each fenced code block in the markdown."""
    blocks: List[Tuple[str, str]] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        m = FENCE_RE.match(lines[i])
        if m:
            lang = m.group(1).strip()
            buf: List[str] = []
            i += 1
            while i < len(lines) and not FENCE_RE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            blocks.append((lang, "\n".join(buf)))
        i += 1
    return blocks


def cangjie_blocks(md: str) -> List[str]:
    """All cangjie / cj / empty-lang code blocks (used as example candidates)."""
    out = []
    for lang, content in split_code_blocks(md):
        lang = lang.lower()
        if lang in ("", "cangjie", "cj", "java2cangjie"):
            out.append(content)
    return out


def strip_links(text: str) -> str:
    """Remove markdown links `[text](url)` keeping text; normalize whitespace."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_desc(text: str) -> str:
    """Strip a leading '功能：' style label."""
    text = text.strip()
    m = re.match(r"^(功能|说明|描述|作用|简述)\s*[:：]\s*(.*)$", text, re.S)
    if m:
        return m.group(2).strip()
    return text


def parse_labeled_sections(md: str) -> Dict[str, List[str]]:
    """Parse '标签：' sections into dict[label -> [bullet items]].

    Handles 参数/返回值/异常 etc. A section runs from its label line to the
    next heading (## or ###) or code fence.
    """
    result: Dict[str, List[str]] = {}
    lines = md.splitlines()
    current_label: Optional[str] = None
    for line in lines:
        stripped = line.strip()
        if HEADING_RE.match(line) or FENCE_RE.match(line):
            current_label = None
            continue
        if not stripped:
            continue
        m = re.match(r"^(参数|返回值|返回|异常|错误|抛出|类型|父类型|父接口)\s*[:：]\s*(.*)$", stripped)
        if m:
            current_label = m.group(1)
            rest = m.group(2).strip()
            if rest:
                result.setdefault(current_label, []).append(rest)
            continue
        if current_label and stripped.startswith("-"):
            item = strip_links(stripped.lstrip("- "))
            if item:
                result.setdefault(current_label, []).append(item)
    return result


def parse_params(items: List[str]) -> List[Dict[str, str]]:
    """'name: Type - description' or 'name: Type' -> dict records."""
    params = []
    for item in items:
        m = re.match(r"^\s*([A-Za-z_]\w*)\s*[:：]\s*(.+)$", item)
        if m:
            name, rest = m.group(1), m.group(2)
            desc = ""
            mm = re.match(r"^(.*?)\s*-\s*(.*)$", rest)
            if mm:
                rest, desc = mm.group(1).strip(), mm.group(2).strip()
            params.append({"name": name, "type": strip_links(rest), "description": desc})
        else:
            params.append({"name": "", "type": item, "description": ""})
    return params


# ---------------------------------------------------------------------------
# API reference docs  (libs/std/<mod>/<mod>_package_api/*.md)
# ---------------------------------------------------------------------------

def _parse_api_doc(path: str, module: str, library: str, source_url: str) -> List[ApiRecord]:
    with open(path, encoding="utf-8") as f:
        md = f.read()

    records: List[ApiRecord] = []
    # Split into top-level sections by '## '
    sections = re.split(r"(?m)^(?=##\s)", md)
    for sec in sections:
        lines = sec.splitlines()
        if not lines:
            continue
        h = HEADING_RE.match(lines[0])
        if not h or h.group(1) != "##":
            continue
        title = h.group(2).strip()
        m = API_HEADING_RE.match(title)
        if not m:
            continue
        kind = m.group("kind")
        name = m.group("name") or ""
        # strip markdown escapes (`ArrayList\<T>` -> `ArrayList`) and generics
        name = re.sub(r"[\\<>].*$", "", name).strip()

        blocks = cangjie_blocks("\n".join(lines))
        signature = blocks[0] if blocks else title
        # first line of the code block is the canonical declaration
        decl = signature.splitlines()[0].strip() if signature else ""

        body = re.sub(r"```.*?```", "", "\n".join(lines), flags=re.S)
        labeled = parse_labeled_sections(body)
        desc = clean_desc("\n".join(line for line in body.splitlines() if line.strip() and not line.strip().startswith("#")))

        exceptions = [strip_links(i) for i in labeled.get("异常", []) + labeled.get("错误", [])]
        records.append(ApiRecord(
            name=name or decl,
            kind=kind,
            module=module,
            library=library,
            signature=decl,
            description=desc[:2000],
            params=parse_params(labeled.get("参数", [])),
            returns="; ".join(labeled.get("返回值", [])),
            exceptions=exceptions,
            source=os.path.relpath(path),
            source_url=source_url,
            examples=blocks[1:] if len(blocks) > 1 else [],
            tags=[kind, module],
        ))

        # member sections: '### prop x' etc.
        for msec in re.split(r"(?m)^(?=###\s)", sec):
            mlines = msec.splitlines()
            if not mlines:
                continue
            mh = HEADING_RE.match(mlines[0])
            if not mh or mh.group(1) != "###":
                continue
            mtitle = mh.group(2).strip()
            mm = MEMBER_HEADING_RE.match(mtitle)
            if not mm:
                continue
            mkind = mm.group("kind")
            mname = mm.group("name") or ""
            msig = mm.group("sig") or ""
            full_name = (mname + msig) if mname else mtitle

            mblocks = cangjie_blocks("\n".join(mlines))
            mdecl = (mblocks[0] if mblocks else mtitle).splitlines()[0].strip()
            mbody = re.sub(r"```.*?```", "", "\n".join(mlines), flags=re.S)
            mlabeled = parse_labeled_sections(mbody)
            mdesc = clean_desc("\n".join(
                ln for ln in mbody.splitlines() if ln.strip() and not ln.strip().startswith("#")))
            records.append(ApiRecord(
                name=full_name,
                kind=mkind,
                module=module,
                library=library,
                signature=mdecl,
                description=mdesc[:1000],
                params=parse_params(mlabeled.get("参数", [])),
                returns="; ".join(mlabeled.get("返回值", [])),
                exceptions=[strip_links(i) for i in mlabeled.get("异常", []) + mlabeled.get("错误", [])],
                parent=name or module,
                source=os.path.relpath(path),
                source_url=source_url,
                examples=mblocks[1:] if len(mblocks) > 1 else [],
                tags=[mkind, module],
            ))
    return records


# ---------------------------------------------------------------------------
# Sample / example docs
# ---------------------------------------------------------------------------

def _parse_sample_doc(path: str, module: str, library: str, source_url: str,
                      title_fallback: str) -> List[ExampleRecord]:
    with open(path, encoding="utf-8") as f:
        md = f.read()
    lines = md.splitlines()
    title = title_fallback
    for ln in lines[:5]:
        m = HEADING_RE.match(ln)
        if m and m.group(1) == "#":
            title = m.group(2).strip()
            break
    desc_lines = []
    for ln in lines[1:20]:
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith("```"):
            continue
        if s.startswith("运行结果") or s.startswith("输出"):
            break
        desc_lines.append(strip_links(s))
    description = " ".join(desc_lines)[:300]

    records = []
    for code in cangjie_blocks(md):
        if len(code.strip()) < 10:
            continue
        records.append(ExampleRecord(
            title=title,
            code=code,
            module=module,
            library=library,
            source=os.path.relpath(path),
            description=description,
            tags=[module],
        ))
    return records


# ---------------------------------------------------------------------------
# Generic markdown walker (manual / extra / tools)
# ---------------------------------------------------------------------------

def _walk_md(root: str) -> List[str]:
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in sorted(files):
            if fn.endswith(".md") and not fn.startswith("."):
                found.append(os.path.join(dirpath, fn))
    return sorted(found)


def _module_from_path(path: str, root: str) -> str:
    rel = os.path.relpath(path, root).replace(os.sep, "/")
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return ""
    if len(parts) >= 3 and parts[-2].endswith("_package_api"):
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else parts[-1]


def _title_from_path(path: str, root: str) -> str:
    rel = os.path.relpath(path, root).replace(os.sep, "/")
    stem = os.path.splitext(os.path.basename(rel))[0]
    return stem.replace("_", " ")


def _parse_generic_md(path: str, root: str, library: str) -> Tuple[List[ApiRecord], List[ExampleRecord]]:
    """Extract example code blocks + sectioned API headings from any markdown."""
    with open(path, encoding="utf-8") as f:
        md = f.read()
    module = _module_from_path(path, root)
    title = _title_from_path(path, root)

    examples = []
    blocks = cangjie_blocks(md)
    if blocks:
        # pick the biggest block as the main example
        main = max(blocks, key=lambda b: len(b))
        examples.append(ExampleRecord(
            title=title, code=main, module=module, library=library,
            source=os.path.relpath(path),
            description=clean_desc(_first_paragraph(md))[:200],
            tags=[library, module],
        ))

    apis = []
    for sec in re.split(r"(?m)^(?=##\s)", md):
        lines = sec.splitlines()
        if not lines:
            continue
        h = HEADING_RE.match(lines[0])
        if not h or h.group(1) != "##":
            continue
        m = API_HEADING_RE.match(h.group(2).strip())
        if not m:
            continue
        decl = ""
        for lang, content in split_code_blocks("\n".join(lines)):
            if lang.lower() in ("cangjie", "cj", ""):
                decl = content.splitlines()[0].strip()
                break
        if not decl:
            continue
        apis.append(ApiRecord(
            name=re.sub(r"[\\<>].*$", "", m.group("name") or decl).strip() or decl,
            kind=m.group("kind"),
            module=module,
            library=library,
            signature=decl,
            description=clean_desc(_first_paragraph("\n".join(lines)))[:500],
            source=os.path.relpath(path),
            tags=[library, module],
        ))
    return apis, examples


def _first_paragraph(md: str) -> str:
    for ln in md.splitlines():
        s = ln.strip()
        if s and not s.startswith("#") and not s.startswith("```") and not s.startswith(">") and not s.startswith("-"):
            return strip_links(s)
    return ""


# ---------------------------------------------------------------------------
# Top-level collector
# ---------------------------------------------------------------------------

def collect_corpus(corpus_dir: str, data_dir: str) -> KnowledgeBase:
    kb = KnowledgeBase()
    libs_root = os.path.join(corpus_dir, "libs")

    # ---- libs/std and libs/stdx: per-module api + samples + overview ----
    for lib in ("std", "stdx"):
        lib_path = os.path.join(libs_root, lib)
        if not os.path.isdir(lib_path):
            continue
        for module_dir in sorted(os.listdir(lib_path)):
            mpath = os.path.join(lib_path, module_dir)
            if not os.path.isdir(mpath):
                continue
            module = f"{lib}.{module_dir}"
            kb.modules[module] = {"library": lib, "module_dir": module_dir, "apis": 0, "examples": 0}
            api_count = ex_count = 0

            # API reference dir: <module>_package_api/ or flat per-class files
            api_dir = os.path.join(mpath, f"{module_dir}_package_api")
            if os.path.isdir(api_dir):
                for fpath in _walk_md(api_dir):
                    for rec in _parse_api_doc(fpath, module, lib, _gitcode_url(fpath, corpus_dir)):
                        kb.apis.append(rec)
                        api_count += 1

            # samples dir: <module>_package_samples/
            samples_dir = os.path.join(mpath, f"{module_dir}_package_samples")
            if os.path.isdir(samples_dir):
                for fpath in _walk_md(samples_dir):
                    for rec in _parse_sample_doc(fpath, module, lib, _gitcode_url(fpath, corpus_dir),
                                                 f"{module_dir}: {_title_from_path(fpath, samples_dir)}"):
                        kb.examples.append(rec)
                        ex_count += 1

            # flat per-class/per-func docs directly under the module dir
            for fpath in _walk_md(mpath):
                base = os.path.basename(fpath)
                if base.endswith("_package_overview.md") or "_package_" in base:
                    continue
                apis, exs = _parse_generic_md(fpath, mpath, lib)
                for rec in apis:
                    rec.module = module
                    kb.apis.append(rec)
                    api_count += 1
                for rec in exs:
                    rec.module = module
                    kb.examples.append(rec)
                    ex_count += 1

            kb.modules[module]["apis"] = api_count
            kb.modules[module]["examples"] = ex_count

    # ---- extra/ : language feature guides ----
    extra_dir = os.path.join(corpus_dir, "extra")
    if os.path.isdir(extra_dir):
        for fpath in _walk_md(extra_dir):
            apis, exs = _parse_generic_md(fpath, extra_dir, "std")
            for rec in apis:
                rec.module = rec.module or "extra"
                kb.apis.append(rec)
            for rec in exs:
                rec.module = rec.module or "extra"
                kb.examples.append(rec)
            kb.modules.setdefault("extra", {"library": "std", "module_dir": "extra", "apis": 0, "examples": 0})
            kb.modules["extra"]["apis"] += len(apis)
            kb.modules["extra"]["examples"] += len(exs)

    # ---- manual/ : language reference ----
    manual_dir = os.path.join(corpus_dir, "manual", "source_zh_cn")
    if os.path.isdir(manual_dir):
        for fpath in _walk_md(manual_dir):
            apis, exs = _parse_generic_md(fpath, manual_dir, "manual")
            kb.apis.extend(apis)
            kb.examples.extend(exs)

    # ---- tools/ : cjpm etc ----
    tools_dir = os.path.join(corpus_dir, "tools", "source_zh_cn")
    if os.path.isdir(tools_dir):
        for fpath in _walk_md(tools_dir):
            _apis, exs = _parse_generic_md(fpath, tools_dir, "tools")
            for rec in exs:
                rec.library = "tools"
                rec.tags.append("tools")
            kb.examples.extend(exs)

    _dedup_records(kb)
    return kb


def _gitcode_url(relpath: str, corpus_dir: str) -> str:
    """Best-effort URL of the doc inside the gitcode corpus repo."""
    try:
        rel = os.path.relpath(relpath, corpus_dir).replace(os.sep, "/")
        return f"https://gitcode.com/Cangjie/cangjie_runtime/blob/master/{rel}"
    except Exception:
        return ""


def _dedup_records(kb: KnowledgeBase) -> None:
    """Remove exact duplicates (same source + signature) keeping the first."""
    seen_api = set()
    kept_api = []
    for r in kb.apis:
        key = (r.source, r.signature, r.name, r.parent)
        if key in seen_api:
            continue
        seen_api.add(key)
        kept_api.append(r)
    kb.apis = kept_api

    seen_ex = set()
    kept_ex = []
    for r in kb.examples:
        key = (r.source, r.code[:200])
        if key in seen_ex:
            continue
        seen_ex.add(key)
        kept_ex.append(r)
    kb.examples = kept_ex
