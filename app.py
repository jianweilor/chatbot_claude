"""
EV Fleet Cost Assistant — Streamlit chatbot entry point.

A real chat interface (st.chat_message / st.chat_input) driving the same
3-step workflow as before:
  1. Optionally refresh scraped SGCarmart EV data
  2. Look up one or more vehicles — by name OR by spec criteria
     ("SUV with at least 7 seats", "sedans that do 0-100 in under 8 seconds")
  3. Generate a Summary / Capital Cost / Recurrent Cost workbook

Matching and cost logic stay fully deterministic (regex + pandas, in
utils/csv_tools.py and cost_engine/combinedcode.py) — no LLM/API key is
required for any of this. Step 3's numeric inputs (maintenance costs, tab
names) stay as structured widgets rather than free text: those values feed
directly into cost formulas, and getting one wrong silently is worse than
asking for it explicitly.
"""

from __future__ import annotations

import re as _re
from pathlib import Path

import pandas as pd
import streamlit as st

from utils import claude_assistant, config, csv_tools, electricity, pipeline_runner, ui_theme, workbook_builder

MASCOT_CHATBOT_PATH = str(Path(__file__).parent / "assets" / "mascot_chatbot.jpg")
MASCOT_SIDEBAR_PATH = str(Path(__file__).parent / "assets" / "mascot_sidebar.jpg")

st.set_page_config(page_title="MarIO", page_icon=MASCOT_CHATBOT_PATH, layout="centered")

YES_WORDS = {"yes", "y", "yeah", "yep", "sure", "refresh", "go ahead", "do it"}
NO_WORDS = {"no", "n", "nope", "nah", "skip", "no thanks"}
DONE_WORDS = {"done", "continue", "next", "proceed", "that's all", "thats all", "finish"}
LIST_WORDS = {"list", "show all models", "list models", "list all", "show all"}
RETRY_WORDS = {"retry", "try again"}


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

