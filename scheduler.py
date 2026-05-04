from __future__ import annotations
"""
scheduler.py — Main Orchestrator

Runs two recurring jobs:

  HOURLY:
    1. collect.py   — fetch quotes for all stocks
    2. scan.py      — anomaly detection
    3. themes.py    — theme detection (uses news from scan)

  DAILY (17:45 ET, after market close):
    1. fundamental_filter.py  — quality gate + scoring
    2. analyze.py             — LLM analysis
    3. report.py              — generate daily JSON
    4. push.py                — git commit + push

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

def job_hourly():
    logger.info("═══ HOURLY JOB START ═══")
    try:
        from scripts.collect import run as collect
        collect()
    except Exception as e:
        logger.error(f"collect failed: {e}")

    try:
        from scripts.scan import run as scan
        scan()
    except Exception as e:
        logger.error(f"scan failed: {e}")

    try:
        from scripts.themes import run as themes
        themes()
    except Exception as e:
        logger.error(f"themes failed: {e}")

    logger.info("═══ HOURLY JOB DONE ═══")


def job_daily():
    logger.info("═══ DAILY JOB START ═══")

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
    job_hourly()
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

    # Hourly: collect + scan + themes
    scheduler.add_job(
        job_hourly,
        trigger="interval",
        hours=1,
        id="hourly",
        name="Hourly: collect + scan + themes",
        misfire_grace_time=300,
    )

    # Daily at market close: filter + analyze + report + push
    scheduler.add_job(
        job_daily,
        trigger=CronTrigger(hour=int(hour), minute=int(minute), timezone=tz),
        id="daily",
        name=f"Daily at {analyze_time} ET: analyze + report + push",
        misfire_grace_time=600,
    )

    logger.info(f"Scheduler starting — timezone: {tz}")
    logger.info(f"  Hourly job: every 1h (collect + scan + themes)")
    logger.info(f"  Daily job:  {analyze_time} ET (filter + analyze + report + push)")

    # Run hourly job immediately on start
    logger.info("Running initial hourly job...")
    job_hourly()

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user")


if __name__ == "__main__":
    main()
