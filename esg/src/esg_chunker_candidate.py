"""Compatibility names for the promoted ESG chunk v3 splitter."""

from esg_chunker import (  # noqa: F401
    BGE_INPUT_LIMIT,
    BGE_MODEL_LIMIT,
    CandidateChunk,
    chunk_section_v3 as chunk_section_candidate,
    final_bge_token_count,
    final_embedding_text,
    validate_v3_tiling as validate_candidate_tiling,
)
