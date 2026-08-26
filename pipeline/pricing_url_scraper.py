import os
import time
import gc
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

PRICING_URLS_FILE = os.path.join(CSV_DIR, "sgcarmart_ev_pricing_urls.csv")  # input: /pricing URLs
PRICING_FILE = os.path.join(CSV_DIR, "sgcarmart_ev_pricing.csv")            # output: scraped pricing data (also used to skip done vehicles)

PAGE_DELAY = 12        # delay between each page retrieval (seconds)
RETRY_DELAY = 20       # backoff after a failed fetch
MAX_TRIES = 3          # total attempts per page before giving up
RESTART_EVERY_N_URLS = 15  # proactively recycle the Chrome process to avoid long-run crashes

# Only these fields are kept per sub-model (prefix-matched, nbsp/whitespace normalized)
WANTED_FIELDS = [
    "Current price",
    "COE",   # covers "COE quota premium" regardless of nbsp/space
    "Road tax",
    "OMV",
    "ARF",
    "VES",
    "EEAI",
]

# CSS selectors used on the pricing page (substring match -- resilient to
# CSS module hash changes, since exact hashed classes have broken before)
SEL_SUBMODEL_BLOCK = "[class*='containerSubModel__']"
SEL_SUBMODEL_NAME = "[class*='textSubModelName__']"
SEL_LABELS = "[class*='containerInfoTitle__']"
SEL_VALUES = "[class*='containerInfoValue__']"

CHROME_PROFILE_DIR = os.path.join(os.path.expanduser("~"), "sgcarmart_chrome_profile")
DEBUG_DIR = "debug_timeouts"

BLOCK_INDICATORS = [
    "captcha", "access denied", "are you a robot", "cloudflare",
    "unusual traffic", "verify you are human",
]


# =========================
# Chrome lifecycle
# =========================
def clear_stale_lock():
    lock_file = os.path.join(CHROME_PROFILE_DIR, "SingletonLock")
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
            print("Removed stale SingletonLock from previous crashed session.")
        except Exception as e:
            print("Could not remove stale lock file:", e)


_driver_path = None  # cached chromedriver path -- resolved once, reused on every restart


def create_driver():
    """Launch a fresh Chrome session. Called at startup, after any failed
    fetch (the previous session may be dead even though the exception looked
    generic), and periodically to prevent long-run memory/stability issues."""
    global _driver_path
    os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
    clear_stale_lock()

    if _driver_path is None:
        _driver_path = ChromeDriverManager().install()

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disk-cache-size=1")
    options.add_argument("--disable-application-cache")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    # NOTE: --remote-debugging-port=0 removed -- it has caused
    # SessionNotCreatedException on some Chrome builds instead of helping.
    options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")

    new_driver = webdriver.Chrome(
        service=Service(_driver_path),
        options=options
    )
    new_driver.set_page_load_timeout(45)
    return new_driver


def restart_driver(old_driver):
    """Kill whatever's left of the old session and start a clean one."""
    try:
        old_driver.quit()
    except Exception:
        pass
    gc.collect()
    time.sleep(2)
    return create_driver()


# =========================
# Helpers
# =========================
def load_pending_urls():
    if not os.path.exists(PRICING_URLS_FILE):
        print(f"No input file found at {PRICING_URLS_FILE} -- nothing to scrape.")
        return []
    try:
        df = pd.read_csv(PRICING_URLS_FILE)
        if "url" not in df.columns:
            print(f"'url' column not found in {PRICING_URLS_FILE}.")
            return []
        return df["url"].dropna().astype(str).tolist()
    except Exception as e:
        print(f"Error reading {PRICING_URLS_FILE}:", e)
        return []


def load_scraped_urls():
    scraped = set()
    if os.path.exists(PRICING_FILE):
        try:
            df = pd.read_csv(PRICING_FILE)
            if "pricing_url" in df.columns:
                scraped = set(df["pricing_url"].dropna().astype(str).tolist())
        except Exception as e:
            print("Error reading pricing CSV:", e)
    return scraped


def save_records(records):
    if not records:
        return

    file_exists = os.path.exists(PRICING_FILE)
    fieldnames = ["pricing_url", "SubModel"] + WANTED_FIELDS

    df_new = pd.DataFrame(records, columns=fieldnames)

    if file_exists:
        df_new.to_csv(PRICING_FILE, mode="a", header=False, index=False)
    else:
        df_new.to_csv(PRICING_FILE, mode="w", header=True, index=False)


