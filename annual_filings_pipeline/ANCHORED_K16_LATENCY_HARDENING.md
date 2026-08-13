# Anchored K16 latency hardening

## Frozen quality contract

Latency work must not change the BGE Base bi-encoder, six MiniLM-L12 anchors,
BGE reranker v2-M3 expansion, combined 512-token input, source-identity
deduplication, balanced allocation, or final K16 evidence limit. Database writes,
gold fields, question-ID overrides, and benchmark-specific routing remain
prohibited.

The CPU configuration remains the production-safe default. The GPU profile
changes only batch size and model lifecycle. GPU execution also requires the
process-local `RAG_MODEL_DEVICE=cuda` opt-in.

## Outputs

Each anchored response now includes `runtime_profile` with:

- bi-encoder load, embedding, database fetch, candidate merge, reranker load,
  reranker inference, selection, and total milliseconds;
- hard/soft positions and unique candidates;
- unique requirement/chunk pairs scored by each reranker;
- inference device and model lifecycle;
- peak resident memory; and
- a SHA-256 fingerprint of ranked evidence identity without passage text.

The benchmark runner exports metadata and evidence identities only. It does not
export questions, passage text, embedding text, or generated answers.

## CPU baseline

```bash
python scripts/benchmark_anchored_latency.py \
  --requests inputs/anchored_latency_smoke_requests.jsonl \
  --output-dir /tmp/anchored_latency_cpu \
  --config config/retrieval_anchored_k16_v1.json \
  --device cpu \
  --expected-requests 1 \
  --env-file "$HOME/projects/retail-intelligence-ai/.env"
```

## GPU challenger

Use a CUDA-enabled PyTorch environment on an isolated GPU worker with read-only
database connectivity:

```bash
python scripts/benchmark_anchored_latency.py \
  --requests inputs/anchored_latency_smoke_requests.jsonl \
  --output-dir /tmp/anchored_latency_gpu \
  --config config/retrieval_anchored_k16_gpu_v1.json \
  --device cuda \
  --expected-requests 1 \
  --env-file /secure/path/to/.env
```

## Equivalence gate

```bash
python scripts/validate_anchored_latency_equivalence.py \
  --baseline-dir /tmp/anchored_latency_cpu \
  --challenger-dir /tmp/anchored_latency_gpu
```

Promotion requires all requests to succeed, exact ranked evidence identity, no
database writes, and materially lower latency. The first target is retrieval
p95 at or below 10 seconds. If exact GPU execution misses that target, shortlist
or conditional-BGE experiments must be tuned on development questions and pass
a separate frozen evidence-quality evaluation before production activation.
