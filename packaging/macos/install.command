#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
REPO_ROOT=${SCRIPT_DIR:h:h}
BUILD_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/image23mf-build.XXXXXX")
trap 'rm -rf "$BUILD_ROOT"' EXIT

command -v python3 >/dev/null || { print -u2 "Python 3 is required."; exit 1; }
command -v npm >/dev/null || { print -u2 "Node.js/npm is required."; exit 1; }

python3 -m venv "$BUILD_ROOT/build-venv"
"$BUILD_ROOT/build-venv/bin/python" -m pip install --disable-pip-version-check build

cd "$REPO_ROOT/frontend"
npm ci
npm run build

cd "$REPO_ROOT"
"$BUILD_ROOT/build-venv/bin/python" -m build --wheel --outdir "$BUILD_ROOT/dist"
WHEELS=("$BUILD_ROOT"/dist/*.whl(N))
if (( ${#WHEELS} != 1 )); then
  print -u2 "Expected exactly one wheel, found ${#WHEELS}."
  exit 1
fi
BASE_VERSION=$(PYTHONPATH="$REPO_ROOT/src" python3 -c 'from image23mf import __version__; print(__version__)')
COMMIT=$(git -C "$REPO_ROOT" rev-parse --short=10 HEAD 2>/dev/null || print "source")
VERSION="${BASE_VERSION}+local.$(date -u +%Y%m%d%H%M%S).${COMMIT}"

PYTHONPATH="$REPO_ROOT/src" python3 -m image23mf.macos.install \
  install \
  --wheel "$WHEELS[1]" \
  --frontend "$REPO_ROOT/frontend/dist" \
  --release-version "$VERSION" \
  "$@"
