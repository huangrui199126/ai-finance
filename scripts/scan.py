from __future__ import annotations
"""
scan.py — Scanner & Deduplication Engine
Runs every hour after collect.py.

1. Reads data/raw/*.json
2. Fetches news counts + upcoming earnings for candidate stocks
3. Applies filter rules
4. Deduplicates vs. previous scan
5. Outputs data/filtered/today.json
"""
import json
import logging
import shutil
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import CFG

logger = logging.getLogger(__name__)

RAW_DIR      = Path(CFG["paths"]["raw"])
FILTERED_DIR = Path(CFG["paths"]["filtered"])
FMP_BASE     = CFG["apis"]["fmp"]["base_url"]
FMP_KEY      = CFG["apis"]["fmp"]["key"]
FH_BASE      = CFG["apis"]["finnhub"]["base_url"]
FH_KEY       = CFG["apis"]["finnhub"]["key"]
SC           = CFG["scanner"]


# ─── API helpers ──────────────────────────────────────────────────────────────

def _fmp_get(endpoint: str, params: dict = {}) -> dict | list | None:
    params["apikey"] = FMP_KEY
    try:
        r = requests.get(f"{FMP_BASE}/{endpoint}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"FMP {endpoint}: {e}")
        return None


def _finnhub_get(endpoint: str, params: dict = {}) -> dict | list | None:
    params["token"] = FH_KEY
    try:
        r = requests.get(f"{FH_BASE}/{endpoint}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"Finnhub {endpoint}: {e}")
        return None


# ─── News & Earnings fetchers ─────────────────────────────────────────────────

def fetch_news_count(symbol: str, days_back: int = 2) -> tuple[int, list[str]]:
    """Returns (count, headlines[]) for recent news."""
    today     = datetime.now(timezone.utc)
    from_date = (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
    to_date   = today.strftime("%Y-%m-%d")

    data = _finnhub_get("company-news", {
        "symbol": symbol,
        "from": from_date,
        "to": to_date,
    })
    if not data or not isinstance(data, list):
        return 0, []

    headlines = [item.get("headline", "") for item in data[:5] if item.get("headline")]
    return len(data), headlines


def fetch_earnings_days(symbol: str) -> int | None:
    """Returns days until next earnings, or None if not found."""
    today = datetime.now(timezone.utc).date()
    to    = (today + timedelta(days=30)).strftime("%Y-%m-%d")
    data  = _fmp_get(f"earnings-surprises/{symbol}")

    # Try earnings calendar endpoint
    cal = _fmp_get("earning_calendar", {
        "symbol": symbol,
        "from": today.strftime("%Y-%m-%d"),
        "to": to,
    })
    if cal and isinstance(cal, list) and cal:
        try:
            next_date = datetime.strptime(cal[0]["date"], "%Y-%m-%d").date()
            return (next_date - today).days
        except Exception:
            pass
    return None


# ─── Filter logic ─────────────────────────────────────────────────────────────

def meets_criteria(quote: dict, news_count: int, earnings_days: int | None) -> list[str]:
    """
    Returns list of triggered reasons, empty if stock should be skipped.
    """
    reasons = []

    pct = abs(quote.get("change_pct") or 0)
    if pct >= SC["min_price_change_pct"]:
        reasons.append("price_move")

    ratio = quote.get("volume_ratio") or 0
    if ratio >= SC["min_volume_ratio"]:
        reasons.append("volume_spike")

    if earnings_days is not None and 0 <= earnings_days <= SC["earnings_window_days"]:
        reasons.append("earnings_soon")

    if news_count >= SC["min_news_count"]:
        reasons.append("news_spike")

    return reasons


# ─── Deduplication ────────────────────────────────────────────────────────────

def load_previous_filtered() -> dict[str, list]:
    """Load yesterday's filtered list as {symbol: reasons}."""
    yesterday = FILTERED_DIR / "yesterday.json"
    if not yesterday.exists():
        return {}
    with open(yesterday) as f:
        items = json.load(f)
    return {item["symbol"]: item.get("reasons", []) for item in items}


def is_stale(symbol: str, reasons: list[str], prev: dict[str, list]) -> bool:
    """True if the stock triggered the same or fewer reasons as yesterday."""
    if symbol not in prev:
        return False
    prev_reasons = set(prev[symbol])
    curr_reasons = set(reasons)
    # If current reasons are a subset of previous → no new signal → stale
    return curr_reasons <= prev_reasons


# ─── Main scanner ─────────────────────────────────────────────────────────────

def run() -> dict:
    FILTERED_DIR.mkdir(parents=True, exist_ok=True)

    # Load all raw quotes
    raw_files = list(RAW_DIR.glob("*.json"))
    raw_files = [f for f in raw_files if f.name != "_manifest.json"]
    if not raw_files:
        logger.warning("No raw data found — run collect.py first")
        return {"error": "no_raw_data"}

    logger.info(f"Scanning {len(raw_files)} symbols")

    # Stage 1: cheap in-memory filter (price/volume — no extra API calls)
    candidates: list[dict] = []
    for f in raw_files:
        try:
            with open(f) as fp:
                q = json.load(fp)
            pct   = abs(q.get("change_pct") or 0)
            ratio = q.get("volume_ratio") or 0
            # Pre-filter: if clearly no signal, skip news/earnings API calls
            if pct >= SC["min_price_change_pct"] or ratio >= SC["min_volume_ratio"]:
                candidates.append(q)
            else:
                # Still include low-movers — news/earnings might qualify them
                candidates.append(q)
        except Exception as e:
            logger.debug(f"Could not read {f}: {e}")

    # Stage 2: fetch news + earnings for ALL symbols (but batch cheaply)
    prev_filtered = load_previous_filtered()
    filtered: list[dict] = []

    total = len(candidates)
    for i, quote in enumerate(candidates):
        symbol = quote.get("symbol", "")
        if not symbol:
            continue

        if i % 50 == 0:
            logger.info(f"  scanning {i}/{total}...")

        # Fetch news count
        news_count, headlines = fetch_news_count(symbol)
        quote["news_count"] = news_count
        quote["recent_headlines"] = headlines

        # Fetch earnings
        earnings_days = fetch_earnings_days(symbol)
        quote["earnings_days"] = earnings_days

        # Check filter criteria
        reasons = meets_criteria(quote, news_count, earnings_days)
        if not reasons:
            continue

        # Dedup check
        if is_stale(symbol, reasons, prev_filtered):
            logger.debug(f"  {symbol}: stale (same signal as yesterday)")
            continue

        filtered.append({
            "symbol":          symbol,
            "name":            quote.get("name", ""),
            "price":           quote.get("price"),
            "change_pct":      quote.get("change_pct"),
            "volume_ratio":    quote.get("volume_ratio"),
            "news_count":      news_count,
            "earnings_days":   earnings_days,
            "recent_headlines": headlines,
            "reasons":         reasons,
            "scanned_at":      datetime.now(timezone.utc).isoformat(),
        })

        # Gentle rate limit for Finnhub
        time.sleep(0.5)

        if len(filtered) >= SC["max_output"]:
            logger.info(f"Reached max output ({SC['max_output']}) — stopping scan")
            break

    # Sort by strength (more reasons = more interesting)
    filtered.sort(key=lambda x: len(x["reasons"]), reverse=True)

    # Archive today → yesterday before overwriting
    today_path = FILTERED_DIR / "today.json"
    if today_path.exists():
        shutil.copy(today_path, FILTERED_DIR / "yesterday.json")

    with open(today_path, "w") as f:
        json.dump(filtered, f, indent=2)

    stats = {
        "scanned_at":      datetime.now(timezone.utc).isoformat(),
        "total_scanned":   len(candidates),
        "total_filtered":  len(filtered),
        "symbols":         [s["symbol"] for s in filtered],
    }

    stats_path = FILTERED_DIR / "stats.json"
    with open(stats_path, "w") as f:
        json.dump(stats, f, indent=2)

    logger.info(f"Scanner done: {len(filtered)} stocks selected from {len(candidates)}")
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dotenv import load_dotenv
    load_dotenv()
    result = run()
    print(f"\n✓ Scanner: {result.get('total_filtered', 0)} stocks → data/filtered/today.json")
    if result.get("symbols"):
        print("  Symbols:", ", ".join(result["symbols"]))