def init_state() -> None:
    defaults = {
        "step": 1,
        "messages": [],
        "vehicle_df": None,
        "resolved_vehicles": [],
        "pending_matches": None,
        "pending_keyword": "",
        "step3_confirmed": False,
        "workbook_path": None,
        "csv_uploaded_handled": False,
        "last_data_gap_rows": None,
        "last_data_gap_filters": None,
        "agent_messages": [],
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    if not st.session_state.messages:
        bot_say(step1_greeting())


def restart() -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    init_state()


def bot_say(text: str) -> None:
    st.session_state.messages.append({"role": "assistant", "content": text})


def user_say(text: str) -> None:
    st.session_state.messages.append({"role": "user", "content": text})


# ---------------------------------------------------------------------------
# Greeting text per step
# ---------------------------------------------------------------------------

def step1_greeting() -> str:
    return (
        "👋 Hi, I'm MarIO, your Market Intelligent Orchestrator.\n\n"
        "Would you like me to start a market scouting agent to retrieve updated car listings from websources before "
        "we continue? \n This starts the full pipeline and generate a full data repository "
        " and this can take a while.\n\n\n"
        "Reply **'Yes'** to continue, or **'No'** to proceed with our existing database."
    )


def step2_greeting() -> str:
    return (
        "What is your requirement for your car? "
        "or you can describe your needs (e.g. *'SUV with at least 7 seats'*, *'sedans that do 0-100 in under 8 seconds'.*"
        "We will call on our agent to assist you  "
    )


def step3_greeting() -> str:
    return (
        "Would you like a summary with capital and recurrent cost estimates for "
        "the vehicle(s) you just looked up, for your market survey? (**Yes**/**No**)"
    )


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_sidebar() -> None:
    with st.sidebar:
        st.image(MASCOT_SIDEBAR_PATH, width=1024)
        st.markdown("**MarIO**")
        st.markdown("<div style='height:0.4rem'></div>", unsafe_allow_html=True)

        with st.container(border=True):
            st.markdown("**📍 Status**")
            step_label = {1: "Refreshing data", 2: "Finding vehicles", 3: "Cost workbook"}.get(
                st.session_state.step, "Done"
            )
            st.caption(f"Step {st.session_state.step if isinstance(st.session_state.step, int) else '✓'} · {step_label}")

            if config.COMBINED_CSV_PATH.exists():
                import time
                mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(config.COMBINED_CSV_PATH.stat().st_mtime))
                rows = len(st.session_state.vehicle_df) if st.session_state.vehicle_df is not None else "?"
                st.caption(f"📄 {rows} vehicles loaded · updated {mtime}")
            else:
                st.caption("📄 Data not loaded yet")

            if st.session_state.resolved_vehicles:
                with st.expander(f"🚗 {len(st.session_state.resolved_vehicles)} vehicle(s) in roster"):
                    for i, v in enumerate(st.session_state.resolved_vehicles):
                        rcol1, rcol2 = st.columns([5, 1])
                        with rcol1:
                            st.caption(f"• {v['display_name_hint']}")
                        with rcol2:
                            if st.button("✕", key=f"remove_sidebar_{i}", help="Remove from roster"):
                                st.session_state.resolved_vehicles.pop(i)
                                st.rerun()

        with st.container(border=True):
            has_key = bool(get_anthropic_api_key())
            st.markdown(f"**🤖 AI Assistant** &nbsp; {ui_theme.status_badge(has_key, 'Active', 'Off')}", unsafe_allow_html=True)
            st.text_input(
                "Anthropic API Key", key="anthropic_api_key", type="password",
                label_visibility="collapsed", placeholder="sk-ant-...",
                help="When set, Claude answers all of Step 2's questions directly (search, "
                     "spec filtering, field lookups, recommendations) using real tool calls "
                     "against the CSV — never from its own knowledge. Kept only in this "
                     "browser session — never written to disk.",
            )
            st.caption(
                "[Get a key ↗](https://console.anthropic.com/settings/keys) · "
                "[API docs ↗](https://docs.claude.com)"
            )
            if has_key:
                st.caption("Claude is handling Step 2 — ask it anything, in whatever phrasing feels natural.")
            else:
                st.caption("Without a key, Step 2 still works via the built-in criteria parser.")

        st.markdown("<div style='height:0.6rem'></div>", unsafe_allow_html=True)
        if st.button("🔄 Restart conversation", width="stretch", help="Clears the chat and starts over from Step 1."):
            restart()
            st.rerun()


def get_anthropic_api_key() -> str | None:
    """Session-entered key takes priority (so anyone can try it without
    touching secrets/env), falling back to a server-side key if the
    deployer configured one via .env/secrets."""
    return st.session_state.get("anthropic_api_key") or config.ANTHROPIC_API_KEY


# ---------------------------------------------------------------------------
# Step 1 — refresh data?
# ---------------------------------------------------------------------------

def scraping_ok() -> bool:
    return config.SCRAPING_ENABLED and pipeline_runner.scraping_available()


def render_upload_fallback() -> None:
    """Shown as a persistent widget (not chat text) whenever live scraping
    isn't available — a file upload inherently can't happen through a text
    box."""
    if st.session_state.step != 1 or scraping_ok():
        return
    with st.container(border=True):
        st.markdown("📤 **Upload a freshly-scraped dataset**")
        st.caption(
            "Live scraping (Selenium/Chrome) isn't available in this environment — "
            "expected on Streamlit Community Cloud. Upload a `sgcarmart_ev_combined.csv` "
            "from a local run instead, or just reply **No** below to use the existing data."
        )
        uploaded = st.file_uploader(
            "Upload sgcarmart_ev_combined.csv", type=["csv"], key="csv_uploader",
            label_visibility="collapsed",
        )
    if uploaded is not None and not st.session_state.csv_uploaded_handled:
        config.COMBINED_CSV_PATH.write_bytes(uploaded.getvalue())
        st.session_state.csv_uploaded_handled = True
        bot_say("Uploaded file saved as the active dataset. Data refreshed — moving on.\n\n" + advance_to_step2())
        st.rerun()


