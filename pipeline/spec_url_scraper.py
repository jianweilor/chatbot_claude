import os
import gc
import time
import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# =========================
# Settings
# =========================
# All CSVs are read/written in the shared "csv data" folder, not this script's folder.
CSV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
os.makedirs(CSV_DIR, exist_ok=True)

SPECS_URLS_FILE = os.path.join(CSV_DIR, "sgcarmart_ev_specs_urls.csv")  # input: /specs URLs collected earlier
SPECS_FILE = os.path.join(CSV_DIR, "sgcarmart_ev_specs.csv")            # output: scraped spec data (also used to skip done vehicles)

SPEC_DELAY = 12            # delay between each spec PAGE retrieval (seconds)
SUBMODEL_CLICK_DELAY = 3   # delay after clicking a sub-model tab, before re-reading values
RETRY_DELAY = 20           # backoff after a failed fetch
MAX_TRIES = 3              # total attempts per spec page before giving up

RECYCLE_EVERY_N_URLS = 30  # quit and relaunch Chrome after this many URLs to shed memory

# CSS selectors used on the individual spec page
SEL_TITLE = ".styles_textModelFullName__gsTQv"
SEL_LABELS = ".styles_containerInfoTitle__Td32A"
SEL_VALUES = ".styles_containerInfoValue__lKLR8"

# Sub-model tab selectors (substring match -- these hashes are unstable across page templates)
SEL_SUBMODEL_BUTTON = "[class*='btnSubmodelName']"
SEL_SUBMODEL_TEXT = "[class*='textSubmodelName']"

# Explicit, writable, non-synced profile directory -- avoids the
# "failed to write prefs file" / SessionNotCreatedException error.
CHROME_PROFILE_DIR = os.path.join(os.path.expanduser("~"), "sgcarmart_chrome_profile")
os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)

driver = None  # set by create_driver(), recreated on each recycle
_driver_path = None  # cached chromedriver path -- resolved once, reused on every recycle


# =========================
# Chrome session management
# =========================
def create_driver():
    """Build a fresh Chrome session. Called at startup and again every
    RECYCLE_EVERY_N_URLS URLs to shed the renderer-process memory buildup
    (which, same as on the pricing scraper, is the primary memory pressure
    -- not Python-side objects)."""
    global _driver_path
    if _driver_path is None:
        _driver_path = ChromeDriverManager().install()

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disk-cache-size=1")
    options.add_argument("--disable-application-cache")
    options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")

    return webdriver.Chrome(
        service=Service(_driver_path),
        options=options
    )


def recycle_driver():
    """Quit the current Chrome session and start a new one."""
    global driver
    print("\n--- Recycling Chrome session ---")
    try:
        driver.quit()
    except Exception as e:
        print("Error quitting old driver (continuing anyway):", e)

    driver = None
    gc.collect()

    driver = create_driver()
    print("--- New Chrome session ready ---\n")


# =========================
# Helpers
# =========================
def load_pending_urls():
    """URLs to scrape, read from the specs URL list produced by the collector script."""
    if not os.path.exists(SPECS_URLS_FILE):
        print(f"No input file found at {SPECS_URLS_FILE} -- nothing to scrape.")
        return []
    try:
        df = pd.read_csv(SPECS_URLS_FILE)
        if "url" not in df.columns:
            print(f"'url' column not found in {SPECS_URLS_FILE}.")
            return []
        return df["url"].dropna().astype(str).tolist()
    except Exception as e:
        print(f"Error reading {SPECS_URLS_FILE}:", e)
        return []


def load_scraped_urls():
    """spec_url values already saved -- skip these (all sub-models for a URL
    are scraped together in one run, so one saved row means that URL is done)."""
    scraped = set()
    if os.path.exists(SPECS_FILE):
        try:
            df = pd.read_csv(SPECS_FILE)
            if "spec_url" in df.columns:
                scraped = set(df["spec_url"].dropna().astype(str).tolist())
        except Exception as e:
            print("Error reading specs CSV:", e)
    return scraped


_specs_columns = None  # cached header -- avoids re-reading the CSV on every save


def save_spec(record):
    """Append a spec record (dict of Field -> Value, plus spec_url/SubModel) to
    the specs CSV immediately. Columns can differ between records (different
    models expose different spec fields), so we union the header with
    whatever's already on disk rather than assuming a fixed schema.

    Fast path: if this record introduces no new columns (the common case),
    we append a single row without touching the rest of the file. Only when
    a genuinely new column shows up do we pay for a full rewrite (needed to
    backfill that column on every prior row) -- previously this full rewrite
    happened on *every* save, making each save slower as the file grew."""
    global _specs_columns

    file_exists = os.path.exists(SPECS_FILE)
    if _specs_columns is None:
        _specs_columns = list(pd.read_csv(SPECS_FILE, nrows=0).columns) if file_exists else []

    new_cols = [c for c in record.keys() if c not in _specs_columns]

    if not new_cols and file_exists:
        row = {c: record.get(c, "") for c in _specs_columns}
        pd.DataFrame([row], columns=_specs_columns).to_csv(
            SPECS_FILE, mode="a", header=False, index=False
        )
        return

    all_cols = _specs_columns + new_cols

    if file_exists:
        df_existing = pd.read_csv(SPECS_FILE)
        for c in new_cols:
            df_existing[c] = ""
        df_new = pd.DataFrame([record])
        df_combined = pd.concat([df_existing, df_new], ignore_index=True)
        df_combined = df_combined[all_cols]
        df_combined.to_csv(SPECS_FILE, index=False)
    else:
        pd.DataFrame([record], columns=all_cols).to_csv(SPECS_FILE, index=False)

    _specs_columns = all_cols


