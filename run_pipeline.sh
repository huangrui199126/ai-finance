#!/bin/bash
# AI Finance — full setup + pipeline run
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "📂 Working in: $SCRIPT_DIR"
echo ""

# ─── Install dependencies ────────────────────────────────────────────────────
echo "📦 Installing Python dependencies..."
pip3 install -r requirements.txt -q

# ─── Load .env ───────────────────────────────────────────────────────────────
if [ -f .env ]; then
  export $(grep -v '^#' .env | grep -v '^\s*$' | xargs)
  echo "✅ Loaded .env"
else
  echo "⚠️  No .env file found — API calls will fail"
fi

echo ""

# ─── Fetch S&P 500 symbols ───────────────────────────────────────────────────
echo "📋 Fetching S&P 500 symbol list..."
python3 scripts/fetch_symbols.py

echo ""

# ─── Run full pipeline once ──────────────────────────────────────────────────
echo "🚀 Running full pipeline (collect → scan → analyze → report)..."
python3 scheduler.py --once

echo ""
echo "✅ Pipeline complete!"