def run_refresh_pipeline_chat() -> str:
    status = st.status("Running data refresh pipeline...", expanded=True)
    for event in pipeline_runner.run_pipeline():
        if event.status == "start":
            status.write(f"Stage {event.stage_num}/{event.total}: {event.script_name} — starting")
        elif event.status == "done":
            status.write(f"Stage {event.stage_num}/{event.total}: {event.script_name} — done")
        elif event.status == "failed":
            status.update(label="Pipeline failed", state="error")
            status.write(f"Stage {event.stage_num}/{event.total}: {event.script_name} — FAILED")
            if event.detail:
                status.code(event.detail)
            return (
                f"Refresh stopped at stage {event.stage_num}/{event.total} "
                f"(`{event.script_name}`) — the existing data was **not** overwritten "
                "with partial results.\n\nReply **retry** to try again, or **skip** to "
                "use the existing data instead."
            )
    status.update(label="Data refreshed successfully", state="complete")
    return "Data refreshed — `sgcarmart_ev_combined.csv` is up to date.\n\n" + advance_to_step2()


def advance_to_step2() -> str:
    if not config.COMBINED_CSV_PATH.exists():
        return (
            "No `sgcarmart_ev_combined.csv` found yet — scraping is required at "
            "least once before vehicle lookup can work. Reply **Yes** to refresh, "
            "or upload a CSV above."
        )
    st.session_state.vehicle_df = csv_tools.load_vehicle_df()
    st.session_state.step = 2
    return step2_greeting()


def handle_step1(prompt: str) -> str:
    t = prompt.strip().lower()
    if t in YES_WORDS:
        return run_refresh_pipeline_chat()
    if t in NO_WORDS or t == "skip":
        return advance_to_step2()
    if t in RETRY_WORDS:
        return run_refresh_pipeline_chat()
    return (
        "Sorry, I didn't catch that — reply **Yes** to refresh the data, or "
        "**No** to use what's already loaded."
    )


# ---------------------------------------------------------------------------
# Step 2 — vehicle lookup
# ---------------------------------------------------------------------------

def add_resolved(row: pd.Series, user_keyword: str) -> None:
    st.session_state.resolved_vehicles.append({
        "full_name": row["FullName"],
        "display_name_hint": row["FullName"],
        "vehicle_type": str(row.get("Vehicle Type") or "").strip() or "Unspecified",
        "coe": row.get("COE"),
        "_row": row,  # kept for Claude recommendation grounding (utils/claude_assistant.py)
    })


ALL_WORDS = {"all", "all of them", "select all", "whole list", "everything", "add all", "the whole list"}

REMOVE_PATTERN = _re.compile(r"^(?:remove|delete|drop)\s+(.+)$", _re.IGNORECASE)
ROSTER_VIEW_WORDS = {"show roster", "my roster", "list roster", "view roster", "what's in my roster", "whats in my roster"}


def resolve_roster_target(identifier: str, resolved_vehicles: list) -> int | list[int] | None:
    """Matches a user-typed identifier against the roster: a 1-based
    position, or a substring of the vehicle's name. Returns a single index
    on a unique match, a list of indices if ambiguous, or None if nothing
    matches — so callers can distinguish "not found" from "which one?"."""
    ident = identifier.strip()
    if ident.isdigit():
        idx = int(ident) - 1
        return idx if 0 <= idx < len(resolved_vehicles) else None
    matches = [i for i, v in enumerate(resolved_vehicles) if ident.lower() in v["display_name_hint"].lower()]
    if len(matches) == 1:
        return matches[0]
    return matches if matches else None


def render_roster_list(resolved_vehicles: list) -> str:
    lines = [f"{i + 1}. {v['display_name_hint']}" for i, v in enumerate(resolved_vehicles)]
    return "\n".join(lines)


