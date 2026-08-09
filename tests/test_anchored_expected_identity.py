import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_anchored_expected_identity.py"
SPEC = importlib.util.spec_from_file_location("expected_identity", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_expected_identity_accepts_frozen_structure(tmp_path):
    expected = "a" * 64
    evidence = [
        {
            "final_rank": rank,
            "selection_reason": "l12_anchor" if rank <= 6 else "bge_hard_round_robin",
        }
        for rank in range(1, 17)
    ]
    row = {
        "request_id": "smoke",
        "status": "success",
        "policy_id": "balanced_anchored_round_robin_k16",
        "evidence_count": 16,
        "evidence_identity_sha256": expected,
        "evidence_identity": evidence,
    }
    (tmp_path / "responses.jsonl").write_text(__import__("json").dumps(row) + "\n")
    report = MODULE.validate(tmp_path, expected)
    assert report["structural_pass"] is True
