from __future__ import annotations
"""
themes.py — Market Theme Detection Engine

Automatically discovers trending market themes from news headlines.

Algorithm:
  1. Load all filtered stocks' recent headlines
  2. Match headlines against known theme keyword dictionaries
  3. Count keyword hits per theme over past 24h
  4. Cross-validate with price data: if related stocks moved → boost theme weight
  5. Output data/themes/today.json
"""
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import CFG

logger = logging.getLogger(__name__)

FILTERED_DIR = Path(CFG["paths"]["filtered"])
RAW_DIR      = Path(CFG["paths"]["raw"])
THEMES_DIR   = Path(CFG["paths"]["output"]).parent / "themes"

# ─── Theme keyword dictionary ─────────────────────────────────────────────────
# Each theme maps to: keywords list + related stock symbols
THEME_MAP = {
    "AI & Machine Learning": {
        "keywords": [
            "artificial intelligence", "ai model", "large language model", "llm",
            "generative ai", "chatgpt", "openai", "machine learning", "deep learning",
            "neural network", "gpu demand", "ai chip", "inference", "foundation model",
        ],
        "related": ["NVDA", "AMD", "MSFT", "GOOGL", "META", "AMZN", "SMCI", "INTC", "ARM", "AVGO"],
    },
    "Semiconductors": {
        "keywords": [
            "semiconductor", "chip", "fab", "foundry", "wafer", "transistor",
            "memory chip", "dram", "nand", "tsmc", "chip shortage", "chip supply",
            "advanced packaging", "hbm", "chipmaker",
        ],
        "related": ["NVDA", "AMD", "INTC", "QCOM", "MU", "AVGO", "TXN", "AMAT", "LRCX", "KLAC"],
    },
    "Electric Vehicles": {
        "keywords": [
            "electric vehicle", "ev", "battery", "charging", "range anxiety",
            "lithium", "cathode", "anode", "ev demand", "ev sales", "autopilot",
            "self-driving", "autonomous vehicle",
        ],
        "related": ["TSLA", "F", "GM", "RIVN", "LCID", "NIO", "LI", "XPEV", "CHPT", "BLNK"],
    },
    "Energy & Oil": {
        "keywords": [
            "crude oil", "natural gas", "opec", "energy", "refinery", "pipeline",
            "oil price", "brent", "wti", "shale", "lng", "petroleum", "fossil fuel",
        ],
        "related": ["XOM", "CVX", "COP", "SLB", "HAL", "MPC", "VLO", "PSX", "OXY", "PXD"],
    },
    "Biotech & Pharma": {
        "keywords": [
            "fda approval", "clinical trial", "drug", "biotech", "pharmaceutical",
            "oncology", "immunotherapy", "weight loss drug", "glp-1", "ozempic",
            "vaccine", "antibody", "gene therapy", "mrna",
        ],
        "related": ["LLY", "JNJ", "MRK", "ABBV", "AMGN", "GILD", "BIIB", "REGN", "VRTX", "BMY"],
    },
    "Cloud & SaaS": {
        "keywords": [
            "cloud computing", "saas", "aws", "azure", "google cloud", "cloud revenue",
            "enterprise software", "subscription revenue", "arr", "cloud migration",
        ],
        "related": ["AMZN", "MSFT", "GOOGL", "CRM", "NOW", "SNOW", "DDOG", "ZS", "CRWD", "NET"],
    },
    "Cybersecurity": {
        "keywords": [
            "cybersecurity", "cyberattack", "ransomware", "data breach", "hacking",
            "zero day", "endpoint security", "soc", "threat intelligence", "firewall",
        ],
        "related": ["CRWD", "PANW", "ZS", "FTNT", "OKTA", "S", "CYBR", "SAIL"],
    },
    "Space & Defense": {
        "keywords": [
            "space", "rocket", "satellite", "spacex", "starlink", "defense",
            "military", "weapons", "drone", "geopolitical", "pentagon",
        ],
        "related": ["LMT", "RTX", "NOC", "GD", "BA", "SPCE", "RKLB", "ASTS"],
    },
    "Consumer & Retail": {
        "keywords": [
            "consumer spending", "retail sales", "e-commerce", "holiday season",
            "inflation", "consumer confidence", "discretionary spending",
        ],
        "related": ["AMZN", "WMT", "COST", "TGT", "HD", "NKE", "SBUX", "MCD"],
    },
    "Financials & Banking": {
        "keywords": [
            "interest rate", "fed", "federal reserve", "rate hike", "rate cut",
            "banking", "credit", "loan", "yield curve", "treasury", "inflation",
        ],
        "related": ["JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "V", "MA"],
    },
    "Crypto & Fintech": {
        "keywords": [
            "bitcoin", "ethereum", "crypto", "blockchain", "defi", "stablecoin",
            "digital asset", "web3", "nft", "coinbase", "fintech",
        ],
        "related": ["COIN", "MSTR", "PYPL", "SQ", "HOOD"],
    },
}


# ─── Core detection logic ─────────────────────────────────────────────────────

def score_headline(headline: str) -> dict[str, float]:
    """Return {theme_name: score} for a single headline."""
    lower = headline.lower()
    scores = {}
    for theme, cfg in THEME_MAP.items():
        hits = sum(1 for kw in cfg["keywords"] if kw in lower)
        if hits > 0:
            scores[theme] = hits
    return scores


def load_all_headlines() -> list[tuple[str, str]]:
    """Returns [(symbol, headline), ...] from today's filtered stocks."""
    today_path = FILTERED_DIR / "today.json"
    if not today_path.exists():
        return []

    with open(today_path) as f:
        stocks = json.load(f)

    pairs = []
    for stock in stocks:
        sym = stock.get("symbol", "")
        for h in stock.get("recent_headlines", []):
            if h:
                pairs.append((sym, h))
    return pairs


def load_price_changes() -> dict[str, float]:
    """Returns {symbol: change_pct} from raw data."""
    changes = {}
    for path in RAW_DIR.glob("*.json"):
        if path.name == "_manifest.json":
            continue
        try:
            with open(path) as f:
                q = json.load(f)
            sym = q.get("symbol")
            pct = q.get("change_pct")
            if sym and pct is not None:
                changes[sym] = float(pct)
        except Exception:
            pass
    return changes


def classify_strength(score: float) -> str:
    if score >= 8:
        return "high"
    if score >= 4:
        return "medium"
    return "low"


def run() -> list[dict]:
    THEMES_DIR.mkdir(parents=True, exist_ok=True)

    headlines = load_all_headlines()
    price_changes = load_price_changes()

    if not headlines:
        logger.warning("No headlines found — run collect + scan first")
        return []

    logger.info(f"Analyzing {len(headlines)} headlines for theme detection")

    # Aggregate keyword hits per theme
    theme_scores: dict[str, float]           = defaultdict(float)
    theme_symbols: dict[str, set[str]]       = defaultdict(set)
    theme_headlines: dict[str, list[str]]    = defaultdict(list)

    for symbol, headline in headlines:
        scores = score_headline(headline)
        for theme, score in scores.items():
            theme_scores[theme] += score
            theme_symbols[theme].add(symbol)
            if len(theme_headlines[theme]) < 5:
                theme_headlines[theme].append(headline)

    # Price-based signal boost:
    # If multiple related stocks for a theme are moving → multiply score
    for theme, cfg in THEME_MAP.items():
        related = cfg["related"]
        movers = [s for s in related if abs(price_changes.get(s, 0)) >= 2.0]
        if len(movers) >= 2:
            boost = 1.0 + (0.5 * len(movers))
            theme_scores[theme] = theme_scores.get(theme, 0) * boost
            for s in movers:
                theme_symbols[theme].add(s)
            logger.debug(f"  {theme}: boosted by {len(movers)} movers → {theme_scores[theme]:.1f}")

    # Build output, filter themes with score > 0
    results = []
    for theme, score in sorted(theme_scores.items(), key=lambda x: x[1], reverse=True):
        if score == 0:
            continue

        related_symbols = sorted(theme_symbols[theme])
        # Only include symbols with meaningful price change or in filtered list
        active_symbols = [
            s for s in THEME_MAP[theme]["related"]
            if s in theme_symbols[theme] or abs(price_changes.get(s, 0)) >= 1.5
        ]

        results.append({
            "theme":            theme,
            "score":            round(score, 2),
            "strength":         classify_strength(score),
            "related_symbols":  active_symbols[:8],
            "sample_headlines": theme_headlines.get(theme, [])[:3],
            "movers":           [
                {"symbol": s, "change_pct": round(price_changes.get(s, 0), 2)}
                for s in active_symbols
                if abs(price_changes.get(s, 0)) >= 1.0
            ],
        })

    # Save output
    output = {
        "date":       datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "themes":     results,
    }

    out_path = THEMES_DIR / "today.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    # Also save to dated archive
    dated = THEMES_DIR / f"{output['date']}.json"
    with open(dated, "w") as f:
        json.dump(output, f, indent=2)

    logger.info(f"Theme detection done: {len(results)} themes identified")
    for r in results[:5]:
        logger.info(f"  [{r['strength'].upper():6}] {r['theme']} (score={r['score']})")

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dotenv import load_dotenv
    load_dotenv()
    themes = run()
    print(f"\n✓ {len(themes)} themes detected:")
    for t in themes:
        print(f"  [{t['strength'].upper():6}] {t['theme']} — {t['related_symbols']}")