def handle_step2_disambiguation(prompt: str) -> str:
    matches: pd.DataFrame = st.session_state.pending_matches
    t = prompt.strip()
    t_lower = t.lower()

    if t_lower in ALL_WORDS:
        for _, row in matches.iterrows():
            add_resolved(row, st.session_state.pending_keyword)
        count = len(matches)
        st.session_state.pending_matches = None
        st.session_state.pending_keyword = ""
        return (
            f"Added all **{count}** vehicles from that list. Want to look up another "
            "vehicle, or type **done** to continue to the cost workbook?"
        )

    api_key = get_anthropic_api_key()
    if api_key and claude_assistant.looks_like_decision_request(t):
        with st.spinner("Asking Claude..."):
            recommendation = claude_assistant.recommend_with_claude(
                t, [row for _, row in matches.iterrows()], api_key,
            )
        return recommendation + "\n\n_Still your call — reply with a number, a model name, or **all**._"

    row = None
    if t.isdigit():
        idx = int(t) - 1
        if 0 <= idx < len(matches):
            row = matches.iloc[idx]
    if row is None:
        sub = matches[matches["FullName"].str.contains(t, case=False, na=False)]
        if len(sub) == 1:
            row = sub.iloc[0]

    if row is None:
        hint = (
            "I didn't catch which one — reply with the number from the list, the "
            "exact model name, or **all** to add every vehicle in that list."
        )
        if not api_key:
            hint += " (Add an Anthropic API key in the sidebar if you'd like help deciding.)"
        return hint

    st.session_state.pending_matches = None
    st.session_state.pending_keyword = ""
    add_resolved(row, prompt)
    return (
        f"Got it — **{row['FullName']}** added. Want to look up another vehicle, "
        "or type **done** to continue to the cost workbook?"
    )


MISSING_DATA_WORDS = (
    "missing data", "missing/unlisted", "unlisted data", "show missing", "show excluded",
    "show unlisted", "excluded vehicle", "data gap", "show gap", "those vehicles",
    "see them", "see those", "which ones", "which vehicles", "what are they",
    "why were they", "why are they", "unknown data", "not listed", "no data",
    "them manually", "check them",
)


def _mentions_gap_count(text: str, gap_count: int) -> bool:
    """Catches phrasing like 'the 39 vehicles' / 'those 39' that references
    the exact number just mentioned in the note, even without any of the
    fixed trigger phrases above."""
    if not gap_count:
        return False
    return _re.search(rf"\b{gap_count}\b", text) is not None


def handle_step2(prompt: str) -> str:
    """Step 2's dispatcher. With an Anthropic API key present, Claude
    answers via real tool calls against the CSV (utils/claude_assistant.run_step2_agent)
    — this becomes the primary way ALL Step 2 questions get answered, not
    just a fallback. Without a key, falls back to the deterministic
    regex-based path below so the app still fully works offline."""
    api_key = get_anthropic_api_key()
    if not api_key:
        return handle_step2_deterministic(prompt)

    df = st.session_state.vehicle_df
    with st.spinner("Thinking..."):
        reply = claude_assistant.run_step2_agent(prompt, api_key, df, st.session_state)

    if st.session_state.pop("_transitioned_to_step3", False):
        st.session_state.step = 3
        reply = reply + "\n\n" + step3_greeting()

    return reply


