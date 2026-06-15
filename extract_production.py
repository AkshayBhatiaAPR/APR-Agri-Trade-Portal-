#!/usr/bin/env python3
"""
extract_production.py — DA&FW Advance Estimates Production Extractor

Scrapes desagri.gov.in/statistics-type/advance-estimates/ for PDF links,
downloads unprocessed PDFs, extracts production data (Lakh Tonnes) for
six commodities (Paddy, Wheat, Maize, Sugarcane, Tur, Gram), and writes
commodity-wise JSON files.

Usage:
    python scripts/extract_production.py

Dependencies:
    pip install pdfplumber requests beautifulsoup4
"""

import json
import os
import re
import sys
import tempfile
import logging
from datetime import datetime, timezone

import requests
import pdfplumber
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

SOURCE_URL = "https://desagri.gov.in/statistics-type/advance-estimates/"
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
MANIFEST_PATH = os.path.join(DATA_DIR, "manifest.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Six target commodities: PDF name → canonical key
CROP_ALIASES = {
    "rice":       "paddy",
    "paddy":      "paddy",
    "wheat":      "wheat",
    "maize":      "maize",
    "sugarcane":  "sugarcane",
    "tur":        "tur",
    "arhar":      "tur",
    "tur(arhar)": "tur",
    "tur (arhar)":"tur",
    "arhar/tur":  "tur",
    "arhar (tur)":"tur",
    "gram":       "gram",
}

CANONICAL_COMMODITIES = ["paddy", "wheat", "maize", "sugarcane", "tur", "gram"]
COMMODITY_DISPLAY = {
    "paddy": "Paddy", "wheat": "Wheat", "maize": "Maize",
    "sugarcane": "Sugarcane", "tur": "Tur", "gram": "Gram",
}

VALID_SEASONS = {"kharif", "rabi", "summer", "total"}

# Estimate number extraction patterns
ESTIMATE_MAP = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2,
    "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
}

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("extract_production")

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def ensure_data_dir():
    """Create data/ directory if it doesn't exist."""
    os.makedirs(DATA_DIR, exist_ok=True)


def load_manifest():
    """Load manifest.json or return empty structure."""
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r") as f:
            return json.load(f)
    return {"last_run": None, "processed": []}


def save_manifest(manifest):
    """Write manifest.json."""
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)


def load_commodity_json(commodity):
    """Load existing commodity JSON or return empty structure."""
    path = os.path.join(DATA_DIR, f"{commodity}.json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {
        "meta": {
            "commodity": COMMODITY_DISPLAY[commodity],
            "unit": "Lakh Tonnes",
            "last_updated": None,
            "source_pdf": None,
            "estimate_type": None,
            "estimate_year": None,
        },
        "data": [],
    }


