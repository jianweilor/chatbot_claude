"""
Claude-assisted search & decision support.

`run_step2_agent` is the primary way Step 2 now answers user questions
when an API key is present: a real tool-use agent loop (Claude decides
which tool(s) to call, Python executes them against the actual CSV, the
results go back to Claude, repeat until it has a final answer). Claude
never sees or invents vehicle facts directly — every number, name, and
spec it uses comes back from a tool call against the real dataset, so
whatever Claude gets wrong is a wrong tool call (zero/wrong results), not
a fabricated spec. The system prompt also tells it explicitly which
features (ABS, EBD, ADAS, seats-up-only boot capacity) this dataset
doesn't track, so it reports that rather than guessing from general
knowledge.

`extract_filters_with_claude` and `recommend_with_claude` remain as
smaller, single-purpose helpers other code can reuse, but the agent below
supersedes them as Step 2's main entry point.

Step 1 (subprocess side effects) and Step 3 (financial numeric inputs,
tab-name validation) deliberately stay outside the agent's tool set — see
README's "Claude-assisted search & recommendations" section for why.
"""

from __future__ import annotations

import json

from utils import csv_tools
from utils.config import ANTHROPIC_MODEL

RECOMMEND_KEYWORDS = (
    "recommend", "which one", "which is best", "which should", "help me decide",
    "help me choose", "best for", "suggest one", "what would you pick", "what's best",
)


def looks_like_decision_request(text: str) -> bool:
    t = text.strip().lower()
    return any(k in t for k in RECOMMEND_KEYWORDS)


def _get_client(api_key: str):
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


FILTER_EXTRACTION_TOOL = {
    "name": "extract_vehicle_filters",
    "description": (
        "Extract concrete vehicle search criteria that the user explicitly stated or "
        "clearly implied. Only include a field if a value for it was actually given — "
        "never invent, estimate, or default a number. Every field is optional; omit "
        "anything not mentioned."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "vehicle_type": {
                "type": "string",
                "enum": [t.title() if t != "suv" else "SUV" for t in csv_tools.KNOWN_VEHICLE_TYPES],
                "description": "Body type the user wants, if stated.",
            },
            "accel_max": {"type": "number", "description": "Max 0-100km/h time in seconds (user wants at most this)."},
            "accel_min": {"type": "number", "description": "Min 0-100km/h time in seconds (user wants at least this)."},
            "price_max": {"type": "number", "description": "Max all-in price in Singapore dollars."},
            "seats_min": {"type": "integer", "description": "Min seating capacity."},
            "range_min": {"type": "number", "description": "Min drive range in km on a full charge."},
            "boot_min": {"type": "number", "description": "Min boot/cargo capacity in litres."},
            "height_max_mm": {"type": "number", "description": "Max overall vehicle height in millimetres."},
        },
        "additionalProperties": False,
    },
}


