"""
Design system for the EV Fleet Cost Assistant.

Everything here is presentational — CSS injection and small HTML/markdown
helpers. No search, filtering, workbook, or Claude-agent logic lives in
this file, and nothing here should ever need to change when that logic
changes.

Streamlit doesn't allow arbitrary widgets to be nested inside custom HTML
(each widget is its own DOM node the framework manages), so the pattern
used throughout is: st.container(border=True) for anything that needs to
hold real widgets ("cards" get their rounded-corner/shadow look from one
global CSS rule targeting Streamlit's own bordered-container wrapper),
and raw HTML via st.markdown(..., unsafe_allow_html=True) only for
decorative, non-interactive elements (the hero banner, the stepper).
"""

from __future__ import annotations

import base64
from pathlib import Path

import streamlit as st

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
MASCOT_PATH = ASSETS_DIR / "mascot.jpg"  # legacy default, kept for backward compat


def _mascot_b64(path: str | Path | None = None) -> str:
    target = Path(path) if path else MASCOT_PATH
    try:
        return base64.b64encode(target.read_bytes()).decode("ascii")
    except Exception:
        return ""


_GLOBAL_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}

:root {
    --accent: #7C5CFF;
    --accent-2: #4F8CFF;
    --card-bg: rgba(255,255,255,0.035);
    --card-border: rgba(255,255,255,0.09);
    --radius-lg: 18px;
    --radius-md: 12px;
}

/* ---------- Layout breathing room ---------- */
.block-container {
    padding-top: 1.5rem;
    padding-bottom: 3rem;
    max-width: 880px;
}

