#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
exec .venv/bin/python gui_control.py
