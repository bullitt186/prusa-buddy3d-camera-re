#!/bin/bash
# Runtime launcher for the Buddy3D appliance (AC-13 / WP-R4c).
#
# The runtime units (prusa-cam.service, prusa-rtsp.service, prusa-admin.service)
# all start through this script. It chooses *what to execute* and nothing else:
# no secrets, no identity, no state. Given an optional relative script argument
# (default main.py) it resolves:
#
#   1. an installed signed release under DATA, when it is complete:
#        $RELEASES/current/venv/bin/python  +  $RELEASES/current/<script>
#   2. otherwise the immutable factory application:
#        $APP_ROOT/venv/bin/python          +  $APP_ROOT/<script>
#
# A half-present release — a dangling `current` symlink, or a release that is
# missing its per-release venv or the requested script — must never stop the
# appliance: in that case the factory app runs instead. The updater (WP-R4b)
# owns atomically swapping the `current` symlink; this launcher only reads it.
#
# Usage: launcher.sh [relative-script] [args...]
set -eu

APP_ROOT=/opt/prusa-cam
RELEASES=/data/prusa-cam/releases
DEFAULT_SCRIPT=main.py

if [ $# -gt 0 ]; then
   script="$1"
   shift
else
   script="$DEFAULT_SCRIPT"
fi

release_python="$RELEASES/current/venv/bin/python"
release_script="$RELEASES/current/$script"
factory_python="$APP_ROOT/venv/bin/python"
factory_script="$APP_ROOT/$script"

# Prefer the active release only when it is fully present (executable
# interpreter and a regular entry-point file). `-x`/`-f` follow the `current`
# symlink and are false for a dangling link, so a broken release falls through
# to the factory app rather than failing to start.
if [ -x "$release_python" ] && [ -f "$release_script" ]; then
   exec "$release_python" "$release_script" "$@"
fi

exec "$factory_python" "$factory_script" "$@"
