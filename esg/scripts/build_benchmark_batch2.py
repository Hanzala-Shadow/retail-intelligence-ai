"""Build the batch-2 benchmark handoff: seed passages plus coverage.

Batch 1 returned 50 questions from a 200-passage pool. Validating those
exposed three things this sampler is built around:

* BM25 alone answered 45% of the seeded questions at Hit@5, so passages whose
  distinctive vocabulary is easy to echo make weak questions. We now mark
  passages that carry a table or dense figures, because those were the
  questions no lexical baseline could reach.
* Every batch-1 seed is excluded, so batch 2 cannot re-ask a batch-1 question.
* Companies are capped, so the pool cannot concentrate on a few big reporters.

Outputs land in data/handout_benchmark_brief_v2/.
"""
from __future__ import annotations

import argparse
import csv
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

TRUE = {"1", "true", "yes", "y"}
ROOT = Path(__file__).resolve().parents[2]
IDX = ROOT / "data" / "00_reference" / "esg_chunks_index.csv"
COMPANIES = ROOT / "data" / "00_reference" / "companies.csv"
BATCH1_SEEDS = ROOT / "data" / "handout_benchmark_brief" / "seed_chunks.csv"
BATCH1_QS = ROOT / "scratchpad" / "esg_benchmark_questions.csv"
OUT = ROOT / "data" / "handout_benchmark_brief_v2"

MIN_TOKENS = 90
MAX_PER_COMPANY = 7
FIGURE_RE = re.compile(r"\d")
PCT_RE = re.compile(r"\d[\d,\.]*\s?(%|percent|tons?|tonnes?|mwh|kwh|gj|million|billion)", re.I)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--seeds", type=int, default=600, help="passages to emit")
    p.add_argument("--seed", type=int, default=20260811)
    return p.parse_args()


def body_of(embedding_text: str) -> str:
    return embedding_text.split("\n\n", 1)[1] if "\n\n" in embedding_text else embedding_text


