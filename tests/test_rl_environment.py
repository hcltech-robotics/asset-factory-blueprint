from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from asset_factory_blueprint.manifests import validate_payload
from asset_factory_blueprint.schemas.common import RunPlan, RunRequest
from asset_factory_blueprint.services.fitness import (
    RL_RIGID_BODY_TESTS,
    default_release_scope,
    fitness_tests_for_request,
)
from asset_factory_blueprint.services import rl_environment as rl
from asset_factory_blueprint.utils.checksums import sha256_file
from asset_factory_blueprint.workflow import run_workflow

REPO = Path(__file__).resolve().parents[1]
REQUEST_PATH = REPO / "examples" / "run-requests" / "warehouse_pick_rl.json"


def _rl_constraints() -> dict:
    request = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
    return copy.deepcopy(request["constraints"]["rl"])


def _upstream() -> dict:
    physics = {
        "id": "asset_physics-articulation",
        "status": "validated",
        "affordances": {
            "status": "validated",
            "grasp_points": [
                {
                    "grasp_id": "g0",
                    "frame": [0, 0, 0.1, 1, 0, 0, 0],
                    "frame_space": "asset",
                    "quaternion_order": "wxyz",
                    "approach_vector": [0, 0, -1],
                    "gripper_width": 0.06,
                    "confidence": 0.9,
                    "status": "validated",
                }
            ],
            "affordance_labels": ["graspable"],
        },
        "mass_properties": [
            {
                "prim_path": "/bin",
                "mass": 1.2,
                "confidence": 0.8,
                "validation_status": "validated",
                "uncertainty": {"mass": 0.1},
            }
        ],
        "physics_materials": [
            {
                "prim_path": "/bin/mat",
                "static_friction": 0.6,
                "dynamic_friction": 0.5,
                "status": "validated",
            }
        ],
        "joints": [],
        "drives": [],
        "limits": [],
        "articulation_roots": [],
    }
    material = {
        "id": "asset_material-inference",
        "status": "validated",
        "physical_property_proposals": [
            {
                "property": "density",
                "value": 950.0,
                "unit": "kg/m^3",
                "range": [900.0, 1000.0],
                "distribution": "uniform",
                "confidence": 0.7,
                "status": "validated",
            },
            {
                "property": "roughness",
                "value": 0.4,
                "unit": "dimensionless",
                "range": [0.3, 0.6],
                "status": "proposal",
            },
        ],
    }
    return {
        "simready": {
            "path": "manifests/simready-asset-manifest.json",
            "sha256": "a" * 64,
            "payload": {
                "id": "asset_simready-verification",
                "status": "validated",
                "usd_layer_stack": ["geo.usda"],
                "physics_articulation_manifest_id": "asset_physics-articulation",
                "material_inference_manifest_id": "asset_material-inference",
            },
        },
        "physics": {
            "path": "manifests/physics-articulation-manifest.json",
            "sha256": "b" * 64,
            "payload": physics,
        },
        "material": {
            "path": "manifests/material-inference-manifest.json",
            "sha256": "c" * 64,
            "payload": material,
        },
        "nonvisual": {"path": "", "sha256": "", "payload": None},
        "source": {"path": "", "sha256": "", "payload": None},
        "segmentation": {"path": "", "sha256": "", "payload": None},
        "layout": {"path": "", "sha256": "", "payload": None},
        "runtime": {
            "record": {
                "status": "pass",
                "runtime_id": "isaac-sim",
                "report_sha256": "e" * 64,
                "validated_package_dependency_fingerprint": "sha256:" + "f" * 64,
                "validated_usd_sha256": "d" * 64,
            },
            "report": {
                "physics_dt": 1 / 120,
                "runtime_identity": {
                    "id": "isaac-sim",
                    "physics_backend": "physx",
                    "version": "5.0",
                },
                "validation_parameters": {
                    "settle_steps": 240,
                    "settled_speed_metres_per_second": 0.05,
                    "repeatability_tolerance_metres": 0.001,
                },
            },
            "path": "reports/isaac-load-check.json",
            "sha256": "e" * 64,
        },
    }


