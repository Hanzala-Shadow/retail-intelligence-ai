# Production 10-K Retrieval

Entry point: `src/query_api.py`

## Locked policy

The production path is:

1. require explicit ticker, filing year, accession number, document type and section for every authorized source;
2. hard-filter `rag_eligible_10k_chunks` by that metadata;
3. retrieve the top 20 BGE Base semantic candidates per source;
4. rerank each source's candidates with pinned `cross-encoder/ms-marco-MiniLM-L6-v2`;
5. merge multiple sources deterministically by round robin and return five evidence chunks.

The code does not accept runtime overrides for candidate depth, final depth, model identity, section routing or fusion weights. Changing the policy requires a reviewed code change and new evaluation evidence.

## Request contract

```json
{
  "request_id": "example-1",
  "question": "Why did sales fall and what happened to gross margin?",
  "sources": [
    {
      "ticker": "SNBR",
      "filing_year": 2025,
      "doc_type": "10-K",
      "accession_number": "0000827187-25-000018",
      "section_code": "Item_7"
    }
  ]
}
```

`doc_type` defaults to `10-K` and every other source field is mandatory. A source with no eligible chunks fails closed.

## Invocation

```bash
python src/query_api.py --request request.json --output response.json
```

Use `--request -` to read JSON from standard input. Omit `--output` to write JSON to standard output. An output path is created immutably and is never overwritten.

Database connection follows existing repository conventions: `DB_URL`, then `DATABASE_URL`, then standard PostgreSQL `PG*` environment variables.

Install model dependencies with:

```bash
pip install -r requirements-retrieval.txt
```

## Response and decomposition interface

The response contains the pinned policy identity, authorized sources, candidate counts and five ordered evidence objects. Each evidence object includes citation metadata, original `chunk_text`, embedding text, semantic score/rank, cross-encoder score/rank, its source specification and final rank.

The decomposition component owns sub-query generation. It must call this entry point with one or more explicit authorized sources. This retriever owns filtering, candidate retrieval and reranking. It does not infer sources, compare facts, generate prose or change benchmark labels.

For multi-source requests, candidates are reranked within each source and merged round-robin. This prevents one filing from silently occupying all five evidence positions and gives the aggregation layer evidence from each requested source when available.

## Baseline interpretation

The frozen benchmark must be executed through this entry point before its output is called the official production baseline. Retrieve-20 improves candidate availability; it does not by itself prove that every near-miss will enter the final top five.

The prior controlled full-pool cross-encoder result was Hit@5 70.83%, Recall@5 66.67%, MRR@5 52.08% and nDCG@5 53.03%. Treat these as an experimental reference until the production-path rerun is complete.
