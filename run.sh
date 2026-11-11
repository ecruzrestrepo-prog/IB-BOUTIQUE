#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "Se requiere Python 3.12 o posterior.")'
  python3 -m venv .venv
fi
.venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "Se requiere Python 3.12 o posterior. Recrea .venv con esa version.")'
.venv/bin/python -m pip install -r requirements.txt
exec .venv/bin/python app.py
