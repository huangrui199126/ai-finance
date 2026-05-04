from __future__ import annotations
"""
push.py — GitHub Auto-Publisher

Commits and pushes the docs/data/ directory to GitHub.
Runs after report.py each day.
"""
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import CFG

logger = logging.getLogger(__name__)

ROOT   = Path(CFG["paths"]["output"]).parent.parent  # project root
BRANCH = CFG["github"]["branch"]


def _git(args: list[str], cwd: Path = ROOT) -> tuple[int, str]:
    """Run a git command, return (returncode, output)."""
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def run() -> bool:
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    msg      = CFG["github"]["commit_message"].format(date=today)

    # Configure git identity (needed in CI environments)
    _git(["config", "user.email", os.environ.get("GIT_EMAIL", "bot@ai-finance.local")])
    _git(["config", "user.name",  os.environ.get("GIT_NAME",  "AI Finance Bot")])

    # Stage only the data output + themes directories
    data_rel = os.path.relpath(CFG["paths"]["output"], ROOT)
    themes_rel = os.path.relpath(str(Path(CFG["paths"]["output"]).parent / "themes"), ROOT)

    code, out = _git(["add", data_rel, themes_rel])
    logger.info(f"git add: {out or 'ok'}")

    # Check if there's anything to commit
    code, status = _git(["status", "--porcelain"])
    if not status.strip():
        logger.info("Nothing to commit — data unchanged")
        return True

    code, out = _git(["commit", "-m", msg])
    if code != 0:
        logger.error(f"git commit failed: {out}")
        return False
    logger.info(f"git commit: {out[:80]}")

    code, out = _git(["push", "origin", BRANCH])
    if code != 0:
        logger.error(f"git push failed: {out}")
        return False
    logger.info(f"git push: {out[:80]}")

    logger.info(f"✓ Published to GitHub: {msg}")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dotenv import load_dotenv
    load_dotenv()
    ok = run()
    sys.exit(0 if ok else 1)
