from __future__ import annotations
"""
analyze.py — LLM Analyzer (Put-Selling Strategy)

Evaluates stocks as candidates for selling cash-secured puts
after a sharp drop with elevated implied volatility.

Strict rules:
  - NO trading advice
  - NO buy/sell recommendations
  - Output: candidate evaluation, drop type, IV context, strike zone reasoning
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


def get_iv_estimate(symbol: str, change_pct: float) -> dict:
    """
    Estimate IV context from price action + Finnhub quote data.
    Returns a best-effort options_data dict when live options feed unavailable.
    """
    # Rough IV-level heuristic: big drops → elevated IV
    abs_chg = abs(change_pct or 0)
    if abs_chg >= 10:
        iv_level = "high"
    elif abs_chg >= 5:
        iv_level = "medium"
    else:
        iv_level = "low"

    # Try Finnhub for any available vol metric
    quote = _finnhub("quote", {"symbol": symbol}) or {}
    return {
        "iv_level": iv_level,
        "iv_percentile": "not available — estimated from price action",
        "put_call_ratio": "not available",
        "unusual_flow": "not available",
        "atm_iv": "not available",
        "note": f"Live options data unavailable; IV level estimated from {abs_chg:.1f}% price move",
    }


def get_fundamentals_summary(symbol: str) -> str:
    """Pull basic fundamentals from FMP to inform LLM quality assessment."""
    profile = _fmp(f"profile/{symbol}") or []
    if profile and isinstance(profile, list):
        p = profile[0]
        mktcap = p.get("mktCap", 0)
        mktcap_str = f"${mktcap/1e9:.1f}B" if mktcap > 1e9 else f"${mktcap/1e6:.0f}M"
        return (
            f"Sector: {p.get('sector','N/A')} | Industry: {p.get('industry','N/A')} | "
            f"Market Cap: {mktcap_str} | Beta: {p.get('beta','N/A')} | "
            f"Description: {(p.get('description') or '')[:150]}"
        )
    return "Fundamentals data not available"


def fallback_drop_classification(stock: dict) -> dict:
    """Rule-based drop classification when LLM is unavailable."""
    chg     = stock.get("change_pct", 0) or 0
    reasons = stock.get("reasons", [])

    # Conservative heuristics
    if chg < -3:
        verdict        = "safe_to_consider"
        classification = "unclear"
    else:
        verdict        = "high_risk"
        classification = "unclear"

    return {
        "drop_classification": classification,
        "confidence":          "low",
        "primary_reason":      f"Price moved {chg:+.1f}% — automated classification unavailable",
        "structural_flags":    [],
        "sector_trend":        "unknown",
        "recovery_outlook":    "unclear",
        "key_evidence":        [
            f"Price change: {chg:+.1f}%",
            f"Volume: {stock.get('volume_ratio', 0):.1f}x average",
        ],
        "risk_summary":        "Insufficient data for drop classification; verify manually",
        "verdict":             verdict,
    }


def build_drop_classifier_prompt(
    stock: dict,
    news: list[str],
    earnings: str,
    fundamentals: str = "",
) -> str:
    """Stage 1 prompt: classify the drop as temporary/structural/unclear."""
    symbol  = stock["symbol"]
    name    = stock.get("name", symbol)
    price   = stock.get("price", "N/A")
    chg_pct = stock.get("change_pct", 0)
    vol_rat = stock.get("volume_ratio", 0)

    input_data = {
        "symbol":              symbol,
        "name":                name,
        "price":               price,
        "price_change_pct":    chg_pct,
        "volume_ratio":        f"{vol_rat:.1f}x",
        "recent_news":         news if news else ["No recent news"],
        "earnings_context":    earnings,
        "fundamentals_summary": fundamentals or "Not available",
        "signal_triggers":     stock.get("reasons", []),
    }

    return f"""You are a fundamental analyst specializing in identifying whether a stock's drop is temporary or structural.

Your job is a GATING analysis: determine if this drop is safe to consider for a put-selling strategy, or if it should be avoided entirely.

========================
INPUT DATA
========================

{json.dumps(input_data, indent=2)}

========================
CLASSIFICATION TASK
========================

Classify the reason for this stock's price drop:

TEMPORARY / SENTIMENT-DRIVEN:
- Market overreaction to manageable news
- Sector rotation or macro pressure (not company-specific)
- Earnings miss but guidance intact, business model healthy
- Short-term sentiment shift without fundamental change

STRUCTURAL / FUNDAMENTAL DETERIORATION (FLAG AS HIGH RISK):
- Major guidance cut or revenue warning
- Business model disruption or permanent market-share loss
- Accounting irregularities or fraud concerns
- CEO/CFO departure under bad circumstances
- Regulatory action threatening core business
- Debt crisis or liquidity concerns
- Permanent demand destruction in their market

