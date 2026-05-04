from __future__ import annotations
"""
settings.py — central config loader
Reads config.yaml + environment variables.
"""
import os
import yaml
from pathlib import Path

ROOT = Path(__file__).parent.parent

def load() -> dict:
    cfg_path = ROOT / "config" / "config.yaml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    # Inject API keys from environment
    cfg["apis"]["fmp"]["key"]     = os.environ.get(cfg["apis"]["fmp"]["key_env"], "")
    cfg["apis"]["finnhub"]["key"] = os.environ.get(cfg["apis"]["finnhub"]["key_env"], "")
    # LLM key: check the configured env var, but also accept OPENROUTER_API_KEY as fallback
    llm_key = os.environ.get(cfg["apis"]["llm"]["key_env"], "")
    if not llm_key:
        llm_key = os.environ.get("OPENROUTER_API_KEY", "")
    cfg["apis"]["llm"]["key"] = llm_key

    # Resolve relative paths to absolute
    for k, v in cfg["paths"].items():
        cfg["paths"][k] = str(ROOT / v)

    return cfg


# Singleton — import and use anywhere
CFG = load()
