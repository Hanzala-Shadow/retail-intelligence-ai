# ESG Evaluation Assets

Maintained by: Aziz Daurov (ESG Document Intelligence & RAG Evaluation Lead).

Three durable research assets underpin the quality claims of the ESG corpus:
a labelled gold set for page-layout classification, a deterministic verifier
for structure extraction, and a corpus-wide table-signal feature table. All
documents are public filings and published sustainability reports; pages are
identified by ticker, source PDF, and 1-based page number.

## 1. Page-layout gold set (449 pages)

Files: `data/00_reference/esg_layout_gold_labels.csv` (final),
`esg_layout_gold_annotator1.csv` / `esg_layout_gold_annotator2.csv` (the two
independent passes), `esg_layout_gold_disagreements.csv` (all 43
disagreements with resolutions).

**Sampling** (seed 20260717): population = the 9,078 pages whose layout QA
decision is `auto_pass_column_order_reconstructed`. Two strata: 299
representative pages drawn proportionally by column count (202/70/27 for
2/3/4 columns) and 150 boundary pages enriched from the
`cross_column_fraction_short` band [0.02, 0.30]. Report prevalence numbers
from the representative stratum only — never pool the boundary stratum.

**Protocol:** two independent annotation passes over all 449 pages
(three-class: `prose` / `table_dominant` / `ambiguous_or_mixed`, plus a
subtype), then adjudication of the 43 disagreements under written rules,
then a full page-by-page owner review of all 449 labels. Key adjudication
rules: label–value detachment is the primary test (label and value
vertically contiguous reads as prose regardless of card-like appearance); a
ruled table with period/category headers and at least two data rows makes a
page at least ambiguous even when small; catalogues of self-contained entity
profiles are prose, grids keyed to a diagram legend are ambiguous.

**Agreement:** three-class kappa 0.804; binary `table_dominant`-vs-rest
kappa 0.962 (both passes independently found exactly 103 table-dominant
pages; 41 of 43 disagreements sit on the prose/ambiguous taxonomy seam).
Quote both numbers: 0.804 for the protocol, 0.962 for the decision the
downstream gate actually makes.

**Headline estimate:** in the representative stratum, 20.4% of
reconstructed pages are table-dominant (95% Wilson CI 16.2–25.3).

**Caveat:** the subtype column was not adjudicated on class-agreed rows; do
not report subtype proportions as adjudicated truth.

## 2. Structure-extraction verifier v2

Files: `src/esg_structure_verifier.py`,
`tests/test_esg_structure_verifier.py`.

Deterministic (no LLM at verification time). Extraction records are
word-index-only — every label, value, unit, and qualifier must point at
words on the page's own word list, so no numeric token ships unless it
appears verbatim on the page. Gates: token anchoring with role-collision
detection; a literal numeric grammar for values; spatial coherence (strict
±4pt for label+value+unit within a declared card bbox or row band, loose
±250pt for qualifiers); single consumption of each value token per page;
fail-closed table-header plausibility (a proposed header must be
majority label-like and not value-dominated — this closes the observed
failure class where an adjacent accounting figure passed as a fiscal-year
qualifier); and qualifier provenance (a qualifier physically inside another
record's value band is rejected). Non-gating outputs: a self-containment
grade per record (`full` / `partial` / `bare`) and a per-page numeric-token
reconciliation (orphaned-number share).

Validation: 206/206 records pass on the held-structural pilot (27 pages);
three deliberately planted prose probe pages produced zero records — the
correct fail-closed outcome. The two header parameters were frozen on the
development split before the held-out run. Bare-record shippability rule:
a bare record ships only from a single-value-column table or stat card;
multi-value-column bare records never ship.

## 3. Lines-strategy table-signal table (9,078 pages)

Files: `data/00_reference/esg_lines_area_signals.csv`,
`scripts/build_esg_lines_area_signals.py`.

Per-page ruled-table area share (pdfplumber `find_tables`, lines strategy)
over the full reconstructed frame, with per-table detail and
border-frame-excluded variants at 0.80/0.90/0.95 cutoffs (a near-page-sized
box with <= 4 cells is a decorative border, not a data table; on the gold
set 28 of 36 high-share prose pages are exactly one such box). Zero
per-page errors; the 449 gold pages reproduce the gold feature values
exactly.

This signal was built as a candidate manifest-level exclusion gate and the
measured answer is negative: no threshold removes scrambled table pages
within a 2% prose false-hold budget at useful recall (dominance vs presence:
a page can carry a large ruled table and still read as prose). It is kept as
a documented negative result and as the strongest single feature measured to
date for table-page detection (AUC 0.761 on gold), for reuse in any future
multi-signal gate.