UNCLEAR:
- Insufficient information to classify confidently
- Mixed signals (some temporary, some structural)

========================
STRUCTURAL FLAGS TO WATCH
========================

Immediately flag as high_risk or avoid if ANY present in the news:
- "accounting", "fraud", "restatement", "SEC investigation"
- "going concern", "bankruptcy", "liquidity crisis"
- "guidance cut", "revenue warning", "lowered outlook"
- "CEO resigned", "CFO resigned" under negative circumstances
- "product recall", "regulatory ban"
- "losing market share" to a superior competitor

========================
OUTPUT FORMAT
========================

Respond ONLY in this exact JSON format (no markdown):
{{
  "symbol": "{symbol}",
  "drop_classification": "<temporary | structural | unclear>",
  "confidence": "<high | medium | low>",
  "primary_reason": "<one sentence explaining the main driver>",
  "structural_flags": ["<flag 1 if any — empty list if none>"],
  "sector_trend": "<favorable | neutral | headwinds | unknown>",
  "recovery_outlook": "<likely | possible | unlikely | unclear>",
  "key_evidence": ["<evidence 1>", "<evidence 2>"],
  "risk_summary": "<one sentence on the key risk>",
  "verdict": "<safe_to_consider | avoid | high_risk>"
}}

Rules for verdict:
- safe_to_consider: drop appears temporary, no structural red flags
- avoid: clear structural deterioration, permanent damage likely
- high_risk: unclear or mixed signals with meaningful downside risk"""


def classify_drop(
    stock: dict,
    news: list[str],
    earnings: str,
    fundamentals: str = "",
) -> dict | None:
    """Stage 1 LLM call — drop classifier. Returns classification dict or None."""
    prompt = build_drop_classifier_prompt(stock, news, earnings, fundamentals)
    return call_llm(prompt)


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

def build_prompt(
    stock: dict,
    news: list[str],
    earnings: str,
    theme: str | None,
    fundamentals: str = "",
    options_data: dict | None = None,
) -> str:
    symbol      = stock["symbol"]
    name        = stock.get("name", symbol)
    price       = stock.get("price", "N/A")
    chg_pct     = stock.get("change_pct", 0)
    vol_rat     = stock.get("volume_ratio", 0)
    news_txt    = "\n".join(news) if news else "No recent news available"
    theme_txt   = theme or "Not identified"
    opts        = options_data or {}

    input_data = {
        "symbol": symbol,
        "name": name,
        "price": price,
        "price_change": chg_pct,
        "volume_ratio_vs_avg": f"{vol_rat:.1f}x",
        "sector_theme": theme_txt,
        "signal_triggers": stock.get("reasons", []),
        "recent_news": news if news else ["No recent news available"],
        "earnings_context": earnings,
        "fundamentals_summary": fundamentals or "Not available",
        "technical_context": (
            f"Price dropped {abs(chg_pct):.1f}% on {vol_rat:.1f}x average volume. "
            f"Support/resistance levels not available in current data pipeline."
        ),
        "options_data": {
            "iv_level": opts.get("iv_level", "estimated from price action"),
            "iv_percentile": opts.get("iv_percentile", "not available"),
            "put_call_ratio": opts.get("put_call_ratio", "not available"),
            "unusual_flow": opts.get("unusual_flow", "not available"),
            "atm_iv": opts.get("atm_iv", "not available"),
            "data_note": opts.get("note", ""),
        },
    }

    return f"""You are a quantitative analyst and options strategist.

Your task is to identify high-quality opportunities for a specific strategy:
"Sell cash-secured puts on fundamentally strong stocks after a sharp drop with elevated implied volatility."

This is NOT about predicting price direction.
This is about evaluating whether the risk/reward of selling puts is reasonable.

========================
INPUT DATA
========================

{json.dumps(input_data, indent=2)}

========================
OBJECTIVE
========================

Evaluate whether this stock is a GOOD candidate for selling puts after a drop.

========================
CORE ANALYSIS LOGIC
========================

Step 1 — Reason for the Drop (CRITICAL)
Classify the drop:
- Temporary / sentiment-driven
- Earnings miss but business intact
- Structural / fundamental deterioration (NEGATIVE)
- Unknown / unclear

Reject immediately if: fraud / accounting concerns, major guidance cut, business model breakdown

Step 2 — Fundamental Quality
Is this a fundamentally solid company? Would a long-term investor be comfortable owning it?
Output: strong / acceptable / weak

Step 3 — Volatility & Options Context
Is IV elevated relative to normal? Is premium likely attractive for sellers?
Note: live options data may be unavailable — reason from price action magnitude and news volatility.
Output: favorable / neutral / unfavorable

