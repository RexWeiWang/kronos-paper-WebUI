#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
.venv/bin/python -u app.py backtest --days 63
open reports/backtest/report.html
