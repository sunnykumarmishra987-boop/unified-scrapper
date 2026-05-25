import os
import re
import time
import json
import hashlib
import gc
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

MAX_CAPTCHA_ATTEMPTS = 5
LOCAL_OCR_URL = os.getenv("Ocr_url") or os.getenv("OCR_URL")

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

def solve_captcha_via_local_api(image_bytes):
    if not LOCAL_OCR_URL:
        print("    [-] Ocr_url env var is not set! Cannot solve CAPTCHA.")
        return None
    print(f"    [*] Sending CAPTCHA to OCR server: {LOCAL_OCR_URL}")
    try:
        files    = {"file": ("captcha.png", image_bytes, "image/png")}
        response = requests.post(LOCAL_OCR_URL, files=files, timeout=10)
        response.raise_for_status()
        data       = response.json()
        prediction = data.get("prediction") if data.get("status") == "success" else None
        print(f"    [*] OCR response: status={data.get('status')}, prediction={repr(prediction)}")
        return prediction
    except requests.exceptions.ConnectionError as e:
        print(f"    [-] OCR server unreachable: {repr(e)}")
        return None
    except Exception as e:
        print(f"    [-] OCR API error: {repr(e)}")
        return None

def make_id(source_portal, tender_ref_no):
    return hashlib.sha256(f"{source_portal}::{tender_ref_no}".encode()).hexdigest()

def normalize_date(date_str):
    if not date_str or str(date_str).strip() in ["-", "", "None", "NA"]:
        return None
    try:
        dt = dateutil_parser.parse(str(date_str).strip())
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None

def normalize_value(val_str):
    if not val_str or val_str.strip().upper() in ["NA", "N/A", "-"]:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(val_str))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None

def extract_metadata(raw_text):
    tender_id_match = re.search(r'Tender\s*ID\s*[:\-]\s*([^\n\r\]]+)', raw_text, re.IGNORECASE)
    if not tender_id_match:
        tender_id_match = re.search(r'(20[1-3][0-9]_[A-Z0-9_]+_\d+)', raw_text, re.IGNORECASE)
    nit_match = re.search(r'Reference\s*No\s*[:\-]\s*([^\n\r\]]+)', raw_text, re.IGNORECASE)
    return {
        "tender_id":  tender_id_match.group(1).strip() if tender_id_match else None,
        "nit_number": nit_match.group(1).strip()       if nit_match       else None,
    }

def extract_buyer_name(org_chain):
    if not org_chain:
        return None
    return org_chain.split("||")[-1].strip()

# ── Scraper Core ──────────────────────────────────────────────────────────────

