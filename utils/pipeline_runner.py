"""
Step 1 engine — refresh scraped EV data.

Mirrors start.py's orchestration exactly: each of the 5 stages runs as its
own subprocess (not imported), because the scraper scripts launch a real
Chrome session and run at module level. Running them as isolated
subprocesses means a crash or leftover driver in one stage can't corrupt
the next, and a failure halts the pipeline immediately rather than
continuing with partial data.

This is only usable where Selenium/Chrome can actually run (a local
machine or a host with browser access) — Streamlit Community Cloud has no
browser/network egress for this. Check `scraping_available()` before
offering Step 1's "Yes, refresh" option; if it's False, the UI should fall
back to an upload flow instead.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator

from utils.config import PIPELINE_DIR, PIPELINE_STAGES, PIPELINE_LOG_PATH


@dataclass
class StageEvent:
    stage_num: int
    total: int
    script_name: str
    status: str          # "start" | "done" | "failed"
    detail: str = ""


def scraping_available() -> bool:
    """True if this environment can actually run the Selenium stages.

    Streamlit Community Cloud has no Chrome/browser egress, so Step 1's
    "refresh" option should be hidden or disabled there — the UI can check
    this and offer a CSV-upload fallback instead.
    """
    try:
        import selenium  # noqa: F401
        from selenium import webdriver
        opts = webdriver.ChromeOptions()
        opts.add_argument("--headless=new")
        driver = webdriver.Chrome(options=opts)
        driver.quit()
        return True
    except Exception:
        return False


def _log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    with open(PIPELINE_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_pipeline() -> Iterator[StageEvent]:
    """Runs all 5 stages in order, yielding a StageEvent as each starts,
    finishes, or fails. Stops (and stops yielding) at the first failure —
    the caller must not proceed to Step 2 with stale/partial data without
    surfacing that failure to the user.
    """
    total = len(PIPELINE_STAGES)
    _log("=" * 60)
    _log("Pipeline run started")

    for i, script_name in enumerate(PIPELINE_STAGES, start=1):
        script_path = PIPELINE_DIR / script_name

        if not script_path.exists():
            detail = f"script not found: {script_path}"
            _log(f"STAGE {i}/{total} FAILED -- {detail}")
            yield StageEvent(i, total, script_name, "failed", detail)
            return

        _log(f"STAGE {i}/{total} START -- {script_name}")
        yield StageEvent(i, total, script_name, "start")

        # cwd=PIPELINE_DIR to match start.py's original behaviour; each
        # script resolves its own data folder via its own file location
        # (see the patched CSV_DIR in each script), so this mainly keeps
        # relative imports/behaviour consistent with the original scripts.
        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(PIPELINE_DIR),
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-15:]
            detail = "\n".join(tail) or f"exited with code {result.returncode}"
            _log(f"STAGE {i}/{total} FAILED -- {script_name} exited with code {result.returncode}")
            yield StageEvent(i, total, script_name, "failed", detail)
            return

        _log(f"STAGE {i}/{total} DONE  -- {script_name}")
        yield StageEvent(i, total, script_name, "done")

    _log(f"Pipeline completed successfully -- all {total} stages done.")
    _log("=" * 60)