/* ---------- Hero header ---------- */
.evfc-hero {
    display: flex;
    align-items: center;
    gap: 1.1rem;
    padding: 1.4rem 1.6rem;
    margin-bottom: 1.1rem;
    border-radius: var(--radius-lg);
    background: linear-gradient(135deg, rgba(124,92,255,0.16), rgba(79,140,255,0.08));
    border: 1px solid rgba(124,92,255,0.25);
}
.evfc-hero img {
    width: 120px; height: 120px;
    border-radius: 50%;
    object-fit: cover;
    border: 3px solid rgba(124,92,255,0.55);
    box-shadow: 0 0 0 6px rgba(124,92,255,0.12);
    flex-shrink: 0;
}
.evfc-hero-text h1 {
    font-size: 1.5rem;
    font-weight: 800;
    margin: 0;
    line-height: 1.2;
    background: linear-gradient(90deg, #fff, #cfd4ff);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
}
.evfc-hero-text p {
    margin: 0.15rem 0 0 0;
    font-size: 0.92rem;
    color: rgba(230,237,243,0.65);
}

/* ---------- Stepper ---------- */
.evfc-stepper {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    margin-bottom: 1.3rem;
    padding: 0 0.2rem;
}
.evfc-step {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    flex: 1;
}
.evfc-step-circle {
    width: 30px; height: 30px;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 0.85rem; font-weight: 700;
    flex-shrink: 0;
    transition: all 0.25s ease;
}
.evfc-step-label {
    font-size: 0.82rem;
    font-weight: 600;
    white-space: nowrap;
    transition: color 0.25s ease;
}
.evfc-step-line {
    flex: 1;
    height: 2px;
    margin: 0 0.35rem;
    border-radius: 2px;
    transition: background 0.25s ease;
}
.evfc-step.done .evfc-step-circle { background: rgba(124,92,255,0.9); color: #fff; }
.evfc-step.done .evfc-step-label { color: rgba(230,237,243,0.55); }
.evfc-step.active .evfc-step-circle {
    background: linear-gradient(135deg, var(--accent), var(--accent-2));
    color: #fff;
    box-shadow: 0 0 0 4px rgba(124,92,255,0.18);
}
.evfc-step.active .evfc-step-label { color: #fff; }
.evfc-step.upcoming .evfc-step-circle { background: rgba(255,255,255,0.08); color: rgba(230,237,243,0.45); }
.evfc-step.upcoming .evfc-step-label { color: rgba(230,237,243,0.4); }
.evfc-line-done { background: rgba(124,92,255,0.7) !important; }
.evfc-line-upcoming { background: rgba(255,255,255,0.1) !important; }

/* ---------- Bordered containers -> cards ---------- */
[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: var(--radius-lg) !important;
    border-color: var(--card-border) !important;
    background: var(--card-bg);
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
}
[data-testid="stVerticalBlockBorderWrapper"]:hover {
    border-color: rgba(124,92,255,0.35) !important;
}

/* ---------- Expanders (vehicle cards in Step 3) ---------- */
[data-testid="stExpander"] {
    border-radius: var(--radius-md) !important;
    border-color: var(--card-border) !important;
    overflow: hidden;
}
[data-testid="stExpander"] summary {
    font-weight: 600;
}

/* ---------- Buttons ---------- */
.stButton > button {
    border-radius: 10px !important;
    font-weight: 600 !important;
    transition: transform 0.12s ease, box-shadow 0.12s ease, filter 0.12s ease !important;
}
.stButton > button:hover {
    transform: translateY(-1px);
    filter: brightness(1.08);
    box-shadow: 0 6px 16px rgba(124,92,255,0.25);
}
.stButton > button:active {
    transform: translateY(0px) scale(0.98);
}
.stButton > button:focus-visible {
    outline: 2px solid var(--accent-2) !important;
    outline-offset: 2px;
}

/* ---------- Chat ---------- */
[data-testid="stChatMessage"] {
    border-radius: var(--radius-md);
    border: 1px solid var(--card-border);
    background: var(--card-bg);
    padding: 0.15rem 0.3rem;
    margin-bottom: 0.4rem;
    animation: evfc-fade-in 0.25s ease;
}
@keyframes evfc-fade-in {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
}
[data-testid="stChatInput"] textarea {
    border-radius: var(--radius-md) !important;
}

/* ---------- Alerts ---------- */
[data-testid="stAlert"] {
    border-radius: var(--radius-md) !important;
}

/* ---------- Progress bar ---------- */
[data-testid="stProgress"] > div > div {
    background: linear-gradient(90deg, var(--accent), var(--accent-2)) !important;
    border-radius: 6px;
}

/* ---------- Sidebar ---------- */
[data-testid="stSidebar"] {
    border-right: 1px solid var(--card-border);
}

/* ---------- Status badge ---------- */
.evfc-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.25rem 0.65rem;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 600;
}
.evfc-badge.on { background: rgba(46,204,113,0.15); color: #4ade80; border: 1px solid rgba(46,204,113,0.3); }
.evfc-badge.off { background: rgba(255,255,255,0.06); color: rgba(230,237,243,0.55); border: 1px solid var(--card-border); }

/* ---------- Empty state ---------- */
.evfc-empty {
    text-align: center;
    padding: 1.6rem 1rem;
    border-radius: var(--radius-lg);
    border: 1px dashed var(--card-border);
    color: rgba(230,237,243,0.6);
}
.evfc-empty .evfc-empty-icon { font-size: 1.8rem; margin-bottom: 0.3rem; }

/* ---------- Responsive tweaks ---------- */
@media (max-width: 640px) {
    .evfc-hero { flex-direction: column; text-align: center; padding: 1.1rem; }
    .evfc-step-label { display: none; }
    .evfc-stepper { gap: 0.2rem; }
}
</style>
"""


def inject_global_css() -> None:
    st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)


def render_hero(title: str, subtitle: str, mascot_path: str | Path | None = None) -> None:
    b64 = _mascot_b64(mascot_path)
    img_tag = f'<img src="data:image/jpeg;base64,{b64}" alt="Mascot" />' if b64 else ""
    st.markdown(
        f"""
        <div class="evfc-hero">
            {img_tag}
            <div class="evfc-hero-text">
                <h1>{title}</h1>
                <p>{subtitle}</p>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_stepper(current_step, labels: list[str]) -> None:
    """current_step: 1/2/3 (int) or 'done'."""
    parts = ['<div class="evfc-stepper">']
    n = len(labels)
    for i, label in enumerate(labels, start=1):
        if current_step == "done" or (isinstance(current_step, int) and i < current_step):
            state, glyph = "done", "✓"
        elif current_step == i:
            state, glyph = "active", str(i)
        else:
            state, glyph = "upcoming", str(i)
        parts.append(
            f'<div class="evfc-step {state}">'
            f'<div class="evfc-step-circle">{glyph}</div>'
            f'<div class="evfc-step-label">{label}</div>'
            f"</div>"
        )
        if i < n:
            line_state = "evfc-line-done" if (current_step == "done" or (isinstance(current_step, int) and i < current_step)) else "evfc-line-upcoming"
            parts.append(f'<div class="evfc-step-line {line_state}"></div>')
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def render_empty_state(icon: str, title: str, subtitle: str) -> None:
    st.markdown(
        f"""
        <div class="evfc-empty">
            <div class="evfc-empty-icon">{icon}</div>
            <div style="font-weight:600; color:rgba(230,237,243,0.85);">{title}</div>
            <div style="font-size:0.85rem; margin-top:0.2rem;">{subtitle}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def status_badge(on: bool, on_text: str, off_text: str) -> str:
    cls = "on" if on else "off"
    dot = "●" if on else "○"
    text = on_text if on else off_text
    return f'<span class="evfc-badge {cls}">{dot} {text}</span>'
