#!/bin/sh
# Install qa-traffic-bot as a systemd user service that starts on boot.
#   scripts/install-user-unit.sh            install + enable (does not start)
#   scripts/install-user-unit.sh --start    install, enable and start now
# It never touches system units and needs no sudo, except `loginctl enable-linger`, which lets
# user services run at boot without a login session (it asks for your password if polkit requires it).
set -eu
PROJECT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON=$(command -v python3)
CONF_DIR="$HOME/.config/qa-traffic-bot"
UNIT_DIR="$HOME/.config/systemd/user"

"$PYTHON" - <<'EOF'
import importlib.metadata as m, sys
want = {"web3": "7.16.0", "eth-account": "0.13.7", "requests": "2.32.5"}
bad = [f"{k} {m.version(k)} (want {v})" for k, v in want.items() if m.version(k) != v]
if bad:
    sys.exit("dependency versions differ: " + ", ".join(bad) + "\n  pip install --user -r requirements.txt")
EOF

mkdir -p "$CONF_DIR" "$UNIT_DIR"
chmod 700 "$CONF_DIR"
if [ ! -f "$CONF_DIR/config.env" ]; then
    cp "$PROJECT/config.env.example" "$CONF_DIR/config.env"
    chmod 600 "$CONF_DIR/config.env"
    echo "created $CONF_DIR/config.env from the example: fill it in, then run this script again"
    exit 0
fi
chmod 600 "$CONF_DIR/config.env"
sed -e "s#@PROJECT@#$PROJECT#g" -e "s#@PYTHON@#$PYTHON#g" "$PROJECT/systemd/qa-traffic-bot.service" > "$UNIT_DIR/qa-traffic-bot.service"
systemctl --user daemon-reload
systemctl --user enable qa-traffic-bot.service
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    echo "enabling linger so the service starts at boot without a login"
    loginctl enable-linger "$USER"
fi
echo "selfcheck:"
( set -a; . "$CONF_DIR/config.env"; set +a; cd "$PROJECT"; PYTHONPATH="$PROJECT" "$PYTHON" -m qatraffic selfcheck ) || {
    echo "selfcheck failed: fix config.env before starting"; exit 1; }
if [ "${1:-}" = "--start" ]; then
    systemctl --user restart qa-traffic-bot.service
    systemctl --user --no-pager status qa-traffic-bot.service | head -12
else
    echo "installed and enabled; start with: systemctl --user start qa-traffic-bot"
fi