Step 4 — Risk Assessment
Identify: downside continuation risk, macro or sector risks, earnings uncertainty

Step 5 — Strike Reasoning
DO NOT give trading advice, but provide a logical zone:
- "5–10% below current price may align with support"
- or reason about where the stock might find buyers

========================
IMPORTANT RULES
========================
- Do NOT provide buy/sell recommendations
- Do NOT guarantee outcomes
- Be skeptical of falling knife situations
- Prioritize WHY the stock dropped over technicals
- If the drop is < 3%, note this strategy requires a meaningful drop for elevated IV

========================
OUTPUT FORMAT
========================

Respond ONLY in this exact JSON format (no markdown):
{{
  "symbol": "{symbol}",
  "theme": "{theme_txt}",
  "candidate_score": <integer 1-10>,
  "drop_type": "<Temporary/sentiment-driven | Earnings miss but business intact | Structural deterioration | Unknown/unclear>",
  "fundamental_quality": "<strong | acceptable | weak>",
  "iv_assessment": "<favorable | neutral | unfavorable>",
  "key_drivers": ["<driver 1>", "<driver 2>"],
  "key_risks": ["<risk 1>", "<risk 2>"],
  "options_context": "<1-2 sentences on IV environment and premium attractiveness>",
  "strike_zone_reasoning": "<1-2 sentences on logical strike zone, no specific numbers unless from data>",
  "summary": "<one clear sentence explaining the opportunity or why to avoid>",
  "decision": "<good_candidate | avoid | unclear>",
  "sentiment": "<bullish | bearish | neutral>"
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
    """No-LLM fallback — rule-based put-candidate evaluation from price + news."""
    chg     = stock.get("change_pct") or 0
    abs_chg = abs(chg)
    reasons = stock.get("reasons", [])

    # Sentiment based on direction
    if chg > 3:
        sentiment = "bullish"
    elif chg < -3:
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    # Candidate score heuristic (no LLM, pure rules)
    score = 0
    if chg < -5:   score += 3   # meaningful drop
    elif chg < -3: score += 2
    if "volume_spike" in reasons: score += 2   # volume confirms move
    if "news_spike"   in reasons: score += 1   # catalyst exists
    score = min(score, 7)  # cap at 7 without LLM context

    decision = "unclear"
    if chg < -5 and "volume_spike" in reasons:
        decision = "good_candidate"
    elif chg > 0:
        decision = "avoid"  # not a drop; put-selling strategy requires drop

    key_drivers = []
    if "price_move"   in reasons: key_drivers.append(f"Price dropped {abs_chg:.1f}% today")
    if "volume_spike" in reasons: key_drivers.append(f"Volume at {stock.get('volume_ratio',0):.1f}x average — confirms move")
    if "earnings_soon"in reasons: key_drivers.append(f"Earnings in {stock.get('earnings_days')} days — IV likely elevated")
    if "news_spike"   in reasons: key_drivers.append(f"{stock.get('news_count',0)} news items in 48h — catalyst-driven move")

    return {
        "symbol":               stock["symbol"],
        "theme":                theme or "",
        "candidate_score":      score,
        "drop_type":            "Unknown/unclear — LLM analysis unavailable",
        "fundamental_quality":  "acceptable",
        "iv_assessment":        "favorable" if abs_chg >= 5 else "neutral",
        "key_drivers":          key_drivers or ["Unusual market activity detected"],
        "key_risks":            ["LLM analysis unavailable — fundamental risk unknown", "Verify drop reason before trading"],
        "options_context":      f"{abs_chg:.1f}% move suggests elevated IV; verify with live options data before acting.",
        "strike_zone_reasoning": (
            f"A 5–10% buffer below current price (${stock.get('price', 0) * 0.9:.2f}–"
            f"${stock.get('price', 0) * 0.95:.2f}) is a common starting zone for weekly puts. "
            "Verify with actual support levels."
        ),
        "summary":              (
            f"{stock['symbol']} dropped {chg:+.1f}% on {stock.get('volume_ratio',0):.1f}x volume — "
            f"rule-based scan flags as {decision.replace('_',' ')}; LLM review unavailable."
        ),
        "decision":             decision,
        "sentiment":            sentiment,
        # Drop classification fields (placeholder — filled by run() after stage-1 call)
        "drop_classification":  "unclear",
        "drop_confidence":      "low",
        "structural_flags":     [],
        "sector_trend":         "unknown",
        "recovery_outlook":     "unclear",
        "drop_verdict":         "safe_to_consider",
        "llm_used":             False,
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
        symbol  = stock["symbol"]
        chg_pct = stock.get("change_pct", 0) or 0
        logger.info(f"  [{i+1}/{len(stocks)}] {symbol} (chg={chg_pct:+.1f}%)")

        # Fetch enriched context (shared by both LLM stages)
        news         = get_full_news(symbol)
        earnings     = get_earnings_context(symbol)
        theme        = get_theme_for_symbol(symbol) or stock.get("theme")
        fundamentals = get_fundamentals_summary(symbol)
        options_data = get_iv_estimate(symbol, chg_pct)

        # ── Stage 1: Drop Classifier (gating) ────────────────────────────────
        drop_result = None
        if LLM_ENABLED and llm_calls < MAX_CALLS:
            drop_result = classify_drop(stock, news, earnings, fundamentals)
            if drop_result:
                llm_calls += 1
                verdict = drop_result.get("verdict", "unclear")
                logger.info(
                    f"    S1 [{verdict}] {drop_result.get('drop_classification','?')} "
                    f"confidence={drop_result.get('confidence','?')}"
                )
            time.sleep(0.5)

        if not drop_result:
            drop_result = fallback_drop_classification(stock)
            logger.info(f"    S1 fallback verdict={drop_result['verdict']}")

        verdict = drop_result.get("verdict", "safe_to_consider")

        # ── Stage 2: Put-Candidate Evaluator (safe_to_consider stocks only) ──
        analysis = None

        if verdict == "safe_to_consider" and LLM_ENABLED and llm_calls < MAX_CALLS:
            prompt   = build_prompt(stock, news, earnings, theme, fundamentals, options_data)
            analysis = call_llm(prompt)
            if analysis:
                analysis["llm_used"] = True
                llm_calls += 1
                decision = analysis.get("decision", "?")
                score    = analysis.get("candidate_score", "?")
                logger.info(f"    S2 [{decision}] score={score} | {analysis.get('summary','')[:60]}")
            time.sleep(0.5)

        # Gated out at Stage 1 — synthesise an avoid result without calling Stage 2
        if verdict in ("avoid", "high_risk") and not analysis:
            analysis = {
                "symbol":               symbol,
                "theme":                theme or "",
                "candidate_score":      1,
                "drop_type":            drop_result.get("drop_classification", "structural"),
                "fundamental_quality":  "weak",
                "iv_assessment":        "unfavorable",
                "key_drivers":          drop_result.get("key_evidence", []),
                "key_risks":            [drop_result.get("risk_summary", "Structural risk identified")],
                "options_context":      "Gated out at drop-classification stage — structural risk detected.",
                "strike_zone_reasoning": "",
                "summary":              drop_result.get("risk_summary", f"{symbol} shows structural risk — avoided."),
                "decision":             "avoid",
                "sentiment":            "bearish",
                "llm_used":             True,
            }
            logger.info(f"    S2 skipped — gated out (verdict={verdict})")

        # Fall back to rule-based when LLM unavailable
        if not analysis:
            analysis = fallback_analysis(stock, theme)
            logger.info(f"    fallback [{analysis['decision']}] score={analysis['candidate_score']}")

        # Attach Stage-1 classification fields
        analysis["drop_classification"] = drop_result.get("drop_classification", "unclear")
        analysis["drop_confidence"]     = drop_result.get("confidence", "low")
        analysis["structural_flags"]    = drop_result.get("structural_flags", [])
        analysis["sector_trend"]        = drop_result.get("sector_trend", "unknown")
        analysis["recovery_outlook"]    = drop_result.get("recovery_outlook", "unclear")
        analysis["drop_verdict"]        = drop_result.get("verdict", "safe_to_consider")

        # Merge metadata
        analysis["price"]           = stock.get("price")
        analysis["change_pct"]      = stock.get("change_pct")
        analysis["volume_ratio"]    = stock.get("volume_ratio")
        analysis["news_count"]      = stock.get("news_count")
        analysis["earnings_days"]   = stock.get("earnings_days")
        analysis["score"]           = stock.get("score")
        analysis["reasons"]         = stock.get("reasons", [])
        analysis["news_headlines"]  = stock.get("recent_headlines", [])
        analysis["fundamentals"]    = fundamentals
        analysis["iv_estimate"]     = options_data
        analysis["analyzed_at"]     = datetime.now(timezone.utc).isoformat()

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
    candidates = [r for r in results if r.get("decision") == "good_candidate"]
    print(f"  📊 Good candidates: {len(candidates)} | Avoid: {sum(1 for r in results if r.get('decision')=='avoid')} | Unclear: {sum(1 for r in results if r.get('decision')=='unclear')}")
    for r in results:
        flag     = "🤖" if r.get("llm_used") else "📊"
        decision = r.get("decision", "?")
        score    = r.get("candidate_score", "?")
        icon     = "✅" if decision == "good_candidate" else ("❌" if decision == "avoid" else "⚠️")
        print(f"  {flag} {icon} {r['symbol']:6} [score={score}] {r['summary'][:65]}")
