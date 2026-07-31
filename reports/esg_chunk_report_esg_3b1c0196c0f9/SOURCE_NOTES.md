# Source notes

- Audience: technical.
- Report scope: current chunk dataset `esg_3b1c0196c0f9` and the deterministic 2,000-chunk handoff.
- Chart map: `topic_distribution` answers how the handoff is allocated across ESG topics. It is a single-series bar chart using `topic` and `handoff_chunks`; `safe_pool_chunks` and `rank` remain available for audit tooltips.
- Palette policy: single-root preferred; the portable reader supplies the shared blue palette and neutral scaffolding.
- The report does not state the percentage of chunks eligible for retrieval, as requested.
- The sample is designed and stratified, so its review results are not an unweighted statistical defect estimate.
- The vector manifest was rebuilt against the current chunk index before sampling. The vector database was not rebuilt.
