#!/usr/bin/env bash
# install.sh — Install the agent-eyes Chrome Extension native messaging host
#
# This script registers the Python native messaging host so Chrome can
# communicate with the agent-eyes MCP server.
#
# Usage:
#   ./install.sh                  # Auto-detect extension ID from manifest
#   ./install.sh <EXTENSION_ID>   # Provide extension ID explicitly
#
# Run this AFTER loading the extension in Chrome:
#   1. Open chrome://extensions
#   2. Enable "Developer mode" (top-right toggle)
#   3. Click "Load unpacked" and select this extension/ directory
#   4. Copy the Extension ID shown on the card
#   5. Run:  ./install.sh <EXTENSION_ID>

set -euo pipefail

# ── Resolve paths ──────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Python native messaging host script
NATIVE_HOST_SCRIPT="$REPO_ROOT/src/agent_eyes/native_host.py"

# Fallback: if native_host.py doesn't exist yet, point to server.py
if [[ ! -f "$NATIVE_HOST_SCRIPT" ]]; then
  NATIVE_HOST_SCRIPT="$REPO_ROOT/src/agent_eyes/server.py"
fi

# Resolve the Python interpreter (prefer the venv / uv-managed one)
if command -v uv &>/dev/null; then
  PYTHON_PATH="$(uv run which python 2>/dev/null || command -v python3 || command -v python)"
elif [[ -f "$REPO_ROOT/.venv/bin/python" ]]; then
  PYTHON_PATH="$REPO_ROOT/.venv/bin/python"
else
  PYTHON_PATH="$(command -v python3 || command -v python)"
fi

HOST_NAME="com.agent_eyes.bridge"

# ── Extension ID ───────────────────────────────────────────────────

EXTENSION_ID="${1:-}"

if [[ -z "$EXTENSION_ID" ]]; then
  echo ""
  echo "  agent-eyes native messaging host installer"
  echo "  ─────────────────────────────────────────"
  echo ""
  echo "  To get your Extension ID:"
  echo "    1. Open Chrome and go to:  chrome://extensions"
  echo "    2. Enable 'Developer mode' (toggle, top-right)"
  echo "    3. Click 'Load unpacked' and select:"
  echo "         $SCRIPT_DIR"
  echo "    4. Copy the Extension ID shown on the card"
  echo ""
  echo "  Then re-run:"
  echo "    $0 <EXTENSION_ID>"
  echo ""
  exit 1
fi

# Basic validation — Chrome extension IDs are 32 lowercase letters
if ! [[ "$EXTENSION_ID" =~ ^[a-z]{32}$ ]]; then
  echo "ERROR: '$EXTENSION_ID' does not look like a valid Chrome extension ID."
  echo "       Expected 32 lowercase letters, e.g.: abcdefghijklmnopqrstuvwxyzabcdef"
  exit 1
fi

# ── Platform-specific manifest path ───────────────────────────────

case "$(uname -s)" in
  Darwin)
    # macOS — Chrome (stable), Chrome Canary, Chromium
    MANIFEST_DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
    ;;
  Linux)
    # Linux — Chrome and Chromium both read from ~/.config
    MANIFEST_DIR="$HOME/.config/google-chrome/NativeMessagingHosts"
    ;;
  *)
    echo "ERROR: Unsupported platform: $(uname -s)"
    echo "       Supported platforms: macOS, Linux"
    echo "       On Windows, register the host in the registry manually."
    exit 1
    ;;
esac

MANIFEST_PATH="$MANIFEST_DIR/$HOST_NAME.json"

# ── Write the native messaging host manifest ───────────────────────

mkdir -p "$MANIFEST_DIR"

cat > "$MANIFEST_PATH" <<EOF
{
  "name": "$HOST_NAME",
  "description": "agent-eyes native messaging bridge",
  "path": "$PYTHON_PATH",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://$EXTENSION_ID/"
  ]
}
EOF

echo ""
echo "  Native messaging host installed successfully!"
echo ""
echo "  Manifest:   $MANIFEST_PATH"
echo "  Host name:  $HOST_NAME"
echo "  Python:     $PYTHON_PATH"
echo "  Extension:  $EXTENSION_ID"
echo ""
echo "  If Chrome is already running, reload the extension:"
echo "    chrome://extensions  →  click the refresh icon on agent-eyes"
echo ""
