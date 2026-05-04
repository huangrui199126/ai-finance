from __future__ import annotations
"""
collect.py — Data Collector
Runs every hour. Fetches quotes for all S&P 500 stocks via yfinance
(free, no API key required). Saves raw JSON per symbol.

Strategy:
  1. yfinance.download() — one batch call for all symbols (~1-2s for 500)
  2. Enrich each symbol with name/market-cap from yfinance Ticker if needed
  3. Save each symbol to data/raw/{SYMBOL}.json
"""
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import CFG
from scripts.fetch_symbols import load_symbols

logger = logging.getLogger(__name__)

RAW_DIR = Path(CFG["paths"]["raw"])
FMP_V3  = CFG["apis"]["fmp"].get("base_url_v3", "https://financialmodelingprep.com/api/v3")
FMP_KEY = CFG["apis"]["fmp"]["key"]
BATCH   = CFG["apis"]["fmp"]["batch_size"]


# ─── yfinance quote fetcher ──────────────────────────────────────────────────

def fetch_all_quotes_yf(symbols: list[str]) -> dict[str, dict]:
    """
    Fetch all quotes in one yfinance batch call.
    Returns {symbol: quote_dict} with price, change, volume, etc.
    """
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed — run: pip install yfinance")
        return {}

    if not symbols:
        return {}

    now = datetime.now(timezone.utc).isoformat()
    result: dict[str, dict] = {}

    try:
        # download() fetches 1d history for all symbols in one HTTP call
        tickers = yf.Tickers(" ".join(symbols))

        # Use download for price/volume (fast batch)
        import yfinance as yf_mod
        df = yf_mod.download(
            tickers=symbols,
            period="2d",      # last 2 trading days
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
        )

        for sym in symbols:
            try:
                if len(symbols) == 1:
                    # Single symbol: df has simple columns
                    today = df.iloc[-1]
                    prev  = df.iloc[-2] if len(df) > 1 else today
                    price      = float(today["Close"])
                    prev_close = float(prev["Close"])
                    volume     = float(today["Volume"])
                    day_high   = float(today["High"])
                    day_low    = float(today["Low"])
                    open_price = float(today["Open"])
                else:
                    today = df[sym].iloc[-1]
                    prev  = df[sym].iloc[-2] if len(df[sym].dropna()) > 1 else today
                    price      = float(today["Close"])
                    prev_close = float(prev["Close"])
                    volume     = float(today["Volume"])
                    day_high   = float(today["High"])
                    day_low    = float(today["Low"])
                    open_price = float(today["Open"])

                change     = price - prev_close
                change_pct = (change / prev_close * 100) if prev_close else 0.0

                result[sym] = {
                    "symbol":     sym,
                    "name":       sym,       # enriched below if needed
                    "price":      round(price, 4),
                    "change":     round(change, 4),
                    "change_pct": round(change_pct, 4),
                    "volume":     int(volume),
                    "avg_volume": None,      # enriched below
                    "market_cap": None,      # enriched below
                    "pe":         None,
                    "52w_high":   None,
                    "52w_low":    None,
                    "open":       round(open_price, 4),
                    "prev_close": round(prev_close, 4),
                    "day_high":   round(day_high, 4),
                    "day_low":    round(day_low, 4),
                    "timestamp":  now,
                }
            except Exception as e:
                logger.debug(f"  Skip {sym}: {e}")

        logger.info(f"yfinance: got quotes for {len(result)}/{len(symbols)} symbols")

    except Exception as e:
        logger.error(f"yfinance download failed: {e}")
        return {}

    # ── Enrich with fast_info (avg volume, market cap, name) ─────────────────
    # batch Tickers().tickers dict gives fast_info per symbol
    enriched = 0
    for sym, q in result.items():
        try:
            t = tickers.tickers.get(sym)
            if t is None:
                continue
            fi = t.fast_info
            q["avg_volume"] = getattr(fi, "three_month_average_volume", None)
            q["market_cap"] = getattr(fi, "market_cap", None)
            q["52w_high"]   = getattr(fi, "year_high", None)
            q["52w_low"]    = getattr(fi, "year_low", None)
            # name from info (cached, fast)
            info = t.info
            if info:
                q["name"] = info.get("shortName") or info.get("longName") or sym
                q["pe"]   = info.get("trailingPE")
            enriched += 1
        except Exception:
            pass  # fast_info/info can fail silently

    logger.debug(f"Enriched {enriched}/{len(result)} symbols with fast_info")
    return result


def compute_volume_ratio(quote: dict) -> float:
    """Safe volume / avg_volume ratio."""
    vol = quote.get("volume") or 0
    avg = quote.get("avg_volume") or 0
    if avg and avg > 0:
        return round(vol / avg, 2)
    return 0.0


# ─── Main collection ──────────────────────────────────────────────────────────

def run() -> dict:
    """
    Main entry point — collect data for all S&P 500 symbols.
    Returns stats dict.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    symbols = load_symbols()
    total   = len(symbols)
    logger.info(f"Starting collection for {total} symbols via yfinance")

    all_quotes = fetch_all_quotes_yf(symbols)

    # Enrich and save each symbol
    saved = 0
    errors = 0
    for symbol, quote in all_quotes.items():
        try:
            quote["volume_ratio"] = compute_volume_ratio(quote)
            quote["news_count"]   = 0     # populated by scan.py
            quote["earnings_days"] = None  # populated by scan.py

            out_path = RAW_DIR / f"{symbol}.json"
            with open(out_path, "w") as f:
                json.dump(quote, f, indent=2)
            saved += 1
        except Exception as e:
            logger.error(f"Failed to save {symbol}: {e}")
            errors += 1

    # Save collection manifest
    manifest = {
        "collected_at":  datetime.now(timezone.utc).isoformat(),
        "total_symbols": total,
        "fetched":       len(all_quotes),
        "saved":         saved,
        "errors":        errors,
        "source":        "yfinance",
    }
    manifest_path = RAW_DIR / "_manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    logger.info(f"Collection done: {saved} saved, {errors} errors")
    return manifest


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    from dotenv import load_dotenv
    load_dotenv()
    stats = run()
    print(f"\n✓ Collected {stats['saved']}/{stats['total_symbols']} symbols")
