"""Build a diverse hand-validation chunk sample for Melek.

Why this exists
---------------
A reparse is now unavoidable: the Drive sync of 2026-07-29 renamed or removed
documents, so the corpus must be rebuilt. The last reparse churned ~13.6% of
chunk IDs. Anything validated against a chunk_id alone would be thrown away by
that rebuild.

So every row carries `chunk_text_sha256`. After the reparse, any chunk whose
text is unchanged carries her judgement forward by content match, and only
genuinely changed chunks need redoing.

Sampling
--------
The sample is small (100-150) and must cover almost every ESG topic, so it is
stratified, not random:

  1. Every topic present in the corpus gets at least --min-per-topic chunks,
     so no topic is missing from the sample.
  2. The remaining quota is distributed proportionally to topic size, so the
     sample still resembles the corpus.
  3. Within a topic, the quality-tier mix (narrative / layout_sensitive) is
     held near the topic's true ratio, so the sample is not quietly biased
     toward clean prose -- layout_sensitive chunks are where defects live.
  4. No document contributes more than --max-per-doc chunks, so a handful of
     long reports cannot dominate.
  5. Selection is seeded: the same corpus and seed give the same sample.

Determinism boundary: `chunks.csv` and `chunks.jsonl` are byte-identical across
runs for a given corpus and seed -- that is the claim that matters, and it is
what makes a re-run auditable. `PACKAGE_METADATA.json` carries a build
timestamp, so it and its line in `SHA256SUMS` do change. That is deliberate:
SHA256SUMS exists to prove the package survived transfer intact, which is a
different job from reproducing the build.

Read-only with respect to the corpus. Nothing under data/ is modified.

Run `scripts/build_esg_embedding_context.py` first if the headers are stale --
this script reads its output rather than rebuilding the header logic.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config

REPO_ROOT = config.REPO_ROOT

# Overridable so the package can be built from an extracted corpus snapshot as
# well as from the live tree.
ROOT = REPO_ROOT


def _rel(path) -> str:
    """Repo-relative POSIX form of a config path.

    These stay relative because they are joined against ``ROOT``, which
    ``--root`` can repoint at a corpus snapshot. Deriving them from config
    keeps the layout in one place without hardcoding the live tree.
    """
    return config.as_repo_relative(path).as_posix()


REL_CHUNKS_ENRICHED = _rel(config.ESG_CHUNKS_INDEX_ENRICHED_CSV)
REL_MANIFEST = _rel(config.VECTOR_INDEX_MANIFEST_CSV)
REL_SECTIONS_INDEX = _rel(config.ESG_SECTIONS_INDEX_CSV)
REL_CONTEXT_INDEX = _rel(config.ESG_CHUNK_EMBEDDING_CONTEXT_CSV)

PACKAGE_VERSION = "melek_validation_v2_sampled"

FIELDS = [
    "chunk_id",
    "chunk_text_sha256",
    "dataset_id",
    "ticker",
    "company_name",
    "report_year",
    "report_year_span",
    "section_code",
    "section_title",
    "page_start",
    "page_end",
    "chunk_quality_tier",
    "token_count",
    "chunk_text",
    "embedding_text",
]

FIELD_DOCS = [
    ("chunk_id", "Chunk identifier in the source corpus. NOT stable across a reparse."),
    ("chunk_text_sha256", "SHA256 of the chunk file bytes. Stable across a reparse. Please return this."),
    ("dataset_id", "Frozen identifier for this package. Quote it in any question."),
    ("ticker", "Company ticker."),
    ("company_name", "Canonical company name."),
    ("report_year", "Reporting year the document covers (latest year when it spans two)."),
    ("report_year_span", "Full covered span, e.g. 2023-2024. Empty when a single year."),
    ("section_code", "Normalised ESG topic."),
    ("section_title", "Heading as extracted. See README: some are table fragments."),
    ("page_start", "First source page of the chunk."),
    ("page_end", "Last source page of the chunk."),
    ("chunk_quality_tier", "narrative | layout_sensitive. Noise-tier chunks are excluded by default."),
    ("token_count", "Token count under cl100k_base."),
    ("chunk_text", "The chunk text as extracted. This is what you are validating."),
    ("embedding_text", "Metadata header + chunk_text; what the search engine will read."),
]


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"missing required input: {path}")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def sha256_file(path: Path) -> str:
    """Raw file bytes, matching the convention at src/esg_chunker.py:643."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def doc_of(row: dict) -> str:
    return row.get("pdf_stem") or ""


