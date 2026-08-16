"""Configuration loading (simple YAML subset + env overrides)."""

from __future__ import annotations

import os
from typing import Any, Dict

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml")


def _resolve(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve relative paths against the project root (repo root)."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    for key in ("data_dir",):
        val = cfg.get("output", {}).get(key)
        if val and not os.path.isabs(val):
            cfg["output"][key] = os.path.join(root, val)
    for key in ("cangjie_corpus", "j2cjlib", "java_terms", "gitcode_api_docs"):
        val = cfg.get("corpus", {}).get(key)
        if val and not os.path.isabs(val):
            cfg["corpus"][key] = os.path.join(root, val)
    return cfg


def load_config(path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    """Load config.yaml. yaml is only required when a config file is used;
    the search/index core itself stays dependency-free."""
    cfg: Dict[str, Any] = {}
    if os.path.exists(path):
        try:
            import yaml  # lazy: core works without PyYAML
        except ImportError:  # pragma: no cover
            raise RuntimeError(
                "PyYAML is required to read config.yaml (pip install PyYAML). "
                "Alternatively pass explicit overrides via CLI args.")
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    # env overrides
    env = os.environ
    cfg.setdefault("llm", {})
    cfg["llm"]["base_url"] = cfg["llm"].get("base_url") or env.get("OPENAI_BASE_URL", "")
    cfg["llm"]["api_key"] = cfg["llm"].get("api_key") or env.get("OPENAI_API_KEY", "")
    cfg["llm"]["model"] = cfg["llm"].get("model") or env.get("OPENAI_MODEL", "")
    cfg["llm"].setdefault("rerank", True)

    cfg.setdefault("corpus", {})
    cfg.setdefault("output", {"data_dir": "data"})
    cfg.setdefault("index", {})
    cfg["index"].setdefault("field_weights",
                            {"name": 4.0, "module": 2.0, "signature": 3.0,
                             "description": 1.0, "tags": 1.5})
    cfg["index"].setdefault("top_k", 10)
    cfg["index"].setdefault("min_score", 0.01)
    return _resolve(cfg)
