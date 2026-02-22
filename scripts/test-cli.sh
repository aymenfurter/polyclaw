#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Test the single-command CLI (polyclaw-run)
#
# This script demonstrates how to invoke polyclaw in single-command mode.
# It sends a prompt that exercises memory recall -- proving the CLI has
# access to all memories, tools, skills, and session history acquired
# during live (TUI / web) sessions.
#
# Usage:
#   ./scripts/test-cli.sh                  # run the default demo prompt
#   ./scripts/test-cli.sh "your prompt"    # run a custom prompt
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Syntax reference ──────────────────────────────────────────────────────
echo ""
echo "=== polyclaw-run -- single-command CLI ==="
echo ""
echo "Syntax:"
echo "  polyclaw-run \"<prompt>\"                  # inline prompt"
echo "  polyclaw-run --file <path>               # prompt from file"
echo "  echo \"<prompt>\" | polyclaw-run -          # prompt from stdin"
echo "  polyclaw-run -q \"<prompt>\"                # quiet (no streaming)"
echo "  polyclaw-run --auto-approve \"<prompt>\"    # skip tool approval prompts"
echo "  polyclaw-run --skip-memory \"<prompt>\"     # skip memory post-processing"
echo "  polyclaw-run --model gpt-4.1 \"<prompt>\"   # override model"
echo ""
echo "Flags can be combined:"
echo "  polyclaw-run -q --auto-approve --skip-memory \"quick question\""
echo ""

# ── Activate venv ─────────────────────────────────────────────────────────
if [[ -d "$PROJECT_ROOT/.venv" ]]; then
    source "$PROJECT_ROOT/.venv/bin/activate"
fi

# ── Run the demo ──────────────────────────────────────────────────────────
cd "$PROJECT_ROOT"

PROMPT="${1:-Tell me about what we did the last few days}"

echo "--- Running polyclaw-run ---"
echo "Prompt: \"$PROMPT\""
echo ""

python -m app.cli.run --auto-approve "$PROMPT"
