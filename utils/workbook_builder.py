"""
Step 3 engine — builds the Summary / Capital Cost / Recurrent Cost workbook.

Deliberately does NOT reimplement any cost logic: it assembles the same
in-memory shapes combinedcode.py's own batch mode (main_batch) already
consumes — RosterVehicle list + recurrent_inputs list — and calls
assemble_and_save() directly (in-process, no subprocess needed here since
this is pure pandas/openpyxl with no Chrome/Selenium dependency).

Critical invariant carried over from the instructions: every vehicle's
display_name must be identical across the Summary/Capital/Recurrent tabs.
That's guaranteed here because the SAME RosterVehicle.display_name is what
CapitalCostBuilder, RecurrentCostBuilder, and SummaryBuilder all read from
inside assemble_and_save() — the caller only has to set it once.

PRICE CORRECTION (important): combinedcode.py's CapitalCostBuilder reads
`row["Current price"]` directly via its own parse_money(), which does NOT
account for the "(w/o COE)" suffix ~27% of rows carry — the same bug fixed
for chat display, but that fix never propagated to the actual Excel
output. Left alone, a w/o-COE vehicle's capital cost figure in the
generated workbook would have the correct COE premium subtracted from a
price that never included it, understating capital cost by the COE
amount. build_workbook() now patches every RosterVehicle's row with the
COE-corrected all-in price (via csv_tools.resolve_true_price) before
handing the roster to assemble_and_save(), so the workbook always uses the
same true price shown in chat. For POA/otherwise-unresolvable prices,
where there's no way to compute a correct figure automatically, a
required manual_price on VehicleInput is used instead — see
VehicleInput.manual_price and app.py's Step 3 form.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from utils import csv_tools
from utils.config import (
    COMBINED_CSV_PATH,
    COE_CSV_PATH,
    CAPITAL_TEMPLATE_PATH,
    COST_ENGINE_DIR,
    OUTPUT_DIR,
)


def _cost_engine():
    path_str = str(COST_ENGINE_DIR)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
    import combinedcode
    return combinedcode


@dataclass
class VehicleInput:
    """One vehicle's Step 3 form data, keyed to the row resolved in Step 2."""
    full_name: str            # the CSV's exact FullName for this row — used
                               # to re-resolve the row unambiguously, since
                               # the user's original Step 2 keyword may have
                               # been ambiguous on its own
    display_name: str         # what the user confirmed should appear in all
                               # three tabs (defaults to full_name if unset)
    marked: bool
    maint_lt5: float
    maint_5to10: float
    capital_cost_projection: float | None = None
    vehicle_cat: str | None = None
    manual_price: float | None = None  # required only if the CSV price is
                                        # POA/unresolvable (see needs_manual_price())


def needs_manual_price(full_name: str, vehicle_df=None) -> tuple[bool, str]:
    """Checks whether a vehicle's CSV price can be resolved automatically.
    Returns (True, note) if a manual price is required (POA, unparsable
    COE, or any other reason resolve_true_price() can't produce a number),
    so the UI can ask for it BEFORE the user hits "Generate workbook"
    rather than after — a ValidationError mid-generation with no way to
    fix it in place is a dead end for the user."""
    ce = _cost_engine()
    df = vehicle_df if vehicle_df is not None else ce.load_vehicle_df(str(COMBINED_CSV_PATH))
    matches = df[df["FullName"] == full_name]
    if matches.empty:
        return True, "vehicle not found"
    total, note = csv_tools.resolve_true_price(matches.iloc[0])
    return total is None, note


def validate_tab_names(capital_tab_name: str, recurrent_tab_name: str):
    """Runs both names through combinedcode's own validate_sheet_name(),
    raising combinedcode.ValidationError on any problem (blank, >31 chars,
    invalid characters, or a collision with "Summary" or each other)."""
    ce = _cost_engine()
    taken = {"Summary"}
    capital_tab_name = ce.validate_sheet_name(capital_tab_name, taken)
    taken.add(capital_tab_name)
    recurrent_tab_name = ce.validate_sheet_name(recurrent_tab_name, taken)
    return capital_tab_name, recurrent_tab_name


def build_workbook(
    vehicles: list[VehicleInput],
    capital_tab_name: str,
    recurrent_tab_name: str,
    electricity_rate: float,
    recurrent_category_label: str = "",
    output_filename: str | None = None,
) -> Path:
    """Returns the path to the saved .xlsx workbook.

    Raises combinedcode.ValidationError (a ValueError subclass) on any
    problem — bad tab name, unresolvable/ambiguous vehicle, a POA/
    unparsable price with no manual_price supplied, etc. — so the caller
    (app.py) can show it inline rather than letting a traceback surface.
    """
    ce = _cost_engine()

    capital_tab_name, recurrent_tab_name = validate_tab_names(capital_tab_name, recurrent_tab_name)

    if not vehicles:
        raise ce.ValidationError("At least one vehicle is required to generate a workbook.")

    vehicle_df = ce.load_vehicle_df(str(COMBINED_CSV_PATH))

    # Re-resolve each vehicle by its exact FullName (guaranteed unique,
    # even if the user's original Step 2 keyword was ambiguous on its own)
    # and feed it through build_roster_from_batch so the resulting
    # RosterVehicle objects are identical in shape to what combinedcode.py
    # already expects.
    specs = []
    for v in vehicles:
        specs.append({
            "keyword": v.full_name,
            "display_name": v.display_name or v.full_name,
            "marked": v.marked,
            "vehicle_cat": v.vehicle_cat,
        })
    roster = ce.build_roster_from_batch(vehicle_df, specs)

    # Patch every roster row's price to the COE-corrected all-in figure
    # (or the user-supplied manual price for POA/unresolvable rows) before
    # it ever reaches CapitalCostBuilder — see module docstring.
    for rv, v in zip(roster, vehicles):
        total, note = csv_tools.resolve_true_price(rv.row)
        if total is not None:
            patched_row = rv.row.copy()
            patched_row["Current price"] = f"${total:,.2f}"
            rv.row = patched_row
        elif v.manual_price is not None and v.manual_price > 0:
            patched_row = rv.row.copy()
            patched_row["Current price"] = f"${v.manual_price:,.2f}"
            rv.row = patched_row
        else:
            raise ce.ValidationError(
                f"'{v.display_name}' has no usable price in the CSV ({note}) and no "
                "manual price was provided. Please enter a price for this vehicle and try again."
            )

    recurrent_inputs = [
        {
            "maint_lt5": v.maint_lt5,
            "maint_5to10": v.maint_5to10,
            "capital_cost_projection": v.capital_cost_projection,
        }
        for v in vehicles
    ]

    if not output_filename:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_filename = f"Fleet_Cost_Summary_{stamp}.xlsx"
    output_path = OUTPUT_DIR / output_filename

    saved_path = ce.assemble_and_save(
        capital_tab_name,
        recurrent_tab_name,
        str(CAPITAL_TEMPLATE_PATH),
        str(COMBINED_CSV_PATH),
        str(COE_CSV_PATH),
        roster,
        recurrent_inputs,
        electricity_rate,
        recurrent_category_label,
        str(output_path),
    )
    return Path(saved_path)
