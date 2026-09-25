#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
REPO_ROOT=${SCRIPT_DIR:h:h}
CURRENT_PYTHON="$HOME/Library/Application Support/Image23MF Studio/current/venv/bin/python"

if [[ -x "$CURRENT_PYTHON" ]]; then
  exec "$CURRENT_PYTHON" -m image23mf.macos.install uninstall "$@"
fi

exec env PYTHONPATH="$REPO_ROOT/src" python3 -m image23mf.macos.install uninstall "$@"