def handle_step2_deterministic(prompt: str) -> str:
    """Fallback path used only when no Anthropic API key is set — the
    original regex/pandas-only Step 2 logic, unchanged."""
    df = st.session_state.vehicle_df
    t = prompt.strip().lower()

    if t in ROSTER_VIEW_WORDS:
        if not st.session_state.resolved_vehicles:
            return "Your roster is empty — search for a vehicle first."
        return (
            f"**{len(st.session_state.resolved_vehicles)} vehicle(s) in your roster:**\n\n"
            + render_roster_list(st.session_state.resolved_vehicles)
            + "\n\nType **remove <number or name>** to take one out — useful if its CSV "
            "data turns out to be incomplete."
        )

    remove_match = REMOVE_PATTERN.match(prompt.strip())
    if remove_match:
        if not st.session_state.resolved_vehicles:
            return "Your roster is already empty — nothing to remove."
        identifier = remove_match.group(1).strip()
        target = resolve_roster_target(identifier, st.session_state.resolved_vehicles)
        if target is None:
            return (
                f"I couldn't find '{identifier}' in your roster. Type **show roster** to "
                "see what's there, then remove by number or name."
            )
        if isinstance(target, list):
            options = "\n".join(f"{i + 1}. {st.session_state.resolved_vehicles[i]['display_name_hint']}" for i in target)
            return f"Multiple vehicles match '{identifier}' — which one?\n\n{options}\n\nReply **remove <number>**."
        removed = st.session_state.resolved_vehicles.pop(target)
        remaining = len(st.session_state.resolved_vehicles)
        return (
            f"Removed **{removed['display_name_hint']}** from your roster. "
            f"{remaining} vehicle(s) remaining." if remaining else
            f"Removed **{removed['display_name_hint']}** — your roster is now empty."
        )

    if any(w in t for w in MISSING_DATA_WORDS) or _mentions_gap_count(
        t, len(st.session_state.get("last_data_gap_rows")) if st.session_state.get("last_data_gap_rows") is not None else 0
    ):
        gap_rows: pd.DataFrame | None = st.session_state.get("last_data_gap_rows")
        if gap_rows is None or gap_rows.empty:
            return (
                "There's no pending list of excluded/missing-data vehicles right now — "
                "that note only shows up right after a filtered search that had some "
                "gaps. Try a spec-based search first."
            )
        gap_filters = st.session_state.get("last_data_gap_filters") or {}
        st.session_state.pending_matches = gap_rows
        st.session_state.pending_keyword = "vehicles with missing/unlisted data for your last search"
        lines = [
            csv_tools.format_match_line(r, i + 1, gap_filters)
            for i, (_, r) in enumerate(gap_rows.iterrows())
        ]
        return (
            f"Here are the {len(gap_rows)} vehicles excluded because their own data was "
            "missing/unlisted for one of your criteria (shown as 'unknown' below — these "
            "were never confirmed as non-matches, just unverifiable):\n\n" + "\n".join(lines) +
            "\n\nReply with a number, a model name, or **all** to add any of these to your roster."
        )

    if st.session_state.pending_matches is not None:
        return handle_step2_disambiguation(prompt)

    if t in DONE_WORDS:
        if not st.session_state.resolved_vehicles:
            return "You haven't looked up any vehicles yet — search for one first, then type **done**."
        st.session_state.step = 3
        return "Great — moving on to the cost workbook step.\n\n" + step3_greeting()

    if t in LIST_WORDS:
        models = csv_tools.list_all_models(df)
        return f"Here are all {len(models)} models in the dataset:\n\n" + "\n".join(f"- {m}" for m in models)

    result = csv_tools.smart_search(df, prompt)
    filters_used = None
    ai_note = ""

    unavailable_note = ""
    if result.unavailable_terms:
        unavailable_note = (
            "\n\n_Note: this dataset doesn't have columns for "
            + ", ".join(result.unavailable_terms)
            + " — those criteria were skipped, not guessed at. Everything below is "
            "based only on the criteria the CSV actually tracks._"
        )

    data_gap_note = ""
    if result.data_gap_count:
        st.session_state.last_data_gap_rows = result.data_gap_rows
        st.session_state.last_data_gap_filters = filters_used if filters_used is not None else csv_tools.parse_filters(prompt)
        data_gap_note = (
            f"\n\n_Note: {result.data_gap_count} other vehicle(s) matched the vehicle "
            "type but had missing/unlisted data for one of your other criteria — they "
            "weren't confirmed as non-matches, just excluded because their data isn't "
            "in the CSV. Type **show missing data** if you'd like to see them._"
        )

    if result.status == "exact":
        field_answer = csv_tools.answer_field_questions(result.row, prompt)
        add_resolved(result.row, prompt)
        intro = f"**{result.row['FullName']}**\n\n{field_answer}\n\n" if field_answer else f"Got it — **{result.row['FullName']}** added.\n\n"
        return (
            intro + "Want to look up another vehicle, or type **done** to continue to "
            "the cost workbook?" + unavailable_note + ai_note
        )

    if result.status in ("ambiguous", "filtered"):
        st.session_state.pending_matches = result.matches
        st.session_state.pending_keyword = prompt
        if filters_used is not None:
            filters = filters_used
            label = f"matched: {csv_tools.describe_filters(filters)}"
        elif result.status == "filtered":
            filters = csv_tools.parse_filters(prompt)
            label = f"matched: {csv_tools.describe_filters(filters)}"
        else:
            filters = {}
            label = f"matched '{prompt}'"
        lines = [
            csv_tools.format_match_line(r, i + 1, filters)
            for i, (_, r) in enumerate(result.matches.iterrows())
        ]
        return (
            f"{len(result.matches)} vehicles {label}:\n\n" + "\n".join(lines) +
            "\n\nReply with a number, a model name, or **all** to add every vehicle "
            "in this list, then continue to the cost workbook." + unavailable_note + data_gap_note + ai_note
        )

    if result.unavailable_terms:
        return (
            f"I couldn't find anything matching '{prompt}'. This dataset also doesn't "
            "have columns for " + ", ".join(result.unavailable_terms) + ", so those "
            "criteria can't be filtered on either. Try a brand/model name, or a "
            "request based on vehicle type, price, seating, drive range, boot space, "
            "acceleration, or height." + data_gap_note
        )

    return (
        f"I couldn't find anything matching '{prompt}'. Try a brand/model name, "
        "a spec-based request (e.g. \"SUV with at least 7 seats\", \"sedans under "
        "8s 0-100\"), or type **list** to see every model."
    )


