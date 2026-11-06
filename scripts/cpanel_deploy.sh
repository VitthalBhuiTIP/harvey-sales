#!/bin/bash
# Called by .cpanel.yml after cPanel pulls the repo: builds/updates the venv
# that passenger_wsgi.py runs under, installs requirements, restarts the app.
# Output goes to ~/.cpanel/logs/vc_*_git_deploy.log.
set -euo pipefail

APPDIR=/home/theinsightinc/public_html/harvey-sales
VENV=/home/theinsightinc/venvs/harvey-sales

ok_python() {  # Harvey needs Python 3.11+
  [ -x "$1" ] && "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null
}

if ! ok_python "$VENV/bin/python"; then
  PY=""
  for p in /opt/alt/python313/bin/python3 /opt/alt/python312/bin/python3 /opt/alt/python311/bin/python3 \
           /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
           /usr/bin/python3.13 /usr/bin/python3.12 /usr/bin/python3.11 /usr/bin/python3; do
    if ok_python "$p"; then PY="$p"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "ERROR: no Python 3.11+ found. Create a 3.11/3.12 app in Setup Python App first." >&2
    exit 1
  fi
  echo "Creating venv with $PY ($("$PY" -V))"
  rm -rf "$VENV"
  mkdir -p "$(dirname "$VENV")"
  "$PY" -m venv "$VENV"
fi

"$VENV/bin/python" -V
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$APPDIR/requirements.txt"

mkdir -p "$APPDIR/tmp" "$APPDIR/data"
chmod 700 "$APPDIR/data"
[ -f "$APPDIR/.env" ] && chmod 600 "$APPDIR/.env"
touch "$APPDIR/tmp/restart.txt"
echo "Deployed $(git -C "$APPDIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
