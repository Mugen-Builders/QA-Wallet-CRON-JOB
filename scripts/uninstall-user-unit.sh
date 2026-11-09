#!/bin/sh
# Stop and remove the user service. Keeps config (~/.config/qa-traffic-bot) and state (var/):
# delete those yourself if you want them gone. Linger is left as it is (other user services may need it).
set -eu
systemctl --user disable --now qa-traffic-bot.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/qa-traffic-bot.service"
systemctl --user daemon-reload
echo "removed the qa-traffic-bot user service (config and state kept)"
