"""Tier 4 mathematical QA: content validity of the ESG corpus.

Distributional, like Tiers 2 and 3 -- a check here can only flag a chunk
population as implausible, never prove a defect on its own. Run
qa_tier1_invariants.py first: duplicate/similarity statistics are only
meaningful once chunks are known to tile their sections correctly, and a
containment failure elsewhere would otherwise get misread as "content" noise.

Where Tiers 1-3 ask "does the pipeline's bookkeeping hold together", Tier 4
asks a different question: is the text itself worth retrieving? Boilerplate,
near-duplicate headers/footers, table dumps, and garbled extraction all pass
every earlier tier's checks while still degrading retrieval quality.

Everything is READ-ONLY. The database is opened with mode=ro.

Checks
    20  exact-duplicate rate (SHA-256 of normalized chunk_text; duplicate
        pairs partitioned into within-document / within-company-across-
        documents / cross-company)
    21  near-duplicate rate (MinHash-estimated Jaccard similarity at a 0.9
        threshold, on a per-ticker stratified sample)
    22  digit-and-symbol ratio per chunk (non-alphabetic character fraction;
        flags table/numeric dumps that survived the layout gate)
    23  does the corpus vocabulary follow Zipf's law (log-log rank-frequency
        fit; residual structure by log-rank third)
    24  type-token ratio distribution, per chunk and per document (chunk-level
        outliers standardised within word-count deciles, since raw TTR is
        mechanically biased by length)
    25  year-over-year similarity for the same company (word-shingle Jaccard
        between consecutive-year reports)

Usage
    python esg/scripts/esg_database_tiers/qa_tier4_content_validity.py
    python esg/scripts/esg_database_tiers/qa_tier4_content_validity.py --checks 20,22
    python esg/scripts/esg_database_tiers/qa_tier4_content_validity.py --json-out reports/qa_tier4.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy import stats as sp_stats

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402
from esg_year import report_year  # noqa: E402
from qa_tier1_invariants import (  # noqa: E402
    CheckResult,
    connect,
    describe,
    load_documents,
    percentile,
)

ALL_CHECKS = ["20", "21", "22", "23", "24", "25"]

# ---------------------------------------------------------------------------
# shared constants
# ---------------------------------------------------------------------------

# Word-level shingle length for both the MinHash near-duplicate estimate
# (check 21) and the exact document-level Jaccard used for year-over-year
# similarity (check 25). 8 words is long enough that a shared shingle is
# genuinely shared phrasing, not two chunks that both happen to use "the".
SHINGLE_WORDS = 8

# MinHash sketch width and the 31-bit Mersenne prime used for the universal
# hash family a*h + b (mod PRIME). Coefficients and shingle hashes are kept
# under 2**31 specifically so their product fits in a uint64 without
# overflow -- see minhash_signature().
NUM_PERM = 64
HASH_PRIME = (1 << 31) - 1

# The QA brief names this threshold explicitly ("MinHash/SimHash at threshold
# ~0.9"); it is not derived from this corpus.
NEAR_DUP_SIMILARITY_THRESHOLD = 0.9

# Cap on the stratified sample check 21 MinHashes and compares pairwise. Pairwise
# comparison is O(n^2), but at this size it is a few hundred milliseconds of
# numpy work, not a scaling concern; the cap exists to keep the check's cost
# predictable regardless of how large the corpus grows.
NEAR_DUP_SAMPLE_SIZE = 4000

# A chunk at or above this non-alphabetic fraction is majority digits/symbols
# -- prose runs well below it even with numeric callouts. Not derived from
# this corpus.
NON_ALPHA_HIGH_RATIO = 0.5

# Deviation gates shared with qa_tier1/2/3's convention of a 3-sigma (or
# robust-z) cutoff for "this is not noise".
Z_GATE = 3.0

# check 25: a company needs at least this many OTHER consecutive-year pairs
# before its own leave-one-out median/MAD is trusted as a baseline -- below
# this, "half its usual pairs" is one or two numbers, too noisy to judge a
# third value against.
MIN_PEER_PAIRS = 3


# ---------------------------------------------------------------------------
# small helpers specific to this tier
# ---------------------------------------------------------------------------

WORD_RE = re.compile(r"[a-z0-9]+")
ALPHA_RE = re.compile(r"[A-Za-z]")


def normalize_chunk_text(text: str | None) -> str:
    """Collapse whitespace runs and trim -- enough to make two chunks that
    differ only in a parser's incidental line-wrapping compare equal, without
    touching case or punctuation that could hide a genuine content change."""
    return re.sub(r"\s+", " ", text or "").strip()


def word_tokens(text: str | None) -> list[str]:
    return WORD_RE.findall((text or "").lower())


def shingle_hashes(words: list[str], k: int = SHINGLE_WORDS) -> np.ndarray:
    """Stable 31-bit hash of every distinct k-word shingle in `words`."""
    if len(words) < k:
        return np.array([], dtype=np.uint64)
    shingles = {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}
    return np.array(
        [
            int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=4).digest(), "big")
            % HASH_PRIME
            for s in shingles
        ],
        dtype=np.uint64,
    )


def minhash_signature(hash_values: np.ndarray, coeff_a: np.ndarray, coeff_b: np.ndarray) -> np.ndarray:
    """MinHash signature under the universal hash family h -> (a*h + b) mod PRIME.

    hash_values and coeff_a/coeff_b are all held under 2**31 so their pairwise
    product is bounded by 2**62, comfortably inside uint64 -- avoiding the
    silent 64-bit wraparound a naive 61-bit Mersenne-prime scheme would hit.
    """
    if hash_values.size == 0:
        return np.full(coeff_a.shape[0], HASH_PRIME, dtype=np.uint64)
    combined = (np.outer(hash_values, coeff_a) + coeff_b) % HASH_PRIME
    return combined.min(axis=0)


def stratified_sample_by_ticker(
    rows: list[sqlite3.Row], docs: dict, sample_size: int, rng: np.random.Generator
) -> list[sqlite3.Row]:
    """Proportional-allocation sample across tickers (min 1 per ticker present).

    A plain global random sample would already be roughly proportional by
    ticker in expectation, but a small ticker could easily draw zero chunks by
    chance; explicit per-ticker allocation guarantees every filer that has any
    chunks gets at least one shot at surfacing a near-duplicate.
    """
    by_ticker: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        ticker = docs.get(row["doc_id"], {}).get("ticker") or "UNKNOWN"
        by_ticker[ticker].append(row)

    total = len(rows)
    sample: list[sqlite3.Row] = []
    for ticker_rows in by_ticker.values():
        quota = min(len(ticker_rows), max(1, round(sample_size * len(ticker_rows) / total)))
        idx = rng.choice(len(ticker_rows), size=quota, replace=False)
        sample.extend(ticker_rows[int(i)] for i in idx)
    return sample


def local_z_scores(values: np.ndarray, bin_by: np.ndarray, n_bins: int = 10) -> np.ndarray:
    """z-score of each value within deciles of `bin_by`.

    Same fix as qa_tier3_chunking.check_16's chunks-vs-char_count residual: a
    metric that is mechanically a function of size (here, TTR vs word count)
    is heteroscedastic, so a single corpus-wide SD buries small-sample effects
    at one end of the size range. Standardising within size deciles makes a
    short chunk's anomaly as visible as a long chunk's.
    """
    edges = np.quantile(bin_by, np.linspace(0, 1, n_bins + 1))
    bin_idx = np.clip(np.digitize(bin_by, edges[1:-1], right=True), 0, n_bins - 1)
    global_sd = float(values.std(ddof=1)) if len(values) > 1 else 0.0
    z = np.zeros_like(values, dtype=float)
    for b in range(n_bins):
        mask = bin_idx == b
        if mask.sum() > 1:
            local_sd = float(values[mask].std(ddof=1))
            sd = local_sd if local_sd > 0 else global_sd
            if sd > 0:
                z[mask] = (values[mask] - values[mask].mean()) / sd
    return z


def pairs(n: int) -> int:
    return n * (n - 1) // 2


# ---------------------------------------------------------------------------
# check 20 -- exact-duplicate rate
# ---------------------------------------------------------------------------


def check_20(con, docs, examples_wanted):
    result = CheckResult("20", "Exact-duplicate rate")

    rows = con.execute(
        "SELECT chunk_id, doc_id, company_id, chunk_index, chunk_text FROM chunks"
    ).fetchall()

    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    empty_after_normalize = 0
    for row in rows:
        normalized = normalize_chunk_text(row["chunk_text"])
        if not normalized:
            empty_after_normalize += 1
            continue
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        groups[digest].append(row)

    dup_groups = [g for g in groups.values() if len(g) > 1]
    chunks_in_dup_groups = sum(len(g) for g in dup_groups)
    excess_copies = sum(len(g) - 1 for g in dup_groups)

    total_pairs = same_doc_pairs = same_company_pairs = 0
    for group in dup_groups:
        n = len(group)
        total_pairs += pairs(n)
        same_doc_pairs += sum(pairs(c) for c in Counter(r["doc_id"] for r in group).values())
        same_company_pairs += sum(
            pairs(c) for c in Counter(r["company_id"] for r in group).values()
        )
    cross_company_pairs = total_pairs - same_company_pairs
    within_company_cross_doc_pairs = same_company_pairs - same_doc_pairs

    def distinct_companies(group: list[sqlite3.Row]) -> int:
        return len({r["company_id"] for r in group})

    # Cross-company groups are the most alarming case per the QA brief -- a
    # generic disclaimer letting a query "succeed" against the wrong filer
    # entirely -- so they're ranked first; largest groups otherwise.
    ranked_groups = sorted(dup_groups, key=lambda g: (-distinct_companies(g), -len(g)))
    examples = []
    for group in ranked_groups[:examples_wanted]:
        tickers = sorted({docs.get(r["doc_id"], {}).get("ticker") for r in group})
        examples.append(
            {
                "group_size": len(group),
                "distinct_companies": distinct_companies(group),
                "tickers": tickers,
                "sample_text": normalize_chunk_text(group[0]["chunk_text"])[:120],
            }
        )

    n_cross_company_groups = sum(1 for g in dup_groups if distinct_companies(g) > 1)

    result.stats = {
        "chunks_measured": len(rows),
        "chunks_empty_after_normalization": empty_after_normalize,
        "duplicate_groups": len(dup_groups),
        "chunks_in_duplicate_groups": chunks_in_dup_groups,
        "excess_copies": excess_copies,
        "duplicate_chunk_rate": round(excess_copies / len(rows), 6) if rows else None,
        "duplicate_group_size": describe([float(len(g)) for g in dup_groups]),
        "pair_partition": {
            "total_duplicate_pairs": total_pairs,
            "within_document": same_doc_pairs,
            "within_company_across_documents": within_company_cross_doc_pairs,
            "cross_company": cross_company_pairs,
        },
        "duplicate_groups_spanning_multiple_companies": n_cross_company_groups,
    }
    result.examples = examples

    if cross_company_pairs:
        result.warn(
            f"{cross_company_pairs} exact-duplicate pairs span different companies across "
            f"{n_cross_company_groups} groups -- boilerplate is inflating the index and can "
            "let a query 'succeed' against the wrong filer"
        )
    elif excess_copies:
        result.ok(
            f"{excess_copies} excess exact-duplicate chunks "
            f"({excess_copies / len(rows):.2%}), all confined to a single company"
        )
    else:
        result.ok("no exact-duplicate chunks")
    return result


# ---------------------------------------------------------------------------
# check 21 -- near-duplicate rate (MinHash on a stratified sample)
# ---------------------------------------------------------------------------


def check_21(con, docs, examples_wanted):
    result = CheckResult("21", "Near-duplicate content rate (MinHash, stratified sample)")

    rows = con.execute(
        "SELECT chunk_id, doc_id, company_id, chunk_index, chunk_text FROM chunks"
    ).fetchall()

    rng = np.random.default_rng(0)
    sampled = stratified_sample_by_ticker(rows, docs, NEAR_DUP_SAMPLE_SIZE, rng)

    coeff_a = rng.integers(1, HASH_PRIME, size=NUM_PERM, dtype=np.int64).astype(np.uint64)
    coeff_b = rng.integers(0, HASH_PRIME, size=NUM_PERM, dtype=np.int64).astype(np.uint64)

    kept_rows, signatures, normalized_texts = [], [], []
    skipped_too_short = 0
    for row in sampled:
        words = word_tokens(row["chunk_text"])
        hashes = shingle_hashes(words)
        if hashes.size == 0:
            skipped_too_short += 1
            continue
        kept_rows.append(row)
        signatures.append(minhash_signature(hashes, coeff_a, coeff_b))
        normalized_texts.append(normalize_chunk_text(row["chunk_text"]))

    if len(kept_rows) < 10:
        result.status = "SKIP"
        result.headline = "too few sampled chunks with enough words to shingle"
        return result

    sig_matrix = np.array(signatures, dtype=np.uint64)
    n = sig_matrix.shape[0]
    total_pairs = pairs(n)

    # Estimated Jaccard = fraction of the NUM_PERM permutations where the two
    # signatures agree. Compared row-by-row rather than as one (n, n, NUM_PERM)
    # tensor to keep memory flat regardless of sample size.
    flagged_pairs = []
    for i in range(n - 1):
        sims = (sig_matrix[i + 1:] == sig_matrix[i]).mean(axis=1)
        hits = np.where(sims >= NEAR_DUP_SIMILARITY_THRESHOLD)[0]
        for h in hits:
            j = i + 1 + int(h)
            is_exact = normalized_texts[i] == normalized_texts[j]
            flagged_pairs.append((i, j, float(sims[h]), is_exact))

    # Exact duplicates are check 20's territory; isolate the pairs that are
    # highly similar but NOT identical -- repeated headers/footers with a
    # page number or date swapped in, which exact hashing misses entirely.
    near_not_exact = [p for p in flagged_pairs if not p[3]]

    same_doc = same_company = 0
    for i, j, _sim, _exact in near_not_exact:
        if kept_rows[i]["doc_id"] == kept_rows[j]["doc_id"]:
            same_doc += 1
        if kept_rows[i]["company_id"] == kept_rows[j]["company_id"]:
            same_company += 1
    cross_company = len(near_not_exact) - same_company
    within_company_cross_doc = same_company - same_doc

    touched = {idx for i, j, _s, _e in near_not_exact for idx in (i, j)}

    near_not_exact.sort(key=lambda p: -p[2])
    examples = []
    for i, j, sim, _exact in near_not_exact[:examples_wanted]:
        row_a, row_b = kept_rows[i], kept_rows[j]
        examples.append(
            {
                "similarity": round(sim, 3),
                "a": {
                    "ticker": docs.get(row_a["doc_id"], {}).get("ticker"),
                    "stem": docs.get(row_a["doc_id"], {}).get("stem"),
                    "chunk_index": row_a["chunk_index"],
                },
                "b": {
                    "ticker": docs.get(row_b["doc_id"], {}).get("ticker"),
                    "stem": docs.get(row_b["doc_id"], {}).get("stem"),
                    "chunk_index": row_b["chunk_index"],
                },
            }
        )

    result.stats = {
        "chunks_sampled": len(sampled),
        "chunks_too_short_to_shingle": skipped_too_short,
        "chunks_minhashed": n,
        "shingle_size_words": SHINGLE_WORDS,
        "minhash_permutations": NUM_PERM,
        "similarity_threshold": NEAR_DUP_SIMILARITY_THRESHOLD,
        "sample_pairs_compared": total_pairs,
        "pairs_at_or_above_threshold": len(flagged_pairs),
        "of_which_also_exact_duplicates": len(flagged_pairs) - len(near_not_exact),
        "near_duplicate_not_exact_pairs": len(near_not_exact),
        "near_duplicate_pair_rate": (
            round(len(near_not_exact) / total_pairs, 8) if total_pairs else None
        ),
        "sampled_chunks_touched_by_a_near_duplicate": len(touched),
        "sampled_chunks_touched_rate": round(len(touched) / n, 4) if n else None,
        "pair_partition_near_not_exact": {
            "within_document": same_doc,
            "within_company_across_documents": within_company_cross_doc,
            "cross_company": cross_company,
        },
    }
    result.examples = examples

    touched_rate = len(touched) / n if n else 0.0
    if touched_rate > 0.05:
        result.warn(
            f"{len(touched)}/{n} sampled chunks ({touched_rate:.1%}) sit in a near-duplicate "
            f"pair at or above {NEAR_DUP_SIMILARITY_THRESHOLD} estimated Jaccard similarity"
        )
    else:
        result.ok(
            f"{len(near_not_exact)} near-duplicate (non-exact) pairs found in "
            f"{total_pairs:,} sampled comparisons ({touched_rate:.2%} of sampled chunks touched)"
        )
    return result


# ---------------------------------------------------------------------------
# check 22 -- digit-and-symbol ratio per chunk
# ---------------------------------------------------------------------------


def check_22(con, docs, examples_wanted):
    result = CheckResult("22", "Digit-and-symbol ratio per chunk")

    rows = con.execute(
        "SELECT chunk_id, doc_id, chunk_index, chunk_text, LENGTH(chunk_text) AS text_len "
        "FROM chunks WHERE chunk_text IS NOT NULL AND LENGTH(chunk_text) > 0"
    ).fetchall()

    ratios = np.empty(len(rows))
    for i, row in enumerate(rows):
        text = row["chunk_text"]
        alpha = len(ALPHA_RE.findall(text))
        ratios[i] = 1.0 - alpha / row["text_len"]

    high_mask = ratios >= NON_ALPHA_HIGH_RATIO
    p99 = percentile(ratios.tolist(), 0.99)

    order = np.argsort(-ratios)
    examples = []
    for i in order[:examples_wanted]:
        row = rows[int(i)]
        doc = docs.get(row["doc_id"], {})
        examples.append(
            {
                "ticker": doc.get("ticker"),
                "stem": doc.get("stem"),
                "chunk_index": row["chunk_index"],
                "non_alpha_ratio": round(float(ratios[i]), 4),
                "preview": (row["chunk_text"] or "")[:120],
            }
        )

    result.stats = {
        "chunks_measured": len(rows),
        "non_alpha_char_fraction": describe(ratios.tolist()),
        "p99_non_alpha_fraction": round(p99, 4) if p99 is not None else None,
        "high_ratio_threshold": NON_ALPHA_HIGH_RATIO,
        "chunks_at_or_above_high_threshold": int(high_mask.sum()),
        "high_ratio_rate": round(float(high_mask.mean()), 4),
    }
    result.examples = examples

    if high_mask.mean() > 0.10:
        result.warn(
            f"{high_mask.mean():.1%} of chunks are majority non-alphabetic "
            f"(>= {NON_ALPHA_HIGH_RATIO:.0%}) -- consistent with table/numeric dumps "
            "that survived the layout gate"
        )
    else:
        result.ok(
            f"median non-alpha fraction {percentile(ratios.tolist(), 0.5):.3f}; "
            f"{high_mask.mean():.1%} of chunks at or above {NON_ALPHA_HIGH_RATIO:.0%}"
        )
    return result


# ---------------------------------------------------------------------------
# check 23 -- Zipf's law
# ---------------------------------------------------------------------------


def check_23(con, examples_wanted):
    result = CheckResult("23", "Corpus vocabulary follows Zipf's law")

    counts: Counter = Counter()
    for (text,) in con.execute("SELECT chunk_text FROM chunks"):
        counts.update(word_tokens(text))

    if len(counts) < 20:
        result.status = "SKIP"
        result.headline = "too few distinct words to fit a rank-frequency curve"
        return result

    # most_common() and its derived arrays share one sort, so index i means
    # the same word everywhere below -- no risk of freqs and vocab drifting
    # out of alignment from two independent sorts breaking ties differently.
    vocab_sorted = counts.most_common()
    freqs = np.array([c for _, c in vocab_sorted], dtype=float)
    ranks = np.arange(1, len(freqs) + 1, dtype=float)
    log_rank = np.log10(ranks)
    log_freq = np.log10(freqs)

    regression = sp_stats.linregress(log_rank, log_freq)
    predicted = regression.slope * log_rank + regression.intercept
    residuals = log_freq - predicted
    r_squared = float(regression.rvalue) ** 2

    # Zipf's law is a mid-rank phenomenon: the handful of head stopwords and
    # the long hapax-legomena tail both deviate from a pure power law even in
    # a clean corpus, so one global R^2 can hide where the fit actually holds.
    n_bins = 3
    bin_edges = np.quantile(log_rank, np.linspace(0, 1, n_bins + 1))
    bin_idx = np.clip(np.digitize(log_rank, bin_edges[1:-1], right=True), 0, n_bins - 1)
    residual_by_third = [describe(residuals[bin_idx == b].tolist()) for b in range(n_bins)]

    # Restrict the "most over/under predicted word" examples to freq >= 5:
    # every hapax legomenon sits at log(1)=0, so the tail is trivially full of
    # large negative residuals that say nothing beyond "this word appeared
    # once", which is not a useful example of a Zipf deviation.
    reliable = np.where(freqs >= 5)[0]
    order = reliable[np.argsort(residuals[reliable])]
    most_under = [
        {"word": vocab_sorted[i][0], "count": int(freqs[i]), "residual": round(float(residuals[i]), 4)}
        for i in order[:examples_wanted]
    ]
    most_over = [
        {"word": vocab_sorted[i][0], "count": int(freqs[i]), "residual": round(float(residuals[i]), 4)}
        for i in order[-examples_wanted:][::-1]
    ]

    result.stats = {
        "distinct_words": len(counts),
        "total_word_occurrences": int(freqs.sum()),
        "log_log_regression": {
            "slope": round(float(regression.slope), 4),
            "intercept": round(float(regression.intercept), 4),
            "r_squared": round(r_squared, 4),
        },
        "ideal_zipf_slope": -1.0,
        "residual_by_log_rank_third": {
            "head": residual_by_third[0],
            "middle": residual_by_third[1],
            "tail": residual_by_third[2],
        },
        "most_over_predicted_words_freq_ge_5": most_over,
        "most_under_predicted_words_freq_ge_5": most_under,
    }
    result.examples = {
        "top_words_by_frequency": [
            {"word": w, "count": c} for w, c in vocab_sorted[:examples_wanted]
        ]
    }

    # Threshold is a reasonable analytic bar for "a clean corpus fits
    # closely", not one measured from this corpus -- the QA brief gives no
    # numeric target for this check.
    if r_squared < 0.90:
        result.warn(
            f"log-log fit R^2={r_squared:.3f} (slope {regression.slope:.3f}) -- weaker than a "
            "clean corpus should show; possible extraction corruption at scale"
        )
    else:
        result.ok(
            f"log-log fit R^2={r_squared:.3f}, slope {regression.slope:.3f} "
            "(ideal Zipf slope is -1)"
        )
    return result


# ---------------------------------------------------------------------------
# check 24 -- type-token ratio distribution
# ---------------------------------------------------------------------------


def check_24(con, docs, examples_wanted):
    result = CheckResult("24", "Type-token ratio distribution, per chunk and per document")

    rows = con.execute("SELECT chunk_id, doc_id, chunk_index, chunk_text FROM chunks").fetchall()

    chunk_ttrs, chunk_word_counts, kept_rows = [], [], []
    doc_word_counts: dict[int, Counter] = defaultdict(Counter)
    for row in rows:
        words = word_tokens(row["chunk_text"])
        if not words:
            continue
        word_counter = Counter(words)
        chunk_ttrs.append(len(word_counter) / len(words))
        chunk_word_counts.append(len(words))
        kept_rows.append(row)
        doc_word_counts[row["doc_id"]].update(word_counter)

    if len(chunk_ttrs) < 20:
        result.status = "SKIP"
        result.headline = "too few chunks with words to characterise"
        return result

    chunk_ttrs = np.array(chunk_ttrs)
    chunk_word_counts = np.array(chunk_word_counts, dtype=float)

    # TTR is mechanically length-biased -- a 20-word chunk of all-unique words
    # hits 1.0 by construction, a 500-word chunk almost never does -- so a raw
    # percentile threshold would just rediscover chunk length rather than
    # find garbling. Standardise within word-count deciles instead (same fix
    # as qa_tier3.check_16's chunks-vs-char_count residual).
    z_scores = local_z_scores(chunk_ttrs, chunk_word_counts)
    outlier_mask = np.abs(z_scores) > Z_GATE

    doc_ttrs = [
        len(counter) / total
        for counter in doc_word_counts.values()
        if (total := sum(counter.values())) > 0
    ]

    order = np.argsort(-np.abs(z_scores))
    examples = []
    for i in order:
        if not outlier_mask[i] or len(examples) >= examples_wanted:
            break
        row = kept_rows[int(i)]
        doc = docs.get(row["doc_id"], {})
        examples.append(
            {
                "ticker": doc.get("ticker"),
                "stem": doc.get("stem"),
                "chunk_index": row["chunk_index"],
                "word_count": int(chunk_word_counts[i]),
                "ttr": round(float(chunk_ttrs[i]), 4),
                "local_z_within_word_count_decile": round(float(z_scores[i]), 2),
            }
        )

    result.stats = {
        "chunks_measured": len(chunk_ttrs),
        "chunk_ttr": describe(chunk_ttrs.tolist()),
        "documents_measured": len(doc_ttrs),
        "document_ttr": describe(doc_ttrs),
        "outlier_gate": f"|TTR z-score within word-count decile| > {Z_GATE:.0f}",
        "chunks_beyond_gate_within_length_decile": int(outlier_mask.sum()),
    }
    result.examples = examples

    outlier_rate = float(outlier_mask.mean())
    if outlier_rate > 0.02:
        result.warn(
            f"{int(outlier_mask.sum())} chunks ({outlier_rate:.1%}) have a TTR beyond "
            f"{Z_GATE:.0f} SD of same-length peers -- consistent with OCR stutter (very low "
            "TTR) or fragmented noise (very high TTR)"
        )
    else:
        result.ok(
            f"median chunk TTR {percentile(chunk_ttrs.tolist(), 0.5):.3f}, "
            f"median document TTR {percentile(doc_ttrs, 0.5):.3f}"
        )
    return result


# ---------------------------------------------------------------------------
# check 25 -- year-over-year similarity for the same company
# ---------------------------------------------------------------------------


def check_25(con, docs, examples_wanted):
    result = CheckResult("25", "Year-over-year similarity for the same company")

    # report_year lives on logical_sources but is not always populated
    # upstream of the local database bootstrap; esg_year.report_year is this
    # repo's single canonical fallback, parsed straight from the pdf stem, so
    # this check works regardless of how completely the provenance tables
    # were populated for a given database.
    doc_words: dict[int, list[str]] = defaultdict(list)
    for row in con.execute(
        "SELECT doc_id, chunk_index, chunk_text FROM chunks ORDER BY doc_id, chunk_index"
    ):
        doc_words[row["doc_id"]].extend(word_tokens(row["chunk_text"]))

    doc_company = {
        row["doc_id"]: row["company_id"]
        for row in con.execute("SELECT DISTINCT doc_id, company_id FROM chunks")
    }

    by_company: dict[int, list[tuple[int, int]]] = defaultdict(list)
    unresolved_years = 0
    for doc_id in doc_words:
        doc = docs.get(doc_id)
        company_id = doc_company.get(doc_id)
        if doc is None or company_id is None:
            continue
        year = report_year(doc["stem"])
        if year is None:
            unresolved_years += 1
            continue
        by_company[company_id].append((year, doc_id))

    similarities = []
    pair_details = []
    same_year_pairs_skipped = 0
    for entries in by_company.values():
        entries.sort()
        # Only adjacent-in-sequence report pairs -- if a company skipped a
        # year (no report filed), that gap is a coverage question (Tier 5),
        # not a "how similar is this year's report" question; comparing
        # across the gap would blend the two together.
        for (year_a, doc_a), (year_b, doc_b) in zip(entries, entries[1:]):
            if year_a == year_b:
                same_year_pairs_skipped += 1
                continue
            hashes_a = shingle_hashes(doc_words[doc_a])
            hashes_b = shingle_hashes(doc_words[doc_b])
            if hashes_a.size == 0 or hashes_b.size == 0:
                continue
            set_a, set_b = set(hashes_a.tolist()), set(hashes_b.tolist())
            jaccard = len(set_a & set_b) / len(set_a | set_b)
            similarities.append(jaccard)
            pair_details.append(
                {
                    "company_id": doc_company[doc_a],
                    "ticker": docs.get(doc_a, {}).get("ticker"),
                    "year_a": year_a,
                    "year_b": year_b,
                    "stem_a": docs.get(doc_a, {}).get("stem"),
                    "stem_b": docs.get(doc_b, {}).get("stem"),
                    "jaccard_similarity": round(jaccard, 4),
                }
            )

    if len(similarities) < 5:
        result.status = "SKIP"
        result.headline = "too few consecutive-year report pairs to characterise"
        return result

    sims_arr = np.array(similarities)
    median = float(np.median(sims_arr))
    mad = float(np.median(np.abs(sims_arr - median)))
    # 1.4826 rescales MAD to be comparable to a normal-distribution SD -- the
    # standard robust-z convention, used here (rather than mean/SD) because a
    # single genuine parse-failure outlier would otherwise inflate the SD used
    # to judge it.
    robust_sd = 1.4826 * mad if mad > 0 else float(sims_arr.std(ddof=1) or 1e-9)
    robust_z = (sims_arr - median) / robust_sd
    for detail, z in zip(pair_details, robust_z.tolist()):
        detail["corpus_robust_z"] = round(z, 2)

    # The corpus-wide gate above compares every pair against one global
    # baseline, but companies template their reports to very different
    # degrees -- a company whose reports are naturally always ~0.02 similar
    # would never trip a global z-gate calibrated to the corpus median, and a
    # company whose reports are normally ~0.4 similar could drop to 0.15 (a
    # real parse failure) without ever looking like a corpus-wide outlier.
    # The question asks about deviation from THIS company's own trend, so
    # also compute a leave-one-out robust z within each company's own other
    # pairs, in log space: similarity spans 5e-05 to 0.72 in this corpus, and
    # a ratio/linear-SD comparison over that range is dominated by the large
    # values, making genuinely low pairs at the small end look unremarkable.
    # A company needs MIN_PEER_PAIRS *other* pairs before this baseline is
    # trusted -- a median of one or two peer values is too noisy to judge a
    # third against (this is exactly why the corpus-wide gate above exists as
    # a fallback for thinly-observed companies).
    log_sims = np.log10(sims_arr)
    by_company_idx: dict[int, list[int]] = defaultdict(list)
    for idx, detail in enumerate(pair_details):
        by_company_idx[detail["company_id"]].append(idx)

    for idx, detail in enumerate(pair_details):
        peer_idx = [j for j in by_company_idx[detail["company_id"]] if j != idx]
        detail["within_company_peers"] = len(peer_idx)
        if len(peer_idx) < MIN_PEER_PAIRS:
            detail["within_company_z"] = None
            continue
        peer_logs = log_sims[peer_idx]
        peer_median = float(np.median(peer_logs))
        peer_mad = float(np.median(np.abs(peer_logs - peer_median)))
        peer_sd = 1.4826 * peer_mad if peer_mad > 0 else float(peer_logs.std(ddof=1) or 1e-9)
        detail["within_company_z"] = round((log_sims[idx] - peer_median) / peer_sd, 2)

    corpus_anomalies = [d for d in pair_details if d["corpus_robust_z"] < -Z_GATE]
    within_company_anomalies = [
        d for d in pair_details if d["within_company_z"] is not None and d["within_company_z"] < -Z_GATE
    ]
    flagged = {id(d): d for d in corpus_anomalies + within_company_anomalies}.values()
    ranked = sorted(flagged, key=lambda d: d["jaccard_similarity"])
    all_ranked = sorted(pair_details, key=lambda d: d["corpus_robust_z"])

    result.stats = {
        "companies_with_orderable_years": len(by_company),
        "consecutive_year_pairs_compared": len(similarities),
        "same_year_pairs_skipped": same_year_pairs_skipped,
        "documents_with_unresolved_year": unresolved_years,
        "jaccard_similarity": describe(similarities),
        "corpus_wide_anomaly_gate": f"robust z (median, 1.4826*MAD) < -{Z_GATE:.0f}",
        "corpus_wide_anomalies": len(corpus_anomalies),
        "within_company_anomaly_gate": (
            f"leave-one-out robust z in log10(similarity), among companies with "
            f">= {MIN_PEER_PAIRS} other pairs, < -{Z_GATE:.0f}"
        ),
        "pairs_with_a_within_company_baseline": sum(
            1 for d in pair_details if d["within_company_z"] is not None
        ),
        "within_company_anomalies": len(within_company_anomalies),
        "pairs_flagged_anomalously_low": len(ranked),
    }
    result.examples = (ranked or all_ranked)[:examples_wanted]

    if ranked:
        result.warn(
            f"{len(ranked)} consecutive-year pairs score anomalously low similarity "
            f"({len(corpus_anomalies)} corpus-wide, {len(within_company_anomalies)} relative to "
            "their own company's trend) -- consistent with a parse failure hiding behind an "
            "otherwise valid-looking row"
        )
    else:
        result.ok(
            f"median year-over-year similarity {median:.3f} across "
            f"{len(similarities)} consecutive-year pairs"
        )
    return result


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


SYMBOL = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "SKIP": "SKIP"}


def render(results: list[CheckResult]) -> None:
    print("=" * 78)
    print("Tier 4 -- content validity")
    print("=" * 78)
    for result in results:
        print(f"\n[{SYMBOL[result.status]}] Check {result.key}: {result.title}")
        if result.headline:
            print(f"       {result.headline}")
        for key, value in result.stats.items():
            if isinstance(value, dict):
                print(f"       {key}:")
                for sub_key, sub_value in value.items():
                    print(f"         {sub_key}: {sub_value}")
            else:
                print(f"       {key}: {value}")
        if result.examples:
            print("       examples:")
            payload = json.dumps(result.examples, indent=2, default=str)
            for line in payload.splitlines():
                print(f"         {line}")

    print("\n" + "=" * 78)
    tally = Counter(r.status for r in results)
    print("  ".join(f"{status}={tally.get(status, 0)}"
                    for status in ("PASS", "WARN", "FAIL", "SKIP")))
    # Distributional tier: WARN is the strongest signal a check raises on its
    # own, so there is no pass/fail gate here the way Tier 1 has one.
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=config.ESG_DB)
    parser.add_argument("--checks", default="all",
                        help=f"comma-separated subset of {','.join(ALL_CHECKS)} (default: all)")
    parser.add_argument("--examples", type=int, default=5,
                        help="max example rows to show per check (default: 5)")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="also write the full result set as JSON")
    args = parser.parse_args()

    selected = ALL_CHECKS if args.checks == "all" else [
        c.strip() for c in args.checks.split(",") if c.strip()
    ]
    unknown = [c for c in selected if c not in ALL_CHECKS]
    if unknown:
        parser.error(f"unknown check(s): {', '.join(unknown)}")

    con = connect(args.db)
    try:
        docs = load_documents(con)
        results: list[CheckResult] = []

        if "20" in selected:
            results.append(check_20(con, docs, args.examples))
        if "21" in selected:
            results.append(check_21(con, docs, args.examples))
        if "22" in selected:
            results.append(check_22(con, docs, args.examples))
        if "23" in selected:
            results.append(check_23(con, args.examples))
        if "24" in selected:
            results.append(check_24(con, docs, args.examples))
        if "25" in selected:
            results.append(check_25(con, docs, args.examples))
    finally:
        con.close()

    render(results)

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(
                [
                    {
                        "check": r.key, "title": r.title, "status": r.status,
                        "headline": r.headline, "stats": r.stats, "examples": r.examples,
                    }
                    for r in results
                ],
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"\nJSON written to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