# ---------------------------------------------------------------------------
# Step 3 — generate cost workbook
# ---------------------------------------------------------------------------

def handle_step3(prompt: str) -> str:
    t = prompt.strip().lower()
    if t in NO_WORDS:
        st.session_state.step = "done"
        return "No worries — that's everything then! Restart from the sidebar anytime to look up more vehicles."
    if t in YES_WORDS:
        st.session_state.step3_confirmed = True
        return "Great — fill in the details below and hit **Generate workbook** when you're ready."
    return "Reply **Yes** to build the cost workbook, or **No** if that's all for now."


def render_step3_form() -> None:
    """Structured widgets for the numeric/tab-name inputs — kept out of
    free text on purpose, since these values feed straight into cost
    formulas and a misheard number here would be a silent data error."""
    if st.session_state.step != 3 or not st.session_state.step3_confirmed or st.session_state.workbook_path:
        return

    if not st.session_state.resolved_vehicles:
        st.warning("No vehicles left in your roster.", icon="⚠️")
        if st.button("← Back to vehicle search"):
            st.session_state.step = 2
            st.session_state.step3_confirmed = False
            st.rerun()
        return

    df = st.session_state.vehicle_df

    st.markdown("#### 🚗 Vehicle details")
    vehicle_inputs = []
    missing_price_ok = True
    for i, v in enumerate(st.session_state.resolved_vehicles):
        needs_price, price_note = workbook_builder.needs_manual_price(v["full_name"], df)
        label = f"{'⚠️ ' if needs_price else ''}{v['display_name_hint']}"
        with st.expander(label, expanded=True):
            top_col1, top_col2 = st.columns([4, 1])
            with top_col2:
                if st.button("🗑️ Remove", key=f"remove_step3_{i}", help="Remove this vehicle — useful if its CSV data is incomplete and causing errors."):
                    st.session_state.resolved_vehicles.pop(i)
                    st.rerun()
            with top_col1:
                display_name = st.text_input(
                    "Display name", value=v["display_name_hint"], key=f"display_name_{i}",
                    help="Used identically across the Summary, Capital Cost, and Recurrent Cost tabs.",
                )
            marked = st.radio(
                "Marked or Unmarked?",
                options=["Unmarked (pays road tax, has COE)", "Marked (no road tax, no COE)"],
                index=0, key=f"marked_{i}", horizontal=True,
                help="Most ordinary retail vehicles (like these SGCarmart listings) are "
                     "Unmarked and need COE. 'Marked' is the exception — no road tax, no "
                     "COE — so it's not the default here.",
            ) == "Marked (no road tax, no COE)"

            manual_price = None
            if needs_price:
                st.warning(f"⚠️ No usable price in the dataset ({price_note}). Enter one to include this vehicle.", icon="⚠️")
                manual_price = st.number_input(
                    "Price for this vehicle (SGD, all-in)", min_value=0.0, step=1000.0, key=f"manual_price_{i}",
                )
                if not manual_price or manual_price <= 0:
                    missing_price_ok = False

            col_a, col_b = st.columns(2)
            with col_a:
                maint_lt5 = st.number_input("Maintenance <5 yrs ($/yr)", min_value=0.0, step=100.0, key=f"maint_lt5_{i}")
            with col_b:
                maint_5to10 = st.number_input("Maintenance 5–10 yrs ($/yr)", min_value=0.0, step=100.0, key=f"maint_5to10_{i}")

            has_projection = st.checkbox("Include a capital cost projection", key=f"has_proj_{i}")
            capital_cost_projection = None
            if has_projection:
                capital_cost_projection = st.number_input(
                    "Capital cost projection (informational only)", min_value=0.0, step=1000.0, key=f"proj_{i}",
                )
            vehicle_inputs.append(workbook_builder.VehicleInput(
                full_name=v["full_name"], display_name=display_name, marked=marked,
                maint_lt5=maint_lt5, maint_5to10=maint_5to10,
                capital_cost_projection=capital_cost_projection, vehicle_cat=v["vehicle_type"],
                manual_price=manual_price,
            ))

    st.markdown("#### ⚙️ Workbook settings")
    with st.container(border=True):
        col1, col2 = st.columns(2)
        with col1:
            capital_tab_name = st.text_input("Capital Cost tab name", value="Capital Cost", max_chars=31)
        with col2:
            recurrent_tab_name = st.text_input("Recurrent Cost tab name", value="Recurrent Cost", max_chars=31)
        recurrent_category_label = st.text_input(
            "Recurrent Cost tab title/label (optional)", value="", placeholder="e.g. 'TP Fleet FY28'",
        )
        rate_choice = st.radio(
            "Electricity rate", ["Fetch the current SP Group tariff automatically", "I'll enter my own rate"],
        )
        manual_rate = None
        if rate_choice == "I'll enter my own rate":
            manual_rate = st.number_input("Electricity rate ($/kWh, excl. GST)", min_value=0.0, step=0.01, value=0.30)

    st.markdown("#### ✅ Confirm before generating")
    st.caption("These display names will appear identically in all three tabs.")
    st.dataframe(
        pd.DataFrame([{"Vehicle": vi.display_name, "Status": "Marked" if vi.marked else "Unmarked"} for vi in vehicle_inputs]),
        width="stretch", hide_index=True,
    )

    if not missing_price_ok:
        st.error("Enter a price above for every vehicle marked ⚠️ before generating the workbook.", icon="🚫")

    if st.button(
        "✨ Generate workbook", type="primary", disabled=not missing_price_ok, width="stretch",
    ):
        generate_workbook(vehicle_inputs, capital_tab_name, recurrent_tab_name, recurrent_category_label, rate_choice, manual_rate)


