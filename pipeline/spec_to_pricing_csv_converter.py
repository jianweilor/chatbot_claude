import csv
import os

# --- Update these two paths to match your setup ---
CSV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
input_path = os.path.join(CSV_DIR, "sgcarmart_ev_specs_urls.csv")
output_path = os.path.join(CSV_DIR, "sgcarmart_ev_pricing_urls.csv")
# ----------------------------------------------------

if not os.path.exists(input_path):
    raise FileNotFoundError(
        f"Could not find input file at:\n{input_path}\n"
        "Double-check the file name and location, then update 'input_path' above."
    )

with open(input_path, "r", newline="", encoding="utf-8") as infile:
    reader = csv.reader(infile)
    rows = list(reader)

if not rows:
    raise ValueError("The input CSV is empty.")

header = rows[0]
new_rows = [header]

for row in rows[1:]:
    if not row:
        continue
    url = row[0].strip()
    if url.endswith("/specs"):
        new_url = url[: -len("/specs")] + "/pricing"
    else:
        new_url = url
    new_rows.append([new_url])

with open(output_path, "w", newline="", encoding="utf-8") as outfile:
    writer = csv.writer(outfile)
    writer.writerows(new_rows)

print(f"Done. Converted {len(new_rows) - 1} URLs.")
print(f"Saved to: {output_path}")