from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image

from asset_factory_blueprint.reconstruction_backends import sha256_text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.reproducibility.run_benchmark import (  # noqa: E402
    canonical_sha256,
    backfilled_execution_trace,
    compare_invariants,
    configured_agent_review,
    execution_trace,
    fail_execution_trace,
    finalise_execution_trace,
    _next_remediation,
    _review_unavailable,
    _asset_crop,
)


def test_reproducibility_benchmark_has_two_five_asset_cohorts() -> None:
    benchmark = json.loads((ROOT / "benchmarks" / "reproducibility" / "benchmark.json").read_text(encoding="utf-8"))

    assert benchmark["repeat_count"] == 5
    assert len(benchmark["image_sources"]) == 5
    assert len(benchmark["usd_sources"]) == 5
    assert benchmark["image_reconstruction"]["backend_id"] == "trellisv2"
    assert benchmark["usd_vlm_review"]["provider"] == "nvidia_nim"
    assert benchmark["mesh_verification"]["mandatory"] is True
    assert benchmark["mesh_verification"]["agent_id"] == "mesh-verification-agent"
    assert benchmark["mesh_verification"]["seed"] == 17
    assert benchmark["mesh_verification"]["quality_policy"]["profile"] == "appearance_mesh"
    assert benchmark["mesh_verification"]["quality_policy"]["require_watertight"] is False
    assert benchmark["mesh_verification"]["max_total_inference_attempts"] == 4
    assert benchmark["mesh_verification"]["max_inference_resubmissions"] == 3
    assert benchmark["mesh_verification"]["rejection_policy"] == "adaptive_conditioning_same_backend"
    assert all("alternate_source" in source for source in benchmark["image_sources"])
    assert all(source.get("verification_aliases") for source in benchmark["image_sources"])
    fire_source = next(source for source in benchmark["image_sources"] if source["id"] == "fire_extinguisher")
    assert fire_source["asset_crop_box"] == [1050, 1350, 2200, 3400]


def test_reproducibility_benchmark_keeps_runtime_root_out_of_source_config() -> None:
    benchmark = json.loads((ROOT / "benchmarks" / "reproducibility" / "benchmark.json").read_text(encoding="utf-8"))

    assert "root" not in benchmark["source_cache_layout"]


def test_planned_run_checksum_uses_adapter_serialisation() -> None:
    payload = {"id": "benchmark-1-coffee_mug-repeat-01", "runtime_env": {"AFB_ENV": "local"}}

    assert canonical_sha256(payload) == sha256_text(json.dumps(payload, sort_keys=True))


def test_execution_trace_records_agent_review_and_inference_outcome(tmp_path: Path) -> None:
    benchmark = json.loads((ROOT / "benchmarks" / "reproducibility" / "benchmark.json").read_text(encoding="utf-8"))
    manifest_path = tmp_path / "external-model-run-manifest.json"
    manifest_path.write_text(json.dumps({"run_id": "repeat-01"}), encoding="utf-8")
    trace = execution_trace(manifest_path, benchmark)

    assert trace["agent_review"]["configured"] is True
    assert trace["agent_review"]["reviewer_id"] == "mesh-verification-agent"
    assert trace["agent_review"]["final_decision"] == "pending"
    finalise_execution_trace(trace, {"status": "proposal", "execution_status": "completed"})

    assert trace["events"][-1]["kind"] == "inference_completed"
    assert trace["events"][-1]["inference_attempt"] == 1


def test_execution_trace_records_adapter_errors(tmp_path: Path) -> None:
    benchmark = json.loads((ROOT / "benchmarks" / "reproducibility" / "benchmark.json").read_text(encoding="utf-8"))
    manifest_path = tmp_path / "external-model-run-manifest.json"
    manifest_path.write_text(json.dumps({"run_id": "repeat-01"}), encoding="utf-8")
    trace = execution_trace(manifest_path, benchmark)
    fail_execution_trace(trace, RuntimeError("adapter unavailable"))

    assert trace["final_status"] == "error"
    assert trace["events"][-1]["kind"] == "inference_error"
    assert trace["events"][-1]["message"] == "adapter unavailable"


