#!/bin/bash
# Kill stale Chrome processes using grok_cloak profile (not this script)
pids=$(pgrep -f "chrome.*grok_cloak" 2>/dev/null)
if [ -n "$pids" ]; then
    echo "$pids" | xargs kill -9 2>/dev/null
    sleep 1
fi
rm -f "$HOME/.nova/grok_cloak/SingletonLock"
rm -f "$HOME/.nova/grok_cloak/Default/Last Session"
rm -f "$HOME/.nova/grok_cloak/Default/Last Tabs"
rm -f "$HOME/.nova/grok_cloak/Default/Current Session"
rm -f "$HOME/.nova/grok_cloak/Default/Current Tabs"
exit 0
