#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then echo "usage: $0 batch-NNN" >&2; exit 2; fi
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_ROOT="$REPO_ROOT/data_history/annual_10k_fy2015_2025_v1"
BATCH_ID="$1"; MANIFEST="$DATA_ROOT/00_reference/batches/$BATCH_ID.csv"
WORK="$DATA_ROOT/02_work/$BATCH_ID"; RECEIPT="$DATA_ROOT/03_receipts/$BATCH_ID.json"
test -f "$MANIFEST"; test ! -e "$WORK"; mkdir -p "$WORK"
COUNT="$(($(wc -l < "$MANIFEST")-1))"
source "$REPO_ROOT/venv/bin/activate"
python "$REPO_ROOT/src/html_parser_v2.py" --manifest "$MANIFEST" --raw-root "$DATA_ROOT/01_raw/10k" --output-root "$WORK/parsed" --expected-documents "$COUNT"
python "$REPO_ROOT/src/section_splitter_10k_v2.py" --parsed-root "$WORK/parsed" --output-root "$WORK/sections" --expected-documents "$COUNT"
python "$REPO_ROOT/src/chunker_v2.py" --sections-root "$WORK/sections" --companies "$REPO_ROOT/data_v2/00_reference/approved_companies.csv" --profiles "$REPO_ROOT/config/chunk_profiles_v2_frozen.json" --profile A --output-root "$WORK/chunks" --progress-every 25
python "$REPO_ROOT/scripts/validate_fy2325_v2.py" --parsed-root "$WORK/parsed" --sections-root "$WORK/sections" --chunks-root "$WORK/chunks" > "$WORK/validation.json"
DATASET_SHA="$(awk '{print $1}' "$DATA_ROOT/00_reference/canonical_manifest.csv.sha256")"
CONFIG_SHA="$(sha256sum "$REPO_ROOT/config/chunk_profiles_v2_frozen.json" | awk '{print $1}')"
python "$REPO_ROOT/scripts/annual_history/load_batch.py" --batch-id "$BATCH_ID" --manifest "$MANIFEST" --parsed-root "$WORK/parsed" --sections-root "$WORK/sections" --chunks-root "$WORK/chunks" --dataset-manifest-sha "$DATASET_SHA" --chunker-config-sha "$CONFIG_SHA" --env-file "$REPO_ROOT/.env" | tee "$RECEIPT"
echo "PASS: $BATCH_ID committed. Review $RECEIPT, then remove only $WORK."
