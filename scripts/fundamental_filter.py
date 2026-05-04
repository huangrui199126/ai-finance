from __future__ import annotations
"""
fundamental_filter.py — Quality & Liquidity Gate

Filters out stocks that pass anomaly detection but are:
- Too illiquid (tiny market cap, ultra-low volume)
- Financially distressed in a way that distorts signals
- Likely to be noise (penny stocks, shell companies)

ALSO: runs the scoring model to rank the surviving candidates.

Score formula:
  score = (abs_price_change * 0.4) + (news_score * 0.3) + (earnings_score * 0.3)

Where:
  news_score     = min(news_count, 10) / 10 * 10   (0–10)
  earnings_score = 10 if earnings within 3 days,
                   7  if within 7 days,
                   0  otherwise
"""
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import CFG

logger = logging.getLogger(__name__)

FILTERED_DIR  = Path(CFG["paths"]["filtered"])
ANOMALY_DIR   = FILTERED_DIR  # scan.py writes today.json here
RAW_DIR       = Path(CFG["paths"]["raw"])
FMP_BASE      = CFG["apis"]["fmp"]["base_url"]
FMP_KEY       = CFG["apis"]["fmp"]["key"]

# Minimum quality thresholds
MIN_MARKET_CAP  = 50_000_000   # $50M — avoid micro-caps
MIN_PRICE       = 1.0           # $1 — avoid penny stocks
MIN_AVG_VOLUME  = 100_000       # 100k daily avg volume

MAX_OUTPUT      = CFG["scanner"]["max_output"]  # top N to pass to LLM


# ─── Fundamentals fetch (lightweight) ────────────────────────────────────────

def _fmp_get(endpoint: str, params: dict = {}) -> dict | list | None:
    params["apikey"] = FMP_KEY
    try:
        r = requests.get(f"{FMP_BASE}/{endpoint}", params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"FMP {endpoint}: {e}")
        return None


def passes_quality_gate(quote: dict) -> tuple[bool, str]:
    """
    Returns (passes: bool, reason: str).
    Uses data already in raw quote to avoid extra API calls.
    """
    price     = quote.get("price") or 0
    mkt_cap   = quote.get("market_cap") or 0
    avg_vol   = quote.get("avg_volume") or 0

    if price < MIN_PRICE:
        return False, f"price ${price:.2f} < ${MIN_PRICE}"
    if mkt_cap and mkt_cap < MIN_MARKET_CAP:
        return False, f"market_cap ${mkt_cap/1e6:.0f}M < ${MIN_MARKET_CAP/1e6:.0f}M"
    if avg_vol and avg_vol < MIN_AVG_VOLUME:
        return False, f"avg_volume {avg_vol:,} < {MIN_AVG_VOLUME:,}"

    return True, "ok"


# ─── Scoring ──────────────────────────────────────────────────────────────────

def compute_score(stock: dict) -> float:
    """
    Composite score [0–10]:
      price_component  = abs(change_pct) capped at 20% → normalized 0–10
      news_component   = news_count capped at 10 → 0–10
      earnings_component = 10 / 7 / 0
    """
    # Price component: abs change%, capped at 20 → score 0–10
    abs_chg = min(abs(stock.get("change_pct") or 0), 20.0)
    price_score = (abs_chg / 20.0) * 10.0

    # News component
    news_score = min(stock.get("news_count") or 0, 10) / 10.0 * 10.0

    # Earnings component
    ed = stock.get("earnings_days")
    if ed is not None:
        if 0 <= ed <= 3:
            earnings_score = 10.0
        elif ed <= 7:
            earnings_score = 7.0
        elif ed <= 14:
            earnings_score = 4.0
        else:
            earnings_score = 1.0
    else:
        earnings_score = 0.0

    # Volume bonus: if volume_ratio > 3x avg, small bonus
    vol_ratio = stock.get("volume_ratio") or 0
    vol_bonus = min(vol_ratio / 3.0, 1.0)  # 0–1

    score = (price_score * 0.4) + (news_score * 0.3) + (earnings_score * 0.3) + vol_bonus
    return round(score, 2)


# ─── Main ─────────────────────────────────────────────────────────────────────

def run() -> list[dict]:
    today_path = ANOMALY_DIR / "today.json"
    if not today_path.exists():
        logger.error("No anomaly data found — run scan.py first")
        return []

    with open(today_path) as f:
        candidates = json.load(f)

    logger.info(f"Fundamental filter: evaluating {len(candidates)} anomaly candidates")

    # Load raw quote data for quality checks
    raw_cache: dict[str, dict] = {}
    for path in RAW_DIR.glob("*.json"):
        if path.name == "_manifest.json":
            continue
        try:
            with open(path) as f:
                q = json.load(f)
            raw_cache[q.get("symbol", "")] = q
        except Exception:
            pass

    passed: list[dict] = []
    rejected = 0

    for stock in candidates:
        sym   = stock.get("symbol", "")
        quote = raw_cache.get(sym, stock)

        ok, reason = passes_quality_gate(quote)
        if not ok:
            logger.debug(f"  ✗ {sym}: {reason}")
            rejected += 1
            continue

        # Score the stock
        score = compute_score(stock)
        stock["score"] = score

        # Tag with theme if present (populated later by themes.py, best effort)
        stock["theme"] = None

        passed.append(stock)

    # Sort by score descending, keep top N
    passed.sort(key=lambda x: x["score"], reverse=True)
    selected = passed[:MAX_OUTPUT]

    # Save scored/filtered list
    out_path = FILTERED_DIR / "scored.json"
    with open(out_path, "w") as f:
        json.dump(selected, f, indent=2)

    logger.info(
        f"Fundamental filter: {len(candidates)} → {len(passed)} passed → "
        f"{len(selected)} selected (rejected {rejected} low-quality)"
    )
    for s in selected[:10]:
        logger.info(f"  score={s['score']:5.2f}  {s['symbol']:8}  {s.get('reasons', [])}")

    return selected


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dotenv import load_dotenv
    load_dotenv()
    stocks = run()
    print(f"\n✓ {len(stocks)} stocks selected after fundamental filter + scoring")
    for s in stocks:
        print(f"  {s['symbol']:8} score={s['score']:5.2f}  {s.get('reasons', [])}")
