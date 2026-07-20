"""Offline tests for the VLM pipeline stages. No network: transports are exercised only
up to request construction; the digit screen and cache logic are pure functions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import esg_vlm_stage as vlm  # noqa: E402


# ---------------------------------------------------------------- prompt pinning
def test_prompts_are_pinned():
    """The validated prompt hashes. If these change, the regression check
    (scripts/vlm_regression_check.py) and gold revalidation MUST be re-run — that is the
    point of this test failing."""
    assert vlm.CLASSIFIER_CONFIG.model == "gpt-5-mini-2025-08-07"
    assert vlm.EXTRACTION_CONFIG.model == "gpt-5-mini-2025-08-07"
    assert vlm.CLASSIFIER_CONFIG.instr_hash == vlm.prompt_hash(vlm.CLASSIFY_INSTR)
    assert vlm.EXTRACTION_CONFIG.instr_hash == vlm.prompt_hash(vlm.EXTRACT_INSTR)
    assert "CASY" not in vlm.CLASSIFY_INSTR  # worked example must stay redacted
    assert "**[Graphic]**" in vlm.EXTRACT_INSTR


def test_cache_key_changes_with_inputs():
    a = vlm.cache_key("sha_a", 5, vlm.EXTRACTION_CONFIG)
    assert a == vlm.cache_key("sha_a", 5, vlm.EXTRACTION_CONFIG)  # stable
    assert a != vlm.cache_key("sha_b", 5, vlm.EXTRACTION_CONFIG)  # source changes
    assert a != vlm.cache_key("sha_a", 6, vlm.EXTRACTION_CONFIG)  # page changes
    assert a != vlm.cache_key("sha_a", 5, vlm.CLASSIFIER_CONFIG)  # stage changes


# ---------------------------------------------------------------- digit screen
def test_screen_annotation_two_tier():
    md = (
        "**Head Count**\n"
        "| Metric | FY 2023 |\n| --- | --- |\n| Total employees | 43,272 |\n"
        "\n"
        "**[Graphic]**\n"
        "Emissions trend 2019-2023: declining; endpoint 95,000 tCO2e.\n"
        "\n"
        "Ordinary prose citing 58% compliance.\n"
    )
    words = ["Total", "employees", "43,272", "58%", "FY", "2023"]
    ann = vlm.screen_annotation(md, words)
    # 43,272 / 58 / 2023 corroborated by text layer; graphic numbers are labeled, not gated
    assert "43,272" in vlm.numeric_tokens(md)
    assert ann["body_uncorroborated"] == []  # everything outside graphics is matched
    assert "95,000" in ann["graphic_only_numbers"]
    assert "2019" in ann["graphic_only_numbers"]
    assert ann["screen_version"] == "v3_annotator"


def test_screen_flags_uncorroborated_body_numbers():
    md = "| Revenue | 46,588,226 |\n| --- | --- |\n"
    ann = vlm.screen_annotation(md, ["Revenue", "totally", "different"])
    assert ann["body_uncorroborated"] == ["46,588,226"]


def test_split_token_join_matches():
    # pdfplumber sometimes splits "32%" into "3" + "2%" — bigram join must corroborate
    ann = vlm.screen_annotation("Compliance reached 32% this year.", ["3", "2%", "co"])
    assert ann["body_uncorroborated"] == []


def test_graphic_block_ends_at_blank_line():
    md = "**[Graphic]**\nvalue: 12\n\nProse with 99 apples.\n"
    non_g, g = vlm.split_graphic_blocks(md)
    assert "12" in g and "12" not in non_g
    assert "99" in non_g


# ---------------------------------------------------------------- request construction
def test_request_bodies_match_validated_configs():
    body = vlm.request_body(vlm.CLASSIFIER_CONFIG, b"png")
    assert body["model"] == "gpt-5-mini-2025-08-07"
    assert body["reasoning_effort"] == "low"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["content"][1]["image_url"]["detail"] == "high"

    body = vlm.request_body(vlm.EXTRACTION_CONFIG, b"png")
    assert body["max_completion_tokens"] == 8000
    assert "response_format" not in body  # extraction is markdown, not JSON


def test_batch_sharding_respects_limit(monkeypatch, tmp_path):
    """Sharding math only — submission is stubbed out."""
    submitted = []

    class FakeResp:
        def __init__(self):
            self._id = f"id_{len(submitted)}"
        def raise_for_status(self):
            pass
        def json(self):
            return {"id": self._id}

    class FakeClient:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def post(self, url, **kw):
            submitted.append(url)
            return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(vlm, "BATCH_SHARD_LIMIT_BYTES", 3000)
    items = [(f"k{i}", {"pad": "x" * 800}) for i in range(10)]
    state = tmp_path / "state.json"
    vlm.batch_submit(items, state, "t", log=lambda *_: None)
    st = json.loads(state.read_text())
    assert len(st["batches"]) > 1  # forced multiple shards
    assert sum(b["n"] for b in st["batches"].values()) == 10
    # resubmission is idempotent: same shards skipped
    n_posts = len(submitted)
    vlm.batch_submit(items, state, "t", log=lambda *_: None)
    assert len(submitted) == n_posts


# ---------------------------------------------------------------- guards
def test_api_key_only_from_env(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        vlm._api_key()
        assert False, "should refuse without env key"
    except RuntimeError as exc:
        assert "OPENAI_API_KEY" in str(exc)


def test_ocr_lineage_exclusion():
    assert vlm.is_ocr_lineage("NGVC-NATURAL GROCERS-2021")
    assert not vlm.is_ocr_lineage("AAPL-APPLE INC-2021")


def test_cost_estimate_batch_half():
    sync = vlm.estimate_cost_usd(1000, vlm.EXTRACTION_CONFIG, "sync")
    batch = vlm.estimate_cost_usd(1000, vlm.EXTRACTION_CONFIG, "batch")
    assert abs(batch - sync / 2) < 1e-9