def generate_workbook(vehicle_inputs, capital_tab_name, recurrent_tab_name,
                       recurrent_category_label, rate_choice, manual_rate) -> None:
    ce = workbook_builder._cost_engine()

    rate = manual_rate
    if rate_choice == "Fetch the current SP Group tariff automatically":
        with st.spinner("Fetching current electricity tariff from SP Group..."):
            rate = electricity.fetch_live_rate()
        if rate is None:
            st.error("Could not fetch a live electricity tariff right now. Switch to 'I'll enter my own rate' above and try again.")
            return

    try:
        with st.spinner("Building workbook..."):
            output_path = workbook_builder.build_workbook(
                vehicles=vehicle_inputs, capital_tab_name=capital_tab_name,
                recurrent_tab_name=recurrent_tab_name, electricity_rate=rate,
                recurrent_category_label=recurrent_category_label,
            )
    except ce.ValidationError as exc:
        st.error(str(exc))
        return
    except Exception as exc:  # noqa: BLE001
        st.error(f"Workbook generation failed: {exc}")
        return

    st.session_state.workbook_path = str(output_path)
    bot_say(f"Your workbook is ready — **{Path(output_path).name}**. Download it below.")
    st.rerun()


def render_download() -> None:
    if not st.session_state.workbook_path:
        return
    path = Path(st.session_state.workbook_path)
    with st.container(border=True):
        st.markdown("### 🎉 Your workbook is ready")
        st.caption(f"`{path.name}`")
        col1, col2 = st.columns(2)
        with col1:
            st.download_button(
                "⬇️ Download workbook", data=path.read_bytes(), file_name=path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch", type="primary",
            )
        with col2:
            if st.button("🔍 Start a new lookup", width="stretch"):
                st.session_state.step = 2
                st.session_state.resolved_vehicles = []
                st.session_state.workbook_path = None
                st.session_state.step3_confirmed = False
                bot_say(step2_greeting())
                st.rerun()


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def process_user_message(prompt: str) -> str:
    step = st.session_state.step
    if step == 1:
        return handle_step1(prompt)
    if step == 2:
        return handle_step2(prompt)
    if step == 3:
        return handle_step3(prompt)
    return "No further action needed. Click **Restart conversation** in the sidebar to look up more vehicles."


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def render_progress() -> None:
    """A persistent, always-visible indicator of where the user is and
    what's needed to move forward — added because typing 'done' or
    waiting for Claude to decide to transition wasn't a clear enough
    signal that the user COULD proceed."""
    ui_theme.render_stepper(
        st.session_state.step,
        ["Refresh data", "Find vehicles", "Cost workbook"],
    )


