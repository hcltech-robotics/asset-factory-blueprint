from __future__ import annotations

import ast
import copy
import json
import math
import random
import shutil
from pathlib import Path
from typing import Any

import pytest

from asset_factory_blueprint import capsule as capsule_module
from asset_factory_blueprint import rl_fidelity, rl_probe_import, rl_probes
from asset_factory_blueprint.manifests import validate_payload
from asset_factory_blueprint.rl_collision_fidelity import _output_path as fidelity_output_path
from asset_factory_blueprint.rl_evidence import (
    FIDELITY_REPORT_ID,
    PROBE_IDS,
    PROBE_PROTOCOL_ID,
    PROBE_PROTOCOL_VERSION,
    PROBE_REPORT_ID,
    attest_report,
    environment_contract_sha256,
    load_attested_report,
    producer_bundle_sha256,
    verify_import_receipt,
    verify_report,
    write_attested_report,
)
from asset_factory_blueprint.rl_probe_import import apply_rl_evidence_report
from asset_factory_blueprint.rl_render import _exact_pattern, render, render_to_directory
from asset_factory_blueprint.rl_runtime_probe import _output_path as probe_output_path
from asset_factory_blueprint.services.rl_environment import GATE_IDS
from asset_factory_blueprint.utils.checksums import sha256_file
from asset_factory_blueprint.utils.package_fingerprint import package_inventory_fingerprint
from asset_factory_blueprint.workflow import run_workflow

REPO = Path(__file__).resolve().parents[1]
REQUEST_PATH = REPO / "examples" / "run-requests" / "warehouse_pick_rl.json"
PROBE_SECRET = b"unit-test-probe-attestation-secret-with-at-least-32-bytes"
FIDELITY_SECRET = b"unit-test-fidelity-attestation-secret-with-at-least-32-bytes"
IMPORT_SECRET = b"unit-test-import-attestation-secret-with-at-least-32-bytes"
PHASES = {"approach": 20, "descend": 5, "close": 3, "lift": 20}
MANAGED_RL_EVIDENCE_IDS = (
    "rl_environment_design_report",
    "rl_environment_card",
    "rl_task_fitness_protocol",
    "isaac_runtime_evidence",
    "rl_probe_evidence",
    "rl_probe_import_receipt",
    "rl_collision_fidelity",
    "rl_fidelity_import_receipt",
)


