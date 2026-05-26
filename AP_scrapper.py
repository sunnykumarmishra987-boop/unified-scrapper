import os
import sys
import requests
import json
import re
import time
import base64
import hashlib
import logging
import random
import gc
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import psycopg2
from psycopg2 import extras
from dateutil import parser as dateutil_parser

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)

BASE_URL = "https://tender.apeprocurement.gov.in/"
HOME_URL = f"{BASE_URL}/TenderDetailsHome.html#"
API_URL  = f"{BASE_URL}/TenderDetailsHomeJson.html"

SOURCE_PORTAL = "ap_eprocurement"
BACKFILL_DAYS = 1
PAGE_SIZE     = 20
MAX_PAGES     = 150   # 150 × 20 = 3 000 records hard ceiling
PAGE_DELAY    = 1.5

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0",
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
        corrigendum = EXCLUDED.corrigendum,
        scraped_at  = EXCLUDED.scraped_at
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def make_id(source_portal: str, tender_ref_no: str) -> str:
    return hashlib.sha256(f"{source_portal}::{tender_ref_no}".encode()).hexdigest()

def normalize_date(date_str):
    if not date_str or str(date_str).strip() in {"-", "", "None"}:
        return None
    try:
        dt = dateutil_parser.parse(str(date_str).strip(), dayfirst=True)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None

def normalize_value(val_str):
    if not val_str:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(val_str))
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None

def derive_status(deadline_dt) -> str:
    if not deadline_dt:
        return "unknown"
    return "open" if deadline_dt > datetime.now(tz=timezone.utc) else "closed"

def extract_buyer_name(org_chain: str):
    if not org_chain:
        return None
    parts = [p.strip() for p in re.split(r">>|-(?!>)|,", org_chain)]
    return parts[-1] if parts else org_chain

def classify_category(raw_category: str) -> str:
    if not raw_category:
        return "unknown"
    return {
        "WORKS":       "works",
        "GOODS":       "goods",
        "SERVICES":    "services",
        "CONSULTANCY": "consultancy",
    }.get(raw_category.strip().upper(), "unknown")


# ── DB ────────────────────────────────────────────────────────────────────────

class DBConn:
    """Single persistent connection for the full AP scrape run."""

    def __init__(self):
        self._conn = None

    def connect(self) -> bool:
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            log.warning("[AP] DATABASE_URL not set — DB writes disabled.")
            return False
        try:
            self._conn = psycopg2.connect(db_url)
            return True
        except Exception as e:
            log.error(f"[AP] DB connect failed: {e}")
            return False

    def upsert(self, records: list[dict]) -> int:
        """Upsert a batch. Returns count inserted. Skips silently if no connection."""
        if self._conn is None or not records:
            return 0

        # Deduplicate
        seen    = {r["id"]: r for r in records}
        deduped = list(seen.values())
        seen.clear()

        if len(records) != len(deduped):
            log.info(f"[AP] Deduped {len(records) - len(deduped)} duplicate(s).")

        cursor = self._conn.cursor()
        try:
            values = [tuple(r.get(col) for col in COLUMNS) for r in deduped]
            deduped.clear()
            extras.execute_values(cursor, INSERT_SQL, values, page_size=100)
            self._conn.commit()
            log.info(f"[AP] {len(values)} records upserted.")
            return len(values)
        except Exception as e:
            self._conn.rollback()
            log.error(f"[AP] DB upsert failed: {e}")
            raise
        finally:
            cursor.close()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


# ── Scraper ───────────────────────────────────────────────────────────────────

