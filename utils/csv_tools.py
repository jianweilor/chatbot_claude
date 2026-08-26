"""
Step 2 engine — vehicle lookup against sgcarmart_ev_combined.csv.

Deliberately reuses combinedcode.py's own matching logic (load_vehicle_df,
_match_rows, _resolve_unique_row) rather than reimplementing it, so Step 2's
results and Step 3's roster resolution can never drift out of sync with
each other.

Only ever reads the local CSV — never fetches sgcarmart.com or any other
site, per the instructions' Step 2 data-source rule.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from utils.config import COMBINED_CSV_PATH, COST_ENGINE_DIR

import re as _re

# Words/phrases stripped from a query ONLY as a fallback when the raw text
# doesn't match anything — never applied if the raw query already matched,
# so a real model name/trim containing one of these words (e.g. "Long
# Range") is never affected by this.
_QUESTION_NOISE_WORDS = [
    "what's", "whats", "what is", "how much is", "how much does", "how much",
    "tell me", "show me", "find me", "find", "give me", "i want to know",
    "please", "road tax", "price", "cost", "coe", "omv", "arf", "ves", "eeai",
    "seating capacity", "boot space", "cargo capacity", "drive range",
    "the", "for", "of", "and", "on",
]


def _strip_question_noise(query: str) -> str:
    text = query
    for phrase in sorted(_QUESTION_NOISE_WORDS, key=len, reverse=True):
        text = _re.sub(r"\b" + _re.escape(phrase) + r"\b", " ", text, flags=_re.IGNORECASE)
    text = _re.sub(r"[?]", " ", text)
    text = _re.sub(r"\s+", " ", text).strip()
    return text


def _token_match_rows(df: pd.DataFrame, keyword: str) -> pd.DataFrame:
    """Last-resort fallback: matches rows where every word in the (cleaned)
    query appears SOMEWHERE in FullName, in any order — unlike the strict
    substring match, this survives words like "Electric -" that appear in
    the CSV's own generated name but that no user would ever type. Only
    used when both the raw and noise-stripped substring matches come up
    empty, so it never overrides a more precise match."""
    tokens = [t for t in _re.split(r"\s+", keyword.strip()) if len(t) > 1]
    if not tokens:
        return df.iloc[0:0]
    mask = pd.Series(True, index=df.index)
    for t in tokens:
        mask &= df["FullName"].str.contains(_re.escape(t), case=False, na=False)
    return df[mask].reset_index(drop=True)



def _cost_engine():
    """Import combinedcode.py, ensuring cost_engine/ is on sys.path first
    so its own `import electricity_tariff as et` (inside
    fetch_electricity_rate_or_none) can resolve — that module lives
    alongside it in the same folder, matching the scripts' original
    flat-directory design."""
    path_str = str(COST_ENGINE_DIR)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
    import combinedcode  # noqa: E402
    return combinedcode


@dataclass
class LookupResult:
    status: str                      # "exact" | "ambiguous" | "filtered" | "none"
    row: pd.Series | None = None
    matches: pd.DataFrame | None = None
    keyword: str = ""
    unavailable_terms: list[str] = None   # criteria mentioned but not in the CSV
    data_gap_count: int = 0               # rows excluded for missing/unknown data, not confirmed non-matches
    data_gap_rows: pd.DataFrame | None = None  # those same rows, browsable on request

    def __post_init__(self):
        if self.unavailable_terms is None:
            self.unavailable_terms = []


def load_vehicle_df(csv_path: str | Path | None = None) -> pd.DataFrame:
    ce = _cost_engine()
    path = str(csv_path or COMBINED_CSV_PATH)
    return ce.load_vehicle_df(path)


def search_vehicles(df: pd.DataFrame, keyword: str) -> LookupResult:
    """Case-insensitive substring match against FullName, matching the
    same auto-resolve rules combinedcode.py uses elsewhere:
      - a single match, or an exact FullName/BaseModel match among several
        -> resolved automatically
      - multiple ambiguous matches -> returned for the user to pick from
      - no matches -> "none"

    If the raw query (e.g. "what's the road tax for BYD Seal Premium?")
    doesn't substring-match anything, this falls back to stripping common
    question phrasing and field words and retrying — a full sentence
    almost never appears verbatim in FullName, so without this, any
    natural-language field question about a real model would incorrectly
    report "no matches" instead of resolving the vehicle."""
    ce = _cost_engine()
    keyword = (keyword or "").strip()
    if not keyword:
        return LookupResult(status="none", keyword=keyword)

    matches = ce._match_rows(df, keyword)  # noqa: SLF001 - intentional reuse
    used_keyword = keyword
    via_token_fallback = False

    if matches.empty:
        cleaned = _strip_question_noise(keyword)
        if cleaned and cleaned.lower() != keyword.lower():
            substr_matches = ce._match_rows(df, cleaned)  # noqa: SLF001
            if not substr_matches.empty:
                matches = substr_matches
                used_keyword = cleaned
            else:
                token_matches = _token_match_rows(df, cleaned)
                if not token_matches.empty:
                    matches = token_matches
                    used_keyword = cleaned
                    via_token_fallback = True

    if matches.empty:
        return LookupResult(status="none", keyword=keyword)

    if via_token_fallback:
        # matches came from token-overlap, not strict substring, so
        # combinedcode's own _resolve_unique_row (which re-runs a strict
        # substring match internally) can't be reused here — decide
        # exact/ambiguous directly from what we already found.
        if len(matches) == 1:
            return LookupResult(status="exact", row=matches.iloc[0], keyword=used_keyword)
        return LookupResult(status="ambiguous", matches=matches, keyword=used_keyword)

    row = ce._resolve_unique_row(df, used_keyword)  # noqa: SLF001
    if row is not None:
        return LookupResult(status="exact", row=row, keyword=used_keyword)

    return LookupResult(status="ambiguous", matches=matches, keyword=used_keyword)


def list_all_models(df: pd.DataFrame) -> list[str]:
    return sorted(df["BaseModel"].dropna().unique().tolist())


def field_value(row: pd.Series, field: str) -> str | None:
    """Returns a single field from a resolved row, or None if that column
    isn't present in the CSV at all (e.g. ABS/ADAS, which aren't scraped
    columns) — the caller should say so explicitly rather than guessing."""
    if field not in row.index:
        return None
    value = row[field]
    if pd.isna(value):
        return None
    return str(value)


# Recognised field aliases -> actual CSV column name. Checked longest-alias
# first so e.g. "boot space" matches before a bare "boot". Price/COE fields
# route through resolve_true_price/raw text explicitly in
# answer_field_questions() below rather than being dumped verbatim, since
# "Current price" alone can be the misleading w/o-COE figure.
FIELD_ALIASES = {
    "road tax": "Road tax", "omv": "OMV", "arf": "ARF", "ves": "VES", "eeai": "EEAI",
    "drive range": "Drive Range", "range": "Drive Range",
    "boot space": "Boot/Cargo Capacity", "cargo capacity": "Boot/Cargo Capacity",
    "boot capacity": "Boot/Cargo Capacity", "boot": "Boot/Cargo Capacity",
    "seating capacity": "Seating Capacity", "seats": "Seating Capacity",
    "acceleration": "Acceleration", "0-100": "Acceleration",
    "top speed": "Top Speed",
    "battery capacity": "Battery Capacity", "battery type": "Battery Type",
    "dimensions": "Dimensions (L x W x H)", "kerb weight": "Kerb Weight", "weight": "Kerb Weight",
    "wheelbase": "Wheelbase", "turning radius": "Min Turning Radius",
    "drive type": "Drive Type", "transmission": "Transmission",
    "power": "Power", "torque": "Torque",
    "energy consumption": "Energy Consumption",
    "rim size": "Rim Size",
    "ac charging time": "AC Charging Time", "dc charging time": "DC Charging Time",
    "ac charging": "AC Max Charging Rate", "dc charging": "DC Max Charging Rate",
}


def detect_requested_fields(query: str) -> list[str]:
    """Finds which specific spec/cost fields the query is actually asking
    about, so the bot can answer that instead of silently ignoring the
    question. Returns CSV column names, longest-alias-first so "boot space"
    isn't shadowed by a shorter accidental substring."""
    q = query.lower()
    found = []
    for alias in sorted(FIELD_ALIASES, key=len, reverse=True):
        if alias in q and FIELD_ALIASES[alias] not in found:
            found.append(FIELD_ALIASES[alias])
    return found


