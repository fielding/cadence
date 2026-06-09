#!/usr/bin/env bash
# Install cadence as a launchd user agent (macOS).
#
# The daemon starts DISARMED when require_manual_enable_on_start = true
# (the default). After loading, run `cadence resume` to arm it.
set -euo pipefail

LABEL="com.justfielding.cadence"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cadence"

# Resolve the cadence executable (installed via `uv tool install` or a venv).
CADENCE_BIN="$(command -v cadence || true)"
if [[ -z "$CADENCE_BIN" ]]; then
  echo "error: 'cadence' not found on PATH." >&2
  echo "Install it first, e.g.:  uv tool install --editable ." >&2
  exit 1
fi

mkdir -p "$STATE_DIR" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${CADENCE_BIN}</string>
    <string>daemon</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${STATE_DIR}/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${STATE_DIR}/launchd.err.log</string>
</dict>
</plist>
PLIST_EOF

echo "Wrote $PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Loaded ${LABEL}."
echo "The daemon is disarmed on start. Run 'cadence resume' to arm it."
echo "Uninstall with: launchctl unload \"$PLIST\" && rm \"$PLIST\""
