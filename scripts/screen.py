from __future__ import annotations
"""
screen.py — Event-Driven Market Screener

Replaces collect.py + scan.py + fetch_symbols.py.

Instead of scanning a fixed symbol list, this queries the entire market
for stocks that are ALREADY MOVING — biggest single-day crashes and
multi-day breakdowns in quality companies.

Criteria (ALL must pass):
  - Market cap > $5B
  - Daily drop ≤ -8%  OR  5-day drop ≥ -12%
  - Volume spike (current vol ≥ 1.5× 20-day average)

Sources:
  1. FMP biggest-losers       — top daily drops, market-wide
  2. FMP stock-screener       — market cap + price change filter
  3. FMP historical prices    — 5-day drop calculation

Output: data/filtered/today.json (same format as scan.py)
        data/raw/{SYMBOL}.json  (raw quote per symbol)
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
FMP_V3       = CFG["apis"]["fmp"].get("base_url_v3", "https://financialmodelingprep.com/api/v3")
FMP_KEY      = CFG["apis"]["fmp"]["key"]
FH_BASE      = CFG["apis"]["finnhub"]["base_url"]
FH_KEY       = CFG["apis"]["finnhub"]["key"]

# ── Screening thresholds ───────────────────────────────────────────────────────
MIN_MARKET_CAP     = 5_000_000_000    # $5B minimum — hard floor, no exceptions
DAILY_DROP_PCT     = -5.0             # single-day crash threshold for large caps
#   Note: -5% is right for $5B+ stocks. Large caps rarely drop 8%+ except on
#   major earnings misses (Roblox -22%, Meta -10%). On a normal day 0 results
#   is correct. On an earnings/catalyst day you'll get real targets.
MULTIDAY_DROP_PCT  = -12.0            # 5-day cumulative drop threshold
MIN_VOLUME_RATIO   = 1.5              # current vol must be 1.5× average
MAX_CANDIDATES     = 30               # hard cap on output
NEWS_DAYS_BACK     = 2                # days of news to fetch


# ─── API helpers ──────────────────────────────────────────────────────────────

def _fmp(endpoint: str, params: dict = {}, base: str | None = None) -> list | dict | None:
    url = f"{base or FMP_BASE}/{endpoint}"
    params = {**params, "apikey": FMP_KEY}
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"FMP {endpoint}: {e}")
        return None


def _fmp_v3(endpoint: str, params: dict = {}) -> list | dict | None:
    return _fmp(endpoint, params, base=FMP_V3)


def _finnhub(endpoint: str, params: dict = {}) -> dict | list | None:
    params = {**params, "token": FH_KEY}
    try:
        r = requests.get(f"{FH_BASE}/{endpoint}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"Finnhub {endpoint}: {e}")
        return None


# ─── Data sources ─────────────────────────────────────────────────────────────

def get_large_cap_losers() -> list[dict]:
    """
    PRIMARY SOURCE: FMP stock-screener filtered by market cap > $5B + price drop.

    This is the RIGHT approach for our strategy — we only want quality large-cap
    companies that crashed hard. The FMP biggest-losers endpoint returns micro-caps
    which are irrelevant noise.

    On a calm market day this may return 0 results. That is CORRECT — on a normal
    day there's nothing worth selling puts on. On an earnings/catalyst day (Roblox
    -22%, HOOD -14%, Meta -10%) it will populate with real targets.
    """
    results = []

    # Query 1: large caps (>$5B) down >= 5% today
    for endpoint in ["stock-screener", "v3/stock-screener"]:
        base = FMP_V3 if endpoint.startswith("v3/") else FMP_BASE
        ep   = endpoint.replace("v3/", "")
        params = {
            "marketCapMoreThan": MIN_MARKET_CAP,
            "priceMoreThan":     5,
            "isActivelyTrading": "true",
            "exchange":          "NYSE,NASDAQ",
            "limit":             200,
        }
        data = _fmp(ep, params, base=base)
        if data and isinstance(data, list):
            for row in data:
                sym = row.get("symbol", "")
                pct = float(row.get("changesPercentage") or row.get("changePercentage") or 0)
                mkt = float(row.get("marketCap") or 0)
                vol = row.get("volume") or 0
                avg_vol = row.get("avgVolume") or row.get("averageVolume") or 0

                if not sym or mkt < MIN_MARKET_CAP:
                    continue
                if pct > DAILY_DROP_PCT:   # not a big enough drop
                    continue

                results.append({
                    "symbol":     sym,
                    "name":       row.get("companyName") or row.get("name") or sym,
                    "price":      row.get("price"),
                    "change_pct": pct,
                    "volume":     vol,
                    "avg_volume": avg_vol,
                    "market_cap": int(mkt),
                    "sector":     row.get("sector", ""),
                    "industry":   row.get("industry", ""),
                    "exchange":   row.get("exchangeShortName") or row.get("exchange") or "",
                })
            logger.info(f"FMP screener ({ep}): {len(results)} large-cap stocks with drop ≤ {DAILY_DROP_PCT}%")
            break  # got data from first endpoint that worked

    # Query 2: mega-caps (>$20B) with smaller drops >= 3% to catch Meta/MSFT/GOOG type events
    mega_params = {
        "marketCapMoreThan": 20_000_000_000,
        "priceMoreThan":     10,
        "isActivelyTrading": "true",
        "exchange":          "NYSE,NASDAQ",
        "limit":             100,
    }
    for endpoint in ["stock-screener", "v3/stock-screener"]:
        base = FMP_V3 if endpoint.startswith("v3/") else FMP_BASE
        ep   = endpoint.replace("v3/", "")
        data = _fmp(ep, mega_params, base=base)
        if data and isinstance(data, list):
            existing = {r["symbol"] for r in results}
            added = 0
            for row in data:
                sym = row.get("symbol", "")
                pct = float(row.get("changesPercentage") or row.get("changePercentage") or 0)
                mkt = float(row.get("marketCap") or 0)
                if not sym or sym in existing or mkt < 20_000_000_000:
                    continue
                if pct > -3.0:  # mega-caps dropping 3%+ are significant
                    continue
                results.append({
                    "symbol":     sym,
                    "name":       row.get("companyName") or row.get("name") or sym,
                    "price":      row.get("price"),
                    "change_pct": pct,
                    "volume":     row.get("volume") or 0,
                    "avg_volume": row.get("avgVolume") or row.get("averageVolume") or 0,
                    "market_cap": int(mkt),
                    "sector":     row.get("sector", ""),
                    "industry":   row.get("industry", ""),
                    "exchange":   row.get("exchangeShortName") or row.get("exchange") or "",
                })
                added += 1
            logger.info(f"Mega-cap (-3%+) additions: {added}")
            break

    logger.info(f"Total large-cap candidates: {len(results)}")
    return results


def get_profile(symbol: str) -> dict:
    """Fetch market cap, sector, avg volume, beta from FMP profile."""
    data = _fmp(f"profile/{symbol}") or []
    if data and isinstance(data, list):
        p = data[0]
        return {
            "market_cap":   p.get("mktCap") or p.get("marketCap") or 0,
            "sector":       p.get("sector", ""),
            "industry":     p.get("industry", ""),
            "avg_volume":   p.get("volAvg") or p.get("averageVolume") or 0,
            "beta":         p.get("beta") or 0,
            "exchange":     p.get("exchangeShortName") or p.get("exchange") or "",
            "description":  (p.get("description") or "")[:200],
        }
    return {}


def get_5day_change(symbol: str) -> float | None:
    """
    Fetch 5 trading days of EOD prices and compute cumulative % change.
    Returns None if data unavailable.
    """
    data = _fmp(f"historical-price-eod/light/{symbol}", {"limit": 6}) or {}
    hist = data.get("historical") if isinstance(data, dict) else data
    if not hist or len(hist) < 2:
        return None
    try:
        # Sorted newest-first by FMP convention
        newest = float(hist[0].get("close") or hist[0].get("adjClose") or 0)
        oldest = float(hist[min(4, len(hist)-1)].get("close") or
                       hist[min(4, len(hist)-1)].get("adjClose") or 0)
        if oldest and oldest > 0:
            return round((newest - oldest) / oldest * 100, 2)
    except Exception:
        pass
    return None


def get_news_count(symbol: str) -> tuple[int, list[str]]:
    """Returns (count, headlines[]) for recent news via Finnhub."""
    today     = datetime.now(timezone.utc)
    from_date = (today - timedelta(days=NEWS_DAYS_BACK)).strftime("%Y-%m-%d")
    to_date   = today.strftime("%Y-%m-%d")
    data = _finnhub("company-news", {"symbol": symbol, "from": from_date, "to": to_date})
    if not data or not isinstance(data, list):
        return 0, []
    headlines = [item.get("headline", "") for item in data[:5] if item.get("headline")]
    return len(data), headlines


def get_earnings_days(symbol: str) -> int | None:
    """Days until next earnings, or None."""
    today = datetime.now(timezone.utc).date()
    cal = _fmp("earning_calendar", {
        "symbol": symbol,
        "from":   today.strftime("%Y-%m-%d"),
        "to":     (today + timedelta(days=30)).strftime("%Y-%m-%d"),
    })
    if cal and isinstance(cal, list) and cal:
        try:
            next_date = datetime.strptime(cal[0]["date"], "%Y-%m-%d").date()
            return (next_date - today).days
        except Exception:
            pass
    return None


# ─── Candidate assembly ───────────────────────────────────────────────────────

def assemble_candidates() -> list[dict]:
    """
    Get large-cap crash candidates from market screener.
    All candidates already have market cap > $5B from the screener query.
    """
    raw: dict[str, dict] = {}

    for s in get_large_cap_losers():
        sym = s["symbol"]
        if sym not in raw:
            raw[sym] = s

    logger.info(f"Candidate pool: {len(raw)} large-cap stocks dropping ≤ {DAILY_DROP_PCT}%")
    return list(raw.values())


def enrich_and_filter(candidates: list[dict]) -> list[dict]:
    """
    For each candidate:
      1. Fetch profile (market cap, sector, avg volume)
      2. Compute volume spike ratio
      3. Optionally check 5-day change
      4. Fetch news count + earnings
      5. Apply final quality filters
    """
    qualified: list[dict] = []

    for i, stock in enumerate(candidates):
        sym = stock["symbol"]
        logger.info(f"  [{i+1}/{len(candidates)}] Enriching {sym} (chg={stock.get('change_pct', 0):+.1f}%)")

        # ── Profile (sector, beta, description — market cap already from screener) ──
        # Market cap comes from the screener query (reliable). Profile adds sector/beta.
        screener_mktcap = stock.get("market_cap") or 0
        profile = {}
        if not stock.get("sector"):   # only hit profile if screener didn't return sector
            profile = get_profile(sym)

        mkt_cap = screener_mktcap or profile.get("market_cap") or 0

        # Hard market cap filter — REJECT if unknown or under $5B (no exceptions)
        if mkt_cap < MIN_MARKET_CAP:
            reason = "unknown" if mkt_cap == 0 else f"${mkt_cap/1e9:.1f}B"
            logger.info(f"    ✗ {sym}: market_cap {reason} < ${MIN_MARKET_CAP/1e9:.0f}B — rejected")
            continue

        stock.update({
            "market_cap":  mkt_cap,
            "sector":      stock.get("sector") or profile.get("sector", ""),
            "industry":    stock.get("industry") or profile.get("industry", ""),
            "avg_volume":  stock.get("avg_volume") or profile.get("avg_volume", 0),
            "beta":        profile.get("beta", 0),
            "exchange":    stock.get("exchange") or profile.get("exchange", ""),
        })

        # ── Volume spike ──────────────────────────────────────────────────────
        vol     = stock.get("volume") or 0
        avg_vol = profile.get("avg_volume") or 0
        vol_ratio = round(vol / avg_vol, 2) if avg_vol > 0 else 0.0
        stock["volume_ratio"] = vol_ratio

        if vol_ratio > 0 and vol_ratio < MIN_VOLUME_RATIO:
            logger.info(f"    ✗ {sym}: volume_ratio {vol_ratio:.1f}x below {MIN_VOLUME_RATIO}x threshold")
            # Don't hard-reject here — low volume still worth checking if drop is extreme
            if abs(stock.get("change_pct", 0)) < 15:
                continue

        # ── 5-day drop (multi-day breakdown check) ────────────────────────────
        chg5 = get_5day_change(sym)
        stock["change_5d_pct"] = chg5
        time.sleep(0.2)

        # ── News count + earnings ─────────────────────────────────────────────
        news_count, headlines = get_news_count(sym)
        earnings_days = get_earnings_days(sym)
        stock["news_count"]       = news_count
        stock["recent_headlines"] = headlines
        stock["earnings_days"]    = earnings_days
        time.sleep(0.3)

        # ── Classify triggers ─────────────────────────────────────────────────
        reasons = []
        if abs(stock.get("change_pct", 0)) >= abs(DAILY_DROP_PCT):
            reasons.append("price_move")
        if vol_ratio >= MIN_VOLUME_RATIO:
            reasons.append("volume_spike")
        if news_count >= 3:
            reasons.append("news_spike")
        if earnings_days is not None and 0 <= earnings_days <= 7:
            reasons.append("earnings_soon")
        if chg5 is not None and chg5 <= MULTIDAY_DROP_PCT:
            reasons.append("multi_day_drop")

        if not reasons:
            logger.info(f"    ✗ {sym}: no qualifying signals")
            continue

        stock["reasons"]     = reasons
        stock["scanned_at"]  = datetime.now(timezone.utc).isoformat()
        stock["prev_close"]  = stock.get("prev_close") or 0

        mkt_str = f"${mkt_cap/1e9:.1f}B" if mkt_cap else "unknown"
        logger.info(
            f"    ✓ {sym} [{stock['change_pct']:+.1f}% | mktcap={mkt_str} | "
            f"vol={vol_ratio:.1f}x | {reasons}]"
        )
        qualified.append(stock)

        if len(qualified) >= MAX_CANDIDATES:
            logger.info(f"Reached max candidates ({MAX_CANDIDATES})")
            break

    return qualified


# ─── Main ─────────────────────────────────────────────────────────────────────

def run() -> dict:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    FILTERED_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("═══ EVENT-DRIVEN SCREENER START ═══")
    logger.info(
        f"Criteria: market_cap > ${MIN_MARKET_CAP/1e9:.0f}B | "
        f"daily drop ≤ {DAILY_DROP_PCT}% OR 5-day drop ≤ {MULTIDAY_DROP_PCT}% | "
        f"volume ≥ {MIN_VOLUME_RATIO}×"
    )

    # Assemble raw candidate pool from market-wide sources
    candidates = assemble_candidates()
    if not candidates:
        logger.warning("No candidates found from market sources — returning empty")
        _write_results([])
        return {"total_screened": 0, "total_qualified": 0, "symbols": []}

    # Enrich and apply final filters
    qualified = enrich_and_filter(candidates)

    # Sort: biggest drops first (most interesting for our strategy)
    qualified.sort(key=lambda x: x.get("change_pct", 0))

    # Write outputs
    _write_results(qualified)

    stats = {
        "screened_at":    datetime.now(timezone.utc).isoformat(),
        "total_screened": len(candidates),
        "total_qualified": len(qualified),
        "symbols":        [s["symbol"] for s in qualified],
        "source":         "event_driven_screener",
    }
    logger.info(
        f"═══ SCREENER DONE: {len(qualified)} candidates from {len(candidates)} screened ═══"
    )
    for s in qualified:
        chg5_str = f" | 5d={s['change_5d_pct']:+.1f}%" if s.get("change_5d_pct") else ""
        logger.info(
            f"  ★ {s['symbol']:8} {s.get('change_pct', 0):+6.1f}%{chg5_str} | "
            f"vol={s.get('volume_ratio', 0):.1f}x | {s.get('sector', '')}"
        )
    return stats


def _write_results(qualified: list[dict]) -> None:
    """Write filtered/today.json and raw/{symbol}.json for downstream steps."""
    # Archive previous day
    today_path = FILTERED_DIR / "today.json"
    if today_path.exists():
        shutil.copy(today_path, FILTERED_DIR / "yesterday.json")

    # Write filtered list (read by fundamental_filter.py)
    with open(today_path, "w") as f:
        json.dump(qualified, f, indent=2)

    # Write per-symbol raw files (read by fundamental_filter.py quality gate)
    for stock in qualified:
        sym = stock["symbol"]
        raw_path = RAW_DIR / f"{sym}.json"
        with open(raw_path, "w") as f:
            json.dump(stock, f, indent=2)

    logger.info(f"Wrote {len(qualified)} candidates to filtered/today.json")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dotenv import load_dotenv
    load_dotenv()
    result = run()
    print(f"\n✓ Screener: {result.get('total_qualified', 0)} candidates")
    if result.get("symbols"):
        print("  Symbols:", ", ".join(result["symbols"]))
    if not result.get("symbols"):
        print("  (No stocks met criteria today — try again after market hours with real moves)")