# ---------------------------------------------------------------------------
# sampling
# ---------------------------------------------------------------------------


def allocate(topic_sizes: dict[str, int], target: int, min_per_topic: int) -> dict[str, int]:
    """Floor per topic, remainder proportional to topic size.

    Largest-remainder apportionment, so the quotas sum to exactly `target`
    without a rounding drift that silently drops or adds chunks.
    """
    topics = sorted(topic_sizes)
    quota = {t: min(min_per_topic, topic_sizes[t]) for t in topics}
    used = sum(quota.values())
    left = target - used
    if left <= 0:
        return quota

    headroom = {t: topic_sizes[t] - quota[t] for t in topics}
    pool = sum(headroom.values())
    if pool == 0:
        return quota

    exact = {t: left * headroom[t] / pool for t in topics}
    for t in topics:
        quota[t] += int(exact[t])
    # Hand out what rounding left over, largest fractional part first.
    remainder = target - sum(quota.values())
    order = sorted(topics, key=lambda t: (-(exact[t] - int(exact[t])), t))
    i = 0
    while remainder > 0 and i < len(order) * 4:
        t = order[i % len(order)]
        if quota[t] < topic_sizes[t]:
            quota[t] += 1
            remainder -= 1
        i += 1
    return quota


def pick_within_topic(rows: list[dict], quota: int, max_per_doc: int,
                      doc_used: Counter, rng: random.Random) -> list[dict]:
    """Pick `quota` rows, holding the tier mix near the topic's true ratio."""
    by_tier: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_tier[r.get("chunk_quality_tier") or "unknown"].append(r)

    total = len(rows)
    tier_quota: dict[str, int] = {}
    for tier, items in by_tier.items():
        tier_quota[tier] = max(1, round(quota * len(items) / total)) if quota >= 2 else 0
    # Trim/expand so tier quotas sum to the topic quota.
    while sum(tier_quota.values()) > quota:
        biggest = max(tier_quota, key=lambda t: (tier_quota[t], t))
        tier_quota[biggest] -= 1
    while sum(tier_quota.values()) < quota:
        best = min(tier_quota, key=lambda t: (tier_quota[t] / max(1, len(by_tier[t])), t))
        tier_quota[best] += 1

    picked: list[dict] = []
    for tier in sorted(by_tier):
        items = by_tier[tier][:]
        rng.shuffle(items)
        want = tier_quota.get(tier, 0)
        for r in items:
            if want <= 0:
                break
            if doc_used[doc_of(r)] >= max_per_doc:
                continue
            picked.append(r)
            doc_used[doc_of(r)] += 1
            want -= 1

    # If the per-document cap starved a tier, backfill from anything left.
    if len(picked) < quota:
        chosen = {r["chunk_id"] for r in picked}
        spare = [r for r in rows if r["chunk_id"] not in chosen]
        rng.shuffle(spare)
        for r in spare:
            if len(picked) >= quota:
                break
            if doc_used[doc_of(r)] >= max_per_doc:
                continue
            picked.append(r)
            doc_used[doc_of(r)] += 1
    return picked


