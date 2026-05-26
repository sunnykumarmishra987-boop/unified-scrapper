import os
import re
import time
import json
import hashlib
import gc
import sys
import logging
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import requests
from playwright.sync_api import sync_playwright
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import psycopg2
from psycopg2 import extras
from dateutil import parser as dateutil_parser

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
log = logging.getLogger(__name__)

def portal_log(portal: str, msg: str, level: str = "info"):
    getattr(log, level)(f"[{portal.upper()}] {msg}")

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_CAPTCHA_ATTEMPTS = 5
LOCAL_OCR_URL        = os.getenv("Ocr_url") or os.getenv("OCR_URL")

PORTALS = [
    {"base": "https://mahatenders.gov.in",      "portal": "mahatenders",            "state": "Maharashtra"},
    {"base": "https://tntenders.gov.in",         "portal": "tntenders",              "state": "Tamil Nadu"},
    {"base": "https://etender.up.nic.in",        "portal": "up_eprocurement",        "state": "Uttar Pradesh"},
    {"base": "https://wbtenders.gov.in",         "portal": "wbtenders",              "state": "West Bengal"},
    {"base": "https://eproc.rajasthan.gov.in",   "portal": "rajasthan_eprocurement", "state": "Rajasthan"},
    {"base": "https://etenders.kerala.gov.in",   "portal": "kerala_tenders",         "state": "Kerala"},
]

COLUMNS = [
    "id", "tender_ref_no", "nit_number", "source_portal", "source_url",
    "title", "category", "buyer_name", "buyer_org_chain", "state",
    "location", "value", "currency", "published_at", "deadline_at",
    "opening_at", "status", "corrigendum", "detail_scraped", "scraped_at",
    "portal_metadata",
]

