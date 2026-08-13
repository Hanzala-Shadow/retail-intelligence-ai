# Annual-filings private GPU reranker

## Frozen scope

The production server remains authoritative for PostgreSQL, BGE Base query
embedding, candidate construction, decomposition, balanced K16 selection,
Bedrock generation, citations, API, and UI. The Frankfurt `g4dn.xlarge`
receives only question/passage pairs and returns finite scores from the two
frozen rerankers.

The remote switch does not alter model IDs/revisions, `embedding_text`,
`max_length=512`, six anchors, balanced allocation, source-identity dedupe, or
K16. The default backend remains local until explicitly enabled.

## Environment contract on the production server

```text
RAG_MODEL_DEVICE=cpu
RAG_RERANKER_BACKEND=remote
RAG_GPU_RERANKER_URL=http://10.60.13.59:9000
RAG_GPU_RERANKER_TOKEN=<secure 64-hex worker token>
RAG_GPU_RERANKER_TIMEOUT_SECONDS=600
RAG_ANCHORED_CONFIG=config/retrieval_anchored_k16_gpu_v1.json
```

Never commit the token or pass it on a command line. The GPU endpoint is bound
to its private IP and security-group port 9000 accepts only
`172.31.28.68/32`.

## Rollback

Set `RAG_RERANKER_BACKEND=local`, keep `RAG_MODEL_DEVICE=cpu`, and restart the
calling process. The frozen CPU implementation remains present. File rollback
is separately available through the transfer package.

## Promotion gate

The remote smoke must return 16 evidence rows, six anchors, exact ranks 1..16,
zero errors, and this frozen identity:

```text
9df899d0e738c6ba71fc981c373798a34b2dc90c3c62d3f88aa3a60e1a603060
```

Run at least ten warm sequential requests only after the one-request identity
gate passes. Record p50, p95, maximum, error count, GPU utilization, peak VRAM,
and transfer/inference timing. Do not promote on speed alone if identity drifts.
