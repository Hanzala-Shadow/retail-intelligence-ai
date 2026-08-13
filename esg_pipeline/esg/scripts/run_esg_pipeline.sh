#!/usr/bin/env bash
#
# Ubuntu runner for the ESG pipeline. Counterpart to
# run_docling_fusion_corpus.ps1, which assumes the Windows Python launcher and
# the venv/Scripts layout and therefore cannot run on the deployment server.
#
# Scope is deliberately stages 3-5 by default -- bridge, sections, chunks.
# Those are pure CPU Python on the light requirements.txt, which is what the
# transfer package installs. Stages 1-2 need docling and a CUDA torch build
# that the server is not expected to carry; pass --with-convert only on a
# machine that has venv-docling built.
#
# Usage
#   esg/scripts/run_esg_pipeline.sh
#   esg/scripts/run_esg_pipeline.sh --python .venv/bin/python
#   esg/scripts/run_esg_pipeline.sh --workers 8
#   esg/scripts/run_esg_pipeline.sh --with-convert --time-budget-min 600
#
set -euo pipefail

# Resolve the repository from this script's own location rather than $PWD, so
# it works from anywhere and needs no environment variable to find itself.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

PYTHON=""
PY_DOCLING=""
WORKERS="$(nproc 2>/dev/null || echo 4)"
TIME_BUDGET_MIN=600
WITH_CONVERT=0
WITH_OCR=0
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --python)           PYTHON="$2"; shift 2 ;;
        --python-docling)   PY_DOCLING="$2"; shift 2 ;;
        --workers)          WORKERS="$2"; shift 2 ;;
        --time-budget-min)  TIME_BUDGET_MIN="$2"; shift 2 ;;
        --with-convert)     WITH_CONVERT=1; shift ;;
        --with-ocr)         WITH_OCR=1; shift ;;
        --force)            FORCE=1; shift ;;
        -h|--help)          sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# venv/bin/python is the Linux layout; venv/Scripts/python.exe is checked too
# so this runs unchanged under Git Bash or WSL against a Windows-built venv.
if [[ -z "${PYTHON}" ]]; then
    for candidate in venv/bin/python .venv/bin/python venv/Scripts/python.exe python3; do
        if command -v "${candidate}" >/dev/null 2>&1 || [[ -x "${candidate}" ]]; then
            PYTHON="${candidate}"
            break
        fi
    done
fi
if [[ -z "${PYTHON}" ]]; then
    echo "no Python interpreter found; pass --python <path>" >&2
    exit 1
fi
if [[ -z "${PY_DOCLING}" ]]; then
    for candidate in venv-docling/bin/python venv-docling/Scripts/python.exe; do
        [[ -x "${candidate}" ]] && { PY_DOCLING="${candidate}"; break; }
    done
fi

# Every path comes from common/config.py, the single source of truth the
# PowerShell runner also reads. Nothing here hardcodes a directory.
PATHS_JSON="$("${PYTHON}" common/config.py --json)"
read_path() {
    printf '%s' "${PATHS_JSON}" | "${PYTHON}" -c \
        "import json,sys; print(json.load(sys.stdin)['absolute']['$1'])"
}

RAW_DIR="$(read_path RAW_SUSTAINABILITY_DIR)"
WORK_DIR="$(read_path SUSTAINABILITY_INTERIM_DIR)"
LAYOUT_DIR="$(read_path DOCLING_LAYOUT_DIR)"
FUSED_DIR="$(read_path DOCLING_FUSED_PAGES_DIR)"
FUSED_SUMMARY="$(read_path DOCLING_FUSED_SUMMARY_JSON)"
INTERIM_DIR="$(read_path ESG_TEXT_DIR)"
PARSE_INDEX_IN="$(read_path ESG_PARSE_INDEX_CSV)"
PARSE_INDEX_OUT="$(read_path ESG_PARSE_INDEX_V2_CSV)"
SECTIONS_DIR="$(read_path ESG_SECTIONS_DIR)"
SECTIONS_INDEX="$(read_path ESG_SECTIONS_INDEX_CSV)"
CHUNKS_DIR="$(read_path ESG_CHUNKS_DIR)"
CHUNKS_INDEX="$(read_path ESG_CHUNKS_INDEX_CSV)"

SPIKE="esg/scripts/run_docling_gold_spike.py"
BRIDGE="esg/scripts/bridge_docling_to_pipeline.py"
SPLITTER="esg/src/section_splitter_esg.py"
CHUNKER="esg/src/esg_chunker.py"

FORCE_FLAG=()
[[ "${FORCE}" -eq 1 ]] && FORCE_FLAG=(--force)