INSERT_SQL = f"""
    INSERT INTO tenders ({", ".join(COLUMNS)})
    VALUES %s
    ON CONFLICT (id) DO UPDATE SET
        status      = EXCLUDED.status,
        deadline_at = EXCLUDED.deadline_at,
        value       = EXCLUDED.value,
        scraped_at  = EXCLUDED.scraped_at
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def solve_captcha_via_local_api(image_bytes: bytes) -> str | None:
    if not LOCAL_OCR_URL:
        log.warning("OCR_URL not set — cannot solve CAPTCHA.")
        return None
    log.info(f"Sending CAPTCHA to OCR: {LOCAL_OCR_URL}")
    try:
        # Use a context manager so the response is closed and memory freed immediately
        with requests.post(
            LOCAL_OCR_URL,
            files={"file": ("captcha.png", image_bytes, "image/png")},
            timeout=10,
        ) as resp:
            resp.raise_for_status()
            data = resp.json()
        prediction = data.get("prediction") if data.get("status") == "success" else None
        log.info(f"OCR result: {repr(prediction)}")
        return prediction
    except requests.exceptions.ConnectionError as e:
        log.warning(f"OCR server unreachable: {repr(e)}")
    except Exception as e:
        log.warning(f"OCR API error: {repr(e)}")
    return None


def make_id(source_portal: str, tender_ref_no: str) -> str:
    return hashlib.sha256(f"{source_portal}::{tender_ref_no}".encode()).hexdigest()


def normalize_date(date_str: str):
    if not date_str or str(date_str).strip() in {"-", "", "None", "NA"}:
        return None
    try:
        dt = dateutil_parser.parse(str(date_str).strip())
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None


def normalize_value(val_str: str):
    if not val_str or val_str.strip().upper() in {"NA", "N/A", "-"}:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(val_str))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def extract_metadata(raw_text: str) -> dict:
    tender_id_match = re.search(r'Tender\s*ID\s*[:\-]\s*([^\n\r\]]+)', raw_text, re.IGNORECASE)
    if not tender_id_match:
        tender_id_match = re.search(r'(20[1-3][0-9]_[A-Z0-9_]+_\d+)', raw_text, re.IGNORECASE)
    nit_match = re.search(r'Reference\s*No\s*[:\-]\s*([^\n\r\]]+)', raw_text, re.IGNORECASE)
    return {
        "tender_id":  tender_id_match.group(1).strip() if tender_id_match else None,
        "nit_number": nit_match.group(1).strip()       if nit_match       else None,
    }


def row_to_tuple(row: dict) -> tuple:
    """Convert a record dict to a tuple in COLUMNS order for bulk insert."""
    return tuple(row.get(col) for col in COLUMNS)


# ── DB ────────────────────────────────────────────────────────────────────────

class DBConn:
    """
    One persistent connection per portal run.
    Avoids opening/closing a connection on every page (was ~20 connects per portal).
    """
    def __init__(self, portal: str):
        self.portal = portal
        self._conn  = None

    def connect(self):
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            portal_log(self.portal, "DATABASE_URL not set — DB writes disabled.", "warning")
            return False
        self._conn = psycopg2.connect(db_url)
        return True

    def upsert(self, records: list[dict]):
        if self._conn is None or not records:
            return

        # Deduplicate in-place — no extra list copy
        seen   = {}
        for r in records:
            seen[r["id"]] = r
        deduped = list(seen.values())
        seen.clear()  # free the dict immediately

        if len(records) != len(deduped):
            portal_log(self.portal, f"Deduped {len(records) - len(deduped)} duplicate(s).")

        cursor = self._conn.cursor()
        try:
            # Convert to tuples here so the dict objects can be GC'd sooner
            values = [row_to_tuple(r) for r in deduped]
            deduped.clear()  # free dicts before the network round-trip
            extras.execute_values(cursor, INSERT_SQL, values, page_size=100)
            self._conn.commit()
            portal_log(self.portal, f"{len(values)} records upserted.")
        except Exception as e:
            self._conn.rollback()
            portal_log(self.portal, f"DB upsert failed: {e}", "error")
            raise
        finally:
            cursor.close()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


# ── Scraper ───────────────────────────────────────────────────────────────────

def _solve_captcha(page, target_url: str, p: str) -> bool:
    """
    Isolated CAPTCHA bypass. Returns True if solved.
    Kept separate so its local variables (image_bytes etc.) are freed on return.
    """
    for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
        portal_log(p, f"CAPTCHA attempt {attempt}/{MAX_CAPTCHA_ATTEMPTS}...")

        # Read page content ONCE per attempt — avoid calling page.content() multiple times
        html = page.content()

        if attempt == 1:
            portal_log(p, f"Page snippet: {html[:400].replace(chr(10), ' ')}")

        if "APPLICATION SECURITY ERROR" in html:
            portal_log(p, "Security wall hit. Resetting...", "warning")
            page.goto(target_url, wait_until="load", timeout=60000)
            page.wait_for_timeout(3000)
            html = page.content()

        del html  # free the full HTML string before screenshot

        try:
            captcha_element = page.locator("img[id^='captcha'], img[src*='captcha']")
            visible = captcha_element.is_visible()
            portal_log(p, f"Primary CAPTCHA selector visible: {visible}")

            if not visible:
                captcha_element = page.locator("td img").first
                visible = captcha_element.is_visible()
                portal_log(p, f"Fallback CAPTCHA (td img) visible: {visible}")

            if not visible:
                all_imgs = page.locator("img").all()
                portal_log(p, f"Total <img> on page: {len(all_imgs)}", "warning")
                for i, img in enumerate(all_imgs[:5]):
                    try:
                        portal_log(p, f"  img[{i}] src={img.get_attribute('src')} id={img.get_attribute('id')}")
                    except Exception:
                        pass
                del all_imgs
                page.reload()
                page.wait_for_load_state("load", timeout=60000)
                page.wait_for_timeout(3000)
                continue

            # Screenshot → OCR → immediately delete bytes
            image_bytes  = captcha_element.screenshot(type="png")
            captcha_text = solve_captcha_via_local_api(image_bytes)
            del image_bytes  # free PNG bytes right away

            if not captcha_text or len(captcha_text) < 3:
                portal_log(p, f"Bad OCR result: {repr(captcha_text)} — reloading.", "warning")
                page.reload()
                page.wait_for_load_state("load", timeout=60000)
                continue

            input_box = page.locator("input[type='text'][name*='captcha'], input[id*='captcha']")
            if not input_box.is_visible():
                input_box = page.locator("input[type='text']").first
            input_box.fill(captcha_text)
            portal_log(p, f"Filled CAPTCHA: '{captcha_text}'")

            search_button = page.locator("input[type='submit'][value*='Search'], input[id*='submit']")
            if not search_button.is_visible():
                search_button = page.locator("input[type='submit']").first
            search_button.click()
            time.sleep(2)

            page.reload()
            page.wait_for_load_state("load", timeout=60000)

            tender_rows = page.locator("tr[id^='informal'], table#table").count()
            portal_log(p, f"Tender rows after submit: {tender_rows}")

            if tender_rows > 0:
                portal_log(p, "CAPTCHA bypassed!")
                return True

            portal_log(p, "No tender rows — retrying CAPTCHA...", "warning")

        except Exception as e:
            portal_log(p, f"Attempt {attempt} error: {repr(e)}", "warning")
            time.sleep(1)

    return False


def _parse_page(html: str, config: dict, cutoff, now_utc) -> tuple[list[dict], bool]:
    """
    Parse one page of HTML. Returns (records, reached_cutoff).
    Runs in its own function so BeautifulSoup + rows are freed when it returns.
    """
    soup    = BeautifulSoup(html, "html.parser")
    rows    = soup.find_all("tr", class_=["even", "odd"])
    soup.decompose()   # explicitly free BS4 tree — much faster GC than waiting for refcount
    del soup

    records        = []
    reached_cutoff = False

    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 7:
            continue

        pub_dt = normalize_date(cols[1].get_text(strip=True))
        if pub_dt and pub_dt < cutoff:
            reached_cutoff = True
            break

        title_cell_text = cols[4].get_text(strip=True)
        meta            = extract_metadata(title_cell_text)
        tender_ref_no   = meta["tender_id"] or meta["nit_number"]
        if not tender_ref_no:
            continue

        link_tag     = cols[4].find("a", id=re.compile(r"^DirectLink")) or cols[4].find("a")
        tender_title = link_tag.get_text(strip=True).strip("[]") if link_tag else title_cell_text
        tender_url   = urljoin(config["base"], link_tag.get("href", "")) if link_tag else ""

        deadline_dt  = normalize_date(cols[2].get_text(strip=True))
        opening_dt   = normalize_date(cols[3].get_text(strip=True))
        org_chain    = cols[5].get_text(strip=True)

        records.append({
            "id":              make_id(config["portal"], tender_ref_no),
            "tender_ref_no":   tender_ref_no,
            "nit_number":      meta["nit_number"],
            "source_portal":   config["portal"],
            "source_url":      tender_url,
            "title":           tender_title,
            "category":        "unknown",
            "buyer_name":      org_chain.split("||")[-1].strip(),
            "buyer_org_chain": org_chain,
            "state":           config["state"],
            "location":        None,
            "value":           normalize_value(cols[6].get_text(strip=True)),
            "currency":        "INR",
            "published_at":    pub_dt.isoformat()      if pub_dt      else None,
            "deadline_at":     deadline_dt.isoformat() if deadline_dt else None,
            "opening_at":      opening_dt.isoformat()  if opening_dt  else None,
            "status":          "open" if (deadline_dt and deadline_dt > now_utc) else "closed",
            "corrigendum":     False,
            "detail_scraped":  False,
            "scraped_at":      now_utc.isoformat(),
            "portal_metadata": json.dumps(meta),
        })

    # Free the BS4 row objects explicitly
    del rows
    return records, reached_cutoff


def scrape_portal(page, config: dict, db: DBConn) -> int:
    p = config["portal"]
    portal_log(p, f"--- START: {config['state']} ---")

    target_url = f"{config['base']}/nicgep/app?page=FrontEndLatestActiveTenders&service=page"
    portal_log(p, f"Navigating to: {target_url}")

    loaded = False
    for attempt in range(1, 3):
        try:
            page.goto(target_url, wait_until="load", timeout=60000)
            loaded = True
            break
        except Exception as e:
            portal_log(p, f"Load attempt {attempt}/2 failed: {repr(e)}", "warning")
            if attempt < 2:
                time.sleep(5)

    if not loaded:
        portal_log(p, "Could not load. Skipping.", "error")
        return 0

    portal_log(p, f"Page title: '{page.title()}'")
    page.wait_for_timeout(3000)

    if not _solve_captcha(page, target_url, p):
        portal_log(p, "CAPTCHA failed. Skipping portal.", "error")
        return 0

    cutoff         = datetime.now(timezone.utc) - timedelta(days=1)
    now_utc        = datetime.now(timezone.utc)
    page_num       = 1
    total_inserted = 0

    while True:
        portal_log(p, f"Parsing page {page_num}...")

        # Grab HTML once, pass to parser, free immediately after
        html = page.content()
        records, reached_cutoff = _parse_page(html, config, cutoff, now_utc)
        del html

        if not records and not reached_cutoff:
            portal_log(p, "No rows found. Stopping.", "warning")
            break

        if records:
            db.upsert(records)
            total_inserted += len(records)
            del records
            gc.collect()

        if reached_cutoff:
            portal_log(p, f"1-day cutoff reached. Stopping.")
            break

        # Use only ID-based selectors — a:has-text('Next') matches unrelated links like loadNext
        next_btn = page.locator("a#linkFwd, a#LinkFwd").first
        try:
            next_visible = next_btn.is_visible(timeout=3000)
        except Exception:
            next_visible = False

        if next_visible:
            try:
                next_btn.click(timeout=15000)
                page.wait_for_load_state("load", timeout=60000)
                time.sleep(2)
                page_num += 1
            except Exception as e:
                portal_log(p, f"Next button click failed on page {page_num}: {repr(e)}. Stopping.", "warning")
                break
        else:
            portal_log(p, "No further pages.")
            break

    portal_log(p, f"--- DONE: {config['state']} — {total_inserted} records ---")
    return total_inserted


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("=== Tender Scraper Starting ===")
    log.info(f"OCR_URL set:      {'YES' if LOCAL_OCR_URL else 'NO  ← CAPTCHA WILL FAIL'}")
    log.info(f"DATABASE_URL set: {'YES' if os.getenv('DATABASE_URL') else 'NO'}")

    grand_total = 0

    with sync_playwright() as pw:
        log.info("Launching headless Chromium...")
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--single-process",
                "--js-flags=--max-old-space-size=256",  # cap Chromium's V8 heap
            ],
        )

        for config in PORTALS:
            # Fresh context per portal — clears Chromium's cache, cookies, and JS heap
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                java_script_enabled=True,
            )
            # Block heavy resources; keep images for CAPTCHA
            context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("media", "font", "stylesheet")
                else route.continue_(),
            )
            page = context.new_page()

            # One DB connection for the whole portal run
            db = DBConn(config["portal"])
            if not db.connect():
                portal_log(config["portal"], "Skipping — no DB connection.", "error")
                context.close()
                continue

            try:
                count = scrape_portal(page, config, db)
                grand_total += count
            finally:
                db.close()
                context.close()  # closes page + frees Chromium context memory
                gc.collect()
                time.sleep(3)

        browser.close()

    log.info(f"=== ALL DONE — {grand_total} total records across all portals ===")
