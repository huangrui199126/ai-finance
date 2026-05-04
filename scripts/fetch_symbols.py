from __future__ import annotations
"""
fetch_symbols.py — Download & cache US stock universe.

Supports two modes:
  --universe sp500     → ~500 symbols (default, safest for free API tiers)
  --universe us        → all US-listed stocks (~8000+)

Run once (or refresh monthly):
    python scripts/fetch_symbols.py
    python scripts/fetch_symbols.py --universe us
"""
import argparse
import json
import logging
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import CFG

logger = logging.getLogger(__name__)

FMP_STABLE = CFG["apis"]["fmp"]["base_url"]       # https://financialmodelingprep.com/stable
FMP_V3     = CFG["apis"]["fmp"].get("base_url_v3", "https://financialmodelingprep.com/api/v3")
FMP_KEY    = CFG["apis"]["fmp"]["key"]
# Keep backward compat
FMP_BASE   = FMP_STABLE


# ─── Fetch methods ────────────────────────────────────────────────────────────

def fetch_sp500() -> list[str]:
    """Fetch current S&P 500 constituents from FMP stable API."""
    try:
        # Stable API: /stable/sp500-constituent
        r = requests.get(f"{FMP_STABLE}/sp500-constituent", params={"apikey": FMP_KEY}, timeout=30)
        r.raise_for_status()
        data = r.json()
        symbols = sorted({item["symbol"] for item in data if item.get("symbol")})
        if symbols:
            logger.info(f"Fetched {len(symbols)} S&P 500 symbols")
            return symbols
    except Exception as e:
        logger.error(f"S&P 500 stable fetch failed: {e}")

    # Try v3 fallback
    try:
        r = requests.get(f"{FMP_V3}/sp500_constituent", params={"apikey": FMP_KEY}, timeout=30)
        r.raise_for_status()
        symbols = sorted({item["symbol"] for item in r.json() if item.get("symbol")})
        if symbols:
            logger.info(f"Fetched {len(symbols)} S&P 500 symbols (v3)")
            return symbols
    except Exception as e:
        logger.error(f"S&P 500 v3 fetch failed: {e}")

    return _fallback_sp500()


def fetch_all_us_stocks() -> list[str]:
    """
    Fetch all tradable US stocks from FMP.
    Filters to: NYSE + NASDAQ, price > $1, exclude warrants/notes/ETFs if possible.
    """
    try:
        r = requests.get(f"{FMP_STABLE}/stock-list", params={"apikey": FMP_KEY}, timeout=60)
        if r.status_code == 403:
            r = requests.get(f"{FMP_V3}/stock/list", params={"apikey": FMP_KEY}, timeout=60)
        r.raise_for_status()
        all_stocks = r.json()
        logger.info(f"Raw stock list: {len(all_stocks)} entries")

        filtered = []
        for s in all_stocks:
            sym  = s.get("symbol", "")
            exch = (s.get("exchangeShortName") or "").upper()
            typ  = (s.get("type") or "").lower()
            price = s.get("price") or 0

            # Keep only NYSE / NASDAQ listed common stocks
            if exch not in ("NYSE", "NASDAQ", "AMEX"):
                continue
            # Skip ETFs, ETNs, warrants, preferred shares
            if typ in ("etf", "etn", "fund"):
                continue
            # Skip OTC / penny stocks under $0.50
            if price < 0.5:
                continue
            # Skip symbols with special chars (warrants, rights, units)
            if any(c in sym for c in ["+", ".", "^", "~", "-", "/"]):
                continue

            filtered.append(sym)

        symbols = sorted(set(filtered))
        logger.info(f"Filtered to {len(symbols)} tradable US stocks")
        return symbols
    except Exception as e:
        logger.error(f"Full US stock list fetch failed: {e}")
        return fetch_sp500()  # fallback


# ─── Persistence ─────────────────────────────────────────────────────────────

def save_symbols(symbols: list[str], universe: str = "sp500") -> Path:
    out = Path(CFG["paths"]["symbols"])
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "universe":  universe,
        "count":     len(symbols),
        "symbols":   symbols,
    }
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    logger.info(f"Saved {len(symbols)} symbols → {out}")
    return out


def load_symbols() -> list[str]:
    """Load from cache, fetch S&P 500 if missing."""
    path = Path(CFG["paths"]["symbols"])
    if path.exists():
        with open(path) as f:
            data = json.load(f)
        # Handle both old format (list) and new format (dict)
        if isinstance(data, list):
            return data
        return data.get("symbols", [])

    logger.info("symbols.json not found — fetching S&P 500 as default")
    symbols = fetch_sp500()
    save_symbols(symbols, universe="sp500")
    return symbols


# ─── Fallback list ────────────────────────────────────────────────────────────

def _fallback_sp500() -> list[str]:
    """Hardcoded top-50 for testing when API unavailable."""
    return [
        "AAPL","MSFT","AMZN","NVDA","GOOGL","META","TSLA","BRK.B","UNH","XOM",
        "JPM","JNJ","V","PG","MA","HD","CVX","MRK","LLY","ABBV","PEP","KO",
        "AVGO","COST","TMO","CSCO","ABT","ACN","MCD","DHR","NEE","WMT","ADBE",
        "TXN","CRM","VZ","PM","RTX","AMGN","QCOM","T","HON","LOW","UNP","INTU",
        "LIN","MS","GS","SPGI","CAT",
    ]


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", choices=["sp500", "us"], default="sp500")
    args = parser.parse_args()

    if args.universe == "us":
        symbols = fetch_all_us_stocks()
    else:
        symbols = fetch_sp500()

    save_symbols(symbols, universe=args.universe)
    print(f"✓ {len(symbols)} symbols saved  (universe={args.universe})")
