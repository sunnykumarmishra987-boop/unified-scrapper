"""
main.py — Tender Scraper Orchestrator
Runs all scrapers in sequence and reports a combined summary.
Deployed on Railway as a cron job.
"""

import sys
import logging
import gc
from datetime import datetime, timezone

# ── Logging (must be configured before any scraper import) ────────────────────
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
log = logging.getLogger(__name__)


def run_scraper(name: str, fn) -> int:
    """
    Run a single scraper function safely.
    Returns record count on success, 0 on failure.
    Never lets one scraper crash the whole run.
    """
    log.info(f"{'='*60}")
    log.info(f"  Starting: {name}")
    log.info(f"{'='*60}")
    start = datetime.now(timezone.utc)
    try:
        count = fn()
        elapsed = (datetime.now(timezone.utc) - start).seconds
        log.info(f"  Finished: {name} — {count} records in {elapsed}s")
        return count or 0
    except Exception as e:
        elapsed = (datetime.now(timezone.utc) - start).seconds
        log.error(f"  FAILED:   {name} after {elapsed}s — {e}", exc_info=True)
        return 0
    finally:
        gc.collect()


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  TENDER SCRAPER — JOB START")
    log.info(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 60)

    results: dict[str, int] = {}

    # ── 1. AP eProcurement (pure requests, no browser) ────────────────────────
    try:
        from AP_scrapper import run as run_ap
        results["AP eProcurement"] = run_scraper("AP eProcurement", run_ap)
    except ImportError as e:
        log.error(f"Could not import AP_scrapper: {e}")
        results["AP eProcurement"] = 0

    # ── 2. Unified NIC portals (Playwright / Chromium) ────────────────────────
    # Imported after AP so Chromium only launches when AP is already done
    try:
        from unified_Scrapper import run_all as run_unified
        results["Unified NIC portals"] = run_scraper("Unified NIC portals", run_unified)
    except ImportError as e:
        log.error(f"Could not import unified_Scrapper: {e}")
        results["Unified NIC portals"] = 0

    # ── Summary ───────────────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("  SUMMARY")
    log.info("=" * 60)
    grand_total = 0
    for scraper_name, count in results.items():
        status = "✓" if count > 0 else "✗"
        log.info(f"  {status}  {scraper_name}: {count} records")
        grand_total += count
    log.info(f"  TOTAL: {grand_total} records upserted")
    log.info("=" * 60)

    # Exit 1 if every scraper returned 0 — helps Railway flag failed runs
    if grand_total == 0:
        log.error("All scrapers returned 0 records. Exiting with error.")
        sys.exit(1)
