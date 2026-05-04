#!/bin/bash
# AI Finance Pipeline — double-click me to run!

# Ensure PATH includes common Python install locations
export PATH="/usr/local/bin:/usr/bin:/opt/homebrew/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "================================================"
echo "  AI Finance Pipeline"
echo "  Folder: $SCRIPT_DIR"
echo "================================================"
echo ""

# Find python3
PYTHON=$(command -v python3 || command -v python || echo "NOT_FOUND")
PIP=$(command -v pip3 || command -v pip || echo "NOT_FOUND")

if [ "$PYTHON" = "NOT_FOUND" ]; then
  echo "❌ Python3 not found. Please install Python from python.org"
  read -p "Press Enter to close..."
  exit 1
fi

echo "✅ Python: $PYTHON"
echo "✅ pip: $PIP"
echo ""

# Load .env
if [ -f .env ]; then
  while IFS= read -r line; do
    [[ "$line" =~ ^#.*$ ]] && continue
    [[ -z "$line" ]] && continue
    export "$line"
  done < .env
  echo "✅ Loaded .env"
fi
echo ""

# Install deps
echo "📦 Installing dependencies..."
$PIP install -r requirements.txt -q
echo "✅ Done"
echo ""

# Fetch symbols
echo "📋 Fetching S&P 500 symbols..."
$PYTHON scripts/fetch_symbols.py
echo ""

# Run pipeline
echo "🚀 Running full pipeline..."
$PYTHON scheduler.py --once
echo ""
echo "================================================"
echo "  ✅ Pipeline complete!"
echo "================================================"
read -p "Press Enter to close..."
