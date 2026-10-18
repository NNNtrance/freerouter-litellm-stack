#!/bin/sh
# Hermes gateway launcher wrapper.
#
# Writes Hermes' planned-restart marker (.restart_pending.json) BEFORE launching
# the gateway, so the gateway's own "_send_home_channel_startup_notifications()"
# path broadcasts a "Gateway online" heartbeat to home channels on EVERY cold
# start (boot, reboot, crash recovery via KeepAlive, or manual restart) — not
# just planned/restarts. See GitHub issue #27870.
#
# The marker content is never parsed by the gateway (it only checks .exists()),
# but valid JSON is written in case a future version reads it.
#
# Uses the version-independent /opt/homebrew/bin/hermes symlink so this survives
# `hermes update` (the cellar path changes per version).

HERMES_HOME="${HERMES_HOME:-/Users/YOUR_USER/.hermes}"
: "${HERMES_BIN:=/opt/homebrew/bin/hermes}"

printf '{"requested_at":%s,"via_service":true,"detached":false}\n' "$(date +%s)" > "$HERMES_HOME/.restart_pending.json"

exec "$HERMES_BIN" gateway run --replace
