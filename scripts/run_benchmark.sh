#!/bin/bash
# Run benchmark with dry-run backend and save report
set -e
cd "$(dirname "$0")/.."

echo "Running benchmark (10 iterations, dry-run backend)..."
uv run ariadne benchmark-run --iterations 10 --backend dry-run > benchmark_report.json
echo "Report saved to benchmark_report.json"
cat benchmark_report.json
