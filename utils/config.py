"""
Central configuration: filesystem paths and secret/env access.

All paths are relative to the project root so the app runs the same way
locally and on a host, and secrets are always read defensively (Streamlit's
st.secrets throws if no secrets.toml exists at all, so every lookup here is
wrapped in try/except and falls back to environment variables).
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("DATA_DIR", PROJECT_ROOT / "data"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", PROJECT_ROOT / "outputs"))
LOG_DIR = Path(os.environ.get("LOG_DIR", PROJECT_ROOT / "logs"))
PIPELINE_DIR = PROJECT_ROOT / "pipeline"
COST_ENGINE_DIR = PROJECT_ROOT / "cost_engine"

for _d in (DATA_DIR, OUTPUT_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

COMBINED_CSV_PATH = DATA_DIR / "sgcarmart_ev_combined.csv"
COE_CSV_PATH = DATA_DIR / "COEBiddingResultsPrices.csv"
CAPITAL_TEMPLATE_PATH = DATA_DIR / "Capital Cost.xlsx"
PIPELINE_LOG_PATH = LOG_DIR / "pipeline_run.log"

# The 5 pipeline stages, run in this order as isolated subprocesses.
PIPELINE_STAGES = [
    "sgcarmart_url.py",
    "spec_to_pricing_csv_converter.py",
    "spec_url_scraper.py",
    "pricing_url_scraper.py",
    "combine_ev_csv.py",
]


def get_secret(key: str, default: str | None = None) -> str | None:
    """Read a secret/config value: Streamlit secrets first, then env vars.

    st.secrets raises if no secrets.toml exists at all (e.g. pure local dev
    with only a .env file), so that lookup is always guarded.
    """
    try:
        import streamlit as st
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.environ.get(key, default)


# Only relevant if the optional chat_engine.py free-text layer is enabled.
ANTHROPIC_API_KEY = get_secret("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = get_secret("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Selenium-based scraping (Step 1, when the user opts to refresh) cannot run
# on Streamlit Community Cloud (no browser/network egress for a real Chrome
# session). This flag lets the UI detect that and fall back to "upload a
# freshly-scraped CSV" instead of trying to run the pipeline in-process.
SCRAPING_ENABLED = get_secret("SCRAPING_ENABLED", "true").lower() in ("1", "true", "yes")
