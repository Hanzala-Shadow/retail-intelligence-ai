#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 1 || ! "$1" =~ ^pass[12]$ ]]; then
  echo "Usage: $0 pass1|pass2" >&2
  exit 2
fi

PASS_NAME="$1"
REPO="/home/ubuntu/projects/retail-intelligence-production-retrieval"
VENV_PYTHON="/home/ubuntu/projects/retail-intelligence-ai/venv/bin/python"
REWRITE_ROOT="$REPO/reports/week4_retrieval/claim_requirement_coverage_20260721"
OUTPUT_DIR="$REWRITE_ROOT/$PASS_NAME"
LOG_FILE="$REWRITE_ROOT/$PASS_NAME.log"
TEMP_HOME=""

cd "$REPO"

if [[ -e "$OUTPUT_DIR" || -e "$LOG_FILE" ]]; then
  echo "Refusing existing output for $PASS_NAME: $OUTPUT_DIR or $LOG_FILE" >&2
  exit 3
fi

mkdir -p "$REWRITE_ROOT"

HOME_ORIGINAL_MODE="$(stat -c '%a' /home/ubuntu)"
CACHE_ORIGINAL_MODE="$(stat -c '%a' /home/ubuntu/.cache)"
ROOT_ORIGINAL_MODE="$(stat -c '%a' "$REWRITE_ROOT")"

cleanup() {
  local status=$?
  trap - EXIT INT TERM

  sudo chmod "$HOME_ORIGINAL_MODE" /home/ubuntu || true
  sudo chmod "$CACHE_ORIGINAL_MODE" /home/ubuntu/.cache || true
  chmod "$ROOT_ORIGINAL_MODE" "$REWRITE_ROOT" || true

  if [[ -d "$OUTPUT_DIR" ]]; then
    sudo chown -R ubuntu:ubuntu "$OUTPUT_DIR" || true
  fi
  if [[ -f "$LOG_FILE" ]]; then
    sudo chown ubuntu:ubuntu "$LOG_FILE" || true
  fi
  if [[ -n "$TEMP_HOME" && -d "$TEMP_HOME" && "$TEMP_HOME" == /tmp/postgres_rewrite_home.* ]]; then
    sudo rm -rf -- "$TEMP_HOME" || true
  fi

  echo "RUN_STATUS=$status"
  echo "Restored /home/ubuntu mode to $HOME_ORIGINAL_MODE"
  echo "Restored /home/ubuntu/.cache mode to $CACHE_ORIGINAL_MODE"
  echo "Restored rewrite root mode to $ROOT_ORIGINAL_MODE"
  exit "$status"
}
trap cleanup EXIT INT TERM

TEMP_HOME="$(mktemp -d /tmp/postgres_rewrite_home.XXXXXX)"
sudo chown postgres:postgres "$TEMP_HOME"
sudo chmod 700 "$TEMP_HOME"
sudo -u postgres mkdir -m 700 "$TEMP_HOME/huggingface"

sudo chmod o+x /home/ubuntu
sudo chmod o+x /home/ubuntu/.cache
chmod 777 "$REWRITE_ROOT"

set -o pipefail
sudo -u postgres env \
  PGDATABASE=retail_pipeline \
  PGOPTIONS="-c default_transaction_read_only=on" \
  HOME="$TEMP_HOME" \
  HF_HOME="$TEMP_HOME/huggingface" \
  HF_HUB_CACHE=/home/ubuntu/.cache/huggingface/hub \
  SENTENCE_TRANSFORMERS_HOME=/home/ubuntu/.cache/huggingface/hub \
  HF_HUB_DISABLE_IMPLICIT_TOKEN=1 \
  HF_HUB_OFFLINE=1 \
  TRANSFORMERS_OFFLINE=1 \
  /usr/bin/time -v \
  "$VENV_PYTHON" \
  scripts/run_source_claim_rewriting_benchmark.py \
  --config config/retrieval_rewrite_requirements_v1.json \
  --output-dir "$OUTPUT_DIR" \
  2>&1 | tee "$LOG_FILE"
