#!/bin/sh
set -eu

# Railway volumes are mounted after the image is built, so their ownership can
# differ from /app/data in the image. Repair only this dedicated state path,
# then immediately drop privileges for the agent process.
if [ "$(id -u)" = "0" ]; then
    chown -R botuser:botuser /app/data
    exec gosu botuser "$@"
fi

exec "$@"