def test_reconciliation_accepts_the_supported_rigid_pick_contract() -> None:
    reasons, findings = rl.reconcile_behaviour("pick", _upstream(), _rl_constraints())
    assert reasons == []
    assert {item["check"]: item["status"] for item in findings["checks"]} == {
        "grasp_points": "pass",
        "mass": "pass",
        "friction": "pass",
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("articulation", "rigid objects"),
        ("support", "support_height_m to be zero"),
        ("root_height", "object_initial_position z"),
    ],
)
def test_reconciliation_rejects_unsupported_scene_contracts(change: str, message: str) -> None:
    upstream = _upstream()
    constraints = _rl_constraints()
    if change == "articulation":
        upstream["physics"]["payload"]["articulation_roots"] = [{"prim_path": "/bin", "status": "authored"}]
    elif change == "support":
        constraints["scene"]["support_height_m"] = 0.1
        constraints["scene"]["object_initial_position"][2] = 0.1
    else:
        constraints["scene"]["object_initial_position"][2] = 0.1

    reasons, _ = rl.reconcile_behaviour("pick", upstream, constraints)
    assert any(message in reason for reason in reasons)


def test_non_pick_behaviour_is_rejected() -> None:
    reasons, _ = rl.reconcile_behaviour("open", _upstream(), _rl_constraints())
    assert reasons == ["behaviour 'open' is unsupported; the implemented runtime contract is pick"]


def test_reconciliation_rejects_a_gaming_horizon_different_from_the_oracle() -> None:
    constraints = _rl_constraints()
    constraints["probes"]["gaming_steps"] -= 1

    reasons, _ = rl.reconcile_behaviour("pick", _upstream(), constraints)
    assert "probes.gaming_steps must equal the complete scripted-oracle horizon" in reasons


def test_runtime_binding_requires_isaac_lab_2_3_1() -> None:
    constraints = _rl_constraints()
    bound, backend_reasons, timestep_reasons = rl.bind_runtime(constraints, _upstream()["runtime"])
    assert backend_reasons == []
    assert timestep_reasons == []
    assert bound["isaac_lab_version"] == "2.3.1"
    assert bound["control_dt"] == pytest.approx(4 / 120)

    for version in ("", "2.3.0", "2.4.0"):
        candidate = copy.deepcopy(constraints)
        candidate["runtime"]["isaac_lab_version"] = version
        _, errors, _ = rl.bind_runtime(candidate, _upstream()["runtime"])
        assert any("exactly 2.3.1" in error for error in errors)


