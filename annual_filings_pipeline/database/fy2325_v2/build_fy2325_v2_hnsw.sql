\set ON_ERROR_STOP on
CREATE INDEX CONCURRENTLY IF NOT EXISTS fy2325_v2_embeddings_hnsw
ON fy2325_v2_embeddings USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
ANALYZE fy2325_v2_embeddings;
ANALYZE fy2325_v2_chunks;
UPDATE fy2325_v2_datasets
SET status='indexed'
WHERE dataset_id='fy2325-v2.16' AND status='validated';
