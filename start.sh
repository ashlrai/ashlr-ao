#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────
# Ashlar AO — Launch Script
# ──────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo ""
echo "  ╔═══════════════════════════════════╗"
echo "  ║         A S H L A R   A O        ║"
echo "  ║     Agent Orchestration Platform   ║"
echo "  ╚═══════════════════════════════════╝"
echo ""

# 1. Check Python
if ! command -v python3 &>/dev/null; then
    echo "❌ Python 3 is required. Install it: brew install python3"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "✓ Python $PYTHON_VERSION"

# 2. Check tmux
if ! command -v tmux &>/dev/null; then
    echo "❌ tmux is required. Install it:"
    echo "   macOS:  brew install tmux"
    echo "   Linux:  sudo apt install tmux"
    exit 1
fi
echo "✓ tmux $(tmux -V | cut -d' ' -f2)"

# 3. Check claude CLI (warn but don't fail)
if command -v claude &>/dev/null; then
    echo "✓ claude CLI found"
else
    echo "⚠ claude CLI not found — agents will run in demo mode (bash)"
    echo "  Install: npm install -g @anthropic-ai/claude-code"
fi

# 4. Create virtual environment if needed
if [ ! -d "$VENV_DIR" ]; then
    echo ""
    echo "→ Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# 5. Activate venv and install deps
source "$VENV_DIR/bin/activate"

echo "→ Installing dependencies..."
pip install -q -r "$SCRIPT_DIR/requirements.txt"

# 6. Create config directory
mkdir -p "$HOME/.ashlar"

# 7. Launch server
echo ""
echo "→ Starting Ashlar server..."
echo "  Dashboard: http://localhost:5000"
echo "  Press Ctrl+C to stop"
echo ""

python3 "$SCRIPT_DIR/ashlar_server.py" "$@"
