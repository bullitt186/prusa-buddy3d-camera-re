#!/bin/bash
# Factory fallback launcher for the Buddy3D appliance (AC-13).
#
# WP-2 installs this and the image validator asserts its presence. Wiring the
# runtime units to run through it (and using a per-release venv) is WP-6; today
# it execs the immutable factory application, and would prefer an active release
# under DATA if one exists.
#
# No secrets, no identity: this only chooses which main.py to execute.
set -eu

APP_ROOT=/opt/prusa-cam
RELEASES=/data/prusa-cam/releases
FACTORY_MAIN="$APP_ROOT/main.py"

main="$FACTORY_MAIN"
if [ -d "$RELEASES/current" ] && [ -f "$RELEASES/current/main.py" ]; then
   main="$RELEASES/current/main.py"
fi

exec "$APP_ROOT/venv/bin/python" "$main" "$@"