def main():
    args = parse_args()
    rng = random.Random(args.seed)

    names = {}
    with COMPANIES.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            t = (r.get("ticker") or "").strip().upper()
            if t:
                names[t] = (r.get("name") or r.get("company_name")
                            or r.get("company") or "").strip()

    used_chunks = set()
    if BATCH1_SEEDS.exists():
        with BATCH1_SEEDS.open(encoding="utf-8-sig", newline="") as f:
            used_chunks = {r["chunk_id"] for r in csv.DictReader(f)}

    batch1_pairs = set()
    if BATCH1_QS.exists():
        with BATCH1_QS.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                for t in (r.get("expected_tickers") or "").split(";"):
                    for s in (r.get("expected_sections") or "").split(";"):
                        if t.strip() and s.strip():
                            batch1_pairs.add((t.strip().upper(), s.strip()))

    pool = []
    coverage = defaultdict(set)
    with IDX.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("include_in_esg_index") or "").strip().lower() not in TRUE:
                continue
            cid = r["chunk_id"]
            ticker = (r.get("ticker") or "").upper()
            m = re.search(r"_((?:19|20)\d{2})__", cid)
            year = m.group(1) if m else ""
            topic = r.get("section_code") or ""
            coverage[(ticker, year)].add(topic)
            if cid in used_chunks:
                continue
            try:
                if int(r.get("token_count") or 0) < MIN_TOKENS:
                    continue
            except ValueError:
                continue
            if (r.get("chunk_type") or "") == "table_continuation":
                continue
            body = body_of(r.get("embedding_text") or "").strip()
            if not body:
                continue
            has_table = "|" in body and body.count("|") >= 4
            has_fig = bool(PCT_RE.search(body))
            digits = sum(c.isdigit() for c in body) / max(1, len(body))
            pool.append(dict(
                chunk_id=cid, ticker=ticker, company=names.get(ticker, ticker),
                year=year, topic=topic,
                subsection=(r.get("subsection_context") or "").strip()[:120],
                pages=f"{r.get('page_start','')}-{r.get('page_end','')}",
                tokens=r.get("token_count", ""),
                has_figures="yes" if has_fig else "no",
                has_table="yes" if has_table else "no",
                digit_density=round(digits, 4),
                passage=body,
            ))

    print(f"candidate passages after exclusions: {len(pool)}")

    # Stratify: half table/figure-bearing (the discriminative kind), half prose.
    rng.shuffle(pool)
    quantish = [p for p in pool if p["has_table"] == "yes" or p["has_figures"] == "yes"]
    prose = [p for p in pool if p not in quantish] if False else [
        p for p in pool if p["has_table"] == "no" and p["has_figures"] == "no"
    ]
    rng.shuffle(quantish)
    rng.shuffle(prose)

    want_quant = int(args.seeds * 0.55)
    picked, per_company, per_topic = [], Counter(), Counter()

    def take(source, limit):
        for p in source:
            if len(picked) >= limit:
                break
            if per_company[p["ticker"]] >= MAX_PER_COMPANY:
                continue
            # gently prefer topics we have less of
            if per_topic[p["topic"]] > (limit / 8) and rng.random() < 0.6:
                continue
            picked.append(p)
            per_company[p["ticker"]] += 1
            per_topic[p["topic"]] += 1

    take(quantish, want_quant)
    take(prose, args.seeds)
    take(quantish, args.seeds)          # top up if prose ran short
    take(pool, args.seeds)              # last resort, ignore preferences

    picked.sort(key=lambda p: (p["ticker"], p["year"], p["topic"]))
    for i, p in enumerate(picked, 1):
        p["seed_id"] = f"B2_S{i:03d}"

    OUT.mkdir(exist_ok=True)
    cols = ["seed_id", "chunk_id", "ticker", "company", "year", "topic", "subsection",
            "pages", "tokens", "has_figures", "has_table", "digit_density", "passage"]
    with (OUT / "seed_chunks.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for p in picked:
            w.writerow({c: p[c] for c in cols})

    with (OUT / "seed_chunks.md").open("w", encoding="utf-8") as f:
        f.write("# Batch 2 seed passages\n\n")
        f.write(f"{len(picked)} passages, none of them used in batch 1. Read one, write "
                "the question it answers, and put its id in the **`seed_id` column** of "
                "the template.\n\n")
        f.write("`table` and `figures` tags mark the passages that make the strongest "
                "questions — start there. See the brief for the three-word paraphrase "
                "test every question must pass.\n\n---\n\n")
        for p in picked:
            tags = []
            if p["has_table"] == "yes":
                tags.append("**table**")
            if p["has_figures"] == "yes":
                tags.append("**figures**")
            f.write(f"### {p['seed_id']} — {p['company']} ({p['ticker']}) {p['year']}\n\n")
            f.write(f"*topic:* `{p['topic']}` · *pages:* {p['pages']} · "
                    f"*tokens:* {p['tokens']}"
                    + (f" · {' · '.join(tags)}" if tags else "") + "\n\n")
            f.write("```\n" + p["passage"].strip() + "\n```\n\n")

    with (OUT / "corpus_coverage.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "company_name", "report_year", "topics_present"])
        for (t, y), topics in sorted(coverage.items()):
            if t and y:
                w.writerow([t, names.get(t, t), y, ";".join(sorted(x for x in topics if x))])

    tmpl = ["question_id", "question_type", "refusal_reason", "question",
            "atomic_requirements", "expected_tickers", "expected_years",
            "expected_sections", "decisive_evidence", "difficulty", "notes"]
    with (OUT / "esg_benchmark_questions_TEMPLATE.csv").open("w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow(tmpl)

    print(f"seeds written: {len(picked)}")
    print(f"  distinct companies: {len(per_company)}  (cap {MAX_PER_COMPANY} each)")
    print(f"  distinct topics: {len(per_topic)}")
    print(f"  with a table: {sum(1 for p in picked if p['has_table']=='yes')}")
    print(f"  with figures: {sum(1 for p in picked if p['has_figures']=='yes')}")
    print(f"  years: {min(p['year'] for p in picked)}-{max(p['year'] for p in picked)}")
    print(f"  -> {OUT}")


if __name__ == "__main__":
    main()