echo
echo "esg pipeline (linux runner)"
echo "  repo        : ${REPO_ROOT}"
echo "  python      : ${PYTHON} ($("${PYTHON}" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))'))"
echo "  workers     : ${WORKERS}"
echo "  bridge text : ${INTERIM_DIR}"
echo "  chunks index: ${CHUNKS_INDEX}"
echo

STARTED="$(date +%s)"

run_stage() {
    local name="$1"; shift
    printf '%s\n' "================================================================"
    printf '  %s\n' "${name}"
    printf '%s\n' "================================================================"
    local t0; t0="$(date +%s)"
    "$@"
    local mins; mins="$(awk -v a="$(date +%s)" -v b="${t0}" 'BEGIN{printf "%.1f",(a-b)/60}')"
    printf '  -> %s done in %s min\n\n' "${name}" "${mins}"
}

if [[ "${WITH_CONVERT}" -eq 1 ]]; then
    if [[ -z "${PY_DOCLING}" ]]; then
        echo "--with-convert needs the docling environment; none found" >&2
        exit 1
    fi
    # Mirrors the PowerShell runner: OCR is off unless asked for, because
    # docling re-reads an already-OCR'd page and emits a worse second copy.
    OCR_FLAG=(--no-ocr)
    [[ "${WITH_OCR}" -eq 1 ]] && OCR_FLAG=()
    run_stage "1/5 convert  (docling layout)" \
        "${PY_DOCLING}" "${SPIKE}" convert \
            --pdf-dir "${RAW_DIR}" --work-dir "${WORK_DIR}" --layout-dir "${LAYOUT_DIR}" \
            --all-pages --shards 1 --shard 0 --time-budget-min "${TIME_BUDGET_MIN}" \
            "${OCR_FLAG[@]}" "${FORCE_FLAG[@]}"

    # --table-mode grid rebuilds tables from docling's cell boxes. Without it
    # the stage silently reverts to flattened words and loses the row grid.
    run_stage "2/5 fuse     (regions + words -> page text)" \
        "${PY_DOCLING}" "${SPIKE}" fuse \
            --pdf-dir "${RAW_DIR}" --work-dir "${WORK_DIR}" --layout-dir "${LAYOUT_DIR}" \
            --fused-dir "${FUSED_DIR}" --fused-summary "${FUSED_SUMMARY}" \
            --table-mode grid "${FORCE_FLAG[@]}"
else
    echo "skipping stages 1-2 (pass --with-convert to run them)"
    echo
fi

[[ -f "${PARSE_INDEX_IN}" ]] || { echo "missing source identity index: ${PARSE_INDEX_IN}" >&2; exit 1; }
[[ -d "${RAW_DIR}" ]]        || { echo "missing raw PDF directory: ${RAW_DIR}" >&2; exit 1; }

run_stage "3/5 bridge   (-> pipeline layout + parse index)" \
    "${PYTHON}" "${BRIDGE}" \
        --work-dir "${WORK_DIR}" --layout-dir "${LAYOUT_DIR}" --fused-dir "${FUSED_DIR}" \
        --workers "${WORKERS}" --out "${INTERIM_DIR}" --raw-dir "${RAW_DIR}" \
        --parse-index-in "${PARSE_INDEX_IN}" --parse-index-out "${PARSE_INDEX_OUT}" \
        "${FORCE_FLAG[@]}"

run_stage "4/5 sections (topic split at docling headings)" \
    "${PYTHON}" "${SPLITTER}" \
        --input "${INTERIM_DIR}" --out "${SECTIONS_DIR}" --index "${SECTIONS_INDEX}" \
        --workers "${WORKERS}"

run_stage "5/5 chunks   (+ retrieval gating)" \
    "${PYTHON}" "${CHUNKER}" \
        --input "${SECTIONS_DIR}" --out "${CHUNKS_DIR}" --index "${CHUNKS_INDEX}" \
        --sections-index "${SECTIONS_INDEX}" --parse-index "${PARSE_INDEX_OUT}" \
        --workers "${WORKERS}"

printf '%s\n' "================================================================"
printf '  summary\n'
printf '%s\n' "================================================================"
"${PYTHON}" esg/scripts/summarise_fusion_run.py \
    --parse-index "${PARSE_INDEX_OUT}" --chunks-index "${CHUNKS_INDEX}"

ELAPSED="$(awk -v a="$(date +%s)" -v b="${STARTED}" 'BEGIN{printf "%.1f",(a-b)/60}')"
echo
echo "total elapsed: ${ELAPSED} min"
echo "chunks index : ${CHUNKS_INDEX}"
