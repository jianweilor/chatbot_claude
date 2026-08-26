"""
Step 3 helper — electricity tariff rate.

combinedcode.py's own get_electricity_rate_per_kwh() falls back to a
blocking input() prompt, which can't run inside Streamlit. This wrapper
calls only the non-blocking fetch (fetch_electricity_rate_or_none) and lets
the Streamlit UI collect a manual rate instead when the live fetch fails —
matching the documented constraint that SP Group's live fetch can fail in
sandboxed/cloud environments.
"""

from __future__ import annotations

import sys

from utils.config import COST_ENGINE_DIR


def fetch_live_rate() -> float | None:
    """Attempts the live SP Group fetch. Returns None on any failure
    (network blocked, page layout changed, etc.) rather than raising, so
    the caller can fall back to asking the user for a rate."""
    path_str = str(COST_ENGINE_DIR)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
    try:
        import combinedcode
        return combinedcode.fetch_electricity_rate_or_none()
    except Exception:
        return None