def test_backfilled_execution_trace_preserves_result_manifest_evidence(tmp_path: Path) -> None:
    manifest_path = tmp_path / "external-model-run-manifest.json"
    result_path = tmp_path / "external-model-run-manifest.result.json"
    trace = backfilled_execution_trace(
        manifest_path,
        result_path,
        {"run_id": "repeat-01", "status": "proposal", "execution_status": "completed"},
    )

    assert trace["trace_mode"] == "backfilled_from_result_manifest"
    assert trace["events"][0]["evidence"] == result_path.as_posix()
    assert trace["agent_review"]["mesh_rejections"] == 0


def test_benchmark_refuses_optional_mesh_verification() -> None:
    benchmark = json.loads((ROOT / "benchmarks" / "reproducibility" / "benchmark.json").read_text(encoding="utf-8"))
    benchmark["mesh_verification"]["mandatory"] = False

    try:
        configured_agent_review(benchmark)
    except ValueError as error:
        assert "must be mandatory" in str(error)
    else:
        raise AssertionError("optional mesh verification was accepted")


def test_invariant_comparison_does_not_require_byte_identity() -> None:
    comparison = compare_invariants(
        [
            {"euler_characteristic": 2, "genus_total": 0},
            {"euler_characteristic": 2, "genus_total": 0},
            {"euler_characteristic": 2, "genus_total": 0},
            {"euler_characteristic": 2, "genus_total": 0},
            {"euler_characteristic": 2, "genus_total": 0},
        ],
        ("euler_characteristic", "genus_total"),
        5,
    )

    assert comparison["euler_characteristic"]["matches"] is True
    assert comparison["genus_total"]["matches"] is True


def test_undefined_genus_is_not_reported_as_matching() -> None:
    comparison = compare_invariants(
        [{"genus_total": None} for _ in range(5)],
        ("genus_total",),
        5,
    )

    assert comparison["genus_total"]["comparable"] is False
    assert comparison["genus_total"]["matches"] is None


def test_invariant_comparison_uses_configured_repeat_count() -> None:
    comparison = compare_invariants(
        [{"watertight": True} for _ in range(3)],
        ("watertight",),
        3,
    )

    assert comparison["watertight"]["comparable"] is True
    assert comparison["watertight"]["matches"] is True


def test_adaptive_retry_selects_new_conditioning_instead_of_plain_resubmission() -> None:
    source_mismatch = [{"defect_tag": "source_mismatch"}]
    extra_geometry = [{"defect_tag": "extra_geometry"}]

    assert _next_remediation(source_mismatch, {"original"}) == "rmbg_original"
    assert _next_remediation(extra_geometry, {"original"}) == "rmbg_original"
    assert _next_remediation(extra_geometry, {"original", "rmbg_original"}) == "alternate_photo"
    assert (
        _next_remediation(
            extra_geometry,
            {"original", "rmbg_original", "alternate_photo", "rmbg_alternate"},
        )
        is None
    )


def test_provider_timeout_is_not_counted_as_a_mesh_rejection() -> None:
    assert _review_unavailable(
        {
            "decision_reason": "vision provider call failed: The read operation timed out",
            "reviewer": {"provider": ""},
        }
    )
    assert _review_unavailable(
        {
            "decision_reason": "reviewer response was not a valid JSON verdict",
            "reviewer": {"provider": "nvidia_nim"},
        }
    )
    assert not _review_unavailable(
        {
            "decision_reason": "candidate contains source background",
            "reviewer": {"provider": "nvidia_nim"},
        }
    )


def test_asset_crop_extracts_declared_foreground_region(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    target = tmp_path / "crop.jpg"
    Image.new("RGB", (100, 80), "white").save(source)

    _asset_crop(source, target, [10, 15, 70, 65])

    with Image.open(target) as crop:
        assert crop.size == (60, 50)
