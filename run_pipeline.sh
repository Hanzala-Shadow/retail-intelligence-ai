#!/usr/bin/env bash
# run_pipeline.sh — Ordered Week 1 pipeline runner for the
# Retail Intelligence Pipeline. Default: 3-company end-to-end test.
#
# Usage:
#   ./run_pipeline.sh          # 3-company test (default)
#   ./run_pipeline.sh 10       # override company limit
#   ./run_pipeline.sh full     # full run (all companies, all steps)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

LIMIT="${1:-3}"
# Layout comes from src/config.py so this runner does not fork it.
LOG_DIR="$(python3 -c 'import sys; sys.path.insert(0, "src"); import config; print(config.LOGS_DIR)')"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/run_pipeline_$(date +%Y%m%d_%H%M).log"

log() {
    echo "[$(date '+%H:%M:%S')] $*" | tee -a "$RUN_LOG"
}

fail() {
    log "PIPELINE FAILED at step: $1"
    exit 1
}

if [ ! -d "venv" ]; then
    fail "venv not found — run from repo root with venv/ present"
fi
source venv/bin/activate

log "===== Retail Intelligence Pipeline run starting (limit=$LIMIT) ====="

log "Step 0/5: Validating config and .env"
python3 src/config.py >> "$RUN_LOG" 2>&1 || fail "config validation"

if [ "$LIMIT" = "full" ]; then
    LIMIT_FLAG=""
    log "Running FULL pipeline (no company limit) — this processes all companies."
else
    LIMIT_FLAG="--limit $LIMIT"
    log "Running LIMITED pipeline ($LIMIT companies) — end-to-end test mode."
fi

log "Step 1/5: SEC discovery (sec_discovery.py $LIMIT_FLAG)"
python3 src/sec_discovery.py $LIMIT_FLAG >> "$RUN_LOG" 2>&1 || fail "sec_discovery.py"

log "Step 2/5: SEC download + Drive upload (sec_downloader.py $LIMIT_FLAG)"
python3 src/sec_downloader.py $LIMIT_FLAG >> "$RUN_LOG" 2>&1 || fail "sec_downloader.py"

# Input/output roots come from src/config.py. Passing them here again would
# fork the layout into a second place, so only run-shape flags are set.
log "Step 3/5: HTML parsing of downloaded 10-Ks (html_parser.py)"
python3 src/html_parser.py \
    --num-companies "$([ "$LIMIT" = "full" ] && echo 999 || echo "$LIMIT")" \
    >> "$RUN_LOG" 2>&1 || fail "html_parser.py"

log "Step 4/5: PDF parsing of sustainability reports (pdf_parser.py)"
python3 src/pdf_parser.py \
    --num-companies "$([ "$LIMIT" = "full" ] && echo 999 || echo "$LIMIT")" \
    --workers 2 \
    >> "$RUN_LOG" 2>&1 || fail "pdf_parser.py"

log "Step 5/5: DB metadata load (db_loader.py — seeds companies + filings)"
python3 src/db_loader.py >> "$RUN_LOG" 2>&1 || fail "db_loader.py"

log "===== Pipeline run complete. Full log: $RUN_LOG ====="

echo ""
echo "Quick DB health check:"
python3 -c "
import sys
sys.path.insert(0, 'src')
import db_utils
db_utils.connect()
health = db_utils.db_health_check()
for k, v in health.items():
    print(f'  {k}: {v}')
"