def get_fields(row: pd.Series, requested: list[str]) -> dict[str, str]:
    """Structured (JSON-friendly) sibling of answer_field_questions(), for
    the Claude-agent tools — given arbitrary field-name strings from the
    model (e.g. 'price', 'road tax', 'boot space', 'battery capacity'),
    resolves each to the real CSV column and returns the actual value, or
    an explicit 'not available in this dataset' / 'not listed in the CSV'
    rather than ever leaving a gap for the model to fill in from its own
    knowledge. Passing 'everything' as the only item returns the full row."""
    if len(requested) == 1 and requested[0].strip().lower() in ("everything", "all", "full spec", "all details"):
        result = {col: (field_value(row, col) or "not listed in the CSV") for col in row.index
                  if col not in ("pricing_url", "spec_url")}
        _, price_note = resolve_true_price(row)
        result["Current price (COE-corrected)"] = price_note
        return result

    result: dict[str, str] = {}
    for raw_field in requested:
        f = raw_field.strip().lower()
        if f in ("price", "cost", "all-in price", "total price"):
            _, note = resolve_true_price(row)
            result["price"] = note
            continue
        if f == "coe":
            result["coe"] = field_value(row, "COE") or "not listed"
            continue
        col = FIELD_ALIASES.get(f)
        if col is None:
            col = next((c for c in row.index if c.lower() == f), None)
        if col is None:
            result[raw_field] = "not available in this dataset"
        else:
            val = field_value(row, col)
            result[raw_field] = val if val is not None else "not listed in the CSV"
    return result


