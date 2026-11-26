#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
.venv/bin/python -u app.py fetch
.venv/bin/python -u app.py predict --symbol 510300
open reports/single_prediction.csv