class DeterministicProbeEnvironment:
    """Small deterministic system used to exercise the runtime-independent probe maths."""

    def __init__(self, num_envs: int = 4, jitter: float = 0.0, settle_ok: bool = True) -> None:
        self.num_envs = num_envs
        self.jitter = jitter
        self.settle_ok = settle_ok
        self.component_names = ["reach", "lift"]
        self.grasp_ids = ["g0"]
        self.selected_grasp = "g0"
        self.reset(0)

    def reset(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.ee = [0.5 + self.rng.uniform(-0.05, 0.05) for _ in range(self.num_envs)]
        self.object_z = [0.0] * self.num_envs
        self.gripped = [False] * self.num_envs
        self.joint_violation = [0] * self.num_envs

    def select_grasp(self, grasp_id: str) -> None:
        self.selected_grasp = grasp_id

    def step(self, actions: list[list[float]]) -> rl_probes.StepResult:
        rewards: list[float] = []
        components = {"reach": [], "lift": []}
        for index, action in enumerate(actions):
            reach, grip = float(action[0]), float(action[1])
            self.ee[index] += -0.05 * reach
            if self.jitter:
                self.ee[index] += self.rng.uniform(-self.jitter, self.jitter)
            if abs(self.ee[index]) > 1.0:
                self.joint_violation[index] += 1
            near = abs(self.ee[index]) < 0.03
            if grip < 0 and near:
                self.gripped[index] = True
            if self.gripped[index] and grip < 0:
                self.object_z[index] = min(0.2, self.object_z[index] + 0.01)
            else:
                self.gripped[index] = False
                self.object_z[index] = max(0.0, self.object_z[index] - 0.02)
            reach_reward = 1.0 - math.tanh(abs(self.ee[index]) / 0.1)
            lift_reward = 5.0 if self.object_z[index] > 0.04 else 0.0
            components["reach"].append(reach_reward)
            components["lift"].append(lift_reward)
            rewards.append(reach_reward + lift_reward)
        success = [height > 0.1 for height in self.object_z]
        return rl_probes.StepResult(
            rewards=rewards,
            components=components,
            terminated=[False] * self.num_envs,
            truncated=[False] * self.num_envs,
            success=success,
        )

    def zero_actions(self) -> list[list[float]]:
        return [[0.0, 0.0] for _ in range(self.num_envs)]

    def random_actions(self, rng: random.Random) -> list[list[float]]:
        return [[rng.uniform(-1, 1), rng.uniform(-1, 1)] for _ in range(self.num_envs)]

    def oracle_actions(self, phase: str, _step: int) -> list[list[float]]:
        if phase in {"approach", "descend"}:
            return [[max(-1.0, min(1.0, self.ee[index] / 0.05)), 1.0] for index in range(self.num_envs)]
        return [[0.0, -1.0] for _ in range(self.num_envs)]

    def gaming_actions(self, pattern: str, step: int) -> list[list[float]]:
        if pattern == "proximity_without_contact":
            return [[max(-1.0, min(1.0, (self.ee[index] - 0.05) / 0.05)), 1.0] for index in range(self.num_envs)]
        if pattern == "velocity_without_displacement":
            return [[1.0 if step % 2 == 0 else -1.0, 1.0] for _ in range(self.num_envs)]
        if pattern == "oscillation":
            return [[math.sin(step / 3.0), 1.0] for _ in range(self.num_envs)]
        return [[1.0, 1.0] for _ in range(self.num_envs)]

    def object_positions(self) -> list[list[float]]:
        return [[0.0, 0.0, height] for height in self.object_z]

    def object_speeds(self) -> list[float]:
        return [0.0 if self.settle_ok else 1.0 for _ in range(self.num_envs)]

    def penetration_depths(self) -> list[float]:
        return [0.0 if self.settle_ok else 0.02 for _ in range(self.num_envs)]

    def joint_limit_violations(self) -> list[int]:
        return list(self.joint_violation)

    def grasp_frame_reached(self) -> list[bool]:
        return [abs(value) < 0.03 for value in self.ee]

    def task_success(self) -> list[bool]:
        return [height > 0.1 for height in self.object_z]


class PerEnvironmentGamingSystem:
    num_envs = 2
    component_names = ["reward"]
    grasp_ids = ["g0"]

    def reset(self, _seed: int) -> None:
        return None

    def gaming_actions(self, _pattern: str, _step: int) -> list[list[float]]:
        return [[0.0], [0.0]]

    def step(self, _actions: list[list[float]]) -> rl_probes.StepResult:
        return rl_probes.StepResult(
            rewards=[0.0, 4.0],
            components={"reward": [0.0, 4.0]},
            terminated=[False, False],
            truncated=[False, False],
            success=[False, False],
        )

    def task_success(self) -> list[bool]:
        return [False, False]


def test_runtime_independent_probes_preserve_each_environment() -> None:
    environment = DeterministicProbeEnvironment()
    smoke = rl_probes.run_smoke(environment, seed=1, steps=30)
    oracle = rl_probes.run_oracle(environment, seed=1, phase_steps=PHASES)
    reset = rl_probes.run_reset_feasibility(
        environment,
        seeds=[0, 1],
        settle_steps=5,
        settled_speed=0.25,
        penetration_tolerance_m=0.005,
    )
    repeat = rl_probes.run_repeatability(environment, seed=3, steps=40, tolerance_m=0.001)

    assert smoke["status"] == "pass"
    assert oracle["status"] == "pass", oracle["reason"]
    assert oracle["metrics"]["oracle_success_rate"] == 1.0
    assert len(oracle["metrics"]["oracle_returns"]) == environment.num_envs
    assert reset["status"] == "pass"
    assert repeat["status"] == "pass"


def test_gaming_probe_compares_each_environment_with_its_own_oracle() -> None:
    result = rl_probes.run_gaming_probes(
        PerEnvironmentGamingSystem(),
        seed=1,
        steps=1,
        oracle_returns=[1000.0, 10.0],
        allowed_fraction=0.3,
    )
    proximity = result["metrics"]["patterns"]["proximity_without_contact"]
    assert proximity["fraction_of_oracle"] == pytest.approx(0.2)
    assert proximity["gamed_environment_count"] == 1
    assert result["status"] == "blocked"


@pytest.mark.parametrize(
    "components",
    [
        {},
        {"reach": float("nan")},
        {"reach": float("inf")},
        {"reach": 0.0, "lift": -1.0},
    ],
)
def test_component_dominance_cannot_pass_without_finite_positive_data(components: dict[str, float]) -> None:
    passed, reason = rl_probes.component_dominance(components, 0.7)
    assert not passed
    assert reason


def test_probe_report_requires_the_exact_five_passes() -> None:
    probes = [{"probe": name, "status": "pass", "metrics": {"observed": True}} for name in PROBE_IDS]
    report = rl_probes.assemble_probe_report(
        bindings={"usd_sha256": "a" * 64},
        runtime={},
        execution={},
        probes=probes,
        errors=[],
    )
    assert report["status"] == "pass"

    duplicate = copy.deepcopy(probes)
    duplicate[-1]["probe"] = "smoke"
    assert (
        rl_probes.assemble_probe_report(bindings={}, runtime={}, execution={}, probes=duplicate, errors=[])["status"]
        == "blocked"
    )
    assert (
        rl_probes.assemble_probe_report(bindings={}, runtime={}, execution={}, probes=probes[:-1], errors=[])["status"]
        == "blocked"
    )


def _box(half: float) -> rl_fidelity.Mesh:
    vertices = [(x * half, y * half, z * half) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
    faces = [
        (0, 1, 3),
        (0, 3, 2),
        (4, 6, 7),
        (4, 7, 5),
        (0, 4, 5),
        (0, 5, 1),
        (2, 3, 7),
        (2, 7, 6),
        (0, 2, 6),
        (0, 6, 4),
        (1, 5, 7),
        (1, 7, 3),
    ]
    return vertices, faces


def test_fidelity_requires_the_exact_sample_count() -> None:
    regions = [{"id": "g0", "kind": "grasp_point", "centre": [0.0, 0.0, 0.5], "radius_m": 0.3}]
    result = rl_fidelity.evaluate_fidelity(
        _box(0.5),
        _box(0.5),
        regions,
        tolerance_m=0.002,
        samples_per_region=64,
        seed=1,
    )
    assert result["status"] == "pass"
    assert result["regions"][0]["visual_samples"] == 64
    assert result["regions"][0]["collision_samples"] == 64

    signed = _fidelity_payload(samples_per_region=64, visual_samples=63, collision_samples=64)
    errors = verify_report(signed, FIDELITY_REPORT_ID, secret=FIDELITY_SECRET)
    assert any("incomplete surface sampling" in error for error in errors)


def _producer_files(paths: set[str] | None = None) -> list[dict[str, str]]:
    selected = paths or {"producer.py"}
    return [
        {
            "path": path,
            "sha256": sha256_file(REPO / path) if (REPO / path).is_file() else "1" * 64,
        }
        for path in sorted(selected)
    ]


def _execution(files: list[dict[str, str]] | None = None) -> dict:
    inventory = files or _producer_files()
    return {
        "producer_id": "asset-factory.test-producer",
        "producer_version": "1.0",
        "producer_sha256": producer_bundle_sha256(inventory),
        "producer_files": inventory,
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:00:01Z",
    }


def _attested(payload: dict, secret: bytes) -> dict:
    return {**payload, "attestation": attest_report(payload, secret)}


def _probe_payload(bindings: dict | None = None, files: list[dict[str, str]] | None = None) -> dict:
    bound = bindings or {
        "usd_sha256": "a" * 64,
        "request_digest": "sha256:" + "b" * 64,
        "manifest_sha256": "c" * 64,
        "manifest_path": "manifests/rl-environment-manifest.json",
        "usd_path": "assets/object/object.usda",
        "contract_sha256": "d" * 64,
        "package_dependency_fingerprint": "sha256:" + "e" * 64,
        "runtime_source_report_sha256": "f" * 64,
        "sim_dt": 1 / 120,
        "decimation": 4,
        "rendered_files": {
            "__init__.py": "1" * 64,
            "rl_env_cfg.py": "2" * 64,
            "custom_mdp.py": "3" * 64,
            "rl_env_cfg.render.json": "4" * 64,
        },
        "probe_parameters": {
            "smoke_steps": 200,
            "repeat_steps": 100,
            "gaming_steps": sum(PHASES.values()),
            "reset_seeds": [200, 201],
            "penetration_tolerance_m": 0.002,
            "allowed_gaming_fraction": 0.3,
            "oracle_phase_steps": PHASES,
        },
        "settle_parameters": {
            "settle_steps": 240,
            "settled_speed_metres_per_second": 0.05,
            "repeatability_tolerance_metres": 0.001,
        },
    }
    probe_parameters = bound["probe_parameters"]
    settle = bound["settle_parameters"]
    num_envs = int(bound.get("num_envs") or 1)
    grasp_ids = list(bound.get("grasp_ids") or ["g0"])
    oracle_horizon = sum(int(value) for value in probe_parameters["oracle_phase_steps"].values())
    oracle_returns = [10.0] * num_envs
    patterns = {
        name: {
            "return_mean": 0.0,
            "fraction_of_oracle": 0.0,
            "gamed_environment_count": 0,
            "terminal_steps": [None] * num_envs,
            "terminal_success": [False] * num_envs,
            "status": "pass",
        }
        for name in rl_probes.GAMING_PATTERNS
    }
    payload = {
        "report_identity": {"id": PROBE_REPORT_ID, "version": "1.0"},
        "protocol_identity": {"id": PROBE_PROTOCOL_ID, "version": PROBE_PROTOCOL_VERSION},
        "execution_identity": _execution(files),
        "status": "pass",
        "physics_backend": "physx",
        "usd_sha256": bound["usd_sha256"],
        "request_digest": bound["request_digest"],
        "manifest_sha256": bound["manifest_sha256"],
        "manifest_path": bound["manifest_path"],
        "usd_path": bound["usd_path"],
        "contract_sha256": bound["contract_sha256"],
        "package_dependency_fingerprint": bound["package_dependency_fingerprint"],
        "runtime": {
            "isaac_lab_available": True,
            "isaac_lab_version": "2.3.1",
            "physics_backend": "physx",
            "sim_dt": bound["sim_dt"],
            "decimation": bound["decimation"],
            "source_report_sha256": bound["runtime_source_report_sha256"],
        },
        "rendered_files": bound["rendered_files"],
        "probes": [
            {
                "probe": "smoke",
                "status": "pass",
                "metrics": {
                    "steps": probe_parameters["smoke_steps"],
                    "zero_action_return_mean": 0.0,
                    "random_policy_return_mean": 1.0,
                    "random_policy_return_std": 0.1,
                },
            },
            {
                "probe": "oracle",
                "status": "pass",
                "metrics": {
                    "phase_steps": probe_parameters["oracle_phase_steps"],
                    "oracle_return_mean": 10.0,
                    "oracle_returns": oracle_returns,
                    "oracle_success_rate": 1.0,
                    "reachable_grasp_fraction": 1.0,
                    "limit_violations": 0,
                    "per_grasp": [
                        {
                            "grasp_id": grasp_id,
                            "reachable_fraction": 1.0,
                            "success_rate": 1.0,
                            "return_mean": 10.0,
                            "returns": oracle_returns,
                            "terminal_steps": [oracle_horizon] * num_envs,
                            "limit_violations": 0,
                        }
                        for grasp_id in grasp_ids
                    ],
                    "oracle_component_means": {"reach": 4.0, "lift": 5.0, "action_rate": 1.0},
                    "component_dominance_status": "pass",
                },
            },
            {
                "probe": "reset",
                "status": "pass",
                "metrics": {
                    "seeds": probe_parameters["reset_seeds"],
                    "settle_steps": settle["settle_steps"],
                    "settled_speed_metres_per_second": settle["settled_speed_metres_per_second"],
                    "penetration_tolerance_m": probe_parameters["penetration_tolerance_m"],
                    "penetration_measured": True,
                    "infeasible_reset_fraction": 0.0,
                    "worst_penetration_m": 0.0,
                    "worst_settled_speed": 0.0,
                },
            },
            {
                "probe": "repeat",
                "status": "pass",
                "metrics": {
                    "steps": probe_parameters["repeat_steps"],
                    "tolerance_m": settle["repeatability_tolerance_metres"],
                    "return_tolerance": 1e-6,
                    "max_trajectory_deviation_m": 0.0,
                    "max_return_deviation": 0.0,
                },
            },
            {
                "probe": "gaming",
                "status": "pass",
                "metrics": {
                    "steps": probe_parameters["gaming_steps"],
                    "allowed_fraction_of_oracle": probe_parameters["allowed_gaming_fraction"],
                    "patterns": patterns,
                },
            },
        ],
        "errors": [],
    }
    return _attested(payload, PROBE_SECRET)


def _fidelity_payload(
    *,
    seed: int = 0,
    samples_per_region: int = 64,
    visual_samples: int = 64,
    collision_samples: int = 64,
) -> dict:
    payload = {
        "report_identity": {"id": FIDELITY_REPORT_ID, "version": "1.0"},
        "execution_identity": _execution(),
        "status": "pass",
        "physics_backend": "physx",
        "usd_sha256": "a" * 64,
        "request_digest": "sha256:" + "b" * 64,
        "manifest_sha256": "c" * 64,
        "manifest_path": "manifests/rl-environment-manifest.json",
        "usd_path": "assets/object/object.usda",
        "contract_sha256": "d" * 64,
        "package_dependency_fingerprint": "sha256:" + "e" * 64,
        "runtime": {
            "physics_backend": "physx",
            "sim_dt": 1 / 120,
            "decimation": 4,
            "source_report_sha256": "f" * 64,
        },
        "rendered_files": {
            "__init__.py": "1" * 64,
            "rl_env_cfg.py": "2" * 64,
            "custom_mdp.py": "3" * 64,
            "rl_env_cfg.render.json": "4" * 64,
        },
        "tolerance_m": 0.002,
        "samples_per_region": samples_per_region,
        "seed": seed,
        "regions": [
            {
                "id": "g0",
                "kind": "grasp_point",
                "centre": [0.0, 0.0, 0.0],
                "radius_m": 0.05,
                "visual_samples": visual_samples,
                "collision_samples": collision_samples,
                "visual_to_collision_p95_m": 0.0,
                "visual_to_collision_max_m": 0.0,
                "collision_to_visual_p95_m": 0.0,
                "collision_to_visual_max_m": 0.0,
                "shell_distance_p95_m": 0.0,
                "status": "pass",
                "reason": "",
            }
        ],
        "errors": [],
    }
    return _attested(payload, FIDELITY_SECRET)


def _bound_fidelity_payload(bindings: dict, files: list[dict[str, str]]) -> dict:
    payload = {key: copy.deepcopy(value) for key, value in _fidelity_payload().items() if key != "attestation"}
    fidelity = bindings["fidelity_parameters"]
    payload.update(
        {
            "execution_identity": _execution(files),
            "usd_sha256": bindings["usd_sha256"],
            "request_digest": bindings["request_digest"],
            "manifest_sha256": bindings["manifest_sha256"],
            "manifest_path": bindings["manifest_path"],
            "usd_path": bindings["usd_path"],
            "contract_sha256": bindings["contract_sha256"],
            "package_dependency_fingerprint": bindings["package_dependency_fingerprint"],
            "runtime": {
                "physics_backend": bindings["physics_backend"],
                "sim_dt": bindings["sim_dt"],
                "decimation": bindings["decimation"],
                "source_report_sha256": bindings["runtime_source_report_sha256"],
            },
            "rendered_files": bindings["rendered_files"],
            "tolerance_m": fidelity["tolerance_m"],
            "samples_per_region": fidelity["samples_per_region"],
            "seed": fidelity["seed"],
            "regions": [
                {
                    "id": region["id"],
                    "kind": "grasp_point",
                    "centre": region["centre"],
                    "radius_m": fidelity["region_radius_m"],
                    "visual_samples": fidelity["samples_per_region"],
                    "collision_samples": fidelity["samples_per_region"],
                    "visual_to_collision_p95_m": 0.0,
                    "visual_to_collision_max_m": 0.0,
                    "collision_to_visual_p95_m": 0.0,
                    "collision_to_visual_max_m": 0.0,
                    "shell_distance_p95_m": 0.0,
                    "status": "pass",
                    "reason": "",
                }
                for region in bindings["grasp_regions"]
            ],
        }
    )
    return _attested(payload, FIDELITY_SECRET)


def test_probe_schema_requires_unique_complete_passes_and_isaac_lab_2_3_1() -> None:
    payload = _probe_payload()
    assert validate_payload("rl-probe-evidence", payload) == []

    duplicate = copy.deepcopy(payload)
    duplicate["probes"][-1]["probe"] = "smoke"
    assert validate_payload("rl-probe-evidence", duplicate)

    wrong_version = copy.deepcopy(payload)
    wrong_version["runtime"]["isaac_lab_version"] = "2.3.0"
    assert validate_payload("rl-probe-evidence", wrong_version)


def test_fidelity_schema_and_semantics_reject_negative_seed() -> None:
    payload = _fidelity_payload(seed=-1)
    assert validate_payload("rl-collision-fidelity", payload)
    assert any(
        "non-negative seed" in error for error in verify_report(payload, FIDELITY_REPORT_ID, secret=FIDELITY_SECRET)
    )


def test_report_attestation_secrets_are_required_and_role_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "AFB_RL_PROBE_ATTESTATION_SECRET",
        "AFB_RL_FIDELITY_ATTESTATION_SECRET",
        "AFB_RL_IMPORT_ATTESTATION_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)

    probe = _probe_payload()
    errors = verify_report(probe, PROBE_REPORT_ID)
    assert any("AFB_RL_PROBE_ATTESTATION_SECRET" in error for error in errors)

    monkeypatch.setenv("AFB_RL_PROBE_ATTESTATION_SECRET", FIDELITY_SECRET.decode())
    errors = verify_report(probe, PROBE_REPORT_ID)
    assert any("attestation key_id does not match" in error for error in errors)
    monkeypatch.setenv("AFB_RL_PROBE_ATTESTATION_SECRET", PROBE_SECRET.decode())
    assert verify_report(probe, PROBE_REPORT_ID) == []

    fidelity = _fidelity_payload()
    errors = verify_report(fidelity, FIDELITY_REPORT_ID)
    assert any("AFB_RL_FIDELITY_ATTESTATION_SECRET" in error for error in errors)

    monkeypatch.setenv("AFB_RL_FIDELITY_ATTESTATION_SECRET", PROBE_SECRET.decode())
    errors = verify_report(fidelity, FIDELITY_REPORT_ID)
    assert any("role secrets must be distinct" in error for error in errors)
    monkeypatch.setenv("AFB_RL_FIDELITY_ATTESTATION_SECRET", FIDELITY_SECRET.decode())
    assert verify_report(fidelity, FIDELITY_REPORT_ID) == []


def test_passing_report_rejects_a_gaming_horizon_different_from_the_oracle() -> None:
    payload = _probe_payload()
    unsigned = {key: copy.deepcopy(value) for key, value in payload.items() if key != "attestation"}
    gaming = next(item for item in unsigned["probes"] if item["probe"] == "gaming")
    gaming["metrics"]["steps"] += 1
    signed = _attested(unsigned, PROBE_SECRET)

    errors = verify_report(signed, PROBE_REPORT_ID, secret=PROBE_SECRET)
    assert "passing gaming probe horizon differs from the scripted oracle horizon" in errors


def _prepare_bound_workspace(root: Path) -> Path:
    result = run_workflow(REQUEST_PATH, project_root=root)
    project = Path(result["project_dir"])
    manifest_path = project / "manifests" / "rl-environment-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    block = manifest["extensions"]["rl"]
    asset_path = project / block["scene"]["composed_root"]
    runtime_report = project / "reports" / "isaac-load-check.json"
    runtime_report.write_text(
        json.dumps(
            {
                "status": "pass",
                "physics_dt": 1 / 120,
                "runtime_identity": {
                    "id": "isaac-sim",
                    "version": "5.0",
                    "physics_backend": "physx",
                },
                "validation_parameters": {
                    "settle_steps": 240,
                    "settled_speed_metres_per_second": 0.05,
                    "repeatability_tolerance_metres": 0.001,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    package = package_inventory_fingerprint(asset_path.parent)
    assert package["status"] == "pass"

    physics_path = project / "manifests" / "physics-articulation-manifest.json"
    physics = json.loads(physics_path.read_text(encoding="utf-8"))
    physics.update({"status": "validated", "validation_status": "validated"})
    physics["affordances"] = {
        "status": "validated",
        "affordance_labels": ["graspable"],
        "grasp_points": [
            {
                "grasp_id": "g0",
                "frame": [0.0, 0.0, 0.1, 1.0, 0.0, 0.0, 0.0],
                "frame_space": "asset",
                "quaternion_order": "wxyz",
                "approach_vector": [0.0, 0.0, -1.0],
                "gripper_width": 0.06,
                "confidence": 0.9,
                "status": "validated",
            }
        ],
    }
    physics["mass_properties"] = [
        {
            "prim_path": "/warehouse_pick_rl",
            "mass": 1.2,
            "uncertainty": {"mass": 0.1},
            "confidence": 0.8,
            "validation_status": "validated",
        }
    ]
    physics["physics_materials"] = [
        {
            "prim_path": "/warehouse_pick_rl/PhysicsMaterials/DefaultPhysicsMaterial",
            "static_friction": 0.6,
            "dynamic_friction": 0.5,
            "status": "validated",
        }
    ]
    physics_path.write_text(json.dumps(physics, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    material_path = project / "manifests" / "material-inference-manifest.json"
    material = json.loads(material_path.read_text(encoding="utf-8"))
    material.update({"status": "validated", "validation_status": "validated"})
    material_path.write_text(json.dumps(material, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    simready_path = project / "manifests" / "simready-asset-manifest.json"
    simready = json.loads(simready_path.read_text(encoding="utf-8"))
    simready.update({"status": "validated", "validation_status": "validated"})
    lineage_hashes = {
        "manifests/physics-articulation-manifest.json": sha256_file(physics_path),
        "manifests/material-inference-manifest.json": sha256_file(material_path),
    }
    for item in simready["evidence"]:
        if item.get("uri") in lineage_hashes:
            item["checksum"] = lineage_hashes[item["uri"]]
    simready_path.write_text(json.dumps(simready, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    validation_path = project / "reports" / "generated-asset-validation-report.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["status"] = "validated"
    validation["simready_conformance"]["runtime_validation"].update(
        {
            "runtime_id": "isaac-sim",
            "status": "pass",
            "available": True,
            "executed": True,
            "report_path": "reports/isaac-load-check.json",
            "report_sha256": sha256_file(runtime_report),
            "validated_usd_sha256": sha256_file(asset_path),
            "validated_package_dependency_fingerprint": package["fingerprint"],
        }
    )
    validation_path.write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    _, manifest = rl_probe_import._redesign(project)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert validate_payload("rl-environment-manifest", manifest) == []
    block = manifest["extensions"]["rl"]
    protocol_path = project / block["task_fitness_protocol"]["path"]
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    assert protocol["scope"] == "rigid_body_manipulation"
    assert validate_payload("task-fitness-protocol", protocol) == []
    render_to_directory(manifest_path, project / "envs")
    return project


@pytest.fixture(scope="module")
def workspace_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _prepare_bound_workspace(tmp_path_factory.mktemp("rl-bound-workspace"))


@pytest.fixture
def workspace(workspace_template: Path, tmp_path: Path) -> Path:
    destination = tmp_path / "project"
    shutil.copytree(workspace_template, destination)
    return destination


def _approve_rl_protocol(workspace: Path) -> dict:
    manifest_path = workspace / "manifests" / "rl-environment-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    block = manifest["extensions"]["rl"]
    protocol_path = workspace / block["task_fitness_protocol"]["path"]
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    protocol.update(
        {
            "status": "approved",
            "authority": "unit-test-release-authority",
            "approval_id": "approval-unit-test-1",
            "approved_at": "2026-01-01T00:00:00Z",
        }
    )
    protocol_path.write_text(
        json.dumps(protocol, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    protocol_sha256 = sha256_file(protocol_path)
    block["task_fitness_protocol"]["sha256"] = protocol_sha256
    block["task_fitness_protocol"]["status"] = "approved"
    next(item for item in manifest["evidence"] if item["evidence_id"] == "rl_task_fitness_protocol")["checksum"] = (
        protocol_sha256
    )
    block["contract_sha256"] = environment_contract_sha256(block)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    render_to_directory(manifest_path, workspace / "envs")
    return protocol


def _validated_manifest_payload(manifest: dict) -> dict:
    payload = copy.deepcopy(manifest)
    payload["status"] = "validated"
    payload["blocked_reasons"] = []
    payload["review_reasons"] = []
    retained_evidence = [item for item in payload["evidence"] if item.get("evidence_id") not in MANAGED_RL_EVIDENCE_IDS]
    payload["evidence"] = retained_evidence + [
        {
            "evidence_id": evidence_id,
            "kind": evidence_id,
            "uri": f"reports/{evidence_id}.json",
            "checksum": "0" * 64,
        }
        for evidence_id in MANAGED_RL_EVIDENCE_IDS
    ]
    passing_gates = [{"gate_id": gate_id, "status": "pass"} for gate_id in GATE_IDS]
    payload["validation_gates"] = [
        gate for gate in payload.get("validation_gates", []) if not str(gate.get("gate_id", "")).startswith("rl-")
    ] + copy.deepcopy(passing_gates)
    block = payload["extensions"]["rl"]
    block["status"] = "validated"
    block["gates"] = passing_gates
    block["blocked_reasons"] = []
    block["review_reasons"] = []
    return payload


def test_validated_manifest_requires_aperture_fixed_base_rigid_object_and_zero_support(workspace: Path) -> None:
    manifest_path = workspace / "manifests" / "rl-environment-manifest.json"
    baseline = _validated_manifest_payload(json.loads(manifest_path.read_text(encoding="utf-8")))
    assert validate_payload("rl-environment-manifest", baseline) == []

    changes = [
        (lambda block: block["task"]["embodiment"].pop("maximum_gripper_aperture_m"), "maximum_gripper_aperture_m"),
        (lambda block: block["task"]["embodiment"].__setitem__("fixed_base", False), "fixed_base"),
        (lambda block: block["scene"].__setitem__("object_asset_type", "articulation"), "object_asset_type"),
        (lambda block: block["scene"].__setitem__("support_height_m", 0.1), "support_height_m"),
    ]
    for mutate, field in changes:
        candidate = copy.deepcopy(baseline)
        mutate(candidate["extensions"]["rl"])
        issues = validate_payload("rl-environment-manifest", candidate)
        assert issues, field


def test_validated_and_released_manifests_require_complete_rl_evidence_and_gates(workspace: Path) -> None:
    manifest_path = workspace / "manifests" / "rl-environment-manifest.json"
    baseline = _validated_manifest_payload(json.loads(manifest_path.read_text(encoding="utf-8")))
    assert validate_payload("rl-environment-manifest", baseline) == []

    released = copy.deepcopy(baseline)
    released["status"] = "released"
    assert validate_payload("rl-environment-manifest", released) == []
    released["evidence"] = [
        item for item in released["evidence"] if item["evidence_id"] != "rl_fidelity_import_receipt"
    ]
    assert validate_payload("rl-environment-manifest", released)

    for evidence_id in MANAGED_RL_EVIDENCE_IDS:
        missing = copy.deepcopy(baseline)
        missing["evidence"] = [item for item in missing["evidence"] if item["evidence_id"] != evidence_id]
        assert validate_payload("rl-environment-manifest", missing), evidence_id

    duplicate_evidence = copy.deepcopy(baseline)
    duplicate_evidence["evidence"].append(copy.deepcopy(duplicate_evidence["evidence"][-1]))
    assert validate_payload("rl-environment-manifest", duplicate_evidence)

    for path in (("validation_gates",), ("extensions", "rl", "gates")):
        missing_gate = copy.deepcopy(baseline)
        target = missing_gate
        for part in path:
            target = target[part]
        target[:] = [gate for gate in target if gate["gate_id"] != GATE_IDS[0]]
        assert validate_payload("rl-environment-manifest", missing_gate), path

        failing_gate = copy.deepcopy(baseline)
        target = failing_gate
        for part in path:
            target = target[part]
        next(gate for gate in target if gate["gate_id"] == GATE_IDS[0])["status"] = "blocked"
        assert validate_payload("rl-environment-manifest", failing_gate), path

        duplicate_gate = copy.deepcopy(baseline)
        target = duplicate_gate
        for part in path:
            target = target[part]
        target.append(copy.deepcopy(next(gate for gate in target if gate["gate_id"] == GATE_IDS[0])))
        assert validate_payload("rl-environment-manifest", duplicate_gate), path


def test_rl_registration_uses_materialised_record_ids() -> None:
    registry = json.loads((REPO / "configs" / "skill-registry.json").read_text(encoding="utf-8"))
    skill = next(item for item in registry["skills"] if item["name"] == "rl-environment-design-lead")
    assert "rl-environment-design-report" in skill["outputs"]
    assert "rl-environment-report" not in skill["outputs"]

    gate_config = json.loads((REPO / "configs" / "validation-gates.json").read_text(encoding="utf-8"))
    fidelity_gate = next(item for item in gate_config["gates"] if item["id"] == "rl-collision-fidelity")
    assert fidelity_gate["required_evidence"] == ["rl-collision-fidelity"]

    policy = json.loads((REPO / "configs" / "repository-policy.json").read_text(encoding="utf-8"))
    assert "design-decisions/rl-runtime-evidence-boundary.md" in policy["required_docs"]

    environment = (REPO / "deploy" / ".env.example").read_text(encoding="utf-8")
    for variable in (
        "AFB_RL_PROBE_PRODUCER_SHA256",
        "AFB_RL_FIDELITY_PRODUCER_SHA256",
        "AFB_RL_PROBE_ATTESTATION_SECRET",
        "AFB_RL_FIDELITY_ATTESTATION_SECRET",
        "AFB_RL_IMPORT_ATTESTATION_SECRET",
    ):
        assert f"{variable}=\n" in environment

def test_renderer_binds_ground_contact_and_separates_arm_and_gripper_resets(workspace: Path) -> None:
    manifest = json.loads((workspace / "manifests" / "rl-environment-manifest.json").read_text(encoding="utf-8"))
    rendered = render(manifest)
    for name, source_text in rendered.items():
        if name.endswith(".py"):
            ast.parse(source_text, filename=name)
    source = rendered["rl_env_cfg.py"]
    assert "/World/ground/GroundPlane/CollisionPlane" in source
    arm_reset = next(line for line in source.splitlines() if "reset_arm = EventTerm" in line)
    gripper_reset = next(line for line in source.splitlines() if "reset_gripper = EventTerm" in line)
    assert "(-0.05, 0.05)" in arm_reset
    assert "'^shoulder_pan_joint$'" in arm_reset
    assert "'^wrist_yaw_joint$'" in arm_reset
    assert "(0.0, 0.0)" in gripper_reset
    assert "['^left_finger_joint$', '^right_finger_joint$']" in gripper_reset
    assert "-0.05" not in gripper_reset
    initial_state = next(line for line in source.splitlines() if "InitialStateCfg(joint_pos=" in line)
    gripper_action = next(line for line in source.splitlines() if "BinaryJointPositionActionCfg" in line)
    assert "'^left_finger_joint$': 0.04" in initial_state
    assert "'^right_finger_joint$': 0.04" in initial_state
    assert "open_command_expr={'^left_finger_joint$': 0.04, '^right_finger_joint$': 0.04}" in gripper_action
    assert "close_command_expr={'^left_finger_joint$': 0.0, '^right_finger_joint$': 0.0}" in gripper_action


def test_renderer_escapes_literal_isaac_name_selectors() -> None:
    assert _exact_pattern("joint[0].*") == r"^joint\[0\]\.\*$"
    assert _exact_pattern(r"joint\name") == r"^joint\\name$"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda block: block["actions"].append(copy.deepcopy(block["actions"][0])),
        lambda block: block["actions"].pop(),
        lambda block: block["resets"].append(copy.deepcopy(block["resets"][0])),
        lambda block: block["resets"].pop(),
        lambda block: block["terminations"].append(copy.deepcopy(block["terminations"][0])),
        lambda block: block["terminations"].pop(),
        lambda block: block["commands"].append(copy.deepcopy(block["commands"][0])),
        lambda block: block["observations"]["policy"].append(copy.deepcopy(block["observations"]["policy"][0])),
    ],
)
def test_renderer_rejects_duplicate_or_partial_runtime_contracts(workspace: Path, mutate: Any) -> None:
    manifest = json.loads((workspace / "manifests" / "rl-environment-manifest.json").read_text(encoding="utf-8"))
    block = manifest["extensions"]["rl"]
    mutate(block)
    block["contract_sha256"] = environment_contract_sha256(block)
    with pytest.raises(ValueError):
        render(manifest)


def test_renderer_rejects_a_changed_live_urdf(workspace: Path) -> None:
    manifest_path = workspace / "manifests" / "rl-environment-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    robot_path = workspace / manifest["extensions"]["rl"]["task"]["embodiment"]["project_path"]
    robot_path.write_text(robot_path.read_text(encoding="utf-8") + "\n<!-- changed -->\n", encoding="utf-8")
    with pytest.raises(ValueError, match="robot URDF SHA-256 differs"):
        render_to_directory(manifest_path, workspace / "envs")


def test_producers_cannot_write_canonical_reports(tmp_path: Path) -> None:
    project = tmp_path.resolve()
    assert probe_output_path(project, "reports/incoming/probe.json") == project / "reports" / "incoming" / "probe.json"
    assert (
        fidelity_output_path(project, "reports/incoming/fidelity.json")
        == project / "reports" / "incoming" / "fidelity.json"
    )
    with pytest.raises(ValueError, match="reports/incoming"):
        probe_output_path(project, "reports/rl-probe-evidence.json")
    with pytest.raises(ValueError, match="reports/incoming"):
        fidelity_output_path(project, "reports/rl-collision-fidelity.json")


def test_import_receipt_is_required_and_created(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AFB_RL_PROBE_ATTESTATION_SECRET", PROBE_SECRET.decode())
    bindings = rl_probe_import._project_bindings(workspace)
    files = _producer_files(rl_probe_import.PRODUCER_FILES["probe"])
    producer_digest = producer_bundle_sha256(files)
    monkeypatch.setenv("AFB_RL_PROBE_PRODUCER_SHA256", producer_digest)
    payload = _probe_payload(bindings, files)
    incoming = workspace / "reports" / "incoming" / "probe.json"
    write_attested_report(
        incoming,
        {key: value for key, value in payload.items() if key != "attestation"},
        PROBE_SECRET,
    )
    canonical = workspace / "reports" / "rl-probe-evidence.json"
    canonical.write_bytes(incoming.read_bytes())

    loaded, errors = load_attested_report(
        workspace,
        "reports/rl-probe-evidence.json",
        PROBE_REPORT_ID,
        expected_backend=bindings["physics_backend"],
        expected_usd_sha256=bindings["usd_sha256"],
        expected_request_digest=bindings["request_digest"],
        expected_manifest_sha256=bindings["manifest_sha256"],
        expected_contract_sha256=bindings["contract_sha256"],
        expected_package_fingerprint=bindings["package_dependency_fingerprint"],
        expected_sim_dt=bindings["sim_dt"],
        expected_decimation=bindings["decimation"],
        require_import_receipt=True,
    )
    assert loaded is None
    assert errors == ["reports/rl-probe-evidence.import.json is missing; import the RL evidence report"]

    canonical.unlink()
    receipt = workspace / "reports" / "rl-probe-evidence.import.json"
    monkeypatch.delenv("AFB_RL_IMPORT_ATTESTATION_SECRET", raising=False)
    with pytest.raises(ValueError, match="AFB_RL_IMPORT_ATTESTATION_SECRET"):
        apply_rl_evidence_report(workspace, incoming, "probe")
    assert not canonical.exists()
    assert not receipt.exists()

    monkeypatch.setenv("AFB_RL_IMPORT_ATTESTATION_SECRET", IMPORT_SECRET.decode())
    outcome = apply_rl_evidence_report(workspace, incoming, "probe")
    assert outcome["report_path"] == "reports/rl-probe-evidence.json"
    assert receipt.is_file()

    monkeypatch.delenv("AFB_RL_IMPORT_ATTESTATION_SECRET")
    errors = verify_import_receipt(workspace, "reports/rl-probe-evidence.json", payload)
    assert any("AFB_RL_IMPORT_ATTESTATION_SECRET" in error for error in errors)

    monkeypatch.setenv("AFB_RL_IMPORT_ATTESTATION_SECRET", PROBE_SECRET.decode())
    errors = verify_import_receipt(workspace, "reports/rl-probe-evidence.json", payload)
    assert any("role secrets must be distinct" in error for error in errors)

    monkeypatch.setenv("AFB_RL_IMPORT_ATTESTATION_SECRET", IMPORT_SECRET.decode())
    assert (
        verify_import_receipt(
            workspace,
            "reports/rl-probe-evidence.json",
            payload,
            secret=IMPORT_SECRET,
        )
        == []
    )
    receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_payload["receipt_identity"]["version"] == "1.1"
    assert receipt_payload["importer_identity"]["policy_version"] == "1.1"
    assert receipt_payload["producer_policy"] == {
        "environment": "AFB_RL_PROBE_PRODUCER_SHA256",
        "approved_sha256": producer_digest,
    }

    monkeypatch.setenv("AFB_RL_PROBE_PRODUCER_SHA256", "0" * 64)
    loaded, errors = load_attested_report(
        workspace,
        "reports/rl-probe-evidence.json",
        PROBE_REPORT_ID,
        require_import_receipt=True,
    )
    assert loaded is None
    assert any("current approved pin" in error or "current approved producer" in error for error in errors)
    summary, _ = rl_probe_import._redesign(workspace)
    smoke_gate = next(gate for gate in summary["gates"] if gate["gate_id"] == "rl-smoke")
    assert smoke_gate["status"] == "blocked"
    assert any(
        "current approved pin" in reason or "current approved producer" in reason for reason in smoke_gate["reasons"]
    )


def test_importer_rejects_reused_report_and_import_secrets(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings = rl_probe_import._project_bindings(workspace)
    files = _producer_files(rl_probe_import.PRODUCER_FILES["probe"])
    producer_digest = producer_bundle_sha256(files)
    monkeypatch.setenv("AFB_RL_PROBE_PRODUCER_SHA256", producer_digest)
    monkeypatch.setenv("AFB_RL_PROBE_ATTESTATION_SECRET", PROBE_SECRET.decode())
    monkeypatch.setenv("AFB_RL_IMPORT_ATTESTATION_SECRET", PROBE_SECRET.decode())
    payload = _probe_payload(bindings, files)
    incoming = workspace / "reports" / "incoming" / "probe.json"
    write_attested_report(
        incoming,
        {key: value for key, value in payload.items() if key != "attestation"},
        PROBE_SECRET,
    )

    with pytest.raises(ValueError, match="role secrets must be distinct"):
        apply_rl_evidence_report(workspace, incoming, "probe")
    assert not (workspace / "reports" / "rl-probe-evidence.json").exists()
    assert not (workspace / "reports" / "rl-probe-evidence.import.json").exists()


def test_fidelity_report_uses_the_pinned_import_path(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings = rl_probe_import._project_bindings(workspace)
    files = _producer_files(rl_probe_import.PRODUCER_FILES["fidelity"])
    producer_digest = producer_bundle_sha256(files)
    monkeypatch.setenv("AFB_RL_FIDELITY_PRODUCER_SHA256", producer_digest)
    monkeypatch.setenv("AFB_RL_FIDELITY_ATTESTATION_SECRET", FIDELITY_SECRET.decode())
    monkeypatch.setenv("AFB_RL_IMPORT_ATTESTATION_SECRET", IMPORT_SECRET.decode())
    payload = _bound_fidelity_payload(bindings, files)
    incoming = workspace / "reports" / "incoming" / "fidelity.json"
    write_attested_report(
        incoming,
        {key: value for key, value in payload.items() if key != "attestation"},
        FIDELITY_SECRET,
    )

    outcome = apply_rl_evidence_report(workspace, incoming, "fidelity")
    assert outcome["report_path"] == "reports/rl-collision-fidelity.json"
    assert (
        verify_import_receipt(
            workspace,
            "reports/rl-collision-fidelity.json",
            payload,
            secret=IMPORT_SECRET,
        )
        == []
    )
    manifest = json.loads((workspace / "manifests" / "rl-environment-manifest.json").read_text(encoding="utf-8"))
    evidence = {item["evidence_id"]: item for item in manifest["evidence"]}
    assert evidence["rl_collision_fidelity"]["checksum"] == sha256_file(
        workspace / "reports" / "rl-collision-fidelity.json"
    )
    assert evidence["rl_fidelity_import_receipt"]["checksum"] == sha256_file(
        workspace / "reports" / "rl-collision-fidelity.import.json"
    )


def test_positive_rl_capsule_copies_and_revalidates_both_receipt_chains(
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _approve_rl_protocol(workspace)
    monkeypatch.setenv("AFB_RL_PROBE_ATTESTATION_SECRET", PROBE_SECRET.decode())
    monkeypatch.setenv("AFB_RL_FIDELITY_ATTESTATION_SECRET", FIDELITY_SECRET.decode())
    monkeypatch.setenv("AFB_RL_IMPORT_ATTESTATION_SECRET", IMPORT_SECRET.decode())

    probe_files = _producer_files(rl_probe_import.PRODUCER_FILES["probe"])
    probe_pin = producer_bundle_sha256(probe_files)
    monkeypatch.setenv("AFB_RL_PROBE_PRODUCER_SHA256", probe_pin)
    probe_bindings = rl_probe_import._project_bindings(workspace)
    probe = _probe_payload(probe_bindings, probe_files)
    probe_incoming = workspace / "reports" / "incoming" / "probe.json"
    write_attested_report(
        probe_incoming,
        {key: value for key, value in probe.items() if key != "attestation"},
        PROBE_SECRET,
    )
    apply_rl_evidence_report(workspace, probe_incoming, "probe")

    fidelity_files = _producer_files(rl_probe_import.PRODUCER_FILES["fidelity"])
    fidelity_pin = producer_bundle_sha256(fidelity_files)
    monkeypatch.setenv("AFB_RL_FIDELITY_PRODUCER_SHA256", fidelity_pin)
    fidelity_bindings = rl_probe_import._project_bindings(workspace)
    fidelity = _bound_fidelity_payload(fidelity_bindings, fidelity_files)
    fidelity_incoming = workspace / "reports" / "incoming" / "fidelity.json"
    write_attested_report(
        fidelity_incoming,
        {key: value for key, value in fidelity.items() if key != "attestation"},
        FIDELITY_SECRET,
    )
    apply_rl_evidence_report(workspace, fidelity_incoming, "fidelity")

    manifest_path = workspace / "manifests" / "rl-environment-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "validated"
    manifest["validation_status"] = "validated"
    manifest["blocked_reasons"] = []
    manifest["review_reasons"] = []
    manifest.pop("review_status", None)
    block = manifest["extensions"]["rl"]
    block["status"] = "validated"
    block["blocked_reasons"] = []
    block["review_reasons"] = []
    for gate in manifest["validation_gates"]:
        if str(gate.get("gate_id") or "").startswith("rl-"):
            gate["status"] = "pass"
            gate["reasons"] = []
    for gate in block["gates"]:
        gate["status"] = "pass"
        gate["reasons"] = []
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert validate_payload("rl-environment-manifest", manifest) == []

    staging = tmp_path / "capsule"
    (staging / "validation").mkdir(parents=True)
    protocol_path = workspace / block["task_fitness_protocol"]["path"]
    shutil.copyfile(
        protocol_path,
        staging / capsule_module._POSITIVE_EVIDENCE_PATHS["task_protocol"],
    )
    roles: dict[str, tuple[str, str, str | None]] = {}
    capsule_module._copy_rl_evidence_chain(
        workspace,
        staging,
        protocol=protocol,
        roles=roles,
        licence_expression="MIT",
    )
    assert set(capsule_module._RL_POSITIVE_EVIDENCE_PATHS.values()) <= set(roles)

    monkeypatch.setenv("AFB_RL_PROBE_PRODUCER_SHA256", "0" * 64)
    stale_staging = tmp_path / "stale-capsule"
    (stale_staging / "validation").mkdir(parents=True)
    shutil.copyfile(
        protocol_path,
        stale_staging / capsule_module._POSITIVE_EVIDENCE_PATHS["task_protocol"],
    )
    with pytest.raises(capsule_module.CapsuleCreationError, match="current approved pin"):
        capsule_module._copy_rl_evidence_chain(
            workspace,
            stale_staging,
            protocol=protocol,
            roles={},
            licence_expression="MIT",
        )