def answer_field_questions(row: pd.Series, query: str) -> str | None:
    """Returns a factual answer for whichever specific fields the query
    asked about, or None if the query didn't name any recognisable field.
    Price and COE are handled specially since the raw 'Current price'
    column can itself be misleading (w/o-COE figures) — resolve_true_price
    is used instead of echoing the column verbatim."""
    q = query.lower()
    lines = []

    if any(w in q for w in ("price", "cost", "how much")):
        total, note = resolve_true_price(row)
        lines.append(f"Price: {note}")

    if "coe" in q and "coe" not in FIELD_ALIASES:  # avoid double-answering if already in aliases below
        coe_val = field_value(row, "COE")
        lines.append(f"COE: {coe_val or 'not listed'}")

    for col in detect_requested_fields(query):
        label = col.replace(" (L x W x H)", "")
        val = field_value(row, col)
        lines.append(f"{label}: {val if val is not None else 'not listed in the CSV'}")

    if not lines:
        return None
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Natural-language criteria filtering (chatbot Step 2)
#
# Model names alone don't cover requests like "sedans that do 0-100 in
# under 8 seconds" or "seating capacity of 5 pax, 400L boot, 400km range" —
# those need to be matched against actual spec columns, not FullName. This
# is a small, deterministic regex parser (no LLM call, no API key needed)
# covering the concrete technical criteria this dataset actually supports:
# vehicle type, acceleration, price, seating, drive range, boot/cargo
# capacity, and overall vehicle height (parsed from Dimensions L x W x H).
#
# Anything the query asks for that ISN'T a column in this CSV (ABS, EBD,
# ADAS, "without folding seats", etc.) is explicitly flagged back to the
# user rather than silently ignored or guessed — per the standing rule
# that unmeasurable criteria get flagged, not skipped. Nothing here ever
# looks anything up on the internet; it only reads columns already present
# in the local CSV.
# ---------------------------------------------------------------------------

