#!/bin/zsh
# 日期：2026-10-15；创建人：ScarfQian
set -euo pipefail
SCRIPT_DIR="${0:A:h}"
exec python3 -B "$SCRIPT_DIR/launcher.py" --gui "$@"