# =========================
# Extraction
# =========================
def extract_current_specs(url, title, submodel_name):
    """Read whatever labels/values are currently rendered on the page."""
    labels = driver.execute_script(f"""
        return Array.from(
            document.querySelectorAll('{SEL_LABELS}')
        ).map(e => e.innerText.trim());
    """)

    values = driver.execute_script(f"""
        return Array.from(
            document.querySelectorAll('{SEL_VALUES}')
        ).map(e => e.innerText.trim());
    """)

    record = {"spec_url": url, "Model": title}
    if submodel_name:
        record["SubModel"] = submodel_name

    for label, value in zip(labels, values):
        if label:
            record[label] = value

    return record


def extract_all_submodel_specs(url):
    """Load the page once, then click through every sub-model tab (if any),
    re-reading the specs panel after each click. Returns a list of records --
    one per sub-model, or a single record if the page has no tabs at all."""
    wait = WebDriverWait(driver, 20)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SEL_TITLE)))

    title = driver.execute_script(f"""
        const el = document.querySelector('{SEL_TITLE}');
        return el ? el.innerText.trim() : '';
    """)

    submodel_count = driver.execute_script(f"""
        return document.querySelectorAll("{SEL_SUBMODEL_BUTTON}").length;
    """)

    # No tabs on this page -- single variant, scrape as-is (old behaviour)
    if submodel_count == 0:
        return [extract_current_specs(url, title, None)]

    records = []
    for idx in range(submodel_count):
        # Re-query each time -- clicking can re-render the DOM and stale
        # element references / index shifts otherwise.
        submodel_name = driver.execute_script(f"""
            const btns = document.querySelectorAll("{SEL_SUBMODEL_BUTTON}");
            const btn = btns[{idx}];
            if (!btn) return '';
            const nameEl = btn.querySelector("{SEL_SUBMODEL_TEXT}");
            return nameEl ? nameEl.innerText.trim() : btn.innerText.trim();
        """)

        driver.execute_script(f"""
            const btns = document.querySelectorAll("{SEL_SUBMODEL_BUTTON}");
            if (btns[{idx}]) btns[{idx}].click();
        """)

        time.sleep(SUBMODEL_CLICK_DELAY)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SEL_VALUES)))

        record = extract_current_specs(url, title, submodel_name or f"Variant {idx+1}")
        records.append(record)
        print(f"  -> captured sub-model: {record.get('SubModel')}")

    return records


def scrape_specs():
    pending_urls = load_pending_urls()
    done_urls = load_scraped_urls()

    to_scrape = [u for u in pending_urls if u not in done_urls]
    print(f"Pending URLs: {len(pending_urls)} | Already scraped: {len(done_urls)} | "
          f"Remaining to scrape: {len(to_scrape)}")

    total_saved = 0
    urls_processed_since_recycle = 0

    for i, url in enumerate(to_scrape):
        print(f"\n[{i+1}/{len(to_scrape)}] Scraping:", url)

        records = None
        for attempt in range(1, MAX_TRIES + 1):
            try:
                driver.get(url)
                records = extract_all_submodel_specs(url)
                break
            except Exception as e:
                print(f"Attempt {attempt}/{MAX_TRIES} failed:", e)
                if attempt < MAX_TRIES:
                    time.sleep(RETRY_DELAY)

        if not records:
            print(f"Giving up on this URL after {MAX_TRIES} tries:", url)
            continue

        for record in records:
            save_spec(record)
            total_saved += 1
            print("Saved spec for:", record.get("SubModel") or record.get("Model", url))

        urls_processed_since_recycle += 1

        # Recycle the Chrome session periodically to shed renderer memory buildup
        if urls_processed_since_recycle >= RECYCLE_EVERY_N_URLS:
            recycle_driver()
            urls_processed_since_recycle = 0

        time.sleep(SPEC_DELAY)  # delay between each spec PAGE retrieval

    print("\n=== Spec scraping completed ===")
    print("Specs saved this run:", total_saved)


# =========================
# Entry point
# =========================
driver = create_driver()

try:
    scrape_specs()
finally:
    try:
        driver.quit()
    except Exception:
        pass

print("\nAll done.")