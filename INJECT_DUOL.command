#!/bin/bash
export PATH="/usr/local/bin:/usr/bin:/opt/homebrew/bin:$PATH"
cd "$(dirname "$0")"

if [ -f .env ]; then
  while IFS= read -r line; do
    [[ "$line" =~ ^#.*$ ]] && continue
    [[ -z "$line" ]] && continue
    export "$line"
  done < .env
fi

echo "=================================================="
echo "  Manual Candidate Injector — DUOL Earnings Drop"
echo "  $(date)"
echo "=================================================="
echo ""
echo "📊 Fetching DUOL data and injecting into pipeline..."
echo ""

python3 - <<'PYEOF'
import json, os, sys, requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

FMP_KEY = os.environ.get("FMP_API_KEY", "")
FMP_V3  = "https://financialmodelingprep.com/api/v3"
FMP_V4  = "https://financialmodelingprep.com/api/v4"
FH_KEY  = os.environ.get("FINNHUB_API_KEY", "")

FILTERED_DIR = Path("data/filtered")
RAW_DIR      = Path("data/raw")
FILTERED_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

def fmp(endpoint, params={}, base=FMP_V3):
    params = {**params, "apikey": FMP_KEY}
    try:
        r = requests.get(f"{base}/{endpoint}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  FMP error {endpoint}: {e}")
        return None

# ── Per-stock specs for manual injection ─────────────────────────────────────
# Add any stocks you want to force into the pipeline here
MANUAL_SPECS = [
    {
        "symbol":  "DUOL",
        "reason":  "earnings-driven 14% crash on May 4 — reported after close",
        # Fallback values used if API returns empty (e.g. after hours)
        "fallback_market_cap": 10_000_000_000,  # ~$10B after 14% drop
        "fallback_avg_volume": 2_500_000,        # ~2.5M typical daily vol
        "fallback_change_pct": -14.0,
        "fallback_price":      152.0,            # ~$152 after 14% drop from ~$177
        "fallback_prev_close": 177.0,            # approximate pre-earnings close
        "earnings_days":       0,                # just reported
    },
]

injected = []
for spec in MANUAL_SPECS:
    sym = spec["symbol"]
    print(f"\n  Fetching {sym}... ({spec['reason']})")

    # Try to get profile
    profile_data = fmp(f"profile/{sym}")
    profile = profile_data[0] if profile_data and isinstance(profile_data, list) else {}

    # Try to get real-time quote
    quote_data = fmp(f"quote/{sym}")
    quote = quote_data[0] if quote_data and isinstance(quote_data, list) else {}

    # Try to get recent historical prices (reliable even after-hours)
    hist_data = fmp(f"historical-price-eod/light/{sym}", {"limit": 10}, base=FMP_V4)
    if not isinstance(hist_data, list):
        hist_data = []

    # Build price/change from best available source
    price      = quote.get("price") or (hist_data[0]["close"] if hist_data else 0)
    prev_close = quote.get("previousClose") or (hist_data[1]["close"] if len(hist_data) >= 2 else 0)
    change_pct = quote.get("changesPercentage")
    if change_pct is None and price and prev_close:
        change_pct = round((price - prev_close) / prev_close * 100, 2)
    if not change_pct:
        change_pct = spec["fallback_change_pct"]

    # Price fallback when API returns 0 (e.g. after-hours, API issue)
    if not price and "fallback_price" in spec:
        price = spec["fallback_price"]
        print(f"    ⚠ price=0 from API — using fallback ${price:.2f}")
    if not prev_close and "fallback_prev_close" in spec:
        prev_close = spec["fallback_prev_close"]

    volume     = quote.get("volume") or 0
    avg_volume = quote.get("avgVolume") or profile.get("volAvg") or 0
    mkt_cap    = quote.get("marketCap") or profile.get("mktCap") or 0

    # Use fallbacks when API returns zeros (e.g. after-hours)
    if avg_volume == 0:
        avg_volume = spec["fallback_avg_volume"]
        print(f"    ⚠ avg_volume=0 from API — using fallback {avg_volume:,}")
    if mkt_cap == 0:
        mkt_cap = spec["fallback_market_cap"]
        print(f"    ⚠ market_cap=0 from API — using fallback ${mkt_cap/1e9:.1f}B")

    vol_ratio  = round(volume / avg_volume, 2) if avg_volume and volume else 0.0

    print(f"    Price: ${price:.2f}  Change: {change_pct:+.2f}%")
    print(f"    Market cap: ${mkt_cap/1e9:.1f}B")
    print(f"    Volume: {volume:,}  Avg: {avg_volume:,}  Ratio: {vol_ratio:.1f}x")

    # Multiday drop
    multiday_drop = None
    if len(hist_data) >= 6:
        try:
            multiday_drop = round((hist_data[0]["close"] - hist_data[5]["close"]) / hist_data[5]["close"] * 100, 2)
            print(f"    5-day drop: {multiday_drop:+.1f}%")
        except Exception:
            pass

    # News from Finnhub
    headlines = []
    news_count = 0
    if FH_KEY:
        try:
            today     = datetime.now(timezone.utc)
            from_date = (today - timedelta(days=3)).strftime("%Y-%m-%d")
            to_date   = today.strftime("%Y-%m-%d")
            r = requests.get(
                "https://finnhub.io/api/v1/company-news",
                params={"symbol": sym, "from": from_date, "to": to_date, "token": FH_KEY},
                timeout=15
            )
            if r.ok:
                news_data  = r.json()
                news_count = len(news_data)
                headlines  = [n.get("headline","") for n in news_data[:5] if n.get("headline")]
                print(f"    News: {news_count} articles")
        except Exception as e:
            print(f"    News error: {e}")

    candidate = {
        "symbol":           sym,
        "name":             profile.get("companyName") or sym,
        "price":            round(float(price), 4),
        "change_pct":       round(float(change_pct), 4),
        "volume_ratio":     vol_ratio,
        "market_cap":       mkt_cap,
        "news_count":       news_count,
        "earnings_days":    spec["earnings_days"],
        "recent_headlines": headlines,
        "multiday_drop":    multiday_drop,
        "reasons":          ["price_move", "earnings_event", "news_spike"],
        "screened_at":      datetime.now(timezone.utc).isoformat(),
    }

    raw_entry = {
        "symbol":       sym,
        "name":         profile.get("companyName") or sym,
        "price":        round(float(price), 4),
        "change":       round(float(price - prev_close), 4) if price and prev_close else 0,
        "change_pct":   round(float(change_pct), 4),
        "volume":       int(volume),
        "avg_volume":   int(avg_volume),  # guaranteed non-zero from fallback
        "market_cap":   mkt_cap,
        "pe":           profile.get("pe"),
        "52w_high":     quote.get("yearHigh"),
        "52w_low":      quote.get("yearLow"),
        "open":         quote.get("open"),
        "prev_close":   round(float(prev_close), 4) if prev_close else None,
        "day_high":     quote.get("dayHigh"),
        "day_low":      quote.get("dayLow"),
        "volume_ratio": vol_ratio,
        "news_count":   news_count,
        "earnings_days": spec["earnings_days"],
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    }
    with open(RAW_DIR / f"{sym}.json", "w") as f:
        json.dump(raw_entry, f, indent=2)

    injected.append(candidate)
    print(f"    ✓ {sym} prepared")

# Merge with existing today.json
today_path = FILTERED_DIR / "today.json"
if today_path.exists():
    import shutil
    shutil.copy(today_path, FILTERED_DIR / "yesterday.json")

existing = []
if today_path.exists():
    try:
        with open(today_path) as f:
            existing = json.load(f)
    except Exception:
        existing = []

injected_syms = {c["symbol"] for c in injected}
merged = injected + [c for c in existing if c["symbol"] not in injected_syms]

with open(today_path, "w") as f:
    json.dump(merged, f, indent=2)

print(f"\n✓ Wrote {len(merged)} candidates to {today_path}")
for c in merged:
    print(f"   {c['symbol']:6s}  {c['change_pct']:+.1f}%  mktcap ${c.get('market_cap',0)/1e9:.1f}B")
PYEOF

echo ""
echo "🔬 Running downstream analysis (fundamental_filter → analyze → report → push)..."
echo ""

python3 -c "
import sys, logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s — %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

from dotenv import load_dotenv
load_dotenv()

print('--- fundamental_filter ---')
try:
    from scripts.fundamental_filter import run as ff
    ff()
except Exception as e:
    print(f'fundamental_filter error: {e}')

print('--- analyze ---')
try:
    from scripts.analyze import run as analyze
    analyze()
except Exception as e:
    print(f'analyze error: {e}')

print('--- report ---')
try:
    from scripts.report import run as report
    report()
except Exception as e:
    print(f'report error: {e}')

print('--- push ---')
try:
    from scripts.push import run as push
    push()
except Exception as e:
    print(f'push error: {e}')

print('Done!')
"

echo ""
echo "=================================================="
echo "  ✅ Pipeline complete!"
echo "  Dashboard: https://huangrui199126.github.io/ai-finance"
echo "  (GitHub Pages may take 1-2 minutes to update)"
echo "=================================================="
read -p "Press Enter to close..."
