"""
Combine sgcarmart_ev_pricing.csv and sgcarmart_ev_specs.csv into one CSV.

Join key: (car URL without trailing /pricing or /specs, normalized SubModel)
  - pricing.SubModel sometimes has a trailing "\nFACELIFT" line -> stripped
  - specs.SubModel has a trailing transmission code like " (A)" / " (W)" -> stripped

Output: sgcarmart_ev_combined.csv
"""
import csv
import os
import re

# All CSVs are read/written in the shared "csv data" folder, not this script's folder.
CSV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
os.makedirs(CSV_DIR, exist_ok=True)

PRICING_FILE = os.path.join(CSV_DIR, "sgcarmart_ev_pricing.csv")
SPECS_FILE = os.path.join(CSV_DIR, "sgcarmart_ev_specs.csv")
OUTPUT_FILE = os.path.join(CSV_DIR, "sgcarmart_ev_combined.csv")


def car_key_from_url(url: str) -> str:
    return re.sub(r"/(pricing|specs)$", "", url.strip())


def normalize_pricing_submodel(submodel: str) -> str:
    return submodel.splitlines()[0].strip()


def normalize_specs_submodel(submodel: str) -> str:
    return re.sub(r"\s*\([A-Za-z]+\)$", "", submodel.strip())


def read_rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main() -> None:
    pricing_rows = read_rows(PRICING_FILE)
    specs_rows = read_rows(SPECS_FILE)

    specs_by_key = {}
    for row in specs_rows:
        key = (car_key_from_url(row["spec_url"]), normalize_specs_submodel(row["SubModel"]))
        specs_by_key[key] = row

    specs_extra_fields = [f for f in specs_rows[0].keys() if f not in ("spec_url", "SubModel")]
    pricing_fields = list(pricing_rows[0].keys())

    fieldnames = pricing_fields + ["spec_url"] + specs_extra_fields

    combined_rows = []
    unmatched = 0
    for row in pricing_rows:
        key = (car_key_from_url(row["pricing_url"]), normalize_pricing_submodel(row["SubModel"]))
        specs_row = specs_by_key.get(key)

        combined = dict(row)
        if specs_row:
            combined["spec_url"] = specs_row["spec_url"]
            for field in specs_extra_fields:
                combined[field] = specs_row[field]
        else:
            unmatched += 1
            combined["spec_url"] = ""
            for field in specs_extra_fields:
                combined[field] = ""

        combined_rows.append(combined)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(combined_rows)

    print(f"Wrote {len(combined_rows)} rows to {OUTPUT_FILE}")
    print(f"Unmatched pricing rows (no specs found): {unmatched}")


if __name__ == "__main__":
    main()
