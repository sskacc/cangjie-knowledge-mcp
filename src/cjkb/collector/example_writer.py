"""LLM example writer: generate Cangjie example snippets for APIs that lack them.

Uses an OpenAI-compatible chat completions endpoint (OpenRouter / OpenAI / local).
Only triggered explicitly via `--write-examples`; every generated snippet is
flagged `generated=True` so downstream users can tell it apart from official
corpus examples.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Dict, List, Optional

from cjkb.models import ApiRecord, ExampleRecord

# A reference Cangjie snippet the model can imitate; keeps output idiomatic
# (this is critical: without it models drift into Java syntax like `.put` /
# `ArrayList[Int]` which is NOT valid Cangjie).
STYLE_REFERENCE = """Reference Cangjie style (IMITATE THIS):

```cangjie
import std.collection.*

main() {
    let list = ArrayList<Int64>([1, 2, 3])   // generic type args use angle brackets
    list.add(4)                               // methods are called with `.`, no `put`
    let map = HashMap<String, Int64>()
    map.add("a", 0)                           // HashMap insertion is `add`, not `put`
    for (i in list) {
        println(i)
    }
}
```
"""

SYSTEM_PROMPT = (
    "You are an expert Cangjie (仓颉) language developer. "
    "Given a Cangjie standard library API, write ONE short, correct, idiomatic "
    "Cangjie example that demonstrates its usage. Follow these rules:\n"
    "1. Output ONLY the Cangjie code inside a ```cangjie code block. No explanation.\n"
    "2. Use REAL Cangjie syntax: generic type args in angle brackets "
    "(`ArrayList<Int64>`, never `ArrayList[Int]`); `add(key, value)` on maps "
    "(there is NO `put` in Cangjie); `main()` for executable context.\n"
    "3. Include the required `import` statements.\n"
    "4. Keep it minimal (<= 25 lines) but complete.\n"
    "5. If unsure of exact syntax, still write your best-effort example and note "
    "it with a `// generated` comment.\n"
    f"{STYLE_REFERENCE}"
)

MAX_TOKENS = 2000  # generous: reasoning models consume budget on chain-of-thought


def _llm_available(cfg: Dict) -> bool:
    return bool(cfg.get("api_key"))


def _call_llm(cfg: Dict, user_prompt: str, retries: int = 2) -> Optional[str]:
    base = cfg.get("base_url") or "https://api.openai.com/v1"
    model = cfg.get("model") or "gpt-4o-mini"
    key = cfg.get("api_key")
    if not key:
        return None
    url = base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": MAX_TOKENS,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"].get("content") or ""
            content = content.strip()
        except Exception as exc:  # network / API errors: fail soft
            print(f"  [example_writer] LLM call failed (attempt {attempt + 1}): {exc}")
            content = ""
        if content:
            break
        if attempt < retries:
            print(f"  [example_writer] empty response, retrying ({attempt + 1}/{retries})")
    if not content:
        return None
    return _extract_code(content)


def _extract_code(content: str) -> str:
    """Pull the first cangjie/cj code block; strip fences; drop explanations."""
    m = re.search(r"```(?:cangjie|cj)?\s*\n?(.*?)```", content, re.S)
    if m:
        code = m.group(1).strip()
    else:
        code = content.strip()
    # defensive: remove any leftover fence lines and trailing explanation
    lines = [ln for ln in code.splitlines() if not ln.strip().startswith("```")]
    while lines and lines[0].strip() in ("cangjie", "cj"):
        lines.pop(0)
    code = "\n".join(lines).strip()
    # drop anything after a non-code trailing paragraph (heuristic: keep >= 60% code-looking)
    return code


def write_examples(apis: List[ApiRecord], cfg: Dict, limit: int = 20,
                   skip_titles: Optional[set] = None) -> List[ExampleRecord]:
    """Generate examples for the `limit` most important APIs lacking one.

    Selection priority: class-level APIs (most valuable), then functions with
    empty `examples` and no `generated` counterpart yet.

    skip_titles: set of generated titles (e.g. "std.core.Thread (generated)")
    to skip, so re-running the build does not regenerate existing examples.
    """
    if not _llm_available(cfg):
        print("  [example_writer] no LLM configured (OPENAI_API_KEY); skipping.")
        return []

    skip_titles = skip_titles or set()

    candidates: List[ApiRecord] = []
    seen_names = set()
    for api in apis:
        if api.examples:
            continue
        key = (api.module, api.name)
        if key in seen_names:
            continue
        seen_names.add(key)
        candidates.append(api)
    # class and interface kinds first
    candidates.sort(key=lambda a: (a.kind not in ("class", "interface", "enum"), -len(a.signature)))

    generated: List[ExampleRecord] = []
    skipped = 0
    for api in candidates[:limit + len(skip_titles)]:
        title = f"{api.module}.{api.name} (generated)"
        if title in skip_titles:
            skipped += 1
            continue
        prompt = (
            f"API: {api.name}\n"
            f"Kind: {api.kind}\n"
            f"Module: {api.module}\n"
            f"Signature: {api.signature}\n"
            f"Description: {api.description[:300]}\n\n"
            f"Write a Cangjie example demonstrating this API."
        )
        code = _call_llm(cfg, prompt)
        if code:
            generated.append(ExampleRecord(
                title=f"{api.module}.{api.name} (generated)",
                code=code,
                module=api.module,
                library=api.library,
                source="llm-generated",
                description=f"Auto-generated example for {api.module}.{api.name}",
                tags=[api.kind, api.module, "generated"],
                generated=True,
            ))
    if skipped:
        print(f"  [example_writer] skipped {skipped} already-generated titles")
    return generated