def scrape_portal(page, config):
    print(f"\n{'='*60}")
    print(f"[*] SCRAPING: {config['state']} ({config['portal']})")
    print(f"{'='*60}")

    target_url = f"{config['base']}/nicgep/app?page=FrontEndLatestActiveTenders&service=page"
    print(f"[*] Navigating to: {target_url}")

    loaded = False
    for nav_attempt in range(1, 3):  # 2 attempts before giving up
        try:
            page.goto(target_url, wait_until="load", timeout=60000)
            loaded = True
            break
        except Exception as e:
            print(f"[-] Page load attempt {nav_attempt}/2 failed: {repr(e)}")
            if nav_attempt < 2:
                print("[*] Retrying after 5s...")
                time.sleep(5)

    if not loaded:
        print(f"[-] Could not load {config['state']} after 2 attempts. Skipping.")
        return 0

    print(f"[*] Page title: '{page.title()}'")
    page.wait_for_timeout(3000)

    # ── CAPTCHA Bypass ────────────────────────────────────────────────────────
    captcha_solved = False
    for attempt in range(1, MAX_CAPTCHA_ATTEMPTS + 1):
        print(f"[*] CAPTCHA attempt {attempt}/{MAX_CAPTCHA_ATTEMPTS}...")

        if attempt == 1:
            snippet = page.content()[:1500].replace("\n", " ").strip()
            print(f"    [*] Page HTML snippet: {snippet}")

        if "APPLICATION SECURITY ERROR" in page.content():
            print("[!] Security session wall hit. Resetting...")
            page.goto(target_url, wait_until="load", timeout=60000)
            page.wait_for_timeout(3000)

        try:
            captcha_element = page.locator("img[id^='captcha'], img[src*='captcha']")
            visible = captcha_element.is_visible()
            print(f"    [*] Primary CAPTCHA selector visible: {visible}")

            if not visible:
                captcha_element = page.locator("td img").first
                visible = captcha_element.is_visible()
                print(f"    [*] Fallback CAPTCHA selector (td img) visible: {visible}")

            if not visible:
                all_imgs = page.locator("img").all()
                print(f"    [*] Total <img> elements on page: {len(all_imgs)}")
                for i, img in enumerate(all_imgs[:5]):
                    try:
                        print(f"         img[{i}] src={img.get_attribute('src')}, id={img.get_attribute('id')}")
                    except Exception:
                        pass
                print("    [-] No CAPTCHA image found — page may not have rendered yet.")
                snippet = page.content()[:1000].replace("\n", " ").strip()
                print(f"    [*] Current page HTML: {snippet}")
                page.reload()
                page.wait_for_load_state("load", timeout=60000)
                page.wait_for_timeout(3000)
                continue

            image_bytes  = captcha_element.screenshot(type="png")
            captcha_text = solve_captcha_via_local_api(image_bytes)
            del image_bytes

            if not captcha_text or len(captcha_text) < 3:
                print(f"    [-] OCR returned unusable text: {repr(captcha_text)} — reloading.")
                page.reload()
                page.wait_for_load_state("load", timeout=60000)
                continue

            input_box = page.locator("input[type='text'][name*='captcha'], input[id*='captcha']")
            if not input_box.is_visible():
                input_box = page.locator("input[type='text']").first
            input_box.fill(captcha_text)
            print(f"    [*] Filled CAPTCHA input with: '{captcha_text}'")

            search_button = page.locator("input[type='submit'][value*='Search'], input[id*='submit']")
            if not search_button.is_visible():
                search_button = page.locator("input[type='submit']").first
            search_button.click()
            time.sleep(2)

            page.reload()
            page.wait_for_load_state("load", timeout=60000)

            tender_rows = page.locator("tr[id^='informal'], table#table").count()
            print(f"    [*] Tender rows found after submit: {tender_rows}")

            if tender_rows > 0:
                print("[+] CAPTCHA bypassed successfully!")
                captcha_solved = True
                break
            else:
                print("    [-] CAPTCHA submit did not reveal tender rows. Retrying...")

        except Exception as e:
            print(f"[-] Attempt {attempt} failed: {repr(e)}. Retrying...")
            time.sleep(1)

    if not captcha_solved:
        print(f"[✗] Failed to bypass CAPTCHA for {config['state']}. Skipping portal.")
        return 0
    # ── End CAPTCHA Bypass ────────────────────────────────────────────────────

    cutoff         = datetime.now(timezone.utc) - timedelta(days=1)
    now_utc        = datetime.now(timezone.utc)
    page_num       = 1
    total_inserted = 0

    while True:
        print(f"[*] Parsing page {page_num}...")
        soup = BeautifulSoup(page.content(), "html.parser")
        rows = soup.find_all("tr", class_=["even", "odd"])
        del soup

        if not rows:
            print("[-] No table rows found. Stopping.")
            break

        page_records   = []
        reached_cutoff = False

        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 7:
                continue

            pub_dt = normalize_date(cols[1].text.strip())

            if pub_dt and pub_dt < cutoff:
                print(f"    [✓] Reached 1-day cutoff at {pub_dt.strftime('%Y-%m-%d')}. Stopping.")
                reached_cutoff = True
                break

            title_cell_text = cols[4].text.strip()
            meta            = extract_metadata(title_cell_text)
            tender_ref_no   = meta["tender_id"] or meta["nit_number"]

            if not tender_ref_no:
                print(f"    [!] Skipping row: no ID found in -> '{title_cell_text[:60]}...'")
                continue

            link_tag     = cols[4].find("a", id=re.compile(r"^DirectLink"))
            if not link_tag:
                link_tag = cols[4].find("a")
            tender_title = link_tag.text.strip().strip("[]") if link_tag else title_cell_text
            tender_url   = urljoin(config["base"], link_tag.get("href", "")) if link_tag else ""

            deadline_dt = normalize_date(cols[2].text.strip())
            opening_dt  = normalize_date(cols[3].text.strip())

            page_records.append({
                "id":              make_id(config["portal"], tender_ref_no),
                "tender_ref_no":   tender_ref_no,
                "nit_number":      meta["nit_number"],
                "source_portal":   config["portal"],
                "source_url":      tender_url,
                "title":           tender_title,
                "category":        "unknown",
                "buyer_name":      extract_buyer_name(cols[5].text.strip()),
                "buyer_org_chain": cols[5].text.strip(),
                "state":           config["state"],
                "location":        None,
                "value":           normalize_value(cols[6].text.strip()),
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

        if page_records:
            upsert(page_records)
            total_inserted += len(page_records)
            del page_records
            gc.collect()

        if reached_cutoff:
            break

        next_btn = page.locator("a#linkFwd, a#LinkFwd, a:has-text('Next')").first
        if next_btn.is_visible():
            next_btn.click()
            page.wait_for_load_state("load", timeout=60000)
            time.sleep(2)
            page_num += 1
        else:
            print("[-] No further pages.")
            break

    print(f"[✓] {config['state']} done. {total_inserted} records upserted.")
    return total_inserted


# ── DB Upsert ─────────────────────────────────────────────────────────────────

def upsert(records: list[dict]):
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("[!] DATABASE_URL not set — skipping DB insert.")
        return
    if not records:
        return

    unique  = {r["id"]: r for r in records}
    deduped = list(unique.values())

    if len(records) > len(deduped):
        print(f"[*] Deduped {len(records) - len(deduped)} duplicate(s) before insert.")

    conn   = psycopg2.connect(db_url)
    cursor = conn.cursor()
    try:
        values = [tuple(r.get(col) for col in COLUMNS) for r in deduped]
        extras.execute_values(cursor, INSERT_SQL, values, page_size=100)
        conn.commit()
        print(f"    [✓] {len(deduped)} records upserted.")
    except Exception as e:
        conn.rollback()
        print(f"    [-] DB upsert failed: {e}")
        raise
    finally:
        cursor.close()
        conn.close()


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("[*] Config check:")
    print(f"    OCR_URL set:      {'YES' if LOCAL_OCR_URL else 'NO ← THIS WILL BREAK CAPTCHA'}")
    print(f"    DATABASE_URL set: {'YES' if os.getenv('DATABASE_URL') else 'NO'}")

    grand_total = 0

    with sync_playwright() as p:
        print("[*] Launching browser (headless)...")
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--single-process",
            ],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        # Block heavy resources to save bandwidth; images kept for CAPTCHA
        context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("media", "font", "stylesheet")
            else route.continue_()
        )

        page = context.new_page()

        for config in PORTALS:
            count = scrape_portal(page, config)
            grand_total += count
            gc.collect()
            time.sleep(3)

        browser.close()

    print(f"\n[✓] ALL DONE. {grand_total} total records upserted across all portals.")