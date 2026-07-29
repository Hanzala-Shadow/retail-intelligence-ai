"""Run the ESG VLM stages: page classification and table extraction.

    python esg/scripts/run_esg_vlm.py classify --transport sync|batch [--wait] [--limit N]
    python esg/scripts/run_esg_vlm.py extract  --transport sync|batch [--wait] [--limit N]
    python esg/scripts/run_esg_vlm.py collect            # collect any finished batches
    python esg/scripts/run_esg_vlm.py status

Targets:
  classify: every page with decision == auto_pass_column_order_reconstructed
            (OCR-lineage docs excluded). Verdicts -> data/04_vlm/classifier/{key}.json
  extract : held structural_grid pages + classifier-flagged table_dominant pages.
            Artifacts -> data/04_vlm/extraction/{key}.md + {key}.meta.json

Both stages are cached by (source sha, page, model snapshot, prompt hash): a page already
carrying a current artifact is never re-submitted. Budget cap refuses over-spend
(--budget, default $30). The API key comes only from OPENAI_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402
import esg_vlm_stage as vlm  # noqa: E402

DATA = config.VLM_DIR
RENDERS = DATA / "renders"
QA_PATH = config.ESG_PAGE_LAYOUT_QA_CSV


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / "run.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_qa() -> pd.DataFrame:
    qa = pd.read_csv(QA_PATH)
    qa["page"] = qa["page"].astype(int)
    return qa[~qa["pdf_stem"].map(vlm.is_ocr_lineage)]


def targets_classify(qa: pd.DataFrame) -> pd.DataFrame:
    return qa[qa["decision"] == "auto_pass_column_order_reconstructed"]


def targets_extract(qa: pd.DataFrame) -> pd.DataFrame:
    held = qa[(qa["decision"] == "auto_hold")
              & (qa["reading_order_reason"] == "structural_grid_or_table_layout")]
    flagged_keys = set()
    cls_dir = DATA / "classifier"
    if cls_dir.exists():
        for f in cls_dir.glob("*.json"):
            try:
                if json.loads(f.read_text(encoding="utf-8"))["verdict"]["decision_class"] \
                        == "table_dominant":
                    flagged_keys.add(f.stem)
            except Exception:  # noqa: BLE001 — malformed verdict never crashes targeting
                continue
    frame = qa[qa["decision"] == "auto_pass_column_order_reconstructed"]
    frame_keys = frame.apply(
        lambda r: vlm.page_key(r["ticker"], r["pdf_stem"], r["page"]), axis=1)
    flagged = frame[frame_keys.isin(flagged_keys)]
    return pd.concat([held, flagged], ignore_index=True)


def render_path(row: pd.Series) -> Path:
    return RENDERS / f"{vlm.page_key(row['ticker'], row['pdf_stem'], row['page'])}.png"


def ensure_renders_bulk(rows: list) -> None:
    """Render all missing pages grouped by source PDF, keeping each document open
    across its pages (~5x faster than per-page opening on multi-page reports)."""
    import pypdfium2 as pdfium
    import time as _time
    RENDERS.mkdir(parents=True, exist_ok=True)
    by_src: dict[str, list] = {}
    for row in rows:
        out = render_path(row)
        if not out.exists():
            by_src.setdefault(row["source_pdf"], []).append((int(row["page"]), out))
    total = sum(len(v) for v in by_src.values())
    if not total:
        return
    log(f"rendering {total} missing pages across {len(by_src)} documents...")
    done, t0 = 0, _time.time()
    for src, pages in sorted(by_src.items()):
        pdf = pdfium.PdfDocument(str(ROOT / src))
        try:
            for page, out in sorted(pages):
                pdf[page - 1].render(scale=vlm.RENDER_SCALE).to_pil().save(out)
                done += 1
                if done % 500 == 0:
                    log(f"rendered {done}/{total} ({_time.time()-t0:.0f}s)")
        finally:
            pdf.close()
    log(f"render complete: {done} pages in {_time.time()-t0:.0f}s")


def artifact_current(key: str, ck: str, stage: str) -> bool:
    """True iff an artifact exists for `key` whose recorded cache_key matches `ck`."""
    meta = DATA / stage / (f"{key}.json" if stage == "classifier" else f"{key}.meta.json")
    if not meta.exists():
        return False
    try:
        return json.loads(meta.read_text(encoding="utf-8")).get("cache_key") == ck
    except Exception:  # noqa: BLE001
        return False


def write_artifact(stage: str, key: str, row: pd.Series, cfg: vlm.StageConfig,
                   ck: str, content: str | None, usage: dict) -> None:
    out_dir = DATA / stage
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "key": key, "ticker": row["ticker"], "pdf_stem": row["pdf_stem"],
        "page": int(row["page"]), "source_pdf": row["source_pdf"],
        "source_sha256": row["source_sha256"], "model": cfg.model,
        "prompt_hash": cfg.instr_hash, "cache_key": ck,
        "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "usage": usage, "lineage": f"vlm_{stage}_v1",
    }
    if content is None:
        meta["error"] = usage.get("error", "no content")
        (out_dir / f"{key}.meta.json").write_text(json.dumps(meta, indent=2),
                                                  encoding="utf-8")
        return
    if stage == "classifier":
        try:
            meta["verdict"] = json.loads(content)
        except Exception:  # noqa: BLE001
            meta["verdict"] = {"decision_class": "", "reason": "unparseable"}
            meta["raw"] = content[:500]
        (out_dir / f"{key}.json").write_text(json.dumps(meta, indent=2),
                                             encoding="utf-8")
        return
    (out_dir / f"{key}.md").write_text(content, encoding="utf-8")
    try:
        import pdfplumber
        with pdfplumber.open(str(ROOT / row["source_pdf"])) as pdf:
            words = [w["text"] for w in pdf.pages[int(row["page"]) - 1].extract_words()]
        meta["screen"] = vlm.screen_annotation(content, words)
    except Exception as exc:  # noqa: BLE001 — annotation failure never blocks artifact
        meta["screen"] = {"error": repr(exc)}
    (out_dir / f"{key}.meta.json").write_text(json.dumps(meta, indent=2),
                                              encoding="utf-8")


def run_stage(stage: str, cfg: vlm.StageConfig, sel: pd.DataFrame, args) -> None:
    todo = []
    for _, row in sel.iterrows():
        key = vlm.page_key(row["ticker"], row["pdf_stem"], row["page"])
        ck = vlm.cache_key(row["source_sha256"], int(row["page"]), cfg)
        if artifact_current(key, ck, stage):
            continue
        todo.append((key, ck, row))
    n_uncached = len(todo)
    if args.limit:
        todo = todo[: args.limit]
    est = vlm.estimate_cost_usd(len(todo), cfg, args.transport)
    log(f"{stage}: {len(sel)} targets, {len(sel) - n_uncached} cached, "
        f"{n_uncached} uncached, {len(todo)} this run; est cost "
        f"${est:.2f} via {args.transport} (budget ${args.budget:.2f})")
    if not todo:
        return
    if est > args.budget:
        log(f"{stage}: REFUSED — estimate exceeds budget. Raise --budget to proceed.")
        sys.exit(2)

    rows_by_key = {k: r for k, _, r in todo}
    cks_by_key = {k: c for k, c, _ in todo}

    def on_result(key: str, content: str | None, usage: dict) -> None:
        write_artifact(stage, key, rows_by_key[key], cfg, cks_by_key[key],
                       content, usage)

    ensure_renders_bulk([row for _, _, row in todo])
    items = []
    for key, _, row in todo:
        items.append((key, vlm.request_body(cfg, render_path(row).read_bytes())))

    if args.transport == "sync":
        vlm.run_sync(items, on_result, log=log)
    else:
        vlm.batch_submit(items, DATA / f"batch_state_{stage}.json", stage, log=log)
        if args.wait:
            while not vlm.batch_collect(DATA / f"batch_state_{stage}.json",
                                        on_result, log=log):
                log("waiting 180s for batches...")
                time.sleep(180)
        else:
            log(f"{stage}: batches submitted; run 'collect' later to fetch results.")
    log(f"{stage}: done for this invocation.")


def cmd_collect() -> None:
    qa = load_qa()
    for stage, cfg, sel in [("classifier", vlm.CLASSIFIER_CONFIG, targets_classify(qa)),
                            ("extraction", vlm.EXTRACTION_CONFIG, targets_extract(qa))]:
        rows_by_key = {vlm.page_key(r["ticker"], r["pdf_stem"], r["page"]): r
                       for _, r in sel.iterrows()}
        cfg_local = cfg

        def on_result(key: str, content: str | None, usage: dict,
                      _stage=stage, _cfg=cfg_local, _rows=rows_by_key) -> None:
            row = _rows.get(key)
            if row is None:
                return
            ck = vlm.cache_key(row["source_sha256"], int(row["page"]), _cfg)
            write_artifact(_stage, key, row, _cfg, ck, content, usage)

        state = DATA / f"batch_state_{stage}.json"
        if state.exists():
            done = vlm.batch_collect(state, on_result, log=log)
            log(f"{stage}: batch collection {'complete' if done else 'still pending'}")


def cmd_status() -> None:
    for stage in ["classifier", "extraction"]:
        d = DATA / stage
        n = len(list(d.glob("*.json" if stage == "classifier" else "*.md"))) if d.exists() else 0
        print(f"{stage}: {n} artifacts")
        state = DATA / f"batch_state_{stage}.json"
        if state.exists():
            st = json.loads(state.read_text())
            for name, info in st["batches"].items():
                print(f"  {name}: n={info['n']} collected={info['collected']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["classify", "extract", "collect", "status"])
    ap.add_argument("--transport", choices=["sync", "batch"], default="sync")
    ap.add_argument("--wait", action="store_true",
                    help="batch mode: poll until results are collected")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--budget", type=float, default=vlm.DEFAULT_BUDGET_USD)
    args = ap.parse_args()

    if args.command == "status":
        cmd_status()
        return
    if args.command == "collect":
        cmd_collect()
        return
    qa = load_qa()
    if args.command == "classify":
        run_stage("classifier", vlm.CLASSIFIER_CONFIG, targets_classify(qa), args)
    else:
        run_stage("extraction", vlm.EXTRACTION_CONFIG, targets_extract(qa), args)


if __name__ == "__main__":
    main()
