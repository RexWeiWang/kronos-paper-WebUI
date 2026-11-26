#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
mkdir -p reports/logs
log="reports/logs/paper-$(date +%Y%m%d-%H%M%S).log"
{
  .venv/bin/python -u app.py fetch
  .venv/bin/python -u app.py paper
} 2>&1 | tee "$log"
open reports/paper/report.html
echo "完成：报告已打开。"
