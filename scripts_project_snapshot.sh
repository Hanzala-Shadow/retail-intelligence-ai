#!/usr/bin/env bash
set -u

# Layout comes from common/config.py so this snapshot does not fork it.
# Each constant lands in the shell as CFG_<NAME>, repo-relative.
#
# merged_path_constants(), not path_constants(): this snapshot counts files
# under BOTH pipelines, so it needs the union of the shared, ESG and 10-K
# tables -- exactly the set the single pre-split config.py exposed.
eval "$(python3 - <<'PY'
import sys
sys.path.insert(0, ".")
from common import config
for name, value in config.merged_path_constants()["relative"].items():
    print(f'CFG_{name}="{value}"')
PY
)"

OUT="$CFG_REPORTS_DIR/project_snapshot_$(date +%Y%m%d_%H%M%S).txt"
mkdir -p "$CFG_REPORTS_DIR"

{
  echo "============================================================"
  echo "RETAIL INTELLIGENCE PROJECT SNAPSHOT"
  echo "Generated at: $(date)"
  echo "Host: $(hostname)"
  echo "User: $(whoami)"
  echo "PWD: $(pwd)"
  echo "============================================================"
  echo

  echo "==================== GIT STATUS ===================="
  git branch --show-current 2>/dev/null || true
  git status --short 2>/dev/null || true
  echo
  echo "Latest commits:"
  git log --oneline -5 2>/dev/null || true
  echo

  echo "==================== TOP LEVEL FILES ===================="
  ls -lah
  echo

  echo "==================== DIRECTORY TREE DEPTH 3 ===================="
  find . -maxdepth 3 \
    -path "./.git" -prune -o \
    -path "./venv" -prune -o \
    -path "./$CFG_TABLES_DIR" -prune -o \
    -path "./tables" -prune -o \
    -path "./backups" -prune -o \
    -print | sort
  echo

  echo "==================== SOURCE FILES ===================="
  find common esg/src esg/scripts filings/src -maxdepth 2 -type f -name "*.py" | sort
  echo

  echo "==================== DATA COUNTS ===================="
  echo "10-K raw files:"
  find "$CFG_RAW_10K_DIR" -type f 2>/dev/null | wc -l

  echo "Sustainability raw PDFs:"
  find "$CFG_RAW_SUSTAINABILITY_DIR" -type f \( -iname "*.pdf" -o -iname "*.html" -o -iname "*.htm" \) 2>/dev/null | wc -l

  echo "HTML parsed 10-K text files:"
  find "$CFG_HTML_TEXT_DIR" -type f -name "*.txt" 2>/dev/null | wc -l

  echo "10-K section files:"
  find "$CFG_SECTIONS_10K_DIR" -type f -name "*.txt" 2>/dev/null | wc -l

  echo "Chunk files:"
  find "$CFG_CHUNKS_DIR" -type f 2>/dev/null | wc -l

  echo "PDF raw text files:"
  find "$CFG_ESG_TEXT_DIR" -type f -name "*.txt" 2>/dev/null | wc -l

  echo "HTML table CSV files:"
  find "$CFG_HTML_TABLE_DIR" -type f -name "*.csv" 2>/dev/null | wc -l

  echo "PDF table CSV files:"
  find "$CFG_PDF_TABLE_DIR" -type f -name "*.csv" 2>/dev/null | wc -l
  echo

  echo "==================== REFERENCE CSV STATUS ===================="
  for f in "$CFG_REFERENCE_DIR"/*.csv; do
    if [ -f "$f" ]; then
      echo "$f : $(wc -l < "$f") rows including header"
      echo "Header:"
      head -n 1 "$f"
      echo
    fi
  done
  echo

  echo "==================== SECTIONING QA ===================="
  if [ -f "$CFG_SECTIONS_INDEX_CSV" ]; then
    echo "sections_index.csv rows including header:"
    wc -l "$CFG_SECTIONS_INDEX_CSV"

    echo "FULL_DOCUMENT_FALLBACK count:"
    grep -c "FULL_DOCUMENT_FALLBACK" "$CFG_SECTIONS_INDEX_CSV" || true
  fi

  if [ -f "$CFG_CHUNKABLE_10K_SECTIONS_TXT" ]; then
    echo "Chunkable 10-K section list count:"
    wc -l "$CFG_CHUNKABLE_10K_SECTIONS_TXT"
  fi

  if [ -f "$CFG_FALLBACK_10K_SECTIONS_TXT" ]; then
    echo "Fallback 10-K section list count:"
    wc -l "$CFG_FALLBACK_10K_SECTIONS_TXT"
  fi

  if [ -f "$CFG_FALLBACK_10K_FILES_TXT" ]; then
    echo "Fallback 10-K filing count:"
    wc -l "$CFG_FALLBACK_10K_FILES_TXT"
    echo "Fallback companies count:"
    cut -d'_' -f1 "$CFG_FALLBACK_10K_FILES_TXT" | sort | uniq -c | sort -nr | head -40
  fi
  echo

  echo "==================== REPORTS FOLDER ===================="
  find "$CFG_REPORTS_DIR" -maxdepth 2 -type f -printf "%TY-%Tm-%Td %TH:%TM  %s bytes  %p\n" 2>/dev/null | sort -r | head -80
  echo

  echo "==================== LOGS FOLDER ===================="
  find "$CFG_LOGS_DIR" -maxdepth 2 -type f -printf "%TY-%Tm-%Td %TH:%TM  %s bytes  %p\n" 2>/dev/null | sort -r | head -80
  echo

  echo "==================== RECENT LOG TAILS ===================="
  for log in "$CFG_LOGS_DIR"/html_parser_rerun.log "$CFG_LOGS_DIR"/section_splitter_rerun.log "$CFG_LOGS_DIR"/section_splitter_rerun_v2.log "$CFG_LOGS_DIR"/parse_errors.log nohup.out; do
    if [ -f "$log" ]; then
      echo
      echo "----- $log last 40 lines -----"
      tail -n 40 "$log"
    fi
  done
  echo

  echo "==================== PYTHON ENV ===================="
  if [ -d venv ]; then
    source venv/bin/activate 2>/dev/null || true
  fi
  which python 2>/dev/null || true
  python --version 2>/dev/null || true
  pip freeze 2>/dev/null | sed -n '1,120p'
  echo

  echo "==================== DISK / MEMORY ===================="
  df -h
  echo
  free -h
  echo

  echo "==================== RUNNING PROJECT PROCESSES ===================="
  ps -ef | grep -E "python|tmux|nohup|parser|splitter|chunk|loader" | grep -v grep || true
  echo

  echo "==================== MANUAL NOTES PLACEHOLDER ===================="
  echo "Paste below this report any manual verification notes, current task, errors, or teammate instructions."
  echo "============================================================"

} | tee "$OUT"

echo
echo "Snapshot written to: $OUT"