def save_commodity_json(commodity, data):
    """Write commodity JSON."""
    path = os.path.join(DATA_DIR, f"{commodity}.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def parse_crop_year(text):
    """Extract crop year pattern like '2024-25' from text."""
    m = re.search(r"(\d{4})-(\d{2,4})", text)
    if m:
        start = m.group(1)
        end = m.group(2)
        if len(end) == 4:
            end = end[2:]  # 2024-2025 → 2024-25
        return f"{start}-{end}"
    return None


def parse_estimate_number(text):
    """Extract estimate number (1-4) from title text."""
    text_lower = text.lower()
    for key, num in ESTIMATE_MAP.items():
        if key in text_lower:
            return num
    return None


def parse_estimate_label(num):
    """Convert estimate number to display label."""
    labels = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
    return f"{labels.get(num, str(num))} Advance Estimate"


def parse_number(val):
    """Parse a cell value to float or None. Handles dashes, blanks, commas."""
    if val is None:
        return None
    val = str(val).strip()
    # Remove common non-numeric markers
    if val in ("", "--", "-", "—", "*", "N.A.", "NA", "n.a.", ".."):
        return None
    # Remove commas and spaces within numbers
    val = val.replace(",", "").replace(" ", "")
    # Remove footnote markers like asterisks or hash at end
    val = re.sub(r"[*#@$`^]+$", "", val)
    try:
        return float(val)
    except ValueError:
        return None


def normalize_crop_name(name):
    """Normalize a crop name and return canonical key or None."""
    if not name:
        return None
    # Clean up whitespace and special characters
    clean = name.strip().lower()
    clean = re.sub(r"\s+", " ", clean)
    # Remove serial number prefixes like "1.", "1 ", etc.
    clean = re.sub(r"^\d+[\.\)]\s*", "", clean)
    # Direct lookup
    if clean in CROP_ALIASES:
        return CROP_ALIASES[clean]
    # Try partial matches for compound names like "Tur (Arhar)"
    for alias, canonical in CROP_ALIASES.items():
        if alias in clean or clean in alias:
            return canonical
    return None


def normalize_season(text):
    """Normalize season text to canonical key or None."""
    if not text:
        return None
    clean = text.strip().lower()
    if "kharif" in clean:
        return "kharif"
    if "rabi" in clean:
        return "rabi"
    if "summer" in clean:
        return "summer"
    if "total" in clean:
        return "total"
    return None


# ---------------------------------------------------------------------------
# PHASE 1: DISCOVER
# ---------------------------------------------------------------------------

def discover_pdfs():
    """
    Scrape the advance estimates page for English PDF download links.
    Returns list of dicts: [{url, filename, title, crop_year, estimate_num}]
    """
    log.info(f"Fetching index page: {SOURCE_URL}")
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(SOURCE_URL, headers=headers, timeout=60)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    pdfs = []

    # Find all links to PDF files
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if not href.lower().endswith(".pdf"):
            continue
        # Only English PDFs
        link_text = a_tag.get_text(strip=True).lower()
        if "hindi" in link_text or "hindi" in href.lower():
            continue

        # Get the full URL
        if href.startswith("/"):
            href = "https://desagri.gov.in" + href
        elif not href.startswith("http"):
            continue

        filename = href.split("/")[-1]

        # Try to get context from surrounding elements — walk up to find
        # the row title containing estimate type and crop year
        title_text = ""
        parent = a_tag
        for _ in range(8):  # Walk up a few levels
            parent = parent.parent
            if parent is None:
                break
            parent_text = parent.get_text(" ", strip=True)
            if "advance" in parent_text.lower() and "estimate" in parent_text.lower():
                title_text = parent_text
                break

        # If no context from parent, try filename
        if not title_text:
            title_text = filename

        crop_year = parse_crop_year(title_text) or parse_crop_year(filename)
        estimate_num = parse_estimate_number(title_text) or parse_estimate_number(filename)

        if crop_year and estimate_num:
            pdfs.append({
                "url": href,
                "filename": filename,
                "title": title_text[:200],
                "crop_year": crop_year,
                "estimate_num": estimate_num,
            })
            log.info(f"  Found: {estimate_num} AE {crop_year} → {filename}")

    # Deduplicate by (crop_year, estimate_num) — keep the first URL found
    seen = set()
    unique = []
    for p in pdfs:
        key = (p["crop_year"], p["estimate_num"])
        if key not in seen:
            seen.add(key)
            unique.append(p)

    # Sort by crop year desc, estimate number desc (newest first)
    unique.sort(key=lambda x: (x["crop_year"], x["estimate_num"]), reverse=True)

    log.info(f"Discovered {len(unique)} unique estimate PDFs")
    return unique


def filter_unprocessed(pdfs, manifest):
    """Filter out already-processed PDFs based on manifest."""
    processed_files = {p["filename"] for p in manifest.get("processed", [])}
    new_pdfs = [p for p in pdfs if p["filename"] not in processed_files]
    if new_pdfs:
        log.info(f"{len(new_pdfs)} new PDF(s) to process")
    else:
        log.info("No new PDFs found — everything up to date")
    return new_pdfs


def select_pdfs_for_processing(new_pdfs, manifest):
    """
    Select which PDFs to process.
    - On bootstrap (empty manifest): pick the single latest PDF
    - On steady-state: process all new PDFs, sorted oldest-first so
      newer estimates overwrite older ones
    """
    if not new_pdfs:
        return []

    is_bootstrap = len(manifest.get("processed", [])) == 0

    if is_bootstrap:
        # Pick the single latest PDF (first in the list — already sorted newest-first)
        selected = [new_pdfs[0]]
        log.info(f"Bootstrap mode: selected {selected[0]['filename']}")
    else:
        # Process all new PDFs, sorted oldest-first
        selected = sorted(new_pdfs, key=lambda x: (x["crop_year"], x["estimate_num"]))
        log.info(f"Incremental mode: {len(selected)} PDF(s) to process")

    return selected


# ---------------------------------------------------------------------------
# PHASE 2: DOWNLOAD
# ---------------------------------------------------------------------------

def download_pdf(pdf_info):
    """Download a PDF to a temp file. Returns the file path or None."""
    url = pdf_info["url"]
    log.info(f"Downloading: {pdf_info['filename']}")
    headers = {"User-Agent": USER_AGENT}

    try:
        resp = requests.get(url, headers=headers, timeout=120)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Download failed: {e}")
        return None

    # Validate it's a real PDF
    if resp.content[:5] != b"%PDF-":
        log.error(f"Not a valid PDF: {pdf_info['filename']}")
        return None

    # Save to temp file
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.write(resp.content)
    tmp.close()
    log.info(f"  Saved {len(resp.content)} bytes → {tmp.name}")
    return tmp.name


# ---------------------------------------------------------------------------
# PHASE 3: EXTRACT
# ---------------------------------------------------------------------------

def extract_year_columns(header_row):
    """
    Parse a table header row to find year columns.
    Returns list of (column_index, year_string) tuples.
    """
    year_cols = []
    for i, cell in enumerate(header_row):
        if cell is None:
            continue
        year = parse_crop_year(str(cell))
        if year:
            year_cols.append((i, year))
    return year_cols


def is_production_page(page):
    """Check if a page contains the production table (Lakh Tonnes)."""
    text = page.extract_text() or ""
    text_lower = text.lower()
    # Must have "lakh tonnes" and NOT be primarily an area table
    if "lakh tonnes" in text_lower or "lakh metric tonnes" in text_lower:
        return True
    return False


def is_area_page(page):
    """Check if a page is primarily an area table (skip it)."""
    text = page.extract_text() or ""
    text_lower = text.lower()
    if "lakh hectare" in text_lower:
        # But check if it ALSO mentions tonnes — could be a combined header
        if "lakh tonnes" not in text_lower:
            return True
    return False


def find_crop_and_season_columns(header_row):
    """
    Identify which columns contain crop name and season.
    Returns (crop_col_idx, season_col_idx) or (None, None).
    """
    crop_col = None
    season_col = None
    for i, cell in enumerate(header_row):
        if cell is None:
            continue
        c = str(cell).strip().lower()
        if c in ("crop", "crops", "crop/season"):
            crop_col = i
        elif c in ("season", "seasons"):
            season_col = i
        elif "s no" in c or "s.no" in c or "sl" in c:
            continue  # Skip serial number column
    return crop_col, season_col


def extract_production_from_pdf(pdf_path):
    """
    Extract production data from a DA&FW Advance Estimates PDF.

    Returns dict:
    {
        "paddy": {
            "2024-25": {"kharif": 12061.5, "rabi": 1487.3, ...},
            "2023-24": {...},
        },
        "wheat": {...},
        ...
    }
    """
    extracted = {c: {} for c in CANONICAL_COMMODITIES}

    with pdfplumber.open(pdf_path) as pdf:
        log.info(f"  PDF has {len(pdf.pages)} pages")

        year_columns = []      # [(col_idx, year_str), ...]
        crop_col_idx = None
        season_col_idx = None
        current_crop = None    # Carry-forward crop name
        in_production_section = False

        for page_num, page in enumerate(pdf.pages, 1):
            # Skip area-only pages
            if is_area_page(page) and not is_production_page(page):
                log.info(f"  Page {page_num}: skipping (area table)")
                continue

            if is_production_page(page):
                in_production_section = True
                log.info(f"  Page {page_num}: production table detected")

            if not in_production_section:
                continue

            # If we hit an area page after production, stop
            page_text = (page.extract_text() or "").lower()
            if "lakh hectare" in page_text and "lakh tonnes" not in page_text:
                log.info(f"  Page {page_num}: area table reached — stopping")
                break

            tables = page.extract_tables()
            if not tables:
                continue

            for table in tables:
                if not table or len(table) < 2:
                    continue

                # Check if first row is a header (contains year patterns)
                first_row = table[0]
                potential_years = extract_year_columns(first_row)

                if potential_years:
                    # This is a header row — set up column mapping
                    year_columns = potential_years
                    crop_col_idx, season_col_idx = find_crop_and_season_columns(first_row)
                    current_crop = None  # Reset carry-forward
                    log.info(f"  Header found: {len(year_columns)} year columns, "
                             f"years: {[y for _, y in year_columns[:3]]}...{[y for _, y in year_columns[-1:]]}")

                    # If crop/season columns not found from header, use defaults
                    # Typical layout: col 0 = S.No, col 1 = Crop, col 2 = Season
                    if crop_col_idx is None:
                        crop_col_idx = 1
                    if season_col_idx is None:
                        season_col_idx = 2

                    data_rows = table[1:]  # Skip header
                else:
                    # Continuation rows (no header) — use existing column mapping
                    data_rows = table

                if not year_columns:
                    continue  # No column mapping yet

                for row in data_rows:
                    if not row or len(row) <= max(c for c, _ in year_columns):
                        continue

                    # --- Crop name (carry-forward logic) ---
                    raw_crop = str(row[crop_col_idx] or "").strip() if crop_col_idx < len(row) else ""
                    if raw_crop:
                        # Clean serial number from crop name
                        clean_crop = re.sub(r"^\d+[\.\)]*\s*", "", raw_crop).strip()
                        # Also handle cases like "(11= 1+2+10)" — aggregate rows
                        if re.match(r"^\(?\d+\s*=", clean_crop):
                            current_crop = None  # Aggregate row, skip
                            continue
                        canonical = normalize_crop_name(clean_crop)
                        if canonical:
                            current_crop = canonical
                        else:
                            # Unrecognized crop — if it looks like a section header
                            # (Cereals, Total Foodgrains, etc.), reset carry-forward
                            lower = clean_crop.lower()
                            if any(kw in lower for kw in [
                                "cereal", "foodgrain", "pulse", "oilseed",
                                "total", "commercial", "fibre", "plantation",
                                "condiment", "nutri"
                            ]):
                                current_crop = None
                            continue

                    if current_crop is None:
                        continue

                    # --- Season ---
                    raw_season = str(row[season_col_idx] or "").strip() if season_col_idx < len(row) else ""
                    season = normalize_season(raw_season)
                    if season is None:
                        # Sometimes season is in the same cell as crop
                        combined = f"{raw_crop} {raw_season}".lower()
                        season = normalize_season(combined)
                    if season is None:
                        continue

                    # --- Extract values per year ---
                    for col_idx, year_str in year_columns:
                        if col_idx >= len(row):
                            continue
                        value = parse_number(row[col_idx])
                        if value is not None:
                            if year_str not in extracted[current_crop]:
                                extracted[current_crop][year_str] = {
                                    "kharif": None, "rabi": None,
                                    "summer": None, "total": None,
                                }
                            extracted[current_crop][year_str][season] = value

    # Log summary
    for commodity in CANONICAL_COMMODITIES:
        years = list(extracted[commodity].keys())
        if years:
            log.info(f"  {COMMODITY_DISPLAY[commodity]}: {len(years)} years extracted "
                     f"({min(years)} to {max(years)})")
        else:
            log.warning(f"  {COMMODITY_DISPLAY[commodity]}: NO DATA EXTRACTED")

    return extracted


# ---------------------------------------------------------------------------
# PHASE 4: MERGE
# ---------------------------------------------------------------------------

def merge_into_json(commodity, new_data, pdf_info):
    """
    Merge newly extracted data into existing commodity JSON.

    new_data: {"2024-25": {"kharif": 12061.5, "rabi": 1487.3, ...}, ...}
    """
    cj = load_commodity_json(commodity)

    # Build lookup of existing data by year
    existing_by_year = {}
    for entry in cj["data"]:
        existing_by_year[entry["year"]] = entry

    # Merge
    changes = 0
    for year_str, seasons in new_data.items():
        if year_str not in existing_by_year:
            # New year — add it
            entry = {"year": year_str}
            for s in VALID_SEASONS:
                entry[s] = seasons.get(s)
            existing_by_year[year_str] = entry
            changes += 1
        else:
            # Existing year — overwrite only non-null values
            entry = existing_by_year[year_str]
            for s in VALID_SEASONS:
                new_val = seasons.get(s)
                if new_val is not None:
                    if entry.get(s) != new_val:
                        changes += 1
                    entry[s] = new_val

    # Sort by year descending
    all_entries = sorted(
        existing_by_year.values(),
        key=lambda x: x["year"],
        reverse=True,
    )

    # Update meta
    now = datetime.now(timezone.utc).isoformat()
    cj["meta"]["last_updated"] = now
    cj["meta"]["source_pdf"] = pdf_info["filename"]
    cj["meta"]["estimate_type"] = parse_estimate_label(pdf_info["estimate_num"])
    cj["meta"]["estimate_year"] = pdf_info["crop_year"]
    cj["data"] = all_entries

    return cj, changes


# ---------------------------------------------------------------------------
# PHASE 5: WRITE
# ---------------------------------------------------------------------------

def process_pdf(pdf_info, manifest):
    """Full pipeline for a single PDF: download → extract → merge → write."""
    log.info(f"Processing: {parse_estimate_label(pdf_info['estimate_num'])} "
             f"{pdf_info['crop_year']}")

    # Download
    pdf_path = download_pdf(pdf_info)
    if not pdf_path:
        return False

    try:
        # Extract
        extracted = extract_production_from_pdf(pdf_path)

        # Check if we got any data at all
        total_values = sum(
            len(years) for years in extracted.values()
        )
        if total_values == 0:
            log.error("No production data extracted from PDF — skipping")
            return False

        # Merge and write each commodity
        total_changes = 0
        for commodity in CANONICAL_COMMODITIES:
            if not extracted[commodity]:
                log.warning(f"  No data for {COMMODITY_DISPLAY[commodity]} — skipping")
                continue

            cj, changes = merge_into_json(commodity, extracted[commodity], pdf_info)
            save_commodity_json(commodity, cj)
            total_changes += changes
            log.info(f"  {COMMODITY_DISPLAY[commodity]}: "
                     f"{len(cj['data'])} years, {changes} change(s)")

        # Update manifest
        manifest["processed"].append({
            "filename": pdf_info["filename"],
            "estimate": f"{pdf_info['estimate_num']}",
            "crop_year": pdf_info["crop_year"],
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "url": pdf_info["url"],
        })
        manifest["last_run"] = datetime.now(timezone.utc).isoformat()
        save_manifest(manifest)

        log.info(f"Done: {total_changes} total change(s) written")
        return True

    finally:
        # Clean up temp file
        try:
            os.unlink(pdf_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info("DA&FW Production Extractor — Starting")
    log.info("=" * 60)

    ensure_data_dir()
    manifest = load_manifest()

    # Phase 1: Discover
    try:
        all_pdfs = discover_pdfs()
    except requests.RequestException as e:
        log.error(f"Failed to fetch index page: {e}")
        sys.exit(1)

    if not all_pdfs:
        log.warning("No PDFs found on the page — exiting")
        sys.exit(0)

    # Filter and select
    new_pdfs = filter_unprocessed(all_pdfs, manifest)
    to_process = select_pdfs_for_processing(new_pdfs, manifest)

    if not to_process:
        log.info("Nothing to process — exiting")
        sys.exit(0)

    # Process each PDF
    success_count = 0
    for pdf_info in to_process:
        ok = process_pdf(pdf_info, manifest)
        if ok:
            success_count += 1

    log.info("=" * 60)
    log.info(f"Finished: {success_count}/{len(to_process)} PDF(s) processed successfully")
    log.info("=" * 60)

    if success_count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
