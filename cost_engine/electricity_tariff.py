"""
Scrapes the SP Group Tariff Information page for the current electricity
tariff rate (excluding GST).

Page: https://www.spgroup.com.sg/our-services/utilities/tariff-information

The page renders the tariff figures as static HTML (no JS rendering needed),
so a simple requests + BeautifulSoup approach works fine -- no Selenium/
Playwright required.

Each tariff (electricity / gas / water) sits inside:
    <div class="kui-figure">
        <h4 class="kui-figure__text">34.78 cents/kWh</h4>
        <hr class="kui-figure__divider">
        <p class="kui-figure__description">
            31.91 cents/kWh (w/o GST)<br>
            ELECTRICITY TARIFF<br>
            (wef 1 Jul - 30 Sep 26)
        </p>
    </div>
"""

import re
import requests
from bs4 import BeautifulSoup

URL = "https://www.spgroup.com.sg/our-services/utilities/tariff-information"

HEADERS = {
    # A normal desktop UA avoids some basic bot-blocking
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


def get_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def extract_tariffs(soup: BeautifulSoup) -> list[dict]:
    """
    Finds every 'kui-figure' block on the page and pulls out:
      - headline price (incl. GST)
      - w/o GST price
      - label (ELECTRICITY TARIFF / GAS TARIFF / WATER TARIFF)
      - validity period (e.g. "wef 1 Jul - 30 Sep 26")
    """
    results = []

    for figure in soup.select("div.kui-figure"):
        headline_tag = figure.select_one(".kui-figure__text")
        desc_tag = figure.select_one(".kui-figure__description")
        if not headline_tag or not desc_tag:
            continue

        headline_price = headline_tag.get_text(strip=True)

        # The description <p> uses <br> tags to separate lines, so we
        # split on <br> rather than get_text() (which would smoosh
        # everything together with no separator).
        desc_lines = [
            line.strip()
            for line in desc_tag.get_text(separator="|").split("|")
            if line.strip()
        ]

        # Expected desc_lines, e.g.:
        # ["31.91 cents/kWh (w/o GST)", "ELECTRICITY TARIFF", "(wef 1 Jul - 30 Sep 26)"]
        price_wo_gst = desc_lines[0] if len(desc_lines) > 0 else None
        label = desc_lines[1] if len(desc_lines) > 1 else None
        period = desc_lines[2] if len(desc_lines) > 2 else None

        results.append(
            {
                "label": label,
                "price_incl_gst": headline_price,
                "price_excl_gst": price_wo_gst,
                "period": period,
            }
        )

    return results


def get_electricity_tariff_excl_gst(tariffs: list[dict]) -> str | None:
    for t in tariffs:
        if t["label"] and "ELECTRICITY" in t["label"].upper():
            return t["price_excl_gst"]
    return None


def extract_numeric_cents(price_str: str) -> float | None:
    """'31.91 cents/kWh (w/o GST)' -> 31.91"""
    if not price_str:
        return None
    match = re.search(r"[\d.]+", price_str)
    return float(match.group()) if match else None


def cents_to_dollars_per_kwh(cents: float | None) -> float | None:
    """31.91 (cents/kWh) -> 0.3191 ($/kWh)"""
    if cents is None:
        return None
    return round(cents / 100, 6)


if __name__ == "__main__":
    soup = get_soup(URL)
    tariffs = extract_tariffs(soup)

    print("All tariffs found on the page:\n")
    for t in tariffs:
        print(t)

    electricity_excl_gst = get_electricity_tariff_excl_gst(tariffs)
    cents = extract_numeric_cents(electricity_excl_gst)
    dollars = cents_to_dollars_per_kwh(cents)

    print("\nElectricity tariff (w/o GST):", electricity_excl_gst)
    print("As a number (cents/kWh):", cents)
    print(f"Converted to $/kWh: ${dollars}/kWh")