# ESG corpus provenance: v1 to v2

What the ESG chunk corpus is, how it got here, and where the previous states
are kept. Written 2026-08-11, covering the `esg_docling_fusion_v1` to
`esg_docling_fusion_v2` transition.

The point of this file is that three different chunk indexes existed within
four days, two of them stamped `esg_docling_fusion_v2`. Anyone reading a
handout, a database or an embedding run needs to know which one they have.

## Corpus lineage

Three builds exist. Only the third is current.

| Build | Rows | Retrievable | `dataset_id` | Status |
| --- | --- | --- | --- | --- |
| pre-v2 | 50,792 | 50,039 | `esg_docling_fusion_v1` | superseded, preserved |
| v2 legacy | 50,536 | 49,717 | `esg_docling_fusion_v2` | superseded, preserved |
| **live** | **50,510** | **49,734** | `esg_docling_fusion_v2` | **current** |

The two v2 builds share a `dataset_id` but are not the same corpus. They
differ by 474 chunk IDs present only in the legacy build, 448 present only in
the live build, and 942 differing `embedding_text` values among the rows they
share. The legacy build is the one the first round of handouts was cut from.

The distinguishing change is upstream of chunking: the live build regenerated
`esg_sections_index.csv`, so section boundaries moved and chunk identity moved
with them. `CHUNKER_VERSION` is `esg_chunk_v4` for both v2 builds.

## Timeline

| When | Event |
| --- | --- |
| 2026-08-07 | v1 corpus and sections in place; last pre-v4 commit `e190738` |
| 2026-08-08 | v4 chunker run producing the v2 legacy build; first handouts cut from it |
| 2026-08-10 19:22 | `f846f7c` commits the v4 chunker, its tests and the handoff builder |
| 2026-08-10 22:42-22:46 | operator-run v2 promotion rewrites the canonical index and sections |
| 2026-08-11 | quality checks re-run against the live build; embedding input exported |

The promotion was run by the repository operator, not by the chunker itself.
It rewrote `data/00_reference/esg_chunks_index.csv` and
`data/00_reference/esg_sections_index.csv` in place, and relocated the loose
databases and handout archives out of `data/`.

## What v2 changed, measured against pre-v2

Verified on the live build. Coverage first, because a quality gate that
quietly drops companies is not a quality gate.

| Measure | pre-v2 | live v2 |
| --- | --- | --- |
| Companies | 120 | 120 |
| Source documents | 682 | 682 |
| Company-topic pairs | 1,842 | 1,842 |
| Document-topic pairs | 8,276 | 8,270 |
| U+FFFD in served chunks | 1 | 0 |
| Subsection headers over 200 chars | 1,179 | 0 |
| `unknown`/`other` subsection | 602 | 482 |
| Chunks labelled `table` | 2,611 | 8,404 |
| Chunks labelled `table_continuation` | 239 | 7,136 |

Nine document-topic pairs disappear and three appear. Those nine are a
relabelling, not a loss: Dollar General's 2019 CEO letter, AutoZone's 2021
board-diversity figures and Brilliant Earth's company description were all
traced by text into the live build, still served, under `section_code = other`.

Consequence for retrieval design: `other` now carries substantive disclosure,
not just leftovers. It cannot be treated as a low-value section pool.

The 776 excluded rows are 531 table-of-contents and navigation variants, 212
furniture spans, 32 short-section combinations and 1 encoding-damaged chunk.

Header cost was checked against BGE's window, since the header competes with
body text for the same 512 tokens: header mean 45.3 tokens, maximum 119, and
0 of 4,000 sampled chunks exceed 512. Nothing truncates.

What has *not* been measured is retrieval itself. There is no adjudicated ESG
question set in this repository, so no recall number backs any of the above.
The evidence here is structural.

## Where the previous states are kept

`data/_pre_promotion_backups/20260810T194256Z_v2_promotion/`

| Path | Contents |
| --- | --- |
| `esg_chunks_index_pre_v2.csv` | the v1 chunk index |
| `esg_sections_index_pre_v2.csv` | the v1 sections index |
| `chunks_sustainability_pre_v2/` | the v1 chunk text files |
| `sections_sustainability_pre_v2/` | the v1 section text files |
| `esg_chunks_index_v2_legacy.csv` | the v2 legacy chunk index |
| `chunks_sustainability_v2_legacy/` | the v2 legacy chunk text files |
| `legacy_qa_and_exports/` | QA databases, `esg_v3.zip`, and the first handout archives |

Both prior corpora are recoverable in full. Nothing was deleted to make room
for the live build.

Separately, superseded handout packages and the v4 candidate index and chunks
were deleted on 2026-08-10 after the promotion; all were regenerable from a
chunk index via `esg/scripts/build_chunk_handoff.py`.

## Current artifacts

| Artifact | Built from | Fresh? |
| --- | --- | --- |
| `data/00_reference/esg_chunks_index.csv` | live | yes |
| `data/05_embedding/esg/embedding_input_v2.csv.gz` | live | yes |
| `data/handoff_2000_v2/` | v2 legacy | **stale** |
| `data/handout_full_v2/` | v2 legacy | **stale** |

Both handouts reference 474 chunk IDs that no longer exist in the live corpus.
They are self-contained and still open, but they no longer describe what the
pipeline serves. Rebuild them from the live index before sending either out.

## Open items

- No adjudicated ESG question set exists, so retrieval quality is unmeasured
  and the v1-versus-v2 comparison rests on structure alone.
- Five tests in `esg/tests/test_esg_heading_quality.py` fail on
  `Phase_5.1_Aziz`. They predate `f846f7c` and exercise `section_splitter_esg`,
  which the v4 work did not touch. Sectioning changed in the promotion, so
  these are worth revisiting.
- The handoff manifest records no build timestamp, chunker version or
  checksum of the emitted index.
