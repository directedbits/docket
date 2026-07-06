#!/bin/sh
set -e

# The data dir is a mounted volume whose ownership we can't predict (a fresh named
# volume comes up root-owned; a bind mount keeps the host's owner). If we start as
# root, take ownership of it and drop to the unprivileged user; if we're already
# non-root, just run.
DATA_DIR="$(dirname "${DB_PATH:-/data/docs.db}")"
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    chown -R appuser "$DATA_DIR" 2>/dev/null || true
    exec gosu appuser "$@"
fi
exec "$@"