def dump_debug_info(driver, url, attempt):
    """On a timeout/failure, save what Chrome actually saw so we can tell
    apart: bot block, different page template, or genuinely slow load."""
    os.makedirs(DEBUG_DIR, exist_ok=True)
    safe_name = "".join(c if c.isalnum() else "_" for c in url)[-80:]
    base = os.path.join(DEBUG_DIR, f"{safe_name}_try{attempt}")

    try:
        current_url = driver.current_url
        title = driver.title
        html = driver.page_source
    except Exception as e:
        print(f"  (could not read page state for debug dump: {e})")
        return

    print(f"  Debug: current_url={current_url!r} title={title!r}")

    lowered = html.lower()
    hit = next((kw for kw in BLOCK_INDICATORS if kw in lowered), None)
    if hit:
        print(f"  Debug: page looks like a BLOCK/CAPTCHA page (matched '{hit}')")

    try:
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(html)
        driver.save_screenshot(base + ".png")
        print(f"  Debug: saved {base}.html / .png")
    except Exception as e:
        print(f"  (could not write debug files: {e})")


def normalize(s):
    return s.replace("\xa0", " ").strip()


def is_wanted(label):
    norm = normalize(label)
    return any(norm.startswith(key) for key in WANTED_FIELDS)


# =========================
# Scrape a single pricing page (sub-model aware, filtered fields)
# =========================
def extract_pricing(driver, url):
    wait = WebDriverWait(driver, 25)
    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SEL_SUBMODEL_BLOCK)))

    raw_data = driver.execute_script(f"""
        const subModels = document.querySelectorAll("{SEL_SUBMODEL_BLOCK}");
        const results = [];

        subModels.forEach(block => {{
            const nameEl = block.querySelector("{SEL_SUBMODEL_NAME}");
            const subModelName = nameEl ? nameEl.innerText.trim() : '';

            const labels = Array.from(
                block.querySelectorAll("{SEL_LABELS}")
            ).map(e => e.innerText.trim());

            const values = Array.from(
                block.querySelectorAll("{SEL_VALUES}")
            ).map(e => e.innerText.trim());

            const rows = labels.map((label, i) => ({{
                label: label,
                value: values[i] !== undefined ? values[i] : ''
            }}));

            results.push({{ subModel: subModelName, rows: rows }});
        }});

        return results;
    """)

    records = []
    for entry in raw_data:
        row = {"pricing_url": url, "SubModel": entry["subModel"]}
        for key in WANTED_FIELDS:
            row[key] = ""
        for r in entry["rows"]:
            if is_wanted(r["label"]):
                norm_label = normalize(r["label"])
                for key in WANTED_FIELDS:
                    if norm_label.startswith(key):
                        row[key] = r["value"]
                        break
        records.append(row)

    return records


def scrape_pricing():
    pending_urls = load_pending_urls()
    done_urls = load_scraped_urls()

    to_scrape = [u for u in pending_urls if u not in done_urls]
    print(f"Pending URLs: {len(pending_urls)} | Already scraped: {len(done_urls)} | "
          f"Remaining to scrape: {len(to_scrape)}")

    total_saved_urls = 0
    total_saved_rows = 0

    driver = create_driver()

    try:
        for i, url in enumerate(to_scrape):
            print(f"\n[{i+1}/{len(to_scrape)}] Scraping:", url)

            # Proactively recycle the Chrome process every N URLs, before it
            # has a chance to degrade/crash on its own.
            if i > 0 and i % RESTART_EVERY_N_URLS == 0:
                print(f"Recycling Chrome session after {i} URLs...")
                driver = restart_driver(driver)

            records = None
            for attempt in range(1, MAX_TRIES + 1):
                try:
                    driver.get(url)
                    records = extract_pricing(driver, url)
                    if not records:
                        raise ValueError("No sub-model blocks found on page")
                    break
                except Exception as e:
                    print(f"Attempt {attempt}/{MAX_TRIES} failed: {type(e).__name__}: {e}")
                    dump_debug_info(driver, url, attempt)
                    if attempt < MAX_TRIES:
                        # The session may be dead even though the error looks
                        # generic (e.g. chromedriver crash trace with no message) --
                        # restart Chrome before trying again, don't just retry
                        # against a broken session.
                        driver = restart_driver(driver)
                        time.sleep(RETRY_DELAY)

            if not records:
                print(f"Giving up on this URL after {MAX_TRIES} tries:", url)
                continue

            save_records(records)
            total_saved_urls += 1
            total_saved_rows += len(records)
            print(f"Saved {len(records)} sub-model row(s) for:", url)

            time.sleep(PAGE_DELAY)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    print("\n=== Pricing scraping completed ===")
    print("URLs saved this run:", total_saved_urls)
    print("Total rows saved this run:", total_saved_rows)


# =========================
# Entry point
# =========================
scrape_pricing()
print("\nAll done.")