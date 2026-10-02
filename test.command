#!/bin/sh
set -eu
gateway_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$gateway_root"
if [ -x "$gateway_root/.venv/bin/python" ]; then
  exec "$gateway_root/.venv/bin/python" "$gateway_root/scripts/bootstrap.py" test "$@"
fi
exec python3.12 "$gateway_root/scripts/bootstrap.py" test "$@"