KNOWN_VEHICLE_TYPES = [
    "suv", "sedan", "hatchback", "mpv", "commercial", "sports",
    "stationwagon", "station wagon", "luxury", "coupe", "van", "pickup",
]

# Regex pattern -> human-readable label. If any of these appear in the
# query, they're reported as "not available in this dataset" rather than
# silently dropped or guessed at.
UNAVAILABLE_FEATURE_PATTERNS = [
    (r"\babs\b|anti[- ]lock(?:ing)? braking", "ABS (anti-lock braking system)"),
    (r"\bebd\b|\bedb\b|electronic brakeforce distribution", "EBD/EDB (electronic brakeforce distribution)"),
    (r"\badas\b|advanced driver.assist", "ADAS (advanced driver-assistance systems)"),
    (r"without (?:the )?need to fold|without folding|no need to fold", "boot space specifically without folding seats (the CSV doesn't distinguish seats-up vs seats-down capacity)"),
    (r"\bairbags?\b", "airbag count/type"),
    (r"\bisofix\b", "ISOFIX child-seat mounts"),
]


def _extract_number(value) -> float | None:
    """Grabs the FIRST number found in a value. Correct for columns like
    Acceleration ('9.4 s (0-100 km/h)' — the '100' from the descriptor
    must NOT be picked up) and Seating Capacity/Drive Range, which are
    always single plain numbers."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if text.lower() == "unknown":
        return None
    m = _re.search(r"[\d.]+", text)
    return float(m.group()) if m else None


def _extract_max_number(value) -> float | None:
    """Grabs the LARGEST number found in a value — used specifically for
    Boot/Cargo Capacity, where ~15% of rows are a range like '180 - 580 L'
    (seats-up to seats-down). Filtering on the smaller number would
    silently exclude vehicles whose larger capacity actually satisfies a
    'boot space of at least Xl' request; the raw range is still shown
    alongside every result so the person can see both figures themselves."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if text.lower() == "unknown":
        return None
    numbers = _re.findall(r"[\d.]+", text)
    return max(float(n) for n in numbers) if numbers else None


def resolve_true_price(row: pd.Series) -> tuple[float | None, str]:
    """Returns (numeric all-in price or None, a human-readable note).

    IMPORTANT: ~27% of rows in this dataset list a price with a
    "(w/o COE)" suffix — that figure excludes COE entirely, and COE
    routinely adds $100k+ in Singapore. Comparing that raw number against
    a price threshold (as an earlier version of this filter did) silently
    misrepresents those vehicles' actual cost. This adds the row's own COE
    column back in whenever the price is w/o-COE, and returns None (rather
    than a misleading number) if the COE amount itself can't be parsed."""
    ce = _cost_engine()
    raw = row.get("Current price")
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None, "price not listed"

    text = str(raw)
    if text.strip().upper() == "POA":
        return None, "POA (price on application — not listed)"

    base = ce.parse_money(text)
    if base is None:
        return None, "price unparsable"

    if "w/o coe" in text.lower() or "w/o  coe" in text.lower():
        coe_amount = ce.parse_money(row.get("COE"))
        if coe_amount is None:
            return None, f"${base:,.0f} excl. COE — COE amount unlisted, true total unknown"
        total = base + coe_amount
        return total, f"${total:,.0f} (${base:,.0f} excl. COE + ${coe_amount:,.0f} COE)"

    return base, f"${base:,.0f}"


