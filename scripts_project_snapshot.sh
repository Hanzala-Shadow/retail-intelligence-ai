#!/usr/bin/env bash
set -u

OUT="reports/project_snapshot_$(date +%Y%m%d_%H%M%S).txt"
mkdir -p reports

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
    -path "./data/tables" -prune -o \
    -path "./tables" -prune -o \
    -path "./backups" -prune -o \
    -print | sort
  echo

  echo "==================== SRC FILES ===================="
  find src -maxdepth 2 -type f | sort
  echo

  echo "==================== DATA COUNTS ===================="
  echo "10-K raw files:"
  find data/01_raw/10k -type f 2>/dev/null | wc -l

  echo "Sustainability raw PDFs:"
  find data/01_raw/sustainability -type f \( -iname "*.pdf" -o -iname "*.html" -o -iname "*.htm" \) 2>/dev/null | wc -l

  echo "HTML parsed 10-K text files:"
  find data/02_interim/html_text -type f -name "*.txt" 2>/dev/null | wc -l

  echo "10-K section files:"
  find data/03_sections/10k -type f -name "*.txt" 2>/dev/null | wc -l

  echo "Chunk files:"
  find data/04_chunks -type f 2>/dev/null | wc -l

  echo "PDF raw text files:"
  find data/raw_text/pdf_text -type f -name "*.txt" 2>/dev/null | wc -l

  echo "HTML table CSV files:"
  find data/tables/html_table -type f -name "*.csv" 2>/dev/null | wc -l

  echo "PDF table CSV files:"
  find data/tables/pdf_table -type f -name "*.csv" 2>/dev/null | wc -l
  echo

  echo "==================== REFERENCE CSV STATUS ===================="
  for f in data/00_reference/*.csv; do
    if [ -f "$f" ]; then
      echo "$f : $(wc -l < "$f") rows including header"
      echo "Header:"
      head -n 1 "$f"
      echo
    fi
  done
  echo

  echo "==================== SECTIONING QA ===================="
  if [ -f data/00_reference/sections_index.csv ]; then
    echo "sections_index.csv rows including header:"
    wc -l data/00_reference/sections_index.csv

    echo "FULL_DOCUMENT_FALLBACK count:"
    grep -c "FULL_DOCUMENT_FALLBACK" data/00_reference/sections_index.csv || true
  fi

  if [ -f reports/chunkable_10k_sections.txt ]; then
    echo "Chunkable 10-K section list count:"
    wc -l reports/chunkable_10k_sections.txt
  fi

  if [ -f reports/fallback_10k_sections.txt ]; then
    echo "Fallback 10-K section list count:"
    wc -l reports/fallback_10k_sections.txt
  fi

  if [ -f reports/fallback_10k_files.txt ]; then
    echo "Fallback 10-K filing count:"
    wc -l reports/fallback_10k_files.txt
    echo "Fallback companies count:"
    cut -d'_' -f1 reports/fallback_10k_files.txt | sort | uniq -c | sort -nr | head -40
  fi
  echo

  echo "==================== REPORTS FOLDER ===================="
  find reports -maxdepth 2 -type f -printf "%TY-%Tm-%Td %TH:%TM  %s bytes  %p\n" 2>/dev/null | sort -r | head -80
  echo

  echo "==================== LOGS FOLDER ===================="
  find logs -maxdepth 2 -type f -printf "%TY-%Tm-%Td %TH:%TM  %s bytes  %p\n" 2>/dev/null | sort -r | head -80
  echo

  echo "==================== RECENT LOG TAILS ===================="
  for log in logs/html_parser_rerun.log logs/section_splitter_rerun.log logs/section_splitter_rerun_v2.log logs/parse_errors.log nohup.out; do
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