def stratified_sample(candidates: list[dict], target: int, min_per_topic: int,
                      max_per_doc: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    by_topic: dict[str, list[dict]] = defaultdict(list)
    for r in candidates:
        by_topic[r.get("section_code") or "unknown"].append(r)

    sizes = {t: len(v) for t, v in by_topic.items()}
    quota = allocate(sizes, target, min_per_topic)

    doc_used: Counter = Counter()
    out: list[dict] = []
    # Rarest topics first: they have the least room to manoeuvre against the
    # per-document cap, so they get first claim on their documents.
    for topic in sorted(by_topic, key=lambda t: (sizes[t], t)):
        out.extend(pick_within_topic(by_topic[topic], quota[topic], max_per_doc, doc_used, rng))
    out.sort(key=lambda r: r["chunk_id"])
    return out


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def build(args) -> dict:
    enriched = read_csv(ROOT / REL_CHUNKS_ENRICHED)
    manifest = {r["chunk_id"]: r for r in read_csv(ROOT / REL_MANIFEST)}

    titles = {}
    for r in read_csv(ROOT / REL_SECTIONS_INDEX):
        titles[(r["ticker"], r["pdf_stem"], r["section_instance_id"])] = (
            r.get("section_title") or ""
        ).strip()

    context = {}
    context_index = ROOT / REL_CONTEXT_INDEX
    if context_index.exists():
        context = {r["chunk_id"]: r for r in read_csv(context_index)}
    else:
        print(f"WARNING: {context_index.name} not found -- embedding_text will be empty.\n"
              f"         Run scripts/build_esg_embedding_context.py first.", file=sys.stderr)

    # --- eligible pool ---------------------------------------------------
    pool, skipped = [], Counter()
    for row in enriched:
        m = manifest.get(row["chunk_id"])
        if m is None:
            skipped["not_in_manifest"] += 1
            continue
        state = (m.get("retrieval_state") or "").strip()
        if not args.include_held and state != "eligible":
            skipped[f"state:{state}"] += 1
            continue
        tier = (row.get("chunk_quality_tier") or "").strip()
        if not args.include_noise and tier == "noise":
            skipped["tier:noise"] += 1
            continue
        rel = row.get("chunk_file") or ""
        if not rel or not (ROOT / rel).exists():
            skipped["chunk_file_missing"] += 1
            continue
        pool.append(row)

    if len(pool) < args.target:
        raise SystemExit(f"pool has only {len(pool)} chunks, need {args.target}")

    selected = stratified_sample(pool, args.target, args.min_per_topic,
                                 args.max_per_doc, args.seed)

    # --- materialise -----------------------------------------------------
    problems, out_rows = [], []
    for row in selected:
        path = ROOT / row["chunk_file"]
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{row['chunk_id']}: unreadable ({exc})")
            continue
        if not text.strip():
            problems.append(f"{row['chunk_id']}: empty chunk text")
            continue

        embed = ""
        ctx_rel = (context.get(row["chunk_id"], {}).get("embedding_text_ctx_file") or "").strip()
        if ctx_rel:
            ctx_path = ROOT / ctx_rel
            if not ctx_path.exists():
                problems.append(f"{row['chunk_id']}: embedding context missing ({ctx_rel})")
                continue
            embed = ctx_path.read_text(encoding="utf-8")

        key = (row["ticker"], row["pdf_stem"], row["section_instance_id"])
        out_rows.append({
            "chunk_id": row["chunk_id"],
            "chunk_text_sha256": sha256_file(path),
            "dataset_id": args.dataset_id,
            "ticker": row.get("canonical_ticker") or row.get("ticker") or "",
            "company_name": row.get("company_name") or "",
            "report_year": row.get("report_year") or "",
            "report_year_span": row.get("report_year_span") or "",
            "section_code": row.get("section_code") or "",
            "section_title": titles.get(key, ""),
            "page_start": row.get("page_start") or "",
            "page_end": row.get("page_end") or "",
            "chunk_quality_tier": row.get("chunk_quality_tier") or "",
            "token_count": row.get("token_count") or "",
            "chunk_text": text,
            "embedding_text": embed,
        })

    if problems:
        print(f"\nABORTING -- {len(problems)} chunk(s) could not be read:", file=sys.stderr)
        for p in problems[:25]:
            print(f"  {p}", file=sys.stderr)
        raise SystemExit(2)

    out_rows.sort(key=lambda r: r["chunk_id"])

    # --- write -----------------------------------------------------------
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    with (out_dir / "chunks.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(out_rows)

    with (out_dir / "chunks.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for r in out_rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")

    with (out_dir / "field_dictionary.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["field", "meaning"])
        w.writerows(FIELD_DOCS)

    topics = Counter(r["section_code"] for r in out_rows)
    tiers = Counter(r["chunk_quality_tier"] for r in out_rows)
    years = Counter(r["report_year"] for r in out_rows)
    tickers = Counter(r["ticker"] for r in out_rows)
    docs = Counter(r["chunk_id"].split("__")[1] for r in out_rows if "__" in r["chunk_id"])
    pool_topics = Counter(r.get("section_code") or "unknown" for r in pool)

    (out_dir / "README.md").write_text(
        readme(args, out_rows, topics, tiers, years, tickers, docs, pool_topics, len(pool)),
        encoding="utf-8", newline="\n")

    meta = {
        "dataset_id": args.dataset_id,
        "package_version": PACKAGE_VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sampling": {"target": args.target, "seed": args.seed,
                     "min_per_topic": args.min_per_topic, "max_per_doc": args.max_per_doc},
        "rows": len(out_rows),
        "eligible_pool": len(pool),
        "topics_in_sample": len(topics),
        "topics_in_pool": len(pool_topics),
        "documents": len(docs),
        "tickers": len(tickers),
        "tiers": dict(tiers),
        "years": dict(years),
        "skipped_from_pool": dict(skipped),
        "embedding_text_present": bool(context),
    }
    (out_dir / "PACKAGE_METADATA.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8", newline="\n")

    # SHA256SUMS is written LAST, after every other file is final -- otherwise
    # it records a stale digest for whatever is written after it. Format is the
    # plain coreutils text form "<64-hex><2 spaces><relative path>", matching
    # the packages that actually pass `sha256sum -c`. The manifest never lists
    # itself: a self-listing manifest records the empty-file digest and makes
    # verification false-fail.
    lines = [f"{sha256_file(f)}  {f.relative_to(out_dir).as_posix()}"
             for f in sorted(p for p in out_dir.rglob("*")
                             if p.is_file() and p.name != "SHA256SUMS")]
    (out_dir / "SHA256SUMS").write_text("\n".join(lines) + "\n",
                                        encoding="utf-8", newline="\n")

    # Self-verify: re-read the manifest and check every file, so the package is
    # never handed over unverified and no separate verification step is needed.
    bad, missing, ok = [], [], 0
    for line in (out_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        target = out_dir / rel
        if not target.exists():
            missing.append(rel)
        elif sha256_file(target) == digest:
            ok += 1
        else:
            bad.append(rel)
    if bad or missing:
        for rel in bad:
            print(f"  MISMATCH {rel}", file=sys.stderr)
        for rel in missing:
            print(f"  MISSING  {rel}", file=sys.stderr)
        raise SystemExit("SHA256SUMS verification FAILED -- package not usable")

    meta["files_hashed"] = len(lines)
    meta["verified_files"] = ok
    meta["_topics"] = topics
    meta["_pool_topics"] = pool_topics
    return meta


def readme(args, rows, topics, tiers, years, tickers, docs, pool_topics, pool_n) -> str:
    cover = "\n".join(
        f"| {t} | {topics.get(t, 0)} | {pool_topics[t]:,} |"
        for t in sorted(pool_topics)
    )
    missing = sorted(t for t in pool_topics if t not in topics)
    missing_note = (
        f"\n**Topics not represented:** {', '.join(missing)}\n" if missing
        else "\n**Every topic in the corpus is represented.**\n"
    )
    return f"""# ESG chunk validation sample

**Dataset id:** `{args.dataset_id}`
**Chunks:** {len(rows)} — drawn from an eligible pool of {pool_n:,}
**Documents:** {len(docs)} · **Companies:** {len(tickers)} · **Topics:** {len(topics)}
**Quality tiers:** {", ".join(f"{k} {v}" for k, v in sorted(tiers.items()))}
**Reporting years:** {", ".join(f"{k} {v}" for k, v in sorted(years.items()))}

## What this is

Chunks of text extracted from corporate sustainability reports, for hand
validation. `chunk_text` is what you are validating. `embedding_text` is the
same text with a metadata header on top — that is what the search engine reads.

This is a **designed sample, not a random one**. Every ESG topic in the corpus
gets a minimum allocation so none is missed; the rest is spread in proportion to
how common each topic is. Within a topic the narrative / layout_sensitive mix
follows the corpus, and no single report contributes more than
{args.max_per_doc} chunks. Seed `{args.seed}` — the same corpus and seed
reproduce this exact sample.

## Coverage

| ESG topic | in sample | in corpus |
|---|---:|---:|
{cover}
{missing_note}
## Please return both `chunk_id` and `chunk_text_sha256`

This matters. The corpus is going to be rebuilt, and `chunk_id` values will
change when it is — roughly one in seven, last time.

`chunk_text_sha256` is a fingerprint of the text itself, so it does not change
unless the text changes. If your returned file carries both columns, every
judgement whose text is unchanged is carried forward automatically after the
rebuild, and only genuinely changed chunks need looking at again.

If only `chunk_id` comes back, the work cannot be matched up after the rebuild.

## Known issues — please do not spend time reporting these

- **Messy `section_title`.** Some are table fragments rather than headings,
  e.g. `| Workers' compensation claims | 94 | 162 | NA |`. Already logged.
- **`layout_sensitive` chunks** come from tables and dense layouts. Row and
  column relationships may be weakened, so a number may sit further from its
  label than it did on the page. Flag these only if the *meaning* is wrong.
- **Scope.** A 200-document slice covering reporting years 2023–2024, not the
  full library.

## Do tell us about

- Text that is scrambled, out of order, or has words split oddly
- A chunk whose `report_year`, `company_name` or `ticker` looks wrong
- A chunk that is unusable as evidence for answering a question
- Anything that reads as though it came from a different company or year
"""


def main(argv=None) -> int:
    global ROOT
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset-id", required=True,
                    help="e.g. esg-melek-validation-2026-07-29")
    ap.add_argument("--out", default="outputs/melek_validation_package")
    ap.add_argument("--root", default=None,
                    help="corpus root (defaults to the repo); use to build from a snapshot")
    ap.add_argument("--target", type=int, default=150, help="sample size (default 150)")
    ap.add_argument("--min-per-topic", type=int, default=4)
    ap.add_argument("--max-per-doc", type=int, default=3)
    ap.add_argument("--seed", type=int, default=20260729)
    ap.add_argument("--include-held", action="store_true")
    ap.add_argument("--include-noise", action="store_true")
    args = ap.parse_args(argv)

    if args.root:
        ROOT = Path(args.root).resolve()

    meta = build(args)
    topics, pool_topics = meta.pop("_topics"), meta.pop("_pool_topics")

    print(f"\npackage: {ROOT / args.out}")
    print(f"  rows          : {meta['rows']}  (pool {meta['eligible_pool']:,})")
    print(f"  topics        : {meta['topics_in_sample']} of {meta['topics_in_pool']}")
    print(f"  documents     : {meta['documents']}   companies: {meta['tickers']}")
    print(f"  tiers         : {meta['tiers']}")
    print(f"  years         : {meta['years']}")
    print(f"  verified      : {meta['verified_files']}/{meta['files_hashed']} files hashed and re-checked")
    print("\n  topic coverage:")
    for t in sorted(pool_topics):
        print(f"    {t:<30} {topics.get(t, 0):>3}  (corpus {pool_topics[t]:,})")
    if not meta["embedding_text_present"]:
        print("\n  WARNING: embedding_text is empty. Run "
              "scripts/build_esg_embedding_context.py and re-run this.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
