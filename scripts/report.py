from __future__ import annotations
"""
report.py — Daily Report Generator

Aggregates all individual analyses + theme data into:
  docs/data/YYYY-MM-DD.json   (dated archive)
  docs/data/latest.json       (always up-to-date, frontend reads this)
  docs/data/index.json        (list of all available dates)
"""
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import CFG

logger = logging.getLogger(__name__)

ANALYZED_DIR = Path(CFG["paths"]["analyzed"])
OUTPUT_DIR   = Path(CFG["paths"]["output"])
THEMES_DIR   = OUTPUT_DIR.parent / "themes"
FILTERED_DIR = Path(CFG["paths"]["filtered"])


def load_themes() -> list[dict]:
    path = THEMES_DIR / "today.json"
    if not path.exists():
        return []
    with open(path) as f:
        data = json.load(f)
    return data.get("themes", [])


def load_analyses() -> list[dict]:
    results = []
    for path in ANALYZED_DIR.glob("*.json"):
        try:
            with open(path) as f:
                results.append(json.load(f))
        except Exception as e:
            logger.debug(f"Could not load {path}: {e}")
    return results


def load_scan_stats() -> dict:
    stats_path = FILTERED_DIR / "stats.json"
    if not stats_path.exists():
        return {}
    with open(stats_path) as f:
        return json.load(f)


def sentiment_counts(analyses: list[dict]) -> dict:
    counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    for a in analyses:
        s = a.get("sentiment", "neutral").lower()
        counts[s] = counts.get(s, 0) + 1
    return counts


def market_mood(counts: dict) -> str:
    b, be, n = counts.get("bullish", 0), counts.get("bearish", 0), counts.get("neutral", 0)
    total = b + be + n
    if total == 0:
        return "neutral"
    bull_pct = b / total
    bear_pct = be / total
    if bull_pct >= 0.5:
        return "broadly bullish"
    if bear_pct >= 0.5:
        return "broadly bearish"
    if bull_pct > bear_pct:
        return "cautiously bullish"
    if bear_pct > bull_pct:
        return "cautiously bearish"
    return "mixed"


def tag_theme_to_stocks(analyses: list[dict], themes: list[dict]) -> list[dict]:
    """Add theme label to each stock if it's in a detected theme."""
    symbol_theme: dict[str, str] = {}
    for t in themes:
        theme_name = t.get("theme") or t.get("name", "")
        for sym in t.get("related_symbols", []) + t.get("symbols", []):
            if sym not in symbol_theme:
                symbol_theme[sym] = theme_name

    for a in analyses:
        if not a.get("theme"):
            a["theme"] = symbol_theme.get(a["symbol"], "")
    return analyses


def run() -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    today    = datetime.now(timezone.utc)
    date_str = today.strftime("%Y-%m-%d")

    analyses = load_analyses()
    themes   = load_themes()
    stats    = load_scan_stats()

    if not analyses:
        logger.warning("No analysis data found — run analyze.py first")

    # Cross-reference themes with stock analyses
    analyses = tag_theme_to_stocks(analyses, themes)

    # Sort by score descending
    analyses.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Sentiment breakdown
    counts = sentiment_counts(analyses)
    mood   = market_mood(counts)

    top_n  = CFG["report"]["top_n"]
    top    = analyses[:top_n]

    # Dominant themes (top 3)
    top_themes = [
        {
            "name":     t.get("theme") or t.get("name", ""),
            "strength": t.get("strength", ""),
            "symbols":  (t.get("related_symbols") or t.get("symbols", []))[:5],
        }
        for t in themes[:3]
    ]

    report = {
        "date":             date_str,
        "generated_at":     today.isoformat(),
        "market_mood":      mood,
        "sentiment_breakdown": counts,
        "total_scanned":    stats.get("total_scanned", 0),
        "total_analyzed":   len(analyses),
        "llm_analyzed":     sum(1 for a in analyses if a.get("llm_used")),
        "top_themes":       top_themes,
        "top_stocks":       top,
        "all_stocks":       analyses,
    }

    # Save dated file
    dated_path = OUTPUT_DIR / f"{date_str}.json"
    with open(dated_path, "w") as f:
        json.dump(report, f, indent=2)

    # Save latest.json (always overwrite)
    latest_path = OUTPUT_DIR / "latest.json"
    with open(latest_path, "w") as f:
        json.dump(report, f, indent=2)

    # Update index.json
    index_path = OUTPUT_DIR / "index.json"
    existing_dates = []
    if index_path.exists():
        with open(index_path) as f:
            existing_dates = json.load(f).get("dates", [])

    if date_str not in existing_dates:
        existing_dates.insert(0, date_str)
    existing_dates = sorted(set(existing_dates), reverse=True)[:90]  # keep 90 days

    with open(index_path, "w") as f:
        json.dump({"dates": existing_dates, "updated_at": today.isoformat()}, f, indent=2)

    logger.info(
        f"Report generated: {len(analyses)} stocks, mood={mood}, "
        f"themes={[t['name'] for t in top_themes]}"
    )
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dotenv import load_dotenv
    load_dotenv()
    r = run()
    print(f"\n✓ Report for {r['date']}")
    print(f"  Market mood:  {r['market_mood']}")
    print(f"  Scanned:      {r['total_scanned']}")
    print(f"  Analyzed:     {r['total_analyzed']} ({r['llm_analyzed']} via LLM)")
    print(f"  Top themes:   {[t['name'] for t in r['top_themes']]}")
    print(f"  Saved to:     {CFG['paths']['output']}/")
