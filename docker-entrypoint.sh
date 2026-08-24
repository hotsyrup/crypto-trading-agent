#!/bin/sh
set -eu

# Railway volumes are mounted after the image is built, so their ownership can
# differ from /app/data in the image. Repair only this dedicated state path,
# then immediately drop privileges for the agent process.
if [ "$(id -u)" = "0" ]; then
    runtime_user="${APP_RUNTIME_USER:-botuser}"
    chown -R "$runtime_user:$runtime_user" /app/data
    exec gosu "$runtime_user" "$@"
fi

exec "$@"