def _extract_height_mm(dimensions_value) -> float | None:
    """'5060 x 1980 x 1790 mm' -> 1790.0 (the third of the L x W x H
    numbers). Returns None if the field isn't in the expected 3-number
    format."""
    if dimensions_value is None or (isinstance(dimensions_value, float) and pd.isna(dimensions_value)):
        return None
    numbers = _re.findall(r"[\d.]+", str(dimensions_value))
    if len(numbers) < 3:
        return None
    return float(numbers[2])


def find_unavailable_terms(query: str) -> list[str]:
    q = query.lower()
    found = []
    for pattern, label in UNAVAILABLE_FEATURE_PATTERNS:
        if _re.search(pattern, q) and label not in found:
            found.append(label)
    return found


def parse_filters(query: str) -> dict:
    """Extracts whatever concrete, CSV-backed criteria it can confidently
    find in a free-text query. Returns an empty dict if nothing
    recognisable is present. Numeric thresholds are all treated as
    minimums/maximums (>=/<=) rather than exact equality, since that's the
    more useful reading for a fleet-shortlisting search."""
    q = query.lower()
    filters: dict = {}

    # Pick whichever known type appears EARLIEST in the actual query text
    # (not first in KNOWN_VEHICLE_TYPES' list order) — otherwise a query
    # mentioning two types, e.g. "sedan or SUV", would silently pick "SUV"
    # every time just because it happens to come first in the list.
    best_type, best_pos = None, None
    for vtype in KNOWN_VEHICLE_TYPES:
        pos = q.find(vtype)
        if pos != -1 and (best_pos is None or pos < best_pos):
            best_type, best_pos = vtype, pos
    if best_type:
        filters["vehicle_type"] = best_type.replace(" ", "")

    # Acceleration (0-100 km/h), e.g. "accelerate ... under 8 seconds"
    if "acceler" in q or "0-100" in q or "0 to 100" in q:
        m = _re.search(r"(?:under|less than|below|within|<=?)\s*([\d.]+)\s*(?:s\b|sec)", q)
        if m:
            filters["accel_max"] = float(m.group(1))
        else:
            m = _re.search(r"(?:over|more than|above|>=?|at least)\s*([\d.]+)\s*(?:s\b|sec)", q)
            if m:
                filters["accel_min"] = float(m.group(1))

    # Price, e.g. "under $150,000", "below 100k"
    m = _re.search(r"(?:under|below|less than|<=?)\s*\$?\s*([\d,]+)\s*k\b", q)
    if m:
        filters["price_max"] = float(m.group(1).replace(",", "")) * 1000
    else:
        m = _re.search(r"(?:under|below|less than|<=?)\s*\$\s*([\d,]+)", q)
        if m:
            filters["price_max"] = float(m.group(1).replace(",", ""))

    # Seating — covers "at least 7 seats", "7-seater", "seating capacity
    # of 5 pax", "5 pax", "5 passengers", "capacity of 5".
    m = _re.search(r"(?:at least|minimum|min|>=)\s*(\d+)\s*(?:seats?|pax|passengers?)", q)
    if m:
        filters["seats_min"] = int(m.group(1))
    else:
        for pattern in (
            r"seating capacity of\s*(\d+)",
            r"capacity of\s*(\d+)\s*(?:pax|seats?|passengers?)?",
            r"(\d+)\s*pax\b",
            r"(\d+)\s*passengers?\b",
            r"(\d+)[\s-]*seater\b",
            r"(\d+)[\s-]*seats?\b",
        ):
            m = _re.search(pattern, q)
            if m:
                filters["seats_min"] = int(m.group(1))
                break

    # Drive range — covers "range of at least 400km", "over 500 km range",
    # "full charge driving endurance of at least 400km", "400km on a full
    # charge", "driving distance of 400km".
    range_keywords = r"(?:range|driving endurance|driving distance|distance per charge|full[- ]charge)"
    m = _re.search(range_keywords + r"[^\d]{0,25}([\d,]+)\s*km", q)
    if not m:
        m = _re.search(r"([\d,]+)\s*km[^\d]{0,25}" + range_keywords, q)
    if m:
        filters["range_min"] = float(m.group(1).replace(",", ""))

    # Boot/cargo capacity, e.g. "400L of boot space", "boot space of at
    # least 400L", "cargo capacity of 400 litres".
    boot_keywords = r"(?:boot|cargo)"
    m = _re.search(boot_keywords + r"[^\d]{0,25}([\d,]+)\s*(?:l\b|litres?|liters?)", q)
    if not m:
        m = _re.search(r"([\d,]+)\s*(?:l\b|litres?|liters?)[^\d]{0,25}" + boot_keywords, q)
    if m:
        filters["boot_min"] = float(m.group(1).replace(",", ""))

    # Overall vehicle height, e.g. "2.1m height clearance for carparks",
    # "height clearance of 2.1m" — read as a maximum (must fit under it).
    m = _re.search(r"([\d.]+)\s*m\b[^\d]{0,20}(?:height|clearance)", q)
    if not m:
        m = _re.search(r"height[^\d]{0,15}(?:clearance|limit|of)?[^\d]{0,10}([\d.]+)\s*m\b", q)
    if m:
        filters["height_max_mm"] = float(m.group(1)) * 1000

    return filters


