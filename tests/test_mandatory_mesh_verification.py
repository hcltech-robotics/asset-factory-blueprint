from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import trimesh
from PIL import Image

from asset_factory_blueprint.config import load_json
from asset_factory_blueprint import agent_loop
from asset_factory_blueprint.manifests import load_schema
from asset_factory_blueprint.orchestrator import build_run_plan, route_ids
from asset_factory_blueprint.schemas.common import RunRequest
from asset_factory_blueprint.services import mesh_verification
from asset_factory_blueprint.services.progress import build_progress
from asset_factory_blueprint.skills.base import ToolResult


def _candidate(project_dir: Path) -> Path:
    target = project_dir / "assets" / "test_asset" / "candidate.glb"
    target.parent.mkdir(parents=True, exist_ok=True)
    trimesh.creation.box(extents=(1.0, 0.75, 0.5)).export(target)
    return target


def test_every_geometry_route_includes_mandatory_mesh_verification() -> None:
    request = RunRequest(
        id="mesh_test",
        objective="create a reviewed asset",
        sources=["source.png"],
        requested_outputs=["textured asset"],
    )

    routed = route_ids(request)
    contracts = load_json("configs/stage-contracts.json")

    assert "mesh-verification" in routed
    assert contracts["stages"]["reconstruction"]["produces"] == [
        "reconstruction-manifest",
        "candidate-geometry",
    ]
    assert contracts["stages"]["mesh-verification"]["produces"] == [
        "mesh-verification-record",
        "canonical-geometry",
    ]
    assert contracts["stages"]["segmentation"]["depends_on"] == ["mesh-verification"]
    mesh_stage = next(stage for stage in build_run_plan(request).stages if stage.id == "mesh-verification")
    assert "mesh-verification" in mesh_stage.validation_gates
    assert "vlm-signoff" not in mesh_stage.validation_gates


def test_geometry_fixes_always_repeat_mandatory_mesh_verification() -> None:
    fixes = {item["fix_id"]: item for item in load_json("configs/fix-library.json")["fixes"]}

    assert "mesh-verification" in fixes["smooth_lumpy_region"]["reverify"]
    assert "mesh-verification" in fixes["prune_floating_fragments"]["reverify"]


def test_mesh_gate_generates_diagnostics_and_blocks_without_agent_approval(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)

    record = mesh_verification.prepare_mesh_verification(
        tmp_path,
        asset_id="test_asset",
        project_id="project_test",
        candidate_path=candidate,
    )

    assert record["gate_status"] == "blocked"
    assert record["diagnostics"]["metrics"]["vertex_count"] == 8
    assert record["render_bundle"]["status"] == "generated"
    assert len(record["render_bundle"]["images"]) == 3
    assert any("approval is missing or stale" in reason for reason in record["blocked_reasons"])
    evidence_uris = {item["evidence_id"]: item["uri"] for item in record["evidence"]}
    assert evidence_uris["candidate_geometry"] == "assets/test_asset/candidate.glb"
    assert evidence_uris["mesh_diagnostics"] == "reports/mesh-verification/diagnostics.json"
    assert all(not Path(uri).is_absolute() for uri in evidence_uris.values())
    assert all((tmp_path / uri).is_file() for uri in evidence_uris.values())


