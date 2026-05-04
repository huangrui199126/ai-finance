#!/bin/bash
# SETUP_GITHUB.command — one-time GitHub setup for ai_finance
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=================================================="
echo "  AI Finance — GitHub Setup"
echo "  Folder: $SCRIPT_DIR"
echo "=================================================="
echo ""

# ── Step 1: Git init ──────────────────────────────────
if [ -d ".git" ]; then
  echo "✅ Git already initialized"
else
  git init -b main
  echo "✅ Git initialized"
fi

# ── Step 2: Initial commit ────────────────────────────
git add -A
git status --short
echo ""
git diff --cached --quiet && echo "Nothing new to commit" || git commit -m "🚀 Initial commit — AI Finance pipeline"
echo ""

# ── Step 3: Check for gh CLI ─────────────────────────
if ! command -v gh &>/dev/null; then
  echo "══════════════════════════════════════════════════"
  echo "  gh CLI not found. Manual setup:"
  echo ""
  echo "  1. Go to https://github.com/new"
  echo "     Create a repo named: ai-finance  (public)"
  echo ""
  echo "  2. Run these in Terminal:"
  echo "     cd $SCRIPT_DIR"
  echo "     git remote add origin https://github.com/YOUR_USERNAME/ai-finance.git"
  echo "     git push -u origin main"
  echo ""
  echo "  3. On GitHub repo → Settings → Pages"
  echo "     Source: Deploy from a branch"
  echo "     Branch: main  /docs"
  echo ""
  echo "  4. Add secrets (Settings → Secrets → Actions):"
  echo "     FMP_API_KEY"
  echo "     FINNHUB_API_KEY"
  echo "     OPENROUTER_API_KEY"
  echo "══════════════════════════════════════════════════"
  read -p "Press Enter to close..."
  exit 0
fi

# ── Step 4: gh auth check ─────────────────────────────
if ! gh auth status &>/dev/null; then
  echo "🔑 You need to log in to GitHub first..."
  gh auth login
fi

# ── Step 5: Create repo ───────────────────────────────
REPO_NAME="ai-finance"
echo "Creating GitHub repo: $REPO_NAME ..."
gh repo create "$REPO_NAME" --public --source=. --remote=origin --push \
  --description "AI-powered stock intelligence dashboard" 2>/dev/null \
  || { echo "Repo may already exist — attempting push..."; git push -u origin main; }

echo ""
echo "✅ Pushed to GitHub!"
echo ""

# ── Step 6: Add secrets ───────────────────────────────
echo "Adding API keys as GitHub Actions secrets..."
if [ -f ".env" ]; then
  source .env 2>/dev/null || true
  [ -n "${FMP_API_KEY:-}" ]         && gh secret set FMP_API_KEY         --body "$FMP_API_KEY"         && echo "  ✅ FMP_API_KEY"
  [ -n "${FINNHUB_API_KEY:-}" ]     && gh secret set FINNHUB_API_KEY     --body "$FINNHUB_API_KEY"     && echo "  ✅ FINNHUB_API_KEY"
  [ -n "${OPENROUTER_API_KEY:-}" ]  && gh secret set OPENROUTER_API_KEY  --body "$OPENROUTER_API_KEY"  && echo "  ✅ OPENROUTER_API_KEY"
fi

# ── Step 7: Enable Pages ──────────────────────────────
echo ""
echo "Enabling GitHub Pages from docs/ ..."
gh api --method POST \
  "repos/$(gh repo view --json owner,name -q '.owner.login + "/" + .name')/pages" \
  -f source='{"branch":"main","path":"/docs"}' 2>/dev/null \
  || echo "  (Pages may already be enabled or needs manual activation)"

REPO_URL=$(gh repo view --json url -q '.url')
echo ""
echo "=================================================="
echo "  ✅ All done!"
echo "  Repo:      $REPO_URL"
echo "  Dashboard: ${REPO_URL/github.com/github.io/}"
echo "  (Pages takes ~1 min to go live)"
echo "=================================================="
echo ""
read -p "Press Enter to close..."
