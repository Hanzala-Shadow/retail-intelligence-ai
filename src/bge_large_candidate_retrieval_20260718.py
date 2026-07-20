#!/usr/bin/env python3
"""Read-only deterministic BGE Large candidate-pool retrieval benchmark."""
import argparse
import csv
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

from sentence_transformers import CrossEncoder

GROUPS = ("Item_1", "Item_1A", "Item_7", "Item_8", "cross_company", "time_change")
CE_REPO = "cross-encoder/ms-marco-MiniLM-L6-v2"
CE_REVISION = "c5ee24cb16019beea0893ab7796b1df96625c6b8"
CE_LICENSE = "apache-2.0"
RRF_K = 60
MODEL_SLUG = "bge_large_en_v15"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_evaluator(path):
    spec = importlib.util.spec_from_file_location("fusion_evaluator", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parts(value):
    return [item.strip() for item in (value or "").split("|") if item.strip()]


def broadcast(values, count, label, qid):
    if len(values) == 1 and count > 1:
        values *= count
    if len(values) != count:
        raise ValueError(f"{qid}: positional {label} mismatch")
    return values


def fetch_candidates(conn, module, config, vector, question, source, section):
    model_cfg = config["models"][MODEL_SLUG]
    table = module.safe_ident(model_cfg["table"])
    snapshot = module.safe_ident(config["snapshot"]["table"].split(".")[-1])
    ticker, year, doc_type, accession = source
    sql = f"""
      SELECT s.chunk_id, s.section_code, s.chunk_index, s.embedding_text,
             1-(e.embedding <=> %s::vector) AS semantic_score,
             ts_rank_cd(
               to_tsvector('english',coalesce(s.embedding_text,'')),
               websearch_to_tsquery('english',%s),32
             ) AS lexical_score
      FROM public.{table} e
      JOIN public.{snapshot} s USING(chunk_id)
      WHERE s.ticker=%s AND s.filing_year=%s AND s.doc_type=%s
        AND s.accession_number=%s AND s.section_code=%s
      ORDER BY s.chunk_id
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                module.vector_literal(vector), question,
                ticker, year, doc_type, accession, section,
            ),
        )
        names = [column.name for column in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def add_cross_encoder_scores(model, question, rows, batch_size):
    pairs = [(question, row["embedding_text"] or "") for row in rows]
    scores = model.predict(
        pairs,
        batch_size=batch_size,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    output = []
    for row, score in zip(rows, scores, strict=True):
        item = dict(row)
        item["semantic_score"] = float(item["semantic_score"])
        item["lexical_score"] = float(item["lexical_score"])
        item["cross_encoder_score"] = float(score)
        output.append(item)
    return output


def rank_map(rows, key, positive_only=False):
    eligible = rows
    if positive_only:
        eligible = [row for row in rows if row[key] > 0.0]
    ordered = sorted(eligible, key=lambda row: (-row[key], int(row["chunk_id"])))
    return {str(row["chunk_id"]): rank for rank, row in enumerate(ordered, 1)}


def fuse(rows, method):
    semantic = rank_map(rows, "semantic_score")
    lexical = rank_map(rows, "lexical_score", positive_only=True)
    cross_encoder = rank_map(rows, "cross_encoder_score")

    def base_rrf(cid):
        score = 1.0 / (RRF_K + semantic[cid])
        if cid in lexical:
            score += 1.0 / (RRF_K + lexical[cid])
        return score

    base_order = sorted(
        rows,
        key=lambda row: (-base_rrf(str(row["chunk_id"])), int(row["chunk_id"])),
    )
    base_rank = {str(row["chunk_id"]): rank for rank, row in enumerate(base_order, 1)}

    def fusion_score(row):
        cid = str(row["chunk_id"])
        if method == "semantic_only":
            return 1.0 / (RRF_K + semantic[cid])
        if method == "lexical_only":
            return (
                1.0 / (RRF_K + lexical[cid])
                if cid in lexical else 0.0
            )
        if method == "semantic_lexical_rrf":
            return base_rrf(cid)
        if method == "cross_encoder_only":
            return 1.0 / (RRF_K + cross_encoder[cid])
        if method == "equal_three_way_rrf":
            score = 1.0 / (RRF_K + semantic[cid])
            score += 1.0 / (RRF_K + cross_encoder[cid])
            if cid in lexical:
                score += 1.0 / (RRF_K + lexical[cid])
            return score
        if method == "hybrid_rank_plus_cross_encoder_rrf":
            return (
                1.0 / (RRF_K + base_rank[cid])
                + 1.0 / (RRF_K + cross_encoder[cid])
            )
        if method == "semantic_2x_lexical_1x_cross_encoder_1x_rrf":
            score = 2.0 / (RRF_K + semantic[cid])
            score += 1.0 / (RRF_K + cross_encoder[cid])
            if cid in lexical:
                score += 1.0 / (RRF_K + lexical[cid])
            return score
        if method == "semantic_3x_cross_encoder_1x_rrf":
            return 3.0 / (RRF_K + semantic[cid]) + 1.0 / (RRF_K + cross_encoder[cid])
        raise ValueError(method)

    ranked = sorted(
        rows,
        key=lambda row: (-fusion_score(row), int(row["chunk_id"])),
    )
    return ranked


def truncate_candidates(rows, depth):
    """Gold-independent union of top semantic and positive lexical candidates."""
    semantic = sorted(rows, key=lambda row: (-row["semantic_score"], int(row["chunk_id"])))[:depth]
    lexical = sorted(
        [row for row in rows if row["lexical_score"] > 0.0],
        key=lambda row: (-row["lexical_score"], int(row["chunk_id"])),
    )[:depth]
    selected = {}
    for row in semantic + lexical:
        selected[str(row["chunk_id"])] = row
    return list(selected.values())


def round_robin(per_source, limit=5):
    output, seen, depth = [], set(), 0
    while len(output) < limit and any(depth < len(rows) for rows in per_source):
        for rows in per_source:
            if depth < len(rows):
                row = rows[depth]
                cid = str(row["chunk_id"])
                if cid not in seen:
                    output.append(row)
                    seen.add(cid)
                    if len(output) == limit:
                        break
        depth += 1
    return output


def merged_full_ranking(per_source):
    return round_robin(per_source, limit=sum(len(rows) for rows in per_source))


def best_gold_rank(ranked, gold):
    gold_set = {str(value) for value in gold}
    ranks = [rank for rank, row in enumerate(ranked, 1) if str(row["chunk_id"]) in gold_set]
    return min(ranks) if ranks else None


def aggregate(records):
    names = ("recall_at_5", "hit_rate_at_5", "mrr", "ndcg_at_5")

    def mean(rows):
        return {name: sum(row["metrics"][name] for row in rows) / len(rows) for name in names}

    return {
        "overall": mean(records),
        "by_group": {
            group: mean([row for row in records if row["question_group"] == group])
            for group in GROUPS
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evaluator", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--questions", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()
    if args.batch_size < 1:
        raise ValueError("--batch-size must be at least 1")
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite: {output}")

    module = load_evaluator(args.evaluator)
    config = module.load_json(args.config)
    module.validate_config(config)
    questions, refusals = module.load_questions(args.questions)
    with open(args.questions, encoding="utf-8-sig", newline="") as handle:
        raw = {row["question_id"]: row for row in csv.DictReader(handle)}

    bge = module.load_model(config["models"][MODEL_SLUG])
    cross_encoder = CrossEncoder(
        CE_REPO, revision=CE_REVISION, device="cpu", max_length=512,
    )
    method_specs = {
        "semantic_only_full": ("semantic_only", None),
        "lexical_only_full": ("lexical_only", None),
        "semantic_lexical_rrf_full": ("semantic_lexical_rrf", None),
        "cross_encoder_only_full": ("cross_encoder_only", None),
        "equal_three_way_rrf_full": ("equal_three_way_rrf", None),
        "hybrid_rank_plus_cross_encoder_rrf_full": ("hybrid_rank_plus_cross_encoder_rrf", None),
        "semantic_2x_lexical_1x_cross_encoder_1x_rrf_full": ("semantic_2x_lexical_1x_cross_encoder_1x_rrf", None),
        "semantic_3x_cross_encoder_1x_rrf_full": ("semantic_3x_cross_encoder_1x_rrf", None),
        "cross_encoder_semantic_lexical_union_5": ("cross_encoder_only", 5),
        "cross_encoder_semantic_lexical_union_10": ("cross_encoder_only", 10),
        "cross_encoder_semantic_lexical_union_20": ("cross_encoder_only", 20),
        "semantic_2x_lexical_1x_cross_encoder_1x_rrf_union_10": ("semantic_2x_lexical_1x_cross_encoder_1x_rrf", 10),
        "semantic_2x_lexical_1x_cross_encoder_1x_rrf_union_20": ("semantic_2x_lexical_1x_cross_encoder_1x_rrf", 20),
    }
    methods = {name: [] for name in method_specs}
    total_candidates = 0
    started = time.monotonic()
    conn = module.connect()
    try:
        for question in questions:
            count = len(question.tickers)
            sections = broadcast(parts(raw[question.qid]["required_sections"]), count, "sections", question.qid)
            sources = list(zip(
                question.tickers, question.years, question.doc_types,
                question.accessions, strict=True,
            ))
            query = config["models"][MODEL_SLUG]["query_prefix"] + question.text
            vector = bge.encode([query], normalize_embeddings=True, convert_to_numpy=True)[0]
            candidates = [
                fetch_candidates(conn, module, config, vector, question.text, source, sections[index])
                for index, source in enumerate(sources)
            ]
            if any(not rows for rows in candidates):
                raise RuntimeError(f"{question.qid}: an authorized source has no candidates")
            scored = [
                add_cross_encoder_scores(cross_encoder, question.text, rows, args.batch_size)
                for rows in candidates
            ]
            total_candidates += sum(len(rows) for rows in scored)
            for method_name, (fusion_method, depth) in method_specs.items():
                selected = [rows if depth is None else truncate_candidates(rows, depth) for rows in scored]
                per_source = [fuse(rows, fusion_method) for rows in selected]
                full = merged_full_ranking(per_source)
                top = full[:5]
                if len(top) != 5:
                    raise RuntimeError(f"{question.qid}/{method_name}: expected 5 results")
                ids = [str(row["chunk_id"]) for row in top]
                methods[method_name].append({
                    "question_id": question.qid,
                    "question_group": question.group,
                    "candidate_counts_by_source": [len(rows) for rows in scored],
                    "selected_counts_by_source": [len(rows) for rows in selected],
                    "ranked_chunk_ids": ids,
                    "ranked_sections": [row["section_code"] for row in top],
                    "best_gold_rank": best_gold_rank(full, question.gold),
                    "metrics": module.metrics(ids, question.gold, 5),
                })
    finally:
        conn.close()

    report = {
        "run_id": config["run_id"],
        "base_model_slug": MODEL_SLUG,
        "experimental": True,
        "database_mutations": False,
        "supported_questions": len(questions),
        "refusals_excluded": len(refusals),
        "total_candidate_pairs_scored": total_candidates,
        "rrf_k": RRF_K,
        "batch_size": args.batch_size,
        "max_length": 512,
        "cross_encoder": {
            "repo_id": CE_REPO,
            "resolved_revision": CE_REVISION,
            "license": CE_LICENSE,
        },
        "inputs": {
            "evaluator_sha256": sha256(args.evaluator),
            "config_sha256": sha256(args.config),
            "questions_sha256": sha256(args.questions),
        },
        "methods": {
            name: {
                **aggregate(rows),
                "misses": [row["question_id"] for row in rows if row["metrics"]["hit_rate_at_5"] == 0.0],
                "questions": rows,
            }
            for name, rows in methods.items()
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps({
        name: {
            "overall": data["overall"],
            "by_group": data["by_group"],
            "misses": data["misses"],
        }
        for name, data in report["methods"].items()
    }, indent=2, sort_keys=True))
    print(f"elapsed_seconds={time.monotonic() - started:.6f}")
    print(f"total_candidate_pairs_scored={total_candidates}")
    print(f"PASS: wrote immutable fusion result: {output}")


if __name__ == "__main__":
    main()
