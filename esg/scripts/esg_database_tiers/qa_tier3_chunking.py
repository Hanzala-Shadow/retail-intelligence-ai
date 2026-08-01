"""Tier 3 mathematical QA: chunking quality of the ESG corpus.

Distributional, like Tier 2 -- a check here can only flag a distribution as
implausible, never prove a defect. Run qa_tier1_invariants.py first: these
statistics are only meaningful once chunks are known to tile their sections
correctly.

The pipeline lever behind every check here is esg_chunker.py.

Everything is READ-ONLY. The database is opened with mode=ro.

Checks
    14  shape of the token-count distribution inside [100, 600]
        (histogram, mean, median, skew, kurtosis; mass at the 590-600 ceiling
        and the 100-110 floor)
    15  systematic terminal-chunk effect (mean token count by position within
        a section; last-vs-middle Welch's t-test; where short_evidence chunks
        actually land)
    16  is chunks-per-section linear in section char_count (OLS regression,
        R^2, residuals)
    17  characters-per-token distribution per chunk (garbled-text detector)
    18  concentration of the short_evidence rate across documents
    19  do chunk boundaries respect sentence boundaries (lowercase starts,
        missing terminal punctuation)

Usage
    python esg/scripts/esg_database_tiers/qa_tier3_chunking.py
    python esg/scripts/esg_database_tiers/qa_tier3_chunking.py --checks 14,17
    python esg/scripts/esg_database_tiers/qa_tier3_chunking.py --json-out reports/qa_tier3.json
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy import stats as sp_stats

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402
from qa_tier1_invariants import (  # noqa: E402
    CheckResult,
    connect,
    describe,
    load_documents,
    percentile,
)
from qa_tier2_sectioning import skew_kurtosis  # noqa: E402

# Single source of truth for the chunk-size contract; fall back to the
# current values (esg_chunker.py, 2026) if the module can't be imported.
try:
    from esg_chunker import (  # noqa: E402
        CHUNK_TYPE_SHORT_EVIDENCE,
        MAX_CHUNK_TOKENS,
        MIN_CHUNK_TOKENS,
    )
except Exception:  # pragma: no cover - keeps QA runnable without tiktoken
    MIN_CHUNK_TOKENS = 100
    MAX_CHUNK_TOKENS = 600
    CHUNK_TYPE_SHORT_EVIDENCE = "short_evidence"

ALL_CHECKS = ["14", "15", "16", "17", "18", "19"]

# Question 14 asks specifically about the 590-600 band at the ceiling; the
# floor band mirrors it at the same 10-token width on the other end of the
# [MIN_CHUNK_TOKENS, MAX_CHUNK_TOKENS] contract.
CEILING_BAND = (MAX_CHUNK_TOKENS - 10, MAX_CHUNK_TOKENS)
FLOOR_BAND = (MIN_CHUNK_TOKENS, MIN_CHUNK_TOKENS + 10)

# English prose runs roughly 4 characters per token under cl100k_base; well
# outside that, a chunk is probably a table/numeric dump (low ratio) or
# whitespace/OCR garbling (high ratio). Thresholds are the ones named in the
# QA document, not derived from this corpus.
CHARS_PER_TOKEN_LOW = 2.5
CHARS_PER_TOKEN_HIGH = 6.0

# A chunk ending in one of these (after stripping a single trailing closing
# quote/bracket) is taken to end on a sentence boundary.
TERMINAL_PUNCTUATION = {".", "!", "?"}
TRAILING_CLOSERS = {'"', "'", ")", "]", "”", "’"}

# A document's short_evidence rate must clear this floor before "top decile"
# is treated as meaningful rather than 1-of-3-chunks noise.
MIN_CHUNKS_FOR_RATE = 5


# ---------------------------------------------------------------------------
# shared loaders
# ---------------------------------------------------------------------------


def load_chunk_rows(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        """
        SELECT c.chunk_id, c.doc_id, c.section_id, c.chunk_index, c.chunk_type,
               c.token_count, LENGTH(c.chunk_text) AS text_len, c.chunk_text,
               s.section_instance_id
        FROM chunks c JOIN sections s ON s.section_id = c.section_id
        """
    ).fetchall()


def chunks_by_section(rows: list[sqlite3.Row]) -> dict[int, list[sqlite3.Row]]:
    by_section: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_section[row["section_id"]].append(row)
    for section_id, chunk_rows in by_section.items():
        chunk_rows.sort(key=lambda r: r["chunk_index"])
    return by_section


def position_label(rank: int, n: int) -> str:
    if n == 1:
        return "only"
    if rank == 0:
        return "first"
    if rank == n - 1:
        return "last"
    return "middle"


# 95% Wilson score lower bound. A raw proportion ranks a 1-of-6 document
# (17%) above a 16-of-113 document (14%) even though the second is far
# better supported; the lower bound shrinks small-n estimates toward 0 so
# ranking/flagging by it, instead of by the raw rate, isn't small-sample noise.
def wilson_lower_bound(successes: int, n: int, z: float = 1.959964) -> float:
    if n == 0:
        return 0.0
    p_hat = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    center = p_hat + z2 / (2 * n)
    margin = z * math.sqrt(p_hat * (1 - p_hat) / n + z2 / (4 * n * n))
    return (center - margin) / denom


# ---------------------------------------------------------------------------
# check 14 -- token-count distribution shape inside [100, 600]
# ---------------------------------------------------------------------------


def check_14(con, examples_wanted):
    result = CheckResult("14", "Token-count distribution shape inside [100, 600]")

    rows = con.execute(
        "SELECT chunk_id, token_count FROM chunks WHERE chunk_type != ?",
        (CHUNK_TYPE_SHORT_EVIDENCE,),
    ).fetchall()
    values = np.array([r["token_count"] for r in rows], dtype=float)

    if len(values) < 10:
        result.status = "SKIP"
        result.headline = "too few non-short_evidence chunks to characterise"
        return result

    out_of_contract = int(((values < MIN_CHUNK_TOKENS) | (values > MAX_CHUNK_TOKENS)).sum())
    skew, kurtosis_excess = skew_kurtosis(values)

    ceiling_mask = (values >= CEILING_BAND[0]) & (values <= CEILING_BAND[1])
    floor_mask = (values >= FLOOR_BAND[0]) & (values <= FLOOR_BAND[1])

    hist_counts, hist_edges = np.histogram(
        values, bins=10, range=(MIN_CHUNK_TOKENS, MAX_CHUNK_TOKENS)
    )

    result.stats = {
        "chunks_measured": len(values),
        "excludes": f"chunk_type == '{CHUNK_TYPE_SHORT_EVIDENCE}' "
                    "(a deliberately shorter, separately-contracted population)",
        "chunks_outside_contract_range": out_of_contract,
        "token_count": describe(list(values)),
        "skewness": round(skew, 4),
        "excess_kurtosis": round(kurtosis_excess, 4),
        "histogram": {
            "bin_edges": [round(e, 1) for e in hist_edges.tolist()],
            "bin_counts": hist_counts.tolist(),
        },
        "ceiling_band": {
            "range": list(CEILING_BAND),
            "count": int(ceiling_mask.sum()),
            "rate": round(float(ceiling_mask.mean()), 4),
        },
        "floor_band": {
            "range": list(FLOOR_BAND),
            "count": int(floor_mask.sum()),
            "rate": round(float(floor_mask.mean()), 4),
        },
    }
    result.examples = {
        "smallest": sorted(values.tolist())[:examples_wanted],
        "largest": sorted(values.tolist())[-examples_wanted:],
    }

    if out_of_contract:
        result.fail(
            f"{out_of_contract} chunks outside the [{MIN_CHUNK_TOKENS}, {MAX_CHUNK_TOKENS}] "
            "contract despite not being coded short_evidence"
        )
    elif ceiling_mask.mean() > 0.25:
        result.warn(
            f"{ceiling_mask.mean():.1%} of chunks sit in the "
            f"{CEILING_BAND[0]}-{CEILING_BAND[1]} ceiling band -- "
            "consistent with greedy packing splitting mid-sentence"
        )
    elif floor_mask.mean() > 0.25:
        result.warn(
            f"{floor_mask.mean():.1%} of chunks sit in the "
            f"{FLOOR_BAND[0]}-{FLOOR_BAND[1]} floor band -- consistent with over-fragmentation"
        )
    else:
        result.ok(
            f"median {percentile(list(values), 0.5):.0f} tokens; "
            f"{ceiling_mask.mean():.1%} in the ceiling band, "
            f"{floor_mask.mean():.1%} in the floor band"
        )
    return result


# ---------------------------------------------------------------------------
# check 15 -- terminal-chunk effect
# ---------------------------------------------------------------------------


def check_15(con, docs, examples_wanted):
    result = CheckResult("15", "Terminal-chunk effect (token count by position)")

    rows = load_chunk_rows(con)
    by_section = chunks_by_section(rows)

    # Position is assigned over the full ordered sequence in a section --
    # short_evidence chunks included -- so that a short_evidence chunk sitting
    # at the tail is correctly recognised as occupying the "last" slot.
    tokens_by_position: dict[str, list[float]] = defaultdict(list)
    last_position_rows: list[sqlite3.Row] = []
    short_evidence_positions: Counter = Counter()

    for chunk_rows in by_section.values():
        n = len(chunk_rows)
        for rank, row in enumerate(chunk_rows):
            label = position_label(rank, n)
            if row["chunk_type"] == CHUNK_TYPE_SHORT_EVIDENCE:
                short_evidence_positions[label] += 1
            else:
                tokens_by_position[label].append(float(row["token_count"]))
                if label == "last":
                    last_position_rows.append(row)

    middle = tokens_by_position.get("middle", [])
    last = tokens_by_position.get("last", [])

    t_stat = p_value = None
    if len(middle) >= 2 and len(last) >= 2:
        t_result = sp_stats.ttest_ind(last, middle, equal_var=False)
        t_stat, p_value = float(t_result.statistic), float(t_result.pvalue)

    n_short_evidence = sum(short_evidence_positions.values())

    result.stats = {
        "sections_with_chunks": len(by_section),
        "token_count_by_position_normal_chunks": {
            label: describe(tokens_by_position.get(label, []))
            for label in ("first", "middle", "last", "only")
        },
        "last_vs_middle_welch_t_test": {
            "t_statistic": round(t_stat, 4) if t_stat is not None else None,
            "p_value": round(p_value, 6) if p_value is not None else None,
            "mean_last": round(sum(last) / len(last), 2) if last else None,
            "mean_middle": round(sum(middle) / len(middle), 2) if middle else None,
        },
        "short_evidence_chunks_by_position": dict(short_evidence_positions),
        "short_evidence_share_at_last_position": (
            round(short_evidence_positions.get("last", 0) / n_short_evidence, 4)
            if n_short_evidence else None
        ),
    }
    # The smallest last-position chunks are the concrete cases an analyst
    # would inspect to decide "should this trailing chunk be merged backward".
    last_position_rows.sort(key=lambda r: r["token_count"])
    result.examples = [
        {
            "ticker": docs.get(row["doc_id"], {}).get("ticker"),
            "stem": docs.get(row["doc_id"], {}).get("stem"),
            "section_instance_id": row["section_instance_id"],
            "chunk_index": row["chunk_index"],
            "token_count": row["token_count"],
        }
        for row in last_position_rows[:examples_wanted]
    ]

    if p_value is not None and p_value < 0.01 and (
        (sum(last) / len(last)) < (sum(middle) / len(middle))
    ):
        result.warn(
            f"last chunks run shorter than middle chunks "
            f"(mean {sum(last) / len(last):.0f} vs {sum(middle) / len(middle):.0f} tokens, "
            f"p={p_value:.4g}) -- orphaned tail-text is a systematic effect, "
            f"not noise"
        )
    elif p_value is not None:
        result.ok(
            f"no significant last-vs-middle shortfall (p={p_value:.4g}); "
            f"{n_short_evidence} short_evidence chunks, "
            f"{short_evidence_positions.get('last', 0)} of them at the last position"
        )
    else:
        result.status = "SKIP"
        result.headline = "not enough middle/last chunks to compare"
    return result


# ---------------------------------------------------------------------------
# check 16 -- chunks-per-section linearity in char_count
# ---------------------------------------------------------------------------


def check_16(con, docs, examples_wanted):
    result = CheckResult("16", "Chunks-per-section linearity in char_count")

    chunk_counts = con.execute(
        "SELECT section_id, COUNT(*) AS n FROM chunks GROUP BY section_id"
    ).fetchall()
    counts_by_section = {r["section_id"]: r["n"] for r in chunk_counts}

    sections = con.execute(
        "SELECT section_id, doc_id, section_instance_id, char_count FROM sections "
        "WHERE char_count IS NOT NULL AND char_count > 0"
    ).fetchall()

    pairs, meta = [], []
    for section in sections:
        n_chunks = counts_by_section.get(section["section_id"])
        if n_chunks is None:
            continue
        pairs.append((float(section["char_count"]), float(n_chunks)))
        meta.append(section)

    if len(pairs) < 5:
        result.status = "SKIP"
        result.headline = "too few sections with chunks to regress"
        return result

    char_counts = np.array([p[0] for p in pairs])
    n_chunks_arr = np.array([p[1] for p in pairs])
    regression = sp_stats.linregress(char_counts, n_chunks_arr)
    predicted = regression.slope * char_counts + regression.intercept
    residuals = n_chunks_arr - predicted
    resid_sd = float(residuals.std(ddof=1))

    # The chunks-vs-char_count residual is heteroscedastic: its spread grows
    # roughly two orders of magnitude from the smallest to the largest
    # sections (more chunks means more places for the chunker to misbehave).
    # A single corpus-wide SD is dominated by a handful of huge documents, so
    # a section a few standard deviations off *for its own size band* never
    # clears a global |z| > 3 gate. Standardise within char_count deciles
    # instead, so a 3-chunk section that should have had ~1 is as visible as
    # a 700-chunk section that should have had ~400.
    n_bins = 10
    quantile_edges = np.quantile(char_counts, np.linspace(0, 1, n_bins + 1))
    bin_idx = np.clip(np.digitize(char_counts, quantile_edges[1:-1], right=True), 0, n_bins - 1)

    # A single OLS line fit across the full char_count range is systematically
    # biased within any one decile (short sections' chunk counts are
    # under-predicted, long sections' are over-predicted -- the true
    # chunks-vs-char_count relationship isn't linear over this range). That
    # per-bin bias must be subtracted before dividing by the local SD, or the
    # "z-score" is just (bias / noise) and flags entire deciles wholesale --
    # e.g. every single-chunk section in the smallest decile scored |z| > 20
    # even though every section in that bin behaves identically to its peers.
    local_mean = np.zeros(n_bins)
    local_sd = np.zeros(n_bins)
    for b in range(n_bins):
        bin_residuals = residuals[bin_idx == b]
        local_mean[b] = bin_residuals.mean() if len(bin_residuals) > 0 else 0.0
        local_sd[b] = bin_residuals.std(ddof=1) if len(bin_residuals) > 1 else 0.0
    # A bin with zero local spread (e.g. every section in it takes exactly
    # the same chunk count) would otherwise divide by zero; fall back to the
    # global SD so such a bin is merely conservative, never a crash.
    local_sd_for_section = np.where(local_sd[bin_idx] > 0, local_sd[bin_idx], resid_sd)
    centered_residuals = residuals - local_mean[bin_idx]
    z_scores = np.where(local_sd_for_section > 0, centered_residuals / local_sd_for_section, 0.0)

    outliers = []
    for section, z, pred, resid, b in zip(meta, z_scores, predicted, residuals, bin_idx):
        if abs(z) > 3:
            doc = docs.get(section["doc_id"], {})
            outliers.append(
                {
                    "ticker": doc.get("ticker"),
                    "stem": doc.get("stem"),
                    "section_instance_id": section["section_instance_id"],
                    "char_count": int(section["char_count"]),
                    "chunks": int(counts_by_section[section["section_id"]]),
                    "predicted_chunks": round(float(pred), 2),
                    "residual_z_within_size_decile": round(float(z), 2),
                }
            )
    outliers.sort(key=lambda e: -abs(e["residual_z_within_size_decile"]))

    result.stats = {
        "sections_regressed": len(pairs),
        "ols_chunks_on_char_count": {
            "slope": round(float(regression.slope), 8),
            "intercept": round(float(regression.intercept), 4),
            "r_squared": round(float(regression.rvalue) ** 2, 4),
            "slope_p_value": round(float(regression.pvalue), 6),
        },
        "global_residual_sd": round(resid_sd, 4),
        "residual_mean_by_char_count_decile": [round(float(m), 4) for m in local_mean],
        "residual_sd_by_char_count_decile": [round(float(s), 4) for s in local_sd],
        "outlier_gate": "|(residual - local decile mean) / local decile SD| > 3 "
                        "(see note above on per-decile OLS bias)",
        "sections_beyond_3sd_residual": len(outliers),
    }
    result.examples = outliers[:examples_wanted]

    if len(outliers) > 0.05 * len(pairs):
        result.warn(
            f"{len(outliers)}/{len(pairs)} sections ({len(outliers) / len(pairs):.1%}) "
            "have a chunks-vs-char_count residual beyond 3 SD"
        )
    else:
        result.ok(
            f"R^2={regression.rvalue ** 2:.3f}; "
            f"{len(outliers)} sections beyond 3 SD residual"
        )
    return result


# ---------------------------------------------------------------------------
# check 17 -- characters-per-token distribution
# ---------------------------------------------------------------------------


def check_17(con, docs, examples_wanted):
    result = CheckResult("17", "Characters-per-token distribution")

    rows = con.execute(
        """
        SELECT c.chunk_id, c.token_count, LENGTH(c.chunk_text) AS text_len,
               k.ticker, d.filepath, c.chunk_index
        FROM chunks c
        JOIN documents d ON d.doc_id = c.doc_id
        JOIN companies k ON k.company_id = c.company_id
        WHERE c.token_count IS NOT NULL AND c.token_count > 0
        """
    ).fetchall()

    ratios = np.array([r["text_len"] / r["token_count"] for r in rows])
    low_mask = ratios < CHARS_PER_TOKEN_LOW
    high_mask = ratios > CHARS_PER_TOKEN_HIGH

    def examples_for(mask: np.ndarray, ascending: bool) -> list[dict]:
        idx = np.where(mask)[0]
        idx = idx[np.argsort(ratios[idx])]
        if not ascending:
            idx = idx[::-1]
        out = []
        for i in idx[:examples_wanted]:
            r = rows[int(i)]
            out.append(
                {
                    "ticker": r["ticker"],
                    "stem": Path(r["filepath"] or "").stem,
                    "chunk_index": r["chunk_index"],
                    "token_count": r["token_count"],
                    "chars_per_token": round(float(ratios[i]), 3),
                }
            )
        return out

    result.stats = {
        "chunks_measured": len(ratios),
        "chars_per_token": describe(ratios.tolist()),
        "low_ratio_threshold": CHARS_PER_TOKEN_LOW,
        "high_ratio_threshold": CHARS_PER_TOKEN_HIGH,
        "chunks_below_low_threshold": int(low_mask.sum()),
        "chunks_above_high_threshold": int(high_mask.sum()),
        "low_ratio_rate": round(float(low_mask.mean()), 4),
        "high_ratio_rate": round(float(high_mask.mean()), 4),
    }
    result.examples = {
        "lowest_ratio": examples_for(low_mask, ascending=True),
        "highest_ratio": examples_for(high_mask, ascending=False),
    }

    flagged_rate = float((low_mask | high_mask).mean())
    if flagged_rate > 0.10:
        result.warn(
            f"{flagged_rate:.1%} of chunks fall outside the "
            f"[{CHARS_PER_TOKEN_LOW}, {CHARS_PER_TOKEN_HIGH}] chars/token band"
        )
    else:
        result.ok(
            f"median {percentile(ratios.tolist(), 0.5):.2f} chars/token; "
            f"{flagged_rate:.1%} of chunks flagged"
        )
    return result


# ---------------------------------------------------------------------------
# check 18 -- concentration of the short_evidence rate
# ---------------------------------------------------------------------------


def check_18(con, docs, examples_wanted):
    result = CheckResult("18", "Concentration of the short_evidence rate")

    rows = con.execute(
        """
        SELECT doc_id,
               SUM(CASE WHEN chunk_type = ? THEN 1 ELSE 0 END) AS n_short_evidence,
               COUNT(*) AS n_total
        FROM chunks
        GROUP BY doc_id
        """,
        (CHUNK_TYPE_SHORT_EVIDENCE,),
    ).fetchall()

    total_chunks = sum(r["n_total"] for r in rows)
    total_short_evidence = sum(r["n_short_evidence"] for r in rows)

    eligible = [r for r in rows if r["n_total"] >= MIN_CHUNKS_FOR_RATE]
    doc_rates = [r["n_short_evidence"] / r["n_total"] for r in eligible]

    # Ranking eligible documents by the raw rate re-introduces exactly the
    # small-sample noise MIN_CHUNKS_FOR_RATE is meant to screen out: with the
    # corpus's actual p90 near 7%, a single short_evidence chunk in a
    # 7-13-chunk document already clears "top decile" on the point estimate
    # alone. The Wilson lower bound shrinks low-n estimates toward 0, so a
    # document only surfaces here when its high rate is actually well
    # supported by its chunk count, not an artefact of having few chunks.
    doc_bounds = [
        wilson_lower_bound(r["n_short_evidence"], r["n_total"]) for r in eligible
    ]
    p90_bound = percentile(doc_bounds, 0.90) if doc_bounds else None
    top_decile = [
        (r, r["n_short_evidence"] / r["n_total"], bound)
        for r, bound in zip(eligible, doc_bounds)
        if p90_bound is not None and bound >= p90_bound
    ]
    top_decile.sort(key=lambda triple: -triple[2])

    result.stats = {
        "documents_with_chunks": len(rows),
        "documents_below_min_chunks_floor": len(rows) - len(eligible),
        "min_chunks_floor": MIN_CHUNKS_FOR_RATE,
        "corpus_wide_short_evidence_rate": (
            round(total_short_evidence / total_chunks, 4) if total_chunks else None
        ),
        "corpus_wide_counts": {"short_evidence": total_short_evidence, "total": total_chunks},
        "per_document_rate": describe(doc_rates),
        "per_document_wilson_lower_bound": describe(doc_bounds),
        "ranking_metric": "95% Wilson score lower bound on the rate, not the raw rate "
                          "(guards against small-n false positives)",
        "p90_wilson_lower_bound": round(p90_bound, 4) if p90_bound is not None else None,
        "documents_at_or_above_p90": len(top_decile),
    }
    result.examples = [
        {
            "ticker": docs.get(r["doc_id"], {}).get("ticker"),
            "stem": docs.get(r["doc_id"], {}).get("stem"),
            "short_evidence_chunks": r["n_short_evidence"],
            "total_chunks": r["n_total"],
            "rate": round(rate, 4),
            "wilson_lower_bound": round(bound, 4),
        }
        for r, rate, bound in top_decile[:examples_wanted]
    ]

    if not doc_rates:
        result.status = "SKIP"
        result.headline = "no document clears the minimum-chunks floor"
    else:
        worst = top_decile[0][1] if top_decile else 0.0
        if worst > 0.30:
            result.warn(
                f"worst well-supported document runs {worst:.1%} short_evidence -- "
                "likely a sectioning problem laundered as chunking policy"
            )
        else:
            result.ok(
                f"corpus rate {total_short_evidence / total_chunks:.1%}; "
                f"p90 Wilson lower bound {p90_bound:.1%}"
            )
    return result


# ---------------------------------------------------------------------------
# check 19 -- sentence-boundary integrity
# ---------------------------------------------------------------------------


def ends_on_sentence_boundary(text: str) -> bool:
    stripped = text.rstrip()
    while stripped and stripped[-1] in TRAILING_CLOSERS:
        stripped = stripped[:-1]
    return bool(stripped) and stripped[-1] in TERMINAL_PUNCTUATION


def starts_lowercase(text: str) -> bool:
    stripped = text.lstrip()
    return bool(stripped) and stripped[0].isalpha() and stripped[0].islower()


def check_19(con, docs, examples_wanted):
    result = CheckResult("19", "Sentence-boundary integrity of chunk boundaries")

    rows = load_chunk_rows(con)
    by_section = chunks_by_section(rows)

    # A chunk's start is a chunker-introduced cut only when it is NOT the
    # first chunk of its section (esg_chunker.py's chunk_token_ranges always
    # starts a section's first chunk at the section's own token 0); its end
    # is a chunker-introduced cut only when it is NOT the last chunk of its
    # section (the final chunk always runs to the section's own end).
    # Blending in first/last chunks would measure "how messy is this
    # section's own natural text edge" -- never a chunking artefact -- along
    # with "how often does the sliding window land mid-sentence", which is
    # what the question actually asks. A short_evidence chunk is always the
    # sole chunk of a 1-chunk section (rank 0 of n=1), so it carries no
    # chunker-introduced edge and is excluded by the same rank test, with no
    # extra filter needed.
    seam_start_total = seam_end_total = 0
    natural_start_total = natural_end_total = 0
    n_seam_lower = n_natural_lower = 0
    n_seam_no_terminal = n_natural_no_terminal = 0
    lowercase_examples, no_terminal_examples = [], []

    for chunk_rows in by_section.values():
        n = len(chunk_rows)
        for rank, row in enumerate(chunk_rows):
            text = row["chunk_text"]
            if not text:
                continue
            is_lower = starts_lowercase(text)
            is_no_terminal = not ends_on_sentence_boundary(text)

            if rank > 0:
                seam_start_total += 1
                if is_lower:
                    n_seam_lower += 1
                    if len(lowercase_examples) < examples_wanted:
                        doc = docs.get(row["doc_id"], {})
                        lowercase_examples.append(
                            {
                                "ticker": doc.get("ticker"),
                                "stem": doc.get("stem"),
                                "section_instance_id": row["section_instance_id"],
                                "chunk_index": row["chunk_index"],
                                "chunk_start": text.lstrip()[:80],
                            }
                        )
            else:
                natural_start_total += 1
                n_natural_lower += is_lower

            if rank < n - 1:
                seam_end_total += 1
                if is_no_terminal:
                    n_seam_no_terminal += 1
                    if len(no_terminal_examples) < examples_wanted:
                        doc = docs.get(row["doc_id"], {})
                        no_terminal_examples.append(
                            {
                                "ticker": doc.get("ticker"),
                                "stem": doc.get("stem"),
                                "section_instance_id": row["section_instance_id"],
                                "chunk_index": row["chunk_index"],
                                "chunk_end": text.rstrip()[-80:],
                            }
                        )
            else:
                natural_end_total += 1
                n_natural_no_terminal += is_no_terminal

    def rate(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 4) if denominator else None

    result.stats = {
        "chunker_introduced_starts": seam_start_total,
        "chunker_introduced_ends": seam_end_total,
        "starts_lowercase_at_chunker_seam": {
            "count": n_seam_lower,
            "rate": rate(n_seam_lower, seam_start_total),
        },
        "ends_without_terminal_punctuation_at_chunker_seam": {
            "count": n_seam_no_terminal,
            "rate": rate(n_seam_no_terminal, seam_end_total),
        },
        "for_contrast_natural_section_edges": {
            "note": "first/last chunk of a section -- not a chunking artefact; "
                    "reported only to show the seam rate above is a real effect",
            "starts_lowercase_rate": rate(n_natural_lower, natural_start_total),
            "ends_without_terminal_punctuation_rate": rate(
                n_natural_no_terminal, natural_end_total
            ),
        },
    }
    result.examples = {
        "starts_lowercase_at_chunker_seam": lowercase_examples,
        "ends_without_terminal_punctuation_at_chunker_seam": no_terminal_examples,
    }

    seam_lower_rate = rate(n_seam_lower, seam_start_total)
    seam_no_terminal_rate = rate(n_seam_no_terminal, seam_end_total)
    natural_lower_rate = rate(n_natural_lower, natural_start_total)
    natural_no_terminal_rate = rate(n_natural_no_terminal, natural_end_total)
    # Gate on the seam rate exceeding its own natural-edge baseline by a wide
    # margin, not on an absolute threshold: this corpus's prose is full of
    # bullets/headers/numeric callouts, so even a natural section edge ends
    # without terminal punctuation most of the time. What is diagnostic of
    # the chunker specifically is the seam rate running well above that.
    MARGIN = 0.10
    if seam_lower_rate is None and seam_no_terminal_rate is None:
        result.status = "SKIP"
        result.headline = "no chunker-introduced seams to measure"
    elif (
        seam_lower_rate > (natural_lower_rate or 0) + MARGIN
        or seam_no_terminal_rate > (natural_no_terminal_rate or 0) + MARGIN
    ):
        result.warn(
            f"at chunker-introduced seams: {seam_lower_rate:.1%} start lowercase "
            f"(vs {natural_lower_rate:.1%} at natural section edges), "
            f"{seam_no_terminal_rate:.1%} end without terminal punctuation "
            f"(vs {natural_no_terminal_rate:.1%} natural) -- "
            "the sliding window is cutting mid-sentence well beyond what the prose itself explains"
        )
    else:
        result.ok(
            f"at chunker-introduced seams: {seam_lower_rate:.1%} start lowercase, "
            f"{seam_no_terminal_rate:.1%} end without terminal punctuation "
            f"(natural-edge baseline: {natural_lower_rate:.1%} / {natural_no_terminal_rate:.1%})"
        )
    return result


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


SYMBOL = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "SKIP": "SKIP"}


def render(results: list[CheckResult]) -> None:
    print("=" * 78)
    print("Tier 3 -- chunking quality")
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

        if "14" in selected:
            results.append(check_14(con, args.examples))
        if "15" in selected:
            results.append(check_15(con, docs, args.examples))
        if "16" in selected:
            results.append(check_16(con, docs, args.examples))
        if "17" in selected:
            results.append(check_17(con, docs, args.examples))
        if "18" in selected:
            results.append(check_18(con, docs, args.examples))
        if "19" in selected:
            results.append(check_19(con, docs, args.examples))
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