def extract_filters_with_claude(query: str, api_key: str, model: str = ANTHROPIC_MODEL) -> tuple[dict, str | None]:
    """Returns (filters_dict, error_message). filters_dict matches
    csv_tools.parse_filters()'s schema exactly, so the caller runs it
    through the identical deterministic apply_criteria_filter() — this
    function never touches the actual vehicle data."""
    try:
        client = _get_client(api_key)
        response = client.messages.create(
            model=model,
            max_tokens=300,
            tools=[FILTER_EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": "extract_vehicle_filters"},
            messages=[{"role": "user", "content": query}],
        )
    except Exception as exc:  # noqa: BLE001 - surface plainly, never crash the chat
        return {}, f"Claude couldn't be reached to help interpret that ({exc})."

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        return {}, None

    raw = dict(tool_use.input)
    filters = {}
    if "vehicle_type" in raw and raw["vehicle_type"]:
        filters["vehicle_type"] = str(raw["vehicle_type"]).lower().replace(" ", "")
    for key in ("accel_max", "accel_min", "price_max", "range_min", "boot_min", "height_max_mm"):
        if key in raw and raw[key] is not None:
            filters[key] = float(raw[key])
    if "seats_min" in raw and raw["seats_min"] is not None:
        filters["seats_min"] = int(raw["seats_min"])

    return filters, None


RECOMMEND_SYSTEM_PROMPT = (
    "You are helping compare a short list of electric vehicles for a Singapore "
    "fleet-procurement decision. You may ONLY use the vehicle data provided in the "
    "user message below — do not use any outside knowledge about these models' "
    "specs, pricing, or features, even if you recognise them, since the data provided "
    "is the authoritative source and general knowledge may be outdated or wrong for "
    "the Singapore market specifically. If information needed to answer isn't in the "
    "data provided, say so explicitly rather than guessing. Keep your answer to "
    "roughly 100-150 words, refer to vehicles by the exact name given, and cite the "
    "specific figures from the data that your recommendation is based on."
)


def _candidate_dict(row) -> dict:
    total_price, price_note = csv_tools.resolve_true_price(row)
    return {
        "name": row["FullName"],
        "price": price_note,
        "vehicle_type": str(row.get("Vehicle Type") or "unknown"),
        "seating_capacity": csv_tools.field_value(row, "Seating Capacity"),
        "drive_range": csv_tools.field_value(row, "Drive Range"),
        "boot_cargo_capacity": csv_tools.field_value(row, "Boot/Cargo Capacity"),
        "acceleration_0_100": csv_tools.field_value(row, "Acceleration"),
        "dimensions_l_w_h": csv_tools.field_value(row, "Dimensions (L x W x H)"),
    }


def recommend_with_claude(query: str, candidate_rows, api_key: str, model: str = ANTHROPIC_MODEL) -> str:
    """candidate_rows: iterable of pandas Series (actual CSV rows). Returns
    Claude's grounded recommendation text, or a plain error string on
    failure — never raises, so a bad/missing key can't crash the chat."""
    candidates = [_candidate_dict(row) for row in candidate_rows]
    if not candidates:
        return "There's nothing to recommend from yet — search for some vehicles first."

    user_content = (
        f"Candidate vehicles (JSON, from the SGCarmart dataset):\n{json.dumps(candidates, indent=2)}\n\n"
        f"User's question: {query}"
    )
    try:
        client = _get_client(api_key)
        response = client.messages.create(
            model=model,
            max_tokens=500,
            temperature=0.2,
            system=RECOMMEND_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except Exception as exc:  # noqa: BLE001
        return f"Claude couldn't be reached for a recommendation right now ({exc})."

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or "Claude didn't return a recommendation — try rephrasing your question."


# ---------------------------------------------------------------------------
# Step 2 agent — Claude answers all Step 2 questions via real tool calls
# ---------------------------------------------------------------------------

AGENT_TOOLS = [
    {
        "name": "search_vehicles",
        "description": (
            "Look up vehicles by brand/model keyword against the real dataset. Returns "
            "status 'exact' (one vehicle, with its data), 'ambiguous' (multiple matches — "
            "ask the user which one, or offer 'all'), or 'none'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Brand/model/trim keyword."}},
            "required": ["query"],
        },
    },
    {
        "name": "filter_vehicles",
        "description": (
            "Filter vehicles by concrete spec/price criteria. Only pass fields the user "
            "actually specified or clearly implied — never invent a number. All numeric "
            "fields are lower/upper bounds (>=/<=), not exact equality. Returns matches "
            "plus a count of vehicles excluded only because their own data was missing/"
            "unlisted for one of the criteria (not confirmed non-matches — mention this "
            "count to the user and offer show_missing_data_vehicles if they want to see them)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "vehicle_type": {
                    "type": "string",
                    "enum": [t.title() if t != "suv" else "SUV" for t in csv_tools.KNOWN_VEHICLE_TYPES],
                },
                "accel_max": {"type": "number", "description": "Max 0-100km/h time in seconds."},
                "accel_min": {"type": "number", "description": "Min 0-100km/h time in seconds."},
                "price_max": {"type": "number", "description": "Max all-in price in SGD."},
                "seats_min": {"type": "integer", "description": "Min seating capacity."},
                "range_min": {"type": "number", "description": "Min drive range in km."},
                "boot_min": {"type": "number", "description": "Min boot/cargo capacity in litres."},
                "height_max_mm": {"type": "number", "description": "Max overall height in mm."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_vehicle_fields",
        "description": (
            "Get specific factual field(s) for ONE vehicle, identified by its exact "
            "full_name as returned by search_vehicles/filter_vehicles — never guess or "
            "reuse a name from your own knowledge. Price is automatically COE-corrected. "
            "Pass fields=['everything'] for the full spec sheet. Any field this CSV "
            "doesn't track (ABS, EBD, ADAS, airbags, ISOFIX, etc.) comes back explicitly "
            "marked 'not available in this dataset'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "full_name": {"type": "string"},
                "fields": {
                    "type": "array", "items": {"type": "string"},
                    "description": "e.g. ['price','road tax','coe','drive range','boot space','seating capacity','acceleration','top speed','battery capacity','dimensions']",
                },
            },
            "required": ["full_name", "fields"],
        },
    },
    {
        "name": "list_all_models",
        "description": "List every vehicle model name in the dataset.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "show_missing_data_vehicles",
        "description": (
            "Shows the vehicles excluded from the most recent filter_vehicles call "
            "purely because their own data was missing/unlisted for one of the "
            "criteria — never confirmed as non-matches. Use when the user asks to see "
            "those excluded vehicles."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_to_roster",
        "description": (
            "Adds ONE vehicle (by its exact full_name from a prior search_vehicles/"
            "filter_vehicles result) to the user's roster for the Step 3 cost workbook. "
            "Only call this once the user has actually confirmed they want that vehicle "
            "added — don't add speculatively."
        ),
        "input_schema": {"type": "object", "properties": {"full_name": {"type": "string"}}, "required": ["full_name"]},
    },
    {
        "name": "get_roster",
        "description": "Lists vehicles currently in the user's roster for the cost workbook.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "remove_from_roster",
        "description": (
            "Removes a vehicle from the user's roster — by its exact full_name, or by its "
            "1-based position from get_roster's list. Use this when the user asks to remove/"
            "delete/drop a vehicle, e.g. because its CSV data turned out to be incomplete "
            "and is causing errors, or it was added by mistake."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "full_name": {"type": "string", "description": "Exact full_name to remove, if known."},
                "position": {"type": "integer", "description": "1-based position in the roster, if full_name isn't known — call get_roster first."},
            },
        },
    },
    {
        "name": "transition_to_step3",
        "description": (
            "Call this once the user confirms they're done searching and ready to move "
            "on to generating the cost workbook (Step 3). Requires at least one vehicle "
            "already in the roster."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]

UNAVAILABLE_FEATURES_NOTE = (
    "This dataset does NOT include: ABS, EBD/EDB, ADAS, airbag count/type, ISOFIX, or a "
    "seats-up-only (vs. seats-down) boot capacity distinction. If asked about any of "
    "these, say plainly the data isn't available in this dataset — never guess or infer "
    "from general knowledge of these car models."
)


def _agent_system_prompt(df) -> str:
    return (
        f"You are the search & information assistant for Step 2 of a Streamlit chatbot "
        f"that helps with EV fleet cost analysis for the Singapore market. The dataset "
        f"you have access to (via your tools) is {len(df)} EV listings scraped from "
        f"SGCarmart, covering pricing (COE, road tax, OMV, ARF, VES, EEAI), specs "
        f"(acceleration, range, seating, boot capacity, dimensions, battery, charging), "
        f"and vehicle type.\n\n"
        f"CRITICAL: you must never answer a factual question about a specific vehicle's "
        f"price, spec, or feature from your own general knowledge, even if you recognise "
        f"the model — always call a tool. Your training data can easily be stale or wrong "
        f"for Singapore-market pricing and trims, and the dataset is the authoritative "
        f"source for this task. Every number or name you state to the user must have come "
        f"back from a tool call in this conversation.\n\n"
        f"{UNAVAILABLE_FEATURES_NOTE}\n\n"
        f"Workflow notes:\n"
        f"- Resolve a vehicle to its exact full_name via search_vehicles or "
        f"filter_vehicles before calling get_vehicle_fields or add_to_roster.\n"
        f"- Current price in the raw data can exclude COE — get_vehicle_fields and "
        f"filter_vehicles always return the COE-corrected all-in price, so quote that, "
        f"never a raw figure you haven't retrieved.\n"
        f"- If filter_vehicles reports excluded-due-to-missing-data vehicles, mention the "
        f"count and that you can show them via show_missing_data_vehicles if asked — "
        f"don't just drop that information.\n"
        f"- Only call add_to_roster once the user has actually confirmed a vehicle — "
        f"don't add things speculatively.\n"
        f"- If the user wants to remove a vehicle — including because its CSV data turns "
        f"out to be incomplete/causing errors — use remove_from_roster (call get_roster "
        f"first if you need the exact name or position).\n"
        f"- Call transition_to_step3 once the user confirms they're ready to move on.\n"
        f"- Keep answers concise, use Markdown, and return only the fields the user "
        f"actually asked about rather than dumping everything, unless they ask for "
        f"'everything'."
    )


def _row_public_dict(row) -> dict:
    _, price_note = csv_tools.resolve_true_price(row)
    return {
        "full_name": row["FullName"],
        "price": price_note,
        "vehicle_type": str(row.get("Vehicle Type") or "unknown"),
    }


def _detail_fields(row, filters: dict) -> dict:
    details = {}
    if "seats_min" in filters:
        details["seating_capacity"] = csv_tools.field_value(row, "Seating Capacity")
    if "range_min" in filters:
        details["drive_range"] = csv_tools.field_value(row, "Drive Range")
    if "boot_min" in filters:
        details["boot_cargo_capacity"] = csv_tools.field_value(row, "Boot/Cargo Capacity")
    if "height_max_mm" in filters:
        details["dimensions"] = csv_tools.field_value(row, "Dimensions (L x W x H)")
    if "accel_max" in filters or "accel_min" in filters:
        details["acceleration"] = csv_tools.field_value(row, "Acceleration")
    return details


def _tool_search_vehicles(df, tool_input: dict) -> dict:
    query = (tool_input.get("query") or "").strip()
    if not query:
        return {"status": "none"}
    result = csv_tools.search_vehicles(df, query)
    if result.status == "exact":
        return {"status": "exact", "vehicle": _row_public_dict(result.row)}
    if result.status == "ambiguous":
        matches = [_row_public_dict(r) for _, r in result.matches.head(20).iterrows()]
        return {"status": "ambiguous", "count": len(result.matches), "matches": matches}
    return {"status": "none"}


def _tool_filter_vehicles(df, tool_input: dict, session_state) -> dict:
    filters = {}
    if tool_input.get("vehicle_type"):
        filters["vehicle_type"] = str(tool_input["vehicle_type"]).lower().replace(" ", "")
    for key in ("accel_max", "accel_min", "price_max", "range_min", "boot_min", "height_max_mm"):
        if tool_input.get(key) is not None:
            filters[key] = float(tool_input[key])
    if tool_input.get("seats_min") is not None:
        filters["seats_min"] = int(tool_input["seats_min"])

    if not filters:
        return {"error": "No filter criteria provided."}

    matches, gap_rows = csv_tools.apply_criteria_filter(df, filters)
    session_state["last_data_gap_rows"] = gap_rows
    session_state["last_data_gap_filters"] = filters

    return {
        "filters_applied": csv_tools.describe_filters(filters),
        "count": len(matches),
        "matches": [
            {**_row_public_dict(r), **_detail_fields(r, filters)}
            for _, r in matches.head(20).iterrows()
        ],
        "excluded_due_to_missing_data_count": len(gap_rows),
    }


def _resolve_exact_row(df, full_name: str):
    row_matches = df[df["FullName"] == full_name]
    if row_matches.empty:
        row_matches = df[df["FullName"].str.strip().str.lower() == full_name.strip().lower()]
    return None if row_matches.empty else row_matches.iloc[0]


def _tool_get_vehicle_fields(df, tool_input: dict) -> dict:
    full_name = tool_input.get("full_name", "")
    fields = tool_input.get("fields") or []
    row = _resolve_exact_row(df, full_name)
    if row is None:
        return {"error": f"No vehicle found with exact full_name '{full_name}'. Call search_vehicles or filter_vehicles first to get the exact name."}
    return {"full_name": row["FullName"], "fields": csv_tools.get_fields(row, fields)}


def _tool_show_missing_data(session_state) -> dict:
    gap_rows = session_state.get("last_data_gap_rows")
    if gap_rows is None or gap_rows.empty:
        return {"count": 0, "vehicles": [], "note": "No pending excluded/missing-data set from a recent filter_vehicles call."}
    filters = session_state.get("last_data_gap_filters") or {}
    vehicles = [{**_row_public_dict(r), **_detail_fields(r, filters)} for _, r in gap_rows.iterrows()]
    return {"count": len(gap_rows), "vehicles": vehicles}


def _tool_add_to_roster(df, tool_input: dict, session_state) -> dict:
    full_name = tool_input.get("full_name", "")
    row = _resolve_exact_row(df, full_name)
    if row is None:
        return {"error": f"No exact vehicle named '{full_name}' — resolve it via search_vehicles/filter_vehicles first."}
    resolved = session_state.setdefault("resolved_vehicles", [])
    resolved.append({
        "full_name": row["FullName"],
        "display_name_hint": row["FullName"],
        "vehicle_type": str(row.get("Vehicle Type") or "").strip() or "Unspecified",
        "coe": row.get("COE"),
        "_row": row,
    })
    return {"ok": True, "added": row["FullName"], "roster_size": len(resolved)}


def _tool_get_roster(session_state) -> dict:
    resolved = session_state.get("resolved_vehicles") or []
    return {"count": len(resolved), "vehicles": [v["display_name_hint"] for v in resolved]}


def _tool_remove_from_roster(tool_input: dict, session_state) -> dict:
    resolved = session_state.get("resolved_vehicles") or []
    if not resolved:
        return {"error": "Roster is already empty."}

    idx = None
    full_name = tool_input.get("full_name")
    position = tool_input.get("position")
    if full_name:
        idx = next((i for i, v in enumerate(resolved) if v["full_name"] == full_name), None)
    if idx is None and position:
        if 1 <= position <= len(resolved):
            idx = position - 1
    if idx is None:
        return {"error": "Could not find that vehicle in the roster — call get_roster first to see exact names/positions."}

    removed = resolved.pop(idx)
    return {"ok": True, "removed": removed["full_name"], "roster_size": len(resolved)}


def _execute_tool(name: str, tool_input: dict, df, session_state) -> dict:
    if name == "search_vehicles":
        return _tool_search_vehicles(df, tool_input)
    if name == "filter_vehicles":
        return _tool_filter_vehicles(df, tool_input, session_state)
    if name == "get_vehicle_fields":
        return _tool_get_vehicle_fields(df, tool_input)
    if name == "list_all_models":
        return {"models": csv_tools.list_all_models(df)}
    if name == "show_missing_data_vehicles":
        return _tool_show_missing_data(session_state)
    if name == "add_to_roster":
        return _tool_add_to_roster(df, tool_input, session_state)
    if name == "get_roster":
        return _tool_get_roster(session_state)
    if name == "remove_from_roster":
        return _tool_remove_from_roster(tool_input, session_state)
    if name == "transition_to_step3":
        if not (session_state.get("resolved_vehicles") or []):
            return {"error": "Roster is empty — add at least one vehicle before moving to Step 3."}
        session_state["_transitioned_to_step3"] = True
        return {"ok": True, "note": "Moved to Step 3."}
    return {"error": f"unknown tool '{name}'"}


def _serialize_blocks(blocks) -> list[dict]:
    out = []
    for b in blocks:
        if b.type == "text":
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return out


MAX_AGENT_TOOL_ROUNDS = 6
MAX_AGENT_HISTORY_MESSAGES = 20  # ~10 clean user/assistant exchanges


def run_step2_agent(prompt: str, api_key: str, df, session_state, model: str = ANTHROPIC_MODEL) -> str:
    """Step 2's main entry point when an API key is present: Claude
    answers via real tool calls against the actual CSV (never from its
    own knowledge). Only the clean final text exchange is persisted into
    session_state['agent_messages'] for next-turn context — intermediate
    tool_use/tool_result blocks are kept local to this call so history
    can't grow unbounded or end up with orphaned tool blocks.

    Side effects (add_to_roster, transition_to_step3) are applied directly
    to session_state during tool execution, same as the deterministic path.
    """
    try:
        client = _get_client(api_key)
    except Exception as exc:  # noqa: BLE001
        return f"Claude couldn't be reached ({exc}). Check your API key in the sidebar."

    history = session_state.get("agent_messages") or []
    working_messages = list(history) + [{"role": "user", "content": prompt}]
    system_prompt = _agent_system_prompt(df)

    final_text = None
    for _ in range(MAX_AGENT_TOOL_ROUNDS):
        try:
            response = client.messages.create(
                model=model, max_tokens=1024, system=system_prompt,
                tools=AGENT_TOOLS, messages=working_messages,
            )
        except Exception as exc:  # noqa: BLE001
            return f"Claude couldn't be reached right now ({exc})."

        working_messages.append({"role": "assistant", "content": _serialize_blocks(response.content)})
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        if not tool_use_blocks:
            final_text = "".join(b.text for b in response.content if b.type == "text").strip()
            break

        tool_results = []
        for b in tool_use_blocks:
            result = _execute_tool(b.name, b.input, df, session_state)
            tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(result, default=str)})
        working_messages.append({"role": "user", "content": tool_results})

    if final_text is None:
        final_text = "That took more steps than expected to look up — could you rephrase or narrow your question?"

    session_state["agent_messages"] = (
        list(history) + [{"role": "user", "content": prompt}, {"role": "assistant", "content": final_text}]
    )[-MAX_AGENT_HISTORY_MESSAGES:]

    return final_text