def render_continue_button() -> None:
    """Step 2 only: a real, always-visible button to move on — not just a
    typed 'done' or reliance on Claude deciding to call
    transition_to_step3. Shown whenever there's at least one vehicle in
    the roster, so it's always obvious the user CAN proceed."""
    if st.session_state.step != 2 or not st.session_state.resolved_vehicles:
        return
    count = len(st.session_state.resolved_vehicles)
    with st.container(border=True):
        col1, col2 = st.columns([3, 2], vertical_alignment="center")
        with col1:
            plural = "vehicle" if count == 1 else "vehicles"
            st.markdown(f"🚗 **{count} {plural}** in your roster")
            st.caption("Keep searching, or continue whenever you're ready.")
        with col2:
            if st.button(
                "Continue →", width="stretch", type="primary",
                help="Move on to Step 3 and set up the cost workbook for these vehicles.",
            ):
                st.session_state.step = 3
                bot_say(step3_greeting())
                st.rerun()


def render_step2_empty_state() -> None:
    """A friendlier first impression than a bare chat box — only shown
    before any vehicle has been found, so it never gets in the way once
    the conversation is actually underway."""
    if st.session_state.step != 2 or st.session_state.resolved_vehicles:
        return
    ui_theme.render_empty_state(
        "🔍", "No vehicles looked up yet",
        "Try a brand/model name, or describe what you need — "
        "e.g. \"SUV with at least 7 seats\" or \"sedans under 8s 0-100\".",
    )


def main() -> None:
    init_state()
    ui_theme.inject_global_css()
    ui_theme.render_hero(
        "MarIO",
        "Your one stop orchestrator to search online SG car listings and build a market survery workbook - one chat solution.",
        mascot_path=MASCOT_CHATBOT_PATH,
    )

    render_sidebar()
    render_progress()

    for msg in st.session_state.messages:
        avatar = MASCOT_CHATBOT_PATH if msg["role"] == "assistant" else None
        with st.chat_message(msg["role"], avatar=avatar):
            st.markdown(msg["content"])

    render_step2_empty_state()
    render_upload_fallback()
    render_continue_button()
    render_step3_form()
    render_download()

    prompt = st.chat_input("Type your message...")
    if prompt:
        user_say(prompt)
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant", avatar=MASCOT_CHATBOT_PATH):
            reply = process_user_message(prompt)
            st.markdown(reply)
        bot_say(reply)
        st.rerun()


if __name__ == "__main__":
    main()