def test_source_candidate_comparison_labels_foreground_intent(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    beauty = tmp_path / "beauty.png"
    Image.new("RGB", (320, 480), "red").save(source)
    Image.new("RGB", (640, 320), "blue").save(beauty)

    comparison = mesh_verification._source_candidate_comparison(
        {"images": [{"kind": "beauty", "uri": beauty.as_posix()}]},
        [source],
        tmp_path / "reports",
        "fire extinguisher",
    )

    assert comparison == tmp_path / "reports" / "source-candidate-comparison.png"
    assert comparison.is_file()
    with Image.open(comparison) as image:
        assert image.size == (1600, 1650)


def test_blind_identity_check_rejects_a_dominant_box_for_an_extinguisher(tmp_path: Path, monkeypatch) -> None:
    beauty = tmp_path / "beauty.png"
    Image.new("RGB", (640, 320), "blue").save(beauty)
    monkeypatch.setattr(
        mesh_verification,
        "complete_vision",
        lambda *args, **kwargs: SimpleNamespace(
            content='{"dominant_object":"box","encloses_another_object":true,"reason":"enclosing cabinet"}',
            provider="nvidia_nim",
            model="blind-model",
        ),
    )

    result = mesh_verification._blind_identity_check(
        "nvidia_nim",
        "blind-model",
        beauty,
        ["fire extinguisher", "extinguisher"],
    )

    assert result["dominant_object"] == "box"
    assert result["matches_expected_identity"] is False


def test_mesh_verifier_approves_and_binds_the_exact_candidate_checksum(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate = _candidate(tmp_path)
    captured_context: dict[str, object] = {}
    captured_params: dict[str, object] = {}

    def approved_review(params: dict[str, object]) -> ToolResult:
        captured_params.update(params)
        captured_context.update(json.loads(str(params["stage_context"])))
        return ToolResult(
            success=True,
            data={
                "verdict": "approve",
                "action": "approve",
                "verdict_reason": "diagnostic views and tool results support promotion",
                "findings": [],
                "reviewer": {"provider": "nvidia_nim", "model": "review-model", "role": "vlm_reviewer"},
                "rubric_checksum": "a" * 64,
                "provider_trace": [
                    {
                        "provider": "nvidia_nim",
                        "model": "review-model",
                        "role": "vlm_reviewer",
                        "prompt_checksum": "a" * 64,
                    }
                ],
            },
            validation_status="validated",
        )

    monkeypatch.setattr(mesh_verification, "governance_vlm_review", approved_review)
    result = mesh_verification.governance_mesh_verify(
        {
            "project": tmp_path.as_posix(),
            "asset_id": "test_asset",
            "project_id": "project_test",
            "candidate_path": candidate.as_posix(),
            "temperature": 0.0,
            "seed": 23,
            "dry_run": False,
        }
    )

    assert result.success is True
    assert result.data["decision"] == "approve"
    assert captured_params["seed"] == 23
    assert captured_params["temperature"] == 0.0
    assert "diagnostics" not in captured_context
    assert captured_context["deterministic_gate"]["quality_failures"] == []
    assert captured_context["deterministic_gate"]["quality_check_statuses"]["component_count"] == "pass"
    assert result.data["promotion"]["candidate_checksum"] == result.data["candidate"]["checksum"]
    assert result.data["promotion"]["canonical_geometry_checksum"] == result.data["candidate"]["checksum"]

    gate = mesh_verification.prepare_mesh_verification(
        tmp_path,
        asset_id="test_asset",
        project_id="project_test",
        candidate_path=candidate,
    )
    assert gate["gate_status"] == "pass"

    changed_policy_gate = mesh_verification.prepare_mesh_verification(
        tmp_path,
        asset_id="test_asset",
        project_id="project_test",
        candidate_path=candidate,
        quality_policy={"max_component_count": 1},
    )
    assert changed_policy_gate["gate_status"] == "blocked"
    assert changed_policy_gate["quality_policy_checksum"] != result.data["quality_policy_checksum"]
    assert any("approval is missing or stale" in reason for reason in changed_policy_gate["blocked_reasons"])


def test_changed_candidate_invalidates_previous_mesh_approval(tmp_path: Path, monkeypatch) -> None:
    candidate = _candidate(tmp_path)

    monkeypatch.setattr(
        mesh_verification,
        "governance_vlm_review",
        lambda _: ToolResult(
            success=True,
            data={
                "verdict": "approve",
                "action": "approve",
                "verdict_reason": "approved",
                "findings": [],
                "reviewer": {"provider": "nvidia_nim", "model": "review-model", "role": "vlm_reviewer"},
                "rubric_checksum": "a" * 64,
                "provider_trace": [],
            },
        ),
    )
    approved = mesh_verification.governance_mesh_verify(
        {
            "project": tmp_path.as_posix(),
            "asset_id": "test_asset",
            "project_id": "project_test",
            "candidate_path": candidate.as_posix(),
            "dry_run": False,
        }
    )
    old_checksum = approved.data["candidate"]["checksum"]

    trimesh.creation.icosphere(subdivisions=1).export(candidate)
    gate = mesh_verification.prepare_mesh_verification(
        tmp_path,
        asset_id="test_asset",
        project_id="project_test",
        candidate_path=candidate,
    )

    assert gate["candidate"]["checksum"] != old_checksum
    assert gate["gate_status"] == "blocked"
    persisted = json.loads((tmp_path / "manifests" / "mesh-verification-record.json").read_text(encoding="utf-8"))
    assert persisted["promotion"]["candidate_checksum"] == old_checksum


def test_deterministic_quality_failure_overrides_visual_approval(tmp_path: Path, monkeypatch) -> None:
    candidate = tmp_path / "assets" / "test_asset" / "open.glb"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    mesh = trimesh.creation.box()
    mesh.update_faces([False, *([True] * (len(mesh.faces) - 1))])
    mesh.remove_unreferenced_vertices()
    mesh.export(candidate)

    monkeypatch.setattr(
        mesh_verification,
        "governance_vlm_review",
        lambda _: ToolResult(
            success=True,
            data={
                "verdict": "approve",
                "action": "approve",
                "verdict_reason": "looks acceptable",
                "findings": [],
                "reviewer": {"provider": "nvidia_nim", "model": "review-model", "role": "vlm_reviewer"},
                "rubric_checksum": "a" * 64,
                "provider_trace": [],
            },
        ),
    )

    result = mesh_verification.governance_mesh_verify(
        {
            "project": tmp_path.as_posix(),
            "asset_id": "test_asset",
            "project_id": "project_test",
            "candidate_path": candidate.as_posix(),
            "dry_run": False,
        }
    )

    assert result.success is False
    assert result.data["decision"] == "revise_local"
    assert result.data["review_status"] == "rejected"
    assert result.data["promotion"]["approved"] is False
    assert result.data["attempts"]["mesh_rejection_count"] == 1
    assert result.data["diagnostics"]["quality_failures"]
    assert any(item["source"] == "deterministic_mesh_quality_gate" for item in result.data["findings"])


def test_source_mismatch_is_routed_to_regeneration_even_when_agent_says_blocked() -> None:
    decision = mesh_verification._decision_from_review(
        {
            "verdict": "blocked",
            "action": "blocked",
            "findings": [{"defect_tag": "source_mismatch", "severity": "blocker"}],
        },
        [],
        [],
    )

    assert decision == "regenerate"


def test_unknown_mesh_decision_is_reported_as_skipped(tmp_path: Path) -> None:
    (tmp_path / "reports").mkdir()
    (tmp_path / "manifests").mkdir()
    (tmp_path / "project.json").write_text(json.dumps({"project_id": "project_test"}), encoding="utf-8")
    (tmp_path / "run-plan.json").write_text(
        json.dumps(
            {
                "id": "run_test",
                "request_id": "request_test",
                "stages": [{"id": "mesh-verification", "skill": "mesh-verification"}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "manifests" / "mesh-verification-record.json").write_text(
        json.dumps({"decision": "", "attempts": {}, "promotion": {"approved": False}}),
        encoding="utf-8",
    )

    progress = build_progress(tmp_path)

    assert progress["stages"][0]["vlm_review"]["verdict"] == "skipped"
    assert progress["review_pending"] == ["mesh-verification"]


def test_post_approval_rebuild_failure_blocks_agent_iteration(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        agent_loop,
        "governance_mesh_verify",
        lambda _: ToolResult(
            success=True,
            data={
                "decision": "approve",
                "review_status": "approved",
                "findings": [],
                "attempts": {},
            },
        ),
    )

    def fail_rebuild(*args, **kwargs):
        raise RuntimeError("rebuild failed")

    monkeypatch.setattr(agent_loop, "rebuild_project_artefacts", fail_rebuild)

    iteration = agent_loop._review_stage(tmp_path, "mesh-verification", "asset", "project", False, 1)

    assert iteration["final_state"] == "blocked"
    assert iteration["post_approval_rebuild"] == {
        "status": "blocked",
        "blocked_stages": ["mesh-verification"],
        "error": "rebuild failed",
    }


def test_mesh_verification_schema_rejects_malformed_promotion_checksums(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    record = mesh_verification.governance_mesh_verify(
        {
            "project": tmp_path.as_posix(),
            "asset_id": "test_asset",
            "project_id": "project_test",
            "candidate_path": candidate.as_posix(),
            "dry_run": True,
        }
    ).data
    validator = jsonschema.Draft202012Validator(load_schema("mesh-verification-record"))

    assert list(validator.iter_errors(record)) == []
    assert record["promotion"]["canonical_geometry_checksum"] == ""

    malformed_candidate = deepcopy(record)
    malformed_candidate["promotion"]["candidate_checksum"] = "not-a-checksum"
    assert list(validator.iter_errors(malformed_candidate))

    malformed_canonical = deepcopy(record)
    malformed_canonical["promotion"]["canonical_geometry_checksum"] = "not-a-checksum"
    assert list(validator.iter_errors(malformed_canonical))

    incomplete_approval = deepcopy(record)
    incomplete_approval["promotion"]["approved"] = True
    assert list(validator.iter_errors(incomplete_approval))
