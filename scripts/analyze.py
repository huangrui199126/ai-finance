from __future__ import annotations
"""
analyze.py — LLM Analyzer

Runs ONLY on scored/filtered stocks (max 30/day).
Fetches full news + earnings context, then calls Claude.

Strict rules:
  - NO trading advice
  - NO buy/sell recommendations
  - Output: drivers, risks, sentiment, summary
"""
import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import CFG

logger = logging.getLogger(__name__)

FILTERED_DIR  = Path(CFG["paths"]["filtered"])
ANALYZED_DIR  = Path(CFG["paths"]["analyzed"])
THEMES_DIR    = Path(CFG["paths"]["output"]).parent / "themes"
FMP_BASE      = CFG["apis"]["fmp"]["base_url"]
FMP_KEY       = CFG["apis"]["fmp"]["key"]
FH_BASE       = CFG["apis"]["finnhub"]["base_url"]
FH_KEY        = CFG["apis"]["finnhub"]["key"]
LLM_CFG       = CFG["apis"]["llm"]
MAX_CALLS     = LLM_CFG["max_daily_calls"]
LLM_ENABLED   = LLM_CFG["enabled"]


# ─── API helpers ──────────────────────────────────────────────────────────────

def _fmp(endpoint: str, params: dict = {}) -> dict | list | None:
    params["apikey"] = FMP_KEY
    try:
        r = requests.get(f"{FMP_BASE}/{endpoint}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"FMP {endpoint}: {e}")
        return None


def _finnhub(endpoint: str, params: dict = {}) -> dict | list | None:
    params["token"] = FH_KEY
    try:
        r = requests.get(f"{FH_BASE}/{endpoint}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"Finnhub {endpoint}: {e}")
        return None


# ─── Context builders ─────────────────────────────────────────────────────────

def get_full_news(symbol: str) -> list[str]:
    today     = datetime.now(timezone.utc)
    from_date = (today - timedelta(days=3)).strftime("%Y-%m-%d")
    to_date   = today.strftime("%Y-%m-%d")
    data = _finnhub("company-news", {"symbol": symbol, "from": from_date, "to": to_date})
    if not data:
        return []
    return [f"• {item.get('headline', '')}" for item in data[:5] if item.get("headline")]


def get_earnings_context(symbol: str) -> str:
    today = datetime.now(timezone.utc).date()
    cal   = _fmp("earning_calendar", {
        "symbol": symbol,
        "from": today.strftime("%Y-%m-%d"),
        "to": (today + timedelta(days=30)).strftime("%Y-%m-%d"),
    })
    if cal and isinstance(cal, list) and cal:
        try:
            next_date = cal[0].get("date", "")
            eps_est   = cal[0].get("epsEstimated", "N/A")
            return f"Next earnings: {next_date} | EPS estimate: {eps_est}"
        except Exception:
            pass

    # Try recent actuals
    recent = _fmp(f"earnings-surprises/{symbol}")
    if recent and isinstance(recent, list) and recent:
        last = recent[0]
        return (
            f"Last reported: {last.get('date', 'N/A')} | "
            f"EPS actual: {last.get('actualEarningResult', 'N/A')} | "
            f"EPS estimate: {last.get('estimatedEarning', 'N/A')}"
        )
    return "No earnings data available"


def get_theme_for_symbol(symbol: str) -> str | None:
    """Check if symbol appears in today's theme detection output."""
    themes_path = THEMES_DIR / "today.json"
    if not themes_path.exists():
        return None
    try:
        with open(themes_path) as f:
            data = json.load(f)
        for t in data.get("themes", []):
            if symbol in t.get("related_symbols", []):
                return t.get("theme") or t.get("name")
    except Exception:
        pass
    return None


# ─── Prompt builder ───────────────────────────────────────────────────────────

def build_prompt(stock: dict, news: list[str], earnings: str, theme: str | None) -> str:
    symbol   = stock["symbol"]
    name     = stock.get("name", symbol)
    price    = stock.get("price", "N/A")
    chg_pct  = stock.get("change_pct", 0)
    vol_rat  = stock.get("volume_ratio", 0)
    reasons  = ", ".join(stock.get("reasons", []))
    news_txt = "\n".join(news) if news else "No recent news available"
    theme_txt = f"Sector/Theme: {theme}" if theme else "Sector: Not identified"

    return f"""You are a financial data analyst providing objective market intelligence. Your role is information synthesis only — no trading recommendations.

STOCK DATA:
Company: {name} ({symbol})
Current Price: ${price}
Price Change: {chg_pct:+.2f}% today
Volume Ratio: {vol_rat:.1f}x average
{theme_txt}
Signal Triggers: {reasons}
Earnings Context: {earnings}

RECENT NEWS:
{news_txt}

ANALYSIS TASK:
Based on the data above, provide a structured analysis covering:

1. Key DRIVERS — What specific factors appear to be driving today's price/volume movement?
2. Key RISKS — What risks or uncertainties should be monitored?
3. Market SENTIMENT — Based on price action and news: bullish, bearish, or neutral?
4. One-sentence SUMMARY — Objective description of the situation (≤ 30 words)

IMPORTANT RULES:
- Do NOT recommend buying or selling this stock
- Do NOT predict future price direction
- Do NOT use language suggesting investment decisions
- Focus on "what is happening" and "what to watch", not "what to do"

Respond ONLY in this exact JSON format (no markdown):
{{
  "symbol": "{symbol}",
  "theme": "{theme or ''}",
  "drivers": ["driver 1", "driver 2", "driver 3"],
  "risks": ["risk 1", "risk 2"],
  "sentiment": "bullish|bearish|neutral",
  "summary": "one sentence summary here"
}}"""


# ─── LLM call ────────────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    """Remove markdown code fences if the model wraps its JSON in them."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # drop opening fence (```json or ```) and closing fence (```)
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def call_openrouter(prompt: str) -> dict | None:
    """
    Call OpenRouter (OpenAI-compatible API) using plain requests.
    Works with any model available on OpenRouter, including free NVIDIA models.
    """
    key      = LLM_CFG["key"]
    model    = LLM_CFG["model"]
    base_url = LLM_CFG.get("base_url", "https://openrouter.ai/api/v1")
    site_url = LLM_CFG.get("site_url", "")
    app_name = LLM_CFG.get("app_name", "AI Stock Intelligence")

    headers = {
        "Authorization":  f"Bearer {key}",
        "Content-Type":   "application/json",
        "HTTP-Referer":   site_url,   # shown in OpenRouter dashboard
        "X-Title":        app_name,
    }
    payload = {
        "model": model,
        "max_tokens": LLM_CFG.get("max_tokens", 512),
        "temperature": 0.2,           # low temp → consistent JSON
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        r = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"]
        return json.loads(_strip_fences(raw))
    except json.JSONDecodeError as e:
        logger.error(f"LLM JSON parse error: {e} | raw={raw[:200]}")
        return None
    except Exception as e:
        logger.error(f"OpenRouter call failed: {e}")
        return None


def call_anthropic(prompt: str) -> dict | None:
    """Fallback: call Anthropic Claude directly (if anthropic SDK installed)."""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=LLM_CFG["key"])
        msg = client.messages.create(
            model=LLM_CFG["model"],
            max_tokens=LLM_CFG.get("max_tokens", 512),
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text
        return json.loads(_strip_fences(raw))
    except json.JSONDecodeError as e:
        logger.error(f"LLM JSON parse error: {e}")
        return None
    except Exception as e:
        logger.error(f"Anthropic call failed: {e}")
        return None


def call_llm(prompt: str) -> dict | None:
    """Route to the configured LLM provider."""
    provider = LLM_CFG.get("provider", "openrouter").lower()
    if provider == "anthropic":
        return call_anthropic(prompt)
    else:
        # openrouter or openai — both use the same OpenAI-compatible format
        return call_openrouter(prompt)


def fallback_analysis(stock: dict, theme: str | None) -> dict:
    """No-LLM fallback — rule-based sentiment from price + news."""
    chg = stock.get("change_pct") or 0
    if chg > 3:
        sentiment = "bullish"
    elif chg < -3:
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    reasons = stock.get("reasons", [])
    drivers = []
    if "price_move" in reasons:
        drivers.append(f"Significant price movement of {chg:+.1f}%")
    if "volume_spike" in reasons:
        drivers.append(f"Volume {stock.get('volume_ratio', 0):.1f}x above average")
    if "earnings_soon" in reasons:
        drivers.append(f"Earnings expected in {stock.get('earnings_days')} days")
    if "news_spike" in reasons:
        drivers.append(f"{stock.get('news_count', 0)} news items in past 48h")

    return {
        "symbol":    stock["symbol"],
        "theme":     theme or "",
        "drivers":   drivers or ["Unusual market activity detected"],
        "risks":     ["Limited data available for detailed analysis"],
        "sentiment": sentiment,
        "summary":   (
            f"{stock['symbol']} shows {sentiment} signals with "
            f"{chg:+.1f}% price change and {stock.get('news_count', 0)} recent news items."
        ),
        "llm_used":  False,
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def run() -> list[dict]:
    ANALYZED_DIR.mkdir(parents=True, exist_ok=True)

    scored_path = FILTERED_DIR / "scored.json"
    if not scored_path.exists():
        logger.error("No scored data — run fundamental_filter.py first")
        return []

    with open(scored_path) as f:
        stocks = json.load(f)

    logger.info(
        f"Analyzer: {len(stocks)} stocks to analyze "
        f"(LLM {'ON' if LLM_ENABLED else 'OFF'}, max {MAX_CALLS}/day)"
    )

    results: list[dict] = []
    llm_calls = 0

    for i, stock in enumerate(stocks):
        symbol = stock["symbol"]
        logger.info(f"  [{i+1}/{len(stocks)}] Analyzing {symbol} (score={stock.get('score', 0)})")

        # Fetch enriched context
        news     = get_full_news(symbol)
        earnings = get_earnings_context(symbol)
        theme    = get_theme_for_symbol(symbol) or stock.get("theme")

        analysis = None
        if LLM_ENABLED and llm_calls < MAX_CALLS:
            prompt   = build_prompt(stock, news, earnings, theme)
            analysis = call_llm(prompt)
            if analysis:
                analysis["llm_used"] = True
                llm_calls += 1
                logger.info(f"    → {analysis.get('sentiment', '?')} | {analysis.get('summary', '')[:60]}")
            time.sleep(0.5)  # gentle rate limit

        if not analysis:
            analysis = fallback_analysis(stock, theme)
            logger.info(f"    → fallback: {analysis['sentiment']}")

        # Merge metadata
        analysis["price"]         = stock.get("price")
        analysis["change_pct"]    = stock.get("change_pct")
        analysis["volume_ratio"]  = stock.get("volume_ratio")
        analysis["news_count"]    = stock.get("news_count")
        analysis["earnings_days"] = stock.get("earnings_days")
        analysis["score"]         = stock.get("score")
        analysis["reasons"]       = stock.get("reasons", [])
        analysis["news_headlines"] = stock.get("recent_headlines", [])
        analysis["analyzed_at"]   = datetime.now(timezone.utc).isoformat()

        # Save individual file
        out = ANALYZED_DIR / f"{symbol}.json"
        with open(out, "w") as f:
            json.dump(analysis, f, indent=2)

        results.append(analysis)

    logger.info(f"Analysis complete: {len(results)} stocks, {llm_calls} LLM calls used")
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dotenv import load_dotenv
    load_dotenv()
    results = run()
    print(f"\n✓ Analyzed {len(results)} stocks")
    for r in results:
        flag = "🤖" if r.get("llm_used") else "📊"
        print(f"  {flag} {r['symbol']:8} [{r['sentiment']:7}] {r['summary'][:70]}")
