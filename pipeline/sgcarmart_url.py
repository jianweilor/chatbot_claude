import csv
import os
import time
import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse

# =========================
# Settings
# =========================
LISTING_URL = "https://www.sgcarmart.com/new-cars/listing/search?category=ad&vehicle_type=electric&limit=60&page={}"

# All CSVs are read/written in the shared "csv data" folder, not this script's folder.
CSV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
os.makedirs(CSV_DIR, exist_ok=True)

SPECS_URLS_FILE = os.path.join(CSV_DIR, "sgcarmart_ev_specs_urls.csv")      # collected /specs URLs (pending + done)
PRICING_URLS_FILE = os.path.join(CSV_DIR, "sgcarmart_ev_pricing_urls.csv")  # collected /pricing URLs (pending + done)
SPECS_FILE = os.path.join(CSV_DIR, "sgcarmart_ev_specs.csv")                # already-scraped specs, used to skip vehicles

MAX_PAGES = 60          # fixed page limit as requested
PAGE_DELAY = 10         # longer delay between each listing page retrieval (seconds)

MAX_CONSECUTIVE_EMPTY = 2     # stop after this many empty pages in a row
MAX_CONSECUTIVE_ERRORS = 3    # stop after this many failed/invalid page fetches in a row
MAX_CONSECUTIVE_NO_NEW = 3    # stop after this many pages in a row with 0 new URLs saved

# =========================
# Chrome setup
# =========================
options = Options()
options.add_argument("--headless=new")
options.add_argument("--disable-gpu")
options.add_argument("--window-size=1920,1080")
options.add_argument("--disk-cache-size=1")
options.add_argument("--disable-application-cache")

# Explicit, writable, non-synced profile directory -- avoids the
# "failed to write prefs file" / SessionNotCreatedException error.
CHROME_PROFILE_DIR = os.path.join(os.path.expanduser("~"), "sgcarmart_chrome_profile")
os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")

driver = webdriver.Chrome(
    service=Service(ChromeDriverManager().install()),
    options=options
)


# =========================
# Helpers
# =========================
def clean_url(url):
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def load_existing_urls(file_path):
    """URLs already saved to a given pending/collected list (may or may not be scraped yet)."""
    existing = set()
    if os.path.exists(file_path):
        try:
            df = pd.read_csv(file_path)
            if "url" in df.columns:
                existing = set(df["url"].dropna().astype(str).tolist())
        except Exception as e:
            print(f"Error reading {file_path}:", e)
    return existing


def load_scraped_urls():
    """URLs whose specs are already saved -- these are skipped entirely so we
    don't waste time re-collecting/re-queuing vehicles we already have."""
    scraped = set()
    if os.path.exists(SPECS_FILE):
        try:
            df = pd.read_csv(SPECS_FILE)
            if "spec_url" in df.columns:
                scraped = set(df["spec_url"].dropna().astype(str).tolist())
        except Exception as e:
            print("Error reading specs CSV:", e)
    return scraped


def save_url(url, file_path):
    """Write a single new URL immediately (append) so progress is never lost
    if the run is interrupted."""
    file_exists = os.path.exists(file_path)
    with open(file_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["url"])
        writer.writerow([url])


# =========================
# Fetch + parse one listing page
# =========================
def get_spec_urls(page_number):
    """Returns (urls, invalid). invalid=True means the page failed to load or
    didn't look like a valid listing page -- handled distinctly from a page
    that loaded fine but simply has no cars listed."""
    url = LISTING_URL.format(page_number)
    print("\nScanning:", url)
    try:
        driver.get(url)
        time.sleep(PAGE_DELAY)

        # Basic validity check: does the page even look like a listing page?
        if "sgcarmart" not in driver.current_url.lower():
            print("Redirected away from sgcarmart -- treating page as invalid.")
            return set(), True

        soup = BeautifulSoup(driver.page_source, "html.parser")

        # Diagnostic: surface any "N results/models" style text on the page.
        count_text = soup.find(
            string=lambda s: s and ("result" in s.lower() or "model" in s.lower())
        )
        if count_text:
            print("Page hint text:", count_text.strip()[:120])

        urls = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "/new-cars/info/" not in href:
                continue
            if any(word in href.lower() for word in ["review", "promo"]):
                continue
            if href.startswith("/"):
                href = "https://www.sgcarmart.com" + href
            href = clean_url(href)
            # Only keep the /specs or /pricing sub-pages -- skip the bare
            # model page and any other sub-page (e.g. /features).
            if not (href.endswith("/specs") or href.endswith("/pricing")):
                continue
            urls.add(href)
        return urls, False

    except Exception as e:
        print("Page error (treating as invalid page):", e)
        return set(), True


