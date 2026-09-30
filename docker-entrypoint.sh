#!/bin/sh
# Run pending migrations (api container only), then exec the real CMD
# (uvicorn / celery worker / celery beat). Fail fast if migrations fail.
# Worker/beat set RUN_MIGRATIONS=0 so only one process migrates.
set -e

cd /app

# The persistent browser-profile volume can come up root-owned if it was created before
# the image pre-created the path (see Dockerfile), which makes every case die on
# "Permission denied". The image fix only applies to NEW volumes, so verify here and say
# exactly how to repair an old one instead of failing deep inside Chromium.
for d in "${PROFILE_DIR:-/app/profiles}"; do
    if [ -d "$d" ] && [ ! -w "$d" ]; then
        echo "[entrypoint] WARNING: $d is not writable by $(id -un) ($(id -u)):" >&2
        echo "[entrypoint]   fix once with: docker compose run --rm --user root worker chown -R $(id -u):$(id -g) $d" >&2
    fi
done

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
    echo "[entrypoint] alembic upgrade head"
    alembic upgrade head
fi

exec "$@"
