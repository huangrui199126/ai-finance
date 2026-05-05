from __future__ import annotations
"""
scheduler.py — Main Orchestrator

Pipeline (event-driven, runs once daily after market close):

  SCREEN (replaces collect + scan):
    1. screen.py    — market-wide crash screener (market cap > $5B, drop ≤ -8%)

  ENRICH:
    2. themes.py    — theme detection from screened candidates

  ANALYZE:
    3. fundamental_filter.py  — quality gate + scoring
    4. analyze.py             — two-stage LLM (drop classifier → put evaluator)
    5. report.py              — generate daily JSON
    6. push.py                — git commit + push

Usage:
    python scheduler.py          # run scheduler (blocking)
    python scheduler.py --once   # run full pipeline once immediately (for testing)
"""
import argparse
import logging
import sys
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, ".")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scheduler")


# ─── Job definitions ──────────────────────────────────────────────────────────

def job_screen():
    """
    Event-driven screener — replaces the old collect + scan hourly loop.
    Queries the whole market for biggest crashes in quality companies.
    """
    logger.info("═══ SCREEN JOB START ═══")
    try:
        from scripts.screen import run as screen
        result = screen()
        n = result.get("total_qualified", 0)
        logger.info(f"Screen complete: {n} candidates → {result.get('symbols', [])}")
    except Exception as e:
        logger.error(f"screen failed: {e}")

    try:
        from scripts.themes import run as themes
        themes()
    except Exception as e:
        logger.error(f"themes failed: {e}")

    logger.info("═══ SCREEN JOB DONE ═══")


def job_daily():
    logger.info("═══ DAILY JOB START ═══")

    # Run the screener first to get today's crash candidates
    job_screen()

    try:
        from scripts.fundamental_filter import run as ff
        ff()
    except Exception as e:
        logger.error(f"fundamental_filter failed: {e}")

    try:
        from scripts.analyze import run as analyze
        analyze()
    except Exception as e:
        logger.error(f"analyze failed: {e}")

    try:
        from scripts.report import run as report
        report()
    except Exception as e:
        logger.error(f"report failed: {e}")

    try:
        from scripts.push import run as push
        push()
    except Exception as e:
        logger.error(f"push failed: {e}")

    logger.info("═══ DAILY JOB DONE ═══")


def run_once():
    """Run the full pipeline immediately (for testing)."""
    logger.info("Running full pipeline once...")
    job_daily()
    logger.info("Full pipeline complete.")


# ─── Scheduler setup ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run full pipeline once and exit")
    args = parser.parse_args()

    if args.once:
        run_once()
        return

    from config.settings import CFG
    tz       = CFG["scheduler"]["timezone"]
    analyze_time = CFG["scheduler"]["analyze_time"]  # e.g. "17:45"
    hour, minute = analyze_time.split(":")

    scheduler = BlockingScheduler(timezone=tz)

    # Daily at market close: screen + filter + analyze + report + push
    scheduler.add_job(
        job_daily,
        trigger=CronTrigger(hour=int(hour), minute=int(minute), timezone=tz),
        id="daily",
        name=f"Daily at {analyze_time} ET: screen + analyze + report + push",
        misfire_grace_time=600,
    )

    logger.info(f"Scheduler starting — timezone: {tz}")
    logger.info(f"  Daily job: {analyze_time} ET (screen → themes → filter → analyze → report → push)")

    # Run full pipeline immediately on start
    logger.info("Running initial pipeline...")
    job_daily()

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user")


if __name__ == "__main__":
    main()
