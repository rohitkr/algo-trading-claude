#!/usr/bin/env bash
# Daily login: ICICI Breeze, then Zerodha. Extra flags go to `python3 -m login` (see --help).
set -euo pipefail
cd "$(dirname "$0")"
PY=python3
[ -x venv/bin/python3 ] && PY=venv/bin/python3
exec "$PY" -m login "$@"