def test_runtime_binding_rejects_backend_and_timestep_drift() -> None:
    constraints = _rl_constraints()
    constraints["runtime"]["physics_backend"] = "newton"
    constraints["runtime"]["sim_dt"] = 0.005
    _, backend_reasons, timestep_reasons = rl.bind_runtime(constraints, _upstream()["runtime"])
    assert any("newton" in reason for reason in backend_reasons)
    assert any("matching runtime evidence" in reason for reason in timestep_reasons)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("fixed_base", False, "fixed-base"),
        ("fixed_base", None, "must be explicit"),
        ("maximum_gripper_aperture_m", None, "finite positive"),
        ("maximum_gripper_aperture_m", 0.05, "exceed maximum_gripper_aperture_m"),
    ],
)
def test_embodiment_rejects_unbound_fixed_base_and_aperture(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    source = "examples/sources/robots/two_link_arm.urdf"
    copied = tmp_path / "source-assets" / "two_link_arm.urdf"
    copied.parent.mkdir(parents=True)
    shutil.copy2(REPO / source, copied)
    constraints = _rl_constraints()
    constraints["embodiment"][field] = value
    request = RunRequest.model_validate(
        {
            "id": "asset",
            "objective": "pick",
            "sources": [source],
            "requested_outputs": ["rl"],
            "constraints": {"rl": constraints},
        }
    )
    upstream = _upstream()
    upstream["source"] = {
        "path": "manifests/source-asset-manifest.json",
        "sha256": "9" * 64,
        "payload": {
            "status": "validated",
            "source_assets": [
                {
                    "source_path": source,
                    "project_copy_path": "source-assets/two_link_arm.urdf",
                    "copy_sha256": sha256_file(copied),
                    "status": "copied",
                }
            ],
        },
    }

    embodiment, reasons = rl.resolve_embodiment(tmp_path, request, constraints, upstream)
    assert any(message in reason for reason in reasons)
    assert embodiment["status"] == "blocked"


def test_urdf_parser_keeps_prismatic_gripper_limits() -> None:
    parsed = rl.parse_urdf_embodiment(REPO / "examples" / "sources" / "robots" / "two_link_arm.urdf")
    joints = {item["name"]: item for item in parsed["actuated_joints"]}
    assert len(joints) == 8
    assert list(joints)[-2:] == ["left_finger_joint", "right_finger_joint"]
    assert joints["left_finger_joint"]["type"] == "prismatic"
    assert joints["left_finger_joint"]["upper"] == pytest.approx(0.06)
    assert parsed["effort_ceiling"] == 120.0


def test_embodiment_rejects_a_mutated_source_copy(tmp_path: Path) -> None:
    source = "examples/sources/robots/two_link_arm.urdf"
    copied = tmp_path / "source-assets" / "two_link_arm.urdf"
    copied.parent.mkdir(parents=True)
    shutil.copy2(REPO / source, copied)
    recorded_sha256 = sha256_file(copied)
    copied.write_text(copied.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    constraints = _rl_constraints()
    request = RunRequest.model_validate(
        {
            "id": "asset",
            "objective": "pick",
            "sources": [source],
            "requested_outputs": ["rl"],
            "constraints": {"rl": constraints},
        }
    )
    upstream = _upstream()
    upstream["source"] = {
        "path": "manifests/source-asset-manifest.json",
        "sha256": "9" * 64,
        "payload": {
            "status": "validated",
            "source_assets": [
                {
                    "source_path": source,
                    "project_copy_path": "source-assets/two_link_arm.urdf",
                    "copy_sha256": recorded_sha256,
                    "status": "copied",
                }
            ],
        },
    }

    embodiment, reasons = rl.resolve_embodiment(tmp_path, request, constraints, upstream)

    assert "differs from its source-asset checksum" in "; ".join(reasons)
    assert embodiment["status"] == "blocked"


def test_reconciliation_rejects_reversed_intervals() -> None:
    constraints = _rl_constraints()
    constraints["resets"]["robot_joint_offset_rad"] = [0.05, -0.05]

    reasons, _ = rl.reconcile_behaviour("pick", _upstream(), constraints)

    assert "resets.robot_joint_offset_rad must be a finite interval" in reasons


def test_context_prior_uses_only_accepted_numeric_evidence() -> None:
    prior = {entry["parameter"]: entry for entry in rl.build_context_prior(_upstream())}
    assert prior["mass"]["interval"] == pytest.approx([1.1, 1.3])
    assert prior["friction"]["interval"] == [0.5, 0.6]
    assert prior["density"]["interval"] == [900.0, 1000.0]
    assert "roughness" not in prior


def test_reward_audit_accepts_only_the_implemented_functions() -> None:
    rewards = _rl_constraints()["rewards"]
    components, audit, evidence = rl.static_reward_probes(rewards, _upstream(), 0.7)
    assert audit == []
    assert evidence == []
    assert all(item["static_probe_status"] == "pass" for item in components)

    altered = copy.deepcopy(rewards)
    altered[0]["functional_form"] = "distance"
    components, audit, _ = rl.static_reward_probes(altered, _upstream(), 0.7)
    assert components[0]["static_probe_status"] == "blocked"
    assert any("functional_form" in reason for reason in audit)


def test_task_fitness_protocol_uses_its_schema_scope() -> None:
    constraints = _rl_constraints()
    runtime, _, _ = rl.bind_runtime(constraints, _upstream()["runtime"])
    request = RunRequest.model_validate(
        {
            "id": "asset",
            "objective": "pick",
            "sources": ["robot.urdf"],
            "requested_outputs": ["rl"],
            "constraints": {"rl": constraints},
        }
    )
    plan = RunPlan.model_validate(
        {
            "id": "plan",
            "run_id": "run",
            "request_digest": "sha256:" + "1" * 64,
            "asset_id": "asset",
            "request_id": "asset",
            "objective": "pick",
            "stages": [],
            "provider_assignments": {},
            "missing_evidence": [],
            "validation_gates": [],
            "wandb_plan": {},
            "wandb": {},
        }
    )
    protocol = rl.build_task_fitness_protocol(request, plan, constraints, "pick", runtime)
    assert protocol["scope"] == "rigid_body_manipulation"
    assert validate_payload("task-fitness-protocol", protocol) == []


def test_rl_fitness_tests_do_not_leak_into_generic_simready_releases() -> None:
    generic = RunRequest.model_validate(
        {
            "id": "asset",
            "objective": "build a SimReady asset",
            "sources": ["asset.usd"],
            "requested_outputs": ["simready"],
            "constraints": {},
        }
    )
    rl_request = RunRequest.model_validate(
        {
            "id": "asset",
            "objective": "build a pick environment",
            "sources": ["asset.usd"],
            "requested_outputs": ["rl"],
            "constraints": {"rl": _rl_constraints()},
        }
    )
    assert default_release_scope(generic) == "rigid_body_manipulation"
    assert fitness_tests_for_request(generic, "rigid_body_manipulation") == ("manipulation_contact_fidelity",)
    assert fitness_tests_for_request(rl_request, "rigid_body_manipulation") == RL_RIGID_BODY_TESTS


def test_rl_workflow_blocks_an_incompatible_explicit_release_scope(tmp_path: Path) -> None:
    request = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
    request["constraints"]["release_scope"] = "articulated_training"
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    result = run_workflow(request_path, project_root=tmp_path / "projects")
    project = Path(result["project_dir"])
    manifest = json.loads((project / "manifests" / "rl-environment-manifest.json").read_text(encoding="utf-8"))
    reconciliation = next(
        gate for gate in manifest["extensions"]["rl"]["gates"] if gate["gate_id"] == "rl-reconciliation"
    )
    assert reconciliation["status"] == "blocked"
    assert any("requires release_scope rigid_body_manipulation" in reason for reason in reconciliation["reasons"])


def test_workflow_records_the_fail_closed_rl_contract(tmp_path: Path) -> None:
    result = run_workflow(REQUEST_PATH, project_root=tmp_path)
    stage = next(item for item in result["stage_results"] if item["stage_id"] == "rl-environment")
    project = Path(result["project_dir"])
    manifest = json.loads((project / "manifests" / "rl-environment-manifest.json").read_text(encoding="utf-8"))
    block = manifest["extensions"]["rl"]

    assert stage["manifest_valid"], stage["manifest_errors"]
    assert stage["status"] == "blocked"
    assert block["runtime"]["isaac_lab_version"] == "2.3.1"
    assert block["task"]["embodiment"]["fixed_base"] is True
    assert block["task"]["embodiment"]["maximum_gripper_aperture_m"] == pytest.approx(0.14)
    assert block["scene"]["object_asset_type"] == "rigid_object"
    assert block["scene"]["support_height_m"] == 0.0
    assert any("not been imported" in reason for reason in manifest["blocked_reasons"])
    assert validate_payload("rl-environment-manifest", manifest) == []


def test_direct_rl_route_persists_the_refreshed_manifest(tmp_path: Path) -> None:
    result = run_workflow(REQUEST_PATH, project_root=tmp_path)
    project = Path(result["project_dir"])
    manifest_path = project / "manifests" / "rl-environment-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["extensions"].pop("rl")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    route_result = rl.rl_route({"project": str(project)})
    assert route_result.validation_status == "blocked"
    refreshed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert refreshed["extensions"]["rl"]["contract_sha256"]
    assert any(item["evidence_id"] == "rl_environment_design_report" for item in refreshed["evidence"])
