#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/link-dashboard"
CONFIG_DIR="$HOME/.config/link-dashboard"
BIN_DIR="$HOME/.local/bin"

python3 -m pip install --user -r "$SRC_DIR/requirements.txt"

mkdir -p "$APP_DIR" "$CONFIG_DIR" "$BIN_DIR"

# Core application files
cp -r   "$SRC_DIR/dashboard.py"   "$SRC_DIR/link_health_payload.py"   "$SRC_DIR/templates"   "$SRC_DIR/static"   "$APP_DIR/"

# Optional/helper application files that are still part of the project
cp   "$SRC_DIR/linkctl_local.py"   "$SRC_DIR/gcs_link_viewer.py"   "$SRC_DIR/gcs_json_receiver.py"   "$APP_DIR/"

chmod +x   "$APP_DIR/linkctl_local.py"   "$APP_DIR/gcs_link_viewer.py"   "$APP_DIR/gcs_json_receiver.py"

# Create config on first install; preserve existing values on later installs.
if [[ ! -f "$CONFIG_DIR/dashboard_config.json" ]]; then
  cp "$SRC_DIR/dashboard_config.json" "$CONFIG_DIR/dashboard_config.json"
  echo "Config created: $CONFIG_DIR/dashboard_config.json"
else
  python3 - "$SRC_DIR/dashboard_config.json" "$CONFIG_DIR/dashboard_config.json" <<'PYCFG'
import json
import sys
from pathlib import Path

def merge_defaults(current, defaults):
    if isinstance(current, dict) and isinstance(defaults, dict):
        for key, value in defaults.items():
            if key not in current:
                current[key] = value
            else:
                merge_defaults(current[key], value)

src = Path(sys.argv[1])
dst = Path(sys.argv[2])

defaults = json.loads(src.read_text(encoding="utf-8"))
current = json.loads(dst.read_text(encoding="utf-8"))

merge_defaults(current, defaults)

dst.write_text(
    json.dumps(current, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PYCFG

  echo "Existing config preserved; missing new settings were added: $CONFIG_DIR/dashboard_config.json"
fi

# Install MAVProxy modules.
MODDIR="$(python3 - <<'PYMOD'
import os
import MAVProxy.modules

print(os.path.dirname(os.path.realpath(MAVProxy.modules.__file__)))
PYMOD
)"

install -m 644 "$SRC_DIR/mavproxy_linkstats.py" "$MODDIR/mavproxy_linkstats.py"
install -m 644 "$SRC_DIR/mavproxy_linkcontrol.py" "$MODDIR/mavproxy_linkcontrol.py"
install -m 644 "$SRC_DIR/mavproxy_linkwatch.py" "$MODDIR/mavproxy_linkwatch.py"

# Create the link-dashboard command directly.
# start_dashboard.sh is no longer required.
cat > "$BIN_DIR/link-dashboard" <<EOF
#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$APP_DIR"
CONFIG="$CONFIG_DIR/dashboard_config.json"

export DASHBOARD_DIR="\$APP_DIR"
export LINK_DASHBOARD_CONFIG="\$CONFIG"

exec python3 "\$APP_DIR/dashboard.py" --config "\$CONFIG" "\$@"
EOF

chmod +x "$BIN_DIR/link-dashboard"

echo
echo "Installation complete."
echo "1) Config: $CONFIG_DIR/dashboard_config.json"
echo "2) Dashboard: $BIN_DIR/link-dashboard"
echo "3) MAVProxy module directory: $MODDIR"
echo
echo 'Open a new terminal or run: export PATH="$PATH:$HOME/.local/bin"'
echo 'Start dashboard with: link-dashboard'
echo 'Web-only mode: link-dashboard --no-gui'