# =========================
# Main collection loop
# =========================
def collect_urls():
    existing_specs_urls = load_existing_urls(SPECS_URLS_FILE)
    existing_pricing_urls = load_existing_urls(PRICING_URLS_FILE)
    scraped_urls = load_scraped_urls()
    print("Already collected -- specs list:", len(existing_specs_urls))
    print("Already collected -- pricing list:", len(existing_pricing_urls))
    print("Already scraped (specs done):", len(scraped_urls))

    total_new = 0
    total_skipped_scraped = 0
    empty_pages = 0
    error_pages = 0
    no_new_pages = 0
    previous_urls = None

    for page_number in range(1, MAX_PAGES + 1):
        urls, invalid = get_spec_urls(page_number)
        print("URLs found on page:", len(urls))

        if invalid:
            error_pages += 1
            print(f"Invalid/failed page ({error_pages}/{MAX_CONSECUTIVE_ERRORS} in a row).")
            if error_pages >= MAX_CONSECUTIVE_ERRORS:
                print("Too many consecutive invalid pages, stopping.")
                break
            time.sleep(PAGE_DELAY * 2)  # extra backoff before retrying next page
            continue
        error_pages = 0

        if not urls:
            empty_pages += 1
            print(f"Empty page ({empty_pages}/{MAX_CONSECUTIVE_EMPTY} in a row).")
            if empty_pages >= MAX_CONSECUTIVE_EMPTY:
                print("No more listing pages -- stopping.")
                break
            continue
        empty_pages = 0

        # Detect a stuck pagination: identical URL set to the previous page
        # means the 'page' param likely isn't actually paginating.
        if previous_urls is not None and urls == previous_urls:
            print("\n\u26a0\ufe0f  Same URL set as previous page -- pagination may be stuck.")
            print("Stopping early; check the site's real pagination mechanism.")
            break
        previous_urls = urls

        new_this_page = 0
        skipped_this_page = 0
        for url in urls:
            if url in scraped_urls:
                total_skipped_scraped += 1
                skipped_this_page += 1
                continue  # specs already exist -- no need to queue this vehicle again

            if url.endswith("/specs"):
                if url in existing_specs_urls:
                    continue  # already queued/collected before
                save_url(url, SPECS_URLS_FILE)
                existing_specs_urls.add(url)
                total_new += 1
                new_this_page += 1
                print("Saved new specs URL:", url)
            elif url.endswith("/pricing"):
                if url in existing_pricing_urls:
                    continue  # already queued/collected before
                save_url(url, PRICING_URLS_FILE)
                existing_pricing_urls.add(url)
                total_new += 1
                new_this_page += 1
                print("Saved new pricing URL:", url)

        print(f"New URLs saved this page: {new_this_page} | Already-scraped skipped this page: "
              f"{skipped_this_page}")

        if new_this_page == 0:
            no_new_pages += 1
            print(f"No new URLs on this page ({no_new_pages}/{MAX_CONSECUTIVE_NO_NEW} in a row).")
            if no_new_pages >= MAX_CONSECUTIVE_NO_NEW:
                print(f"\nNo new URLs for {MAX_CONSECUTIVE_NO_NEW} pages in a row -- stopping.")
                break
        else:
            no_new_pages = 0

    print("\n=== URL collection completed ===")
    print("New URLs saved this run:", total_new)
    print("Skipped (already scraped):", total_skipped_scraped)
    print("Total specs URLs pending:", len(existing_specs_urls))
    print("Total pricing URLs pending:", len(existing_pricing_urls))
    return existing_specs_urls, existing_pricing_urls


# =========================
# Entry point
# =========================
try:
    collect_urls()
finally:
    driver.quit()

print("\nAll done.")