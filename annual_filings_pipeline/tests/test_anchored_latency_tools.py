import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "validate_anchored_latency_equivalence.py"
SPEC = importlib.util.spec_from_file_location("latency_equivalence", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def response(request_id="q1", identity="abc", total=1000.0):
    evidence = [{
        "final_rank": rank,
        "source_chunk_id": f"source-{rank}",
        "chunk_id": rank,
        "selected_for_subquery_id": "sq-1",
        "selection_reason": "l12_anchor" if rank <= 6 else "bge_hard_round_robin",
    } for rank in range(1, 17)]
    return {
        request_id: {
            "request_id": request_id,
            "status": "success",
            "policy_id": "balanced_anchored_round_robin_k16",
            "evidence_count": 16,
            "evidence_identity": evidence,
            "evidence_identity_sha256": identity,
            "runtime_profile": {"timings_ms": {"total": total}},
        }
    }


def test_equivalence_accepts_exact_identity_and_reports_speedup():
    report = MODULE.compare_runs(
        response(total=8000.0),
        response(total=1000.0),
    )
    assert report["structural_pass"] is True
    assert report["exact_matches"] == 1
    assert report["mean_speedup"] == 8.0


def test_equivalence_rejects_identity_drift():
    report = MODULE.compare_runs(
        response(identity="baseline"),
        response(identity="changed"),
    )
    assert report["structural_pass"] is False
    assert report["failures"][0]["reasons"] == ["identity_hash"]