class APTenderScraper:

    def __init__(self):
        self.session = requests.Session()

    def _bootstrap(self) -> bool:
        """Hit the homepage to seed session cookies. Returns False on failure."""
        log.info("[AP] Bootstrapping session...")
        self.session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
        try:
            resp = self.session.get(HOME_URL, timeout=30)
            log.info(f"[AP] Homepage: HTTP {resp.status_code} | cookies: {list(self.session.cookies.keys())}")
            resp.close()
            return True
        except Exception as e:
            log.error(f"[AP] Bootstrap failed: {e}")
            return False

    def _build_params(self, start: int, length: int, echo: int) -> dict:
        return {
            "nTenderID": "", "nDepartmentID": "", "subDeptId": "",
            "ddlDistrict": "", "ddlMandal": "", "biddingType": "",
            "sProcurementType": "", "mECVValue1": "", "mECVValue2": "",
            "dtBidClosingselect": "", "dtBidClosing1": "", "dtBidClosing2": "",
            "dtTenderOpening1": "", "dtTenderOpening2": "",
            "hdnSearch4": "", "hdnSearch": "", "hdncorrigendumsDetails": "",
            "hdncorrigendumsDetails1": "", "hdnnoSearch": "",
            "hdncorrigendumsDetails2": "", "hdnadvsearch": "",
            "hdnPreviousPage": "", "hdnIndentID": "", "hdnTenderCategory": "",
            "hdnProcurementID": "", "hdnType": "current",
            "hdnPreviousPge": "TenderDetailsHome.html",
            "hdnFromStatus": "", "typeOfWorkFromConsolidation": "",
            "popUPRequestParameter": "", "selectedCircleDivison": "",
            "selectedDepartmentID": "", "selectedProcurementType": "",
            "selectedTypeofWork": "", "aid": "",
            "hdnEncryptNames": "hdnEncryptNames",
            "hdnEncryptValues": "hdnEncryptValues",
            "sEcho": str(echo), "iColumns": "9", "sColumns": ",,,,,,,,",
            "iDisplayStart": str(start),
            "iDisplayLength": str(length),
            "mDataProp_0": "0", "bSortable_0": "true",
            "mDataProp_1": "1", "bSortable_1": "true",
            "mDataProp_2": "2", "bSortable_2": "true",
            "mDataProp_3": "3", "bSortable_3": "true",
            "mDataProp_4": "4", "bSortable_4": "true",
            "mDataProp_5": "5", "bSortable_5": "true",
            "mDataProp_6": "6", "bSortable_6": "true",
            "mDataProp_7": "7", "bSortable_7": "true",
            "mDataProp_8": "8", "bSortable_8": "false",
            "iSortCol_0": "6", "sSortDir_0": "desc",
            "iSortingCols": "1",
            "_": str(int(time.time() * 1000)),
        }

    def _fetch_page(self, start: int, length: int, echo: int) -> dict:
        params  = self._build_params(start, length, echo)
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": HOME_URL,
            "Accept":  "*/*",
        }

        resp = None
        for attempt in range(5):
            self.session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
            try:
                resp = self.session.get(API_URL, params=params, headers=headers, timeout=30)
                if resp.status_code == 200:
                    break
                elif resp.status_code in {403, 429, 500, 502, 503, 504}:
                    delay = 2.0 * (2 ** attempt) + random.uniform(0, 1)
                    log.warning(f"[AP] HTTP {resp.status_code} — backing off {delay:.1f}s")
                    resp.close()
                    time.sleep(delay)
                else:
                    resp.raise_for_status()
            except requests.RequestException as e:
                delay = 2.0 * (2 ** attempt) + random.uniform(0, 1)
                log.warning(f"[AP] Network error: {e} — retry in {delay:.1f}s")
                if resp:
                    resp.close()
                time.sleep(delay)
        else:
            raise RuntimeError("Max retries reached — server blocking or network issue.")

        # Read and immediately close — don't hold the response body
        raw = resp.text.strip()
        resp.close()

        if not raw:
            raise RuntimeError("Empty response from API.")

        try:
            decoded = base64.b64decode(raw).decode("utf-8")
        except Exception as e:
            raise RuntimeError(f"Base64 decode failed: {e}")

        try:
            return json.loads(decoded)
        except Exception as e:
            # Write debug file to /tmp — always writable on Railway
            debug_path = "/tmp/ap_debug.txt"
            try:
                with open(debug_path, "w", encoding="utf-8") as f:
                    f.write(decoded)
                log.error(f"[AP] JSON parse failed: {e} — debug saved to {debug_path}")
            except Exception:
                pass
            raise RuntimeError(f"JSON parse failed: {e}")

    def _extract_ids(self, html: str) -> dict:
        match = re.search(r"viewBtn\((\d+),(\d+),(\d+)\)", html or "")
        if not match:
            return {}
        return {
            "work_id":          match.group(1),
            "procurement_type": match.group(2),
            "tender_id":        match.group(3),
        }

    def _parse_row(self, row: list) -> dict:
        """Parse a raw API row. No debug prints — removed for production."""
        if len(row) < 8:
            raise ValueError(f"Row too short: {len(row)} columns")
        ids = self._extract_ids(row[-1])
        return {
            "department":     row[0],
            "tender_number":  row[1],
            "nit_number":     row[2],
            "category":       row[3],
            "title":          row[4],
            "tender_value":   row[5],
            "published_date": row[6],
            "closing_date":   row[7],
            "ids":            ids,
        }

    def scrape(self, db: DBConn) -> int:
        """
        Stream-upsert per page — never accumulates all records in memory.
        Returns total count upserted.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)
        now_utc = datetime.now(timezone.utc)

        log.info(f"[AP] --- START: Andhra Pradesh ---")
        log.info(f"[AP] Cutoff: {cutoff.strftime('%Y-%m-%d %H:%M UTC')} | max {MAX_PAGES} pages")

        total_inserted = 0
        reached_cutoff = False

        for page_num in range(MAX_PAGES):
            if reached_cutoff:
                break

            start = page_num * PAGE_SIZE
            echo  = page_num + 1
            log.info(f"[AP] Page {page_num + 1} | offset {start}–{start + PAGE_SIZE - 1}")

            try:
                data = self._fetch_page(start=start, length=PAGE_SIZE, echo=echo)
            except RuntimeError as e:
                log.error(f"[AP] Fetch failed: {e} — stopping.")
                break

            rows = data.get("aaData", [])
            if not rows:
                log.info("[AP] Empty page — end of results.")
                break

            page_records = []

            for row in rows:
                try:
                    raw = self._parse_row(row)
                except Exception as e:
                    log.warning(f"[AP] Row parse error: {e}")
                    continue

                pub_dt = normalize_date(raw["published_date"])

                if pub_dt is not None and pub_dt < cutoff:
                    log.info(f"[AP] Cutoff reached at {pub_dt.strftime('%Y-%m-%d')}. Stopping.")
                    reached_cutoff = True
                    break

                tender_ref_no = str(raw.get("tender_number") or "").strip()
                if not tender_ref_no:
                    continue

                deadline_dt     = normalize_date(raw["closing_date"])
                ids             = raw.get("ids") or {}
                buyer_org_chain = raw.get("department", "")

                page_records.append({
                    "id":              make_id(SOURCE_PORTAL, tender_ref_no),
                    "tender_ref_no":   tender_ref_no,
                    "nit_number":      raw.get("nit_number"),
                    "source_portal":   SOURCE_PORTAL,
                    "source_url":      f"{HOME_URL}tender_number={tender_ref_no}",
                    "title":           raw.get("title"),
                    "category":        classify_category(raw.get("category")),
                    "buyer_name":      extract_buyer_name(buyer_org_chain),
                    "buyer_org_chain": buyer_org_chain,
                    "state":           "Andhra Pradesh",
                    "location":        None,
                    "value":           normalize_value(raw.get("tender_value")),
                    "currency":        "INR",
                    "published_at":    pub_dt.isoformat() if pub_dt else None,
                    "deadline_at":     deadline_dt.isoformat() if deadline_dt else None,
                    "opening_at":      None,
                    "status":          derive_status(deadline_dt),
                    "corrigendum":     False,
                    "detail_scraped":  False,
                    "scraped_at":      now_utc.isoformat(),
                    "portal_metadata": json.dumps(ids) if ids else "{}",
                })

            # Upsert this page immediately — don't accumulate
            if page_records:
                inserted = db.upsert(page_records)
                total_inserted += inserted
                del page_records
                gc.collect()

            time.sleep(PAGE_DELAY)

        log.info(f"[AP] --- DONE: Andhra Pradesh — {total_inserted} records ---")
        return total_inserted


# ── Public entry point (called by main.py) ────────────────────────────────────

def run() -> int:
    """Run the AP scraper. Returns total records upserted."""
    scraper = APTenderScraper()

    if not scraper._bootstrap():
        log.error("[AP] Session bootstrap failed. Aborting AP scraper.")
        return 0

    db = DBConn()
    if not db.connect():
        log.error("[AP] No DB connection. Aborting AP scraper.")
        return 0

    try:
        return scraper.scrape(db)
    finally:
        db.close()
        scraper.session.close()


# ── Standalone run ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-5s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    total = run()
    if total == 0:
        log.warning("No records upserted.")
        sys.exit(1)
