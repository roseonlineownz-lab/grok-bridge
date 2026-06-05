#!/bin/bash
# Kill stale Chrome/bridge holding the grok_cloak profile and free the port.
# HOME-independent: resolve profile dir explicitly so it works under systemd (system or --user).
PROFILE="${GROK_PROFILE_DIR:-$HOME/.nova/grok_cloak}"
[ -d "$PROFILE" ] || PROFILE="$(eval echo ~$(id -un))/.nova/grok_cloak"

# Kill chromium instances bound to this profile (not this script)
pids=$(pgrep -f "chrome.*grok_cloak" 2>/dev/null)
[ -n "$pids" ] && echo "$pids" | xargs kill -9 2>/dev/null
# Free the bridge port if a stale instance holds it
holder=$(ss -ltnp 2>/dev/null | grep ':19997' | grep -oP 'pid=\K[0-9]+' | head -1)
[ -n "$holder" ] && kill -9 "$holder" 2>/dev/null
sleep 1

rm -f "$PROFILE/SingletonLock" "$PROFILE/SingletonCookie" "$PROFILE/SingletonSocket"
rm -f "$PROFILE/Default/Last Session" "$PROFILE/Default/Last Tabs"
rm -f "$PROFILE/Default/Current Session" "$PROFILE/Default/Current Tabs"
exit 0
