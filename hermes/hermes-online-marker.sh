#!/bin/sh
# Writes Hermes' planned-restart marker so the gateway broadcasts a
# "Gateway online" heartbeat to home channels on EVERY cold start
# (boot / reboot / crash recovery), not just planned restarts.
#
# Why: gateway/run.py only sends the "back online" home-channel message when
# .restart_pending.json exists, which is only written on a planned terminal/
# service restart. A launchd/KeepAlive cold start never writes it (GitHub
# issue #27870). This agent writes the marker at load so the gateway's own
# _send_home_channel_startup_notifications() path fires on startup.
#
# Update-safe: content is never parsed by the gateway (it only checks .exists()),
# but we write valid JSON in case a future version reads it.

set -e
HERMES_HOME="${HERMES_HOME:-/Users/YOUR_USER/.hermes}"
MARKER="$HERMES_HOME/.restart_pending.json"
printf '{"requested_at":%s,"via_service":true,"detached":false}\n' "$(date +%s)" > "$MARKER"
exit 0
