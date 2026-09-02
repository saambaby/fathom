#!/usr/bin/env bash
# Fathom — Cloud Agent install script.
# Idempotent: safe to re-run against cached or partially prepared state.
set -euo pipefail

cd "$(dirname "$0")/.."

# The default image ships Python 3.12 but not the venv/ensurepip package, which
# `python3 -m venv` needs. Install it once (apt is idempotent).
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3-venv
fi

# Create the project virtualenv if it does not already exist.
if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
fi

# Install/refresh dependencies from the pinned pyproject (editable + dev extras).
./.venv/bin/python -m pip install --upgrade pip setuptools wheel
./.venv/bin/python -m pip install -e ".[dev]"

echo "Fathom install complete. Activate with: source .venv/bin/activate"