def apply_criteria_filter(df: pd.DataFrame, filters: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Applies every parsed filter and returns (matching rows, rows
    excluded purely because one of the criteria's underlying data was
    missing/unlisted/unparsable — e.g. Acceleration == 'unknown'). The
    second DataFrame is reported and made browsable separately from
    genuine non-matches, so "this vehicle didn't match" and "we don't
    actually know" are never conflated — and the user can actually see
    those excluded rows on request rather than just being told a count."""
    result = df

    if "vehicle_type" in filters:
        result = result[result["Vehicle Type"].str.contains(filters["vehicle_type"], case=False, na=False)]

    candidate_pool = result
    missing_mask = pd.Series(False, index=candidate_pool.index)
    mask = pd.Series(True, index=candidate_pool.index)

    if "accel_max" in filters or "accel_min" in filters:
        accel = candidate_pool["Acceleration"].apply(_extract_number)
        missing_mask |= accel.isna()
        m = accel.notna()
        if "accel_max" in filters:
            m &= accel <= filters["accel_max"]
        if "accel_min" in filters:
            m &= accel >= filters["accel_min"]
        mask &= m

    if "price_max" in filters:
        prices = candidate_pool.apply(lambda r: resolve_true_price(r)[0], axis=1)
        missing_mask |= prices.isna()
        mask &= prices.notna() & (prices <= filters["price_max"])

    if "seats_min" in filters:
        seats = candidate_pool["Seating Capacity"].apply(_extract_number)
        missing_mask |= seats.isna()
        mask &= seats.notna() & (seats >= filters["seats_min"])

    if "range_min" in filters:
        rng = candidate_pool["Drive Range"].apply(_extract_number)
        missing_mask |= rng.isna()
        mask &= rng.notna() & (rng >= filters["range_min"])

    if "boot_min" in filters:
        boot = candidate_pool["Boot/Cargo Capacity"].apply(_extract_max_number)
        missing_mask |= boot.isna()
        mask &= boot.notna() & (boot >= filters["boot_min"])

    if "height_max_mm" in filters:
        height = candidate_pool["Dimensions (L x W x H)"].apply(_extract_height_mm)
        missing_mask |= height.isna()
        mask &= height.notna() & (height <= filters["height_max_mm"])

    # Rows with missing data for a needed field were never counted as
    # matches (mask requires notna()), so this is exactly the "excluded
    # because we don't know" set — distinct from rows we positively know
    # fail the threshold.
    excluded_missing_rows = candidate_pool[missing_mask & ~mask].reset_index(drop=True)

    return candidate_pool[mask].reset_index(drop=True), excluded_missing_rows


FILTER_LABELS = {
    "vehicle_type": lambda v: f"vehicle type: {v}",
    "accel_max": lambda v: f"0-100 km/h in under {v}s",
    "accel_min": lambda v: f"0-100 km/h in over {v}s",
    "price_max": lambda v: f"price under ${v:,.0f}",
    "seats_min": lambda v: f"at least {int(v)} seats",
    "range_min": lambda v: f"drive range of at least {v:.0f}km",
    "boot_min": lambda v: f"boot space of at least {v:.0f}L",
    "height_max_mm": lambda v: f"height under {v / 1000:.2f}m",
}


def describe_filters(filters: dict) -> str:
    return ", ".join(FILTER_LABELS[k](v) for k, v in filters.items() if k in FILTER_LABELS)


# Which raw CSV column(s) to surface per filter key, so a result line shows
# the actual data the match was based on — not just a derived pass/fail —
# and any parsing mistake would be immediately visible rather than hidden.
FILTER_DETAIL_COLUMNS = {
    "seats_min": [("Seats", "Seating Capacity")],
    "range_min": [("Range", "Drive Range")],
    "boot_min": [("Boot", "Boot/Cargo Capacity")],
    "height_max_mm": [("Dimensions", "Dimensions (L x W x H)")],
    "accel_max": [("0-100", "Acceleration")],
    "accel_min": [("0-100", "Acceleration")],
}


def format_match_line(row: pd.Series, index: int, filters: dict | None = None) -> str:
    """One numbered result line, always showing the true price (COE
    corrected) and vehicle type, plus the raw CSV value for every criterion
    actually used to filter — so the person can verify the match against
    real data instead of taking a derived label on faith."""
    _, price_note = resolve_true_price(row)
    parts = [price_note, str(row.get("Vehicle Type") or "n/a")]

    if filters:
        seen_cols = set()
        for key in filters:
            for label, col in FILTER_DETAIL_COLUMNS.get(key, []):
                if col in seen_cols:
                    continue
                seen_cols.add(col)
                value = row.get(col)
                parts.append(f"{label}: {value if pd.notna(value) else 'unknown'}")

    return f"{index}. {row['FullName']} — " + ", ".join(parts)


def smart_search(df: pd.DataFrame, query: str, max_results: int = 15) -> LookupResult:
    """Step 2's main entry point: tries a plain model-name match first
    (FullName substring), and if that comes up empty, falls back to
    criteria filtering (vehicle type / acceleration / price / seating /
    range / boot capacity / height) parsed from the query text. Any
    mentioned criteria that aren't CSV columns at all (ABS, EBD, ADAS,
    ...) are collected in unavailable_terms regardless of outcome, so the
    caller can flag them even when other criteria did match. Vehicles
    dropped only because their own data was missing/unlisted for one of
    the criteria are counted separately in data_gap_count — they were
    never confirmed as non-matches.

    Returns status "exact" | "ambiguous" | "filtered" | "none". "filtered"
    behaves like "ambiguous" for the caller (present a picker) but came
    from spec criteria rather than a name match.
    """
    unavailable = find_unavailable_terms(query)

    name_result = search_vehicles(df, query)
    if name_result.status in ("exact", "ambiguous"):
        name_result.unavailable_terms = unavailable
        return name_result

    filters = parse_filters(query)
    if not filters:
        return LookupResult(status="none", keyword=query, unavailable_terms=unavailable)

    matches, data_gap_rows = apply_criteria_filter(df, filters)
    if matches.empty:
        return LookupResult(
            status="none", keyword=query, unavailable_terms=unavailable,
            data_gap_count=len(data_gap_rows), data_gap_rows=data_gap_rows,
        )

    return LookupResult(
        status="filtered", matches=matches.head(max_results), keyword=query,
        unavailable_terms=unavailable, data_gap_count=len(data_gap_rows), data_gap_rows=data_gap_rows,
    )
