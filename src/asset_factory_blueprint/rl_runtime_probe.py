"""Run the contract-bound RL acceptance probes under Isaac Lab."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import random
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asset_factory_blueprint.rl_evidence as rl_evidence_module
import asset_factory_blueprint.rl_probes as rl_probes_module
import asset_factory_blueprint.rl_render as rl_render_module
from asset_factory_blueprint.execution import durable_replace, durable_unlink, workspace_lease
from asset_factory_blueprint.manifests import validate_payload
from asset_factory_blueprint.rl_evidence import (
    PROBE_IDS,
    PROBE_REPORT_ID,
    attest_report,
    environment_contract_sha256,
    environment_manifest_sha256,
    producer_bundle_sha256,
    report_attestation_secret,
    verify_report,
    write_attested_report,
)
from asset_factory_blueprint.rl_probes import (
    StepResult,
    assemble_probe_report,
    component_dominance,
    penetration_depths_from_separations,
    run_gaming_probes,
    run_oracle,
    run_repeatability,
    run_reset_feasibility,
    run_smoke,
)
from asset_factory_blueprint.rl_render import verify_rendered_package_sources
from asset_factory_blueprint.schemas.common import RunPlan, RunRequest
from asset_factory_blueprint.utils.checksums import sha256_file, sha256_text
from asset_factory_blueprint.utils.package_fingerprint import package_inventory_fingerprint

RENDERED_FILES = ("__init__.py", "rl_env_cfg.py", "custom_mdp.py", "rl_env_cfg.render.json")
ORACLE_PHASES = ("approach", "descend", "close", "lift")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run RL lane probes under the bound Isaac Lab runtime")
    parser.add_argument("probes", nargs="*", choices=PROBE_IDS, default=list(PROBE_IDS))
    parser.add_argument("--project", required=True)
    parser.add_argument("--manifest", default="manifests/rl-environment-manifest.json")
    parser.add_argument("--output", default="reports/incoming/rl-probe-evidence.json")
    return parser.parse_args()


def _project_file(project: Path, value: str, label: str) -> Path:
    raw = Path(value)
    if not value or raw.is_absolute() or ".." in raw.parts:
        raise ValueError(f"{label} must be project-relative")
    cursor = project
    for part in raw.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"{label} must not traverse a symbolic link")
    resolved = (project / raw).resolve(strict=True)
    resolved.relative_to(project)
    if not resolved.is_file():
        raise ValueError(f"{label} must identify a regular file")
    return resolved


def _output_path(project: Path, value: str) -> Path:
    raw = Path(value)
    if not value or raw.is_absolute() or ".." in raw.parts:
        raise ValueError("output must be project-relative")
    cursor = project
    for part in raw.parent.parts:
        cursor /= part
        if cursor.exists() and cursor.is_symlink():
            raise ValueError("output parent must not traverse a symbolic link")
    resolved = (project / raw).resolve(strict=False)
    resolved.relative_to(project)
    incoming = (project / "reports" / "incoming").resolve(strict=False)
    try:
        resolved.relative_to(incoming)
    except ValueError as exc:
        raise ValueError(
            "probe output must be inside reports/incoming so evidence passes through the importer"
        ) from exc
    if resolved.is_symlink():
        raise ValueError("output must not be a symbolic link")
    return resolved


def _producer_files() -> list[dict[str, str]]:
    paths = {
        "src/asset_factory_blueprint/rl_runtime_probe.py": Path(__file__).resolve(),
        "src/asset_factory_blueprint/rl_evidence.py": Path(rl_evidence_module.__file__).resolve(),
        "src/asset_factory_blueprint/rl_probes.py": Path(rl_probes_module.__file__).resolve(),
        "src/asset_factory_blueprint/rl_render.py": Path(rl_render_module.__file__).resolve(),
    }
    return [{"path": label, "sha256": sha256_file(path)} for label, path in sorted(paths.items())]


def _rendered_files(project: Path, manifest: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    hashes = verify_rendered_package_sources(project, manifest)
    record = json.loads((project / "envs" / "rl_env_cfg.render.json").read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("ready") is not True:
        raise ValueError("rendered environment record is not ready")
    if record.get("files") != {name: hashes[name] for name in RENDERED_FILES[:3]}:
        raise ValueError("rendered environment source hashes differ from the render record")
    return hashes, record


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _positive_float(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or value <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return float(value)


def _probe_parameters(protocol: dict[str, Any]) -> dict[str, Any]:
    extensions = protocol.get("extensions") if isinstance(protocol.get("extensions"), dict) else {}
    rl_extensions = extensions.get("rl") if isinstance(extensions.get("rl"), dict) else {}
    raw = rl_extensions.get("probe_parameters") if isinstance(rl_extensions.get("probe_parameters"), dict) else {}
    reset_seeds = raw.get("reset_seeds")
    if (
        not isinstance(reset_seeds, list)
        or not reset_seeds
        or any(not isinstance(seed, int) or isinstance(seed, bool) or seed < 0 for seed in reset_seeds)
        or len(reset_seeds) != len(set(reset_seeds))
    ):
        raise ValueError("task-fitness probe reset_seeds must contain unique non-negative integers")
    phase_steps = raw.get("oracle_phase_steps") if isinstance(raw.get("oracle_phase_steps"), dict) else {}
    if set(phase_steps) != set(ORACLE_PHASES):
        raise ValueError("task-fitness oracle_phase_steps must contain the four canonical phases")
    allowed_fraction = raw.get("allowed_gaming_fraction")
    if (
        not isinstance(allowed_fraction, (int, float))
        or isinstance(allowed_fraction, bool)
        or not math.isfinite(float(allowed_fraction))
        or not 0 <= float(allowed_fraction) < 1
    ):
        raise ValueError("task-fitness allowed_gaming_fraction must be at least zero and less than one")
    orientation_tolerance = _positive_float(
        raw.get("grasp_orientation_tolerance_rad"), "task-fitness grasp_orientation_tolerance_rad"
    )
    if orientation_tolerance > math.pi:
        raise ValueError("task-fitness grasp_orientation_tolerance_rad must not exceed pi")
    parsed_phase_steps = {
        phase: _positive_int(phase_steps.get(phase), f"task-fitness oracle_phase_steps.{phase}")
        for phase in ORACLE_PHASES
    }
    gaming_steps = _positive_int(raw.get("gaming_steps"), "task-fitness gaming_steps")
    if gaming_steps != sum(parsed_phase_steps.values()):
        raise ValueError("task-fitness gaming_steps must equal the complete scripted-oracle horizon")
    return {
        "smoke_steps": _positive_int(raw.get("smoke_steps"), "task-fitness smoke_steps"),
        "repeat_steps": _positive_int(raw.get("repeat_steps"), "task-fitness repeat_steps"),
        "gaming_steps": gaming_steps,
        "reset_seeds": list(reset_seeds),
        "penetration_tolerance_m": _positive_float(
            raw.get("penetration_tolerance_m"), "task-fitness penetration_tolerance_m"
        ),
        "allowed_gaming_fraction": float(allowed_fraction),
        "grasp_position_tolerance_m": _positive_float(
            raw.get("grasp_position_tolerance_m"), "task-fitness grasp_position_tolerance_m"
        ),
        "grasp_orientation_tolerance_rad": orientation_tolerance,
        "oracle_phase_steps": parsed_phase_steps,
    }


def load_bindings(project: Path, manifest_relative: str) -> dict[str, Any]:
    manifest_path = _project_file(project, manifest_relative, "RL environment manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema_errors = validate_payload("rl-environment-manifest", manifest)
    if schema_errors:
        raise ValueError("RL environment manifest schema: " + "; ".join(issue.render() for issue in schema_errors))
    block = (manifest.get("extensions") or {}).get("rl") or {}
    if environment_contract_sha256(block) != block.get("contract_sha256"):
        raise ValueError("RL environment contract SHA-256 is stale")
    runtime = block.get("runtime") if isinstance(block.get("runtime"), dict) else {}
    if runtime.get("physics_backend") != "physx":
        raise ValueError("the probe adapter supports the PhysX contract only")
    if (block.get("task") or {}).get("behaviour") != "pick":
        raise ValueError("the probe adapter supports the pick contract only")
    if (block.get("scene") or {}).get("object_asset_type") != "rigid_object":
        raise ValueError("the probe adapter supports rigid pick objects only")

    request = RunRequest.model_validate_json(
        _project_file(project, "run-request.json", "run request").read_text(encoding="utf-8")
    )
    plan = RunPlan.model_validate_json(_project_file(project, "run-plan.json", "run plan").read_text(encoding="utf-8"))
    current_request_digest = "sha256:" + sha256_text(request.model_dump_json())
    if plan.request_digest != current_request_digest:
        raise ValueError("run plan digest does not identify the persisted run request")
    if plan.request_id != request.id or plan.asset_id != request.id:
        raise ValueError("run plan identity does not match the persisted run request")
    usd_relative = str((block.get("scene") or {}).get("composed_root") or "")
    usd_path = _project_file(project, usd_relative, "validated USD")
    package = package_inventory_fingerprint(usd_path.parent)
    if package["status"] != "pass":
        raise ValueError("validated package inventory cannot be recomputed")
    if sha256_file(usd_path) != runtime.get("validated_usd_sha256"):
        raise ValueError("validated USD differs from the RL contract")
    if package["fingerprint"] != runtime.get("validated_package_dependency_fingerprint"):
        raise ValueError("validated package closure differs from the RL contract")

    runtime_report = _project_file(project, "reports/isaac-load-check.json", "Isaac runtime report")
    runtime_report_sha256 = sha256_file(runtime_report)
    if runtime_report_sha256 != (runtime.get("runtime_evidence") or {}).get("sha256"):
        raise ValueError("Isaac runtime report differs from the RL contract")

    protocol_ref = block.get("task_fitness_protocol") if isinstance(block.get("task_fitness_protocol"), dict) else {}
    protocol_path = _project_file(project, str(protocol_ref.get("path") or ""), "task-fitness protocol")
    if sha256_file(protocol_path) != protocol_ref.get("sha256"):
        raise ValueError("task-fitness protocol checksum differs from the RL contract")
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    protocol_errors = validate_payload("task-fitness-protocol", protocol)
    if protocol_errors:
        raise ValueError("task-fitness protocol schema: " + "; ".join(issue.render() for issue in protocol_errors))
    if protocol.get("protocol_id") != protocol_ref.get("protocol_id"):
        raise ValueError("task-fitness protocol identity differs from the RL contract")
    protocol_rl = (protocol.get("extensions") or {}).get("rl") or {}
    if protocol_rl.get("request_digest") != plan.request_digest:
        raise ValueError("task-fitness protocol belongs to another run request")

    task = block.get("task") if isinstance(block.get("task"), dict) else {}
    grasp_frames = task.get("grasp_frames") if isinstance(task.get("grasp_frames"), list) else []
    if not grasp_frames:
        raise ValueError("RL contract contains no accepted grasp frames")
    embodiment = task.get("embodiment") if isinstance(task.get("embodiment"), dict) else {}
    if embodiment.get("status") != "validated":
        raise ValueError("RL embodiment is not validated")
    if embodiment.get("fixed_base") is not True:
        raise ValueError("the probe adapter requires a fixed-base embodiment")
    robot_relative = str(embodiment.get("project_path") or "")
    robot_path = _project_file(project, robot_relative, "embodiment URDF")
    if sha256_file(robot_path) != embodiment.get("sha256"):
        raise ValueError("embodiment URDF differs from the RL contract")
    gripper_joint_set = set(embodiment.get("gripper_joints") or [])
    arm_joints = [
        str(item.get("name") or "")
        for item in embodiment.get("actuated_joints") or []
        if isinstance(item, dict) and item.get("name") not in gripper_joint_set
    ]
    if not arm_joints:
        raise ValueError("RL embodiment contains no arm joints")

    rewards = [item for item in block.get("rewards") or [] if isinstance(item, dict)]
    dominance_values = {float(item["dominance_bound"]) for item in rewards if item.get("dominance_bound") is not None}
    if len(dominance_values) != 1:
        raise ValueError("reward components do not share one component-dominance bound")

    rendered, render_record = _rendered_files(project, manifest)
    stable_manifest_sha256 = environment_manifest_sha256(manifest)
    expected_record = {
        "manifest_sha256": stable_manifest_sha256,
        "contract_sha256": block["contract_sha256"],
        "package_dependency_fingerprint": package["fingerprint"],
        "asset_usd": usd_relative,
        "asset_usd_sha256": sha256_file(usd_path),
        "robot_urdf": embodiment.get("project_path"),
        "robot_urdf_sha256": embodiment.get("sha256"),
        "physics_backend": "physx",
    }
    for field, expected in expected_record.items():
        if render_record.get(field) != expected:
            raise ValueError(f"rendered environment {field} differs from the current contract")

    settle = runtime.get("settle_parameters") if isinstance(runtime.get("settle_parameters"), dict) else {}
    settle_steps = _positive_int(settle.get("settle_steps"), "runtime settle_steps")
    settled_speed = _positive_float(
        settle.get("settled_speed_metres_per_second"), "runtime settled_speed_metres_per_second"
    )
    repeatability_tolerance = _positive_float(
        settle.get("repeatability_tolerance_metres"), "runtime repeatability_tolerance_metres"
    )
    configured_version = str(runtime.get("isaac_lab_version") or "")
    if not configured_version:
        raise ValueError("RL runtime has no pinned Isaac Lab version")
    return {
        "runtime_contract": runtime,
        "manifest_path": manifest_relative,
        "manifest_sha256": stable_manifest_sha256,
        "contract_sha256": block["contract_sha256"],
        "usd_path": usd_relative,
        "usd_sha256": sha256_file(usd_path),
        "request_digest": plan.request_digest,
        "package_dependency_fingerprint": package["fingerprint"],
        "runtime_source_report_sha256": runtime_report_sha256,
        "rendered_files": rendered,
        "probe_parameters": _probe_parameters(protocol),
        "grasp_frames": grasp_frames,
        "dominance_bound": dominance_values.pop(),
        "embodiment": embodiment,
        "arm_joints": arm_joints,
        "gripper_joints": list(embodiment.get("gripper_joints") or []),
        "success_height_m": _positive_float((task.get("success") or {}).get("height_m"), "task success height_m"),
        "settle_steps": settle_steps,
        "settled_speed": settled_speed,
        "repeatability_tolerance": repeatability_tolerance,
        "isaac_lab_version": configured_version,
    }


class IsaacLabProbeEnvironment:
    """Adapter from the generated ``ManagerBasedRLEnv`` to the acceptance probes."""

    def __init__(self, env: Any, torch_module: Any, bindings: dict[str, Any]) -> None:
        from isaaclab.controllers import DifferentialIKController, DifferentialIKControllerCfg

        self.env = env
        self.torch = torch_module
        self.num_envs = int(env.num_envs)
        if self.num_envs != int(bindings["runtime_contract"]["num_envs"]):
            raise RuntimeError("instantiated environment count differs from the RL contract")
        self.device = env.device
        self.component_names = list(env.reward_manager.active_terms)
        if self.component_names != ["reach", "lift", "action_rate"]:
            raise RuntimeError("generated reward manager does not expose the exact reach, lift and action-rate terms")
        self.grasp_ids = [str(item["id"]) for item in bindings["grasp_frames"]]
        self._grasps = {str(item["id"]): item for item in bindings["grasp_frames"]}
        self._selected_grasp = self._grasps[self.grasp_ids[0]]
        self._position_tolerance = float(bindings["probe_parameters"]["grasp_position_tolerance_m"])
        self._orientation_tolerance = float(bindings["probe_parameters"]["grasp_orientation_tolerance_rad"])
        embodiment = bindings["embodiment"]
        self._action_scale = float(embodiment["action_scale"])
        self._fixed_base = bool(embodiment["fixed_base"])
        self._ee_body_name = str(embodiment["end_effector_body"])
        self._action_dim = int(env.action_manager.total_action_dim)
        self._action_slices: dict[str, slice] = {}
        cursor = 0
        for name, dimension in zip(env.action_manager.active_terms, env.action_manager.action_term_dim, strict=True):
            self._action_slices[str(name)] = slice(cursor, cursor + int(dimension))
            cursor += int(dimension)
        if set(self._action_slices) != {"arm", "gripper"} or cursor != self._action_dim:
            raise RuntimeError("generated action manager does not expose the exact arm and gripper terms")
        arm_slice = self._action_slices["arm"]
        gripper_slice = self._action_slices["gripper"]
        if arm_slice.stop - arm_slice.start != len(bindings["arm_joints"]):
            raise RuntimeError("arm action dimension differs from the embodiment contract")
        if gripper_slice.stop - gripper_slice.start != 1:
            raise RuntimeError("binary gripper action must expose exactly one command dimension")
        arm_term = env.action_manager.get_term("arm")
        gripper_term = env.action_manager.get_term("gripper")
        if list(arm_term.IO_descriptor.joint_names) != bindings["arm_joints"]:
            raise RuntimeError("live arm action joint names differ from the embodiment contract")
        if list(gripper_term.IO_descriptor.joint_names) != bindings["gripper_joints"]:
            raise RuntimeError("live gripper action joint names differ from the embodiment contract")

        robot = env.scene["robot"]
        arm_joint_patterns = [f"^{re.escape(name)}$" for name in bindings["arm_joints"]]
        self._joint_ids, joint_names = robot.find_joints(arm_joint_patterns, preserve_order=True)
        if list(joint_names) != bindings["arm_joints"]:
            raise RuntimeError("arm joint resolution differs from the contract order")
        body_pattern = f"^{re.escape(self._ee_body_name)}$"
        body_ids, body_names = robot.find_bodies([body_pattern], preserve_order=True)
        if len(body_ids) != 1 or list(body_names) != [self._ee_body_name]:
            raise RuntimeError("end-effector body does not resolve uniquely")
        self._ee_body_id = int(body_ids[0])
        self._jacobian_body_id = self._ee_body_id - 1 if self._fixed_base else self._ee_body_id
        if self._jacobian_body_id < 0:
            raise RuntimeError("fixed-base end-effector has no Jacobian row")
        self._controller = DifferentialIKController(
            DifferentialIKControllerCfg(command_type="pose", use_relative_mode=False, ik_method="dls"),
            num_envs=self.num_envs,
            device=self.device,
        )
        self._pending_joint_violations = [0] * self.num_envs
        self._last_success = [False] * self.num_envs
        self._lift_target_position = None
        self._lift_target_quaternion = None
        aperture = _positive_float(
            embodiment.get("maximum_gripper_aperture_m"), "embodiment maximum_gripper_aperture_m"
        )
        self._lift_distance_m = float(bindings["success_height_m"]) + 0.02
        for grasp in self._grasps.values():
            if float(grasp["gripper_width"]) > aperture + 1e-9:
                raise RuntimeError(
                    f"grasp {grasp['id']} requires {grasp['gripper_width']} m aperture; embodiment exposes {aperture} m"
                )

        if "contact_forces" not in env.scene.sensors:
            raise RuntimeError("generated environment has no contact-forces sensor")
        sensor = env.scene.sensors["contact_forces"]
        module_name = type(sensor.contact_physx_view).__module__
        if not module_name.startswith("omni.physics.tensors"):
            raise RuntimeError(f"contact sensor is not backed by the PhysX tensor API: {module_name}")

    def reset(self, seed: int) -> None:
        self.env.reset(seed=seed)
        self._pending_joint_violations = [0] * self.num_envs
        self._last_success = [False] * self.num_envs
        self._lift_target_position = None
        self._lift_target_quaternion = None

    def step(self, actions: list[list[float]]) -> StepResult:
        tensor = self.torch.tensor(actions, dtype=self.torch.float32, device=self.device)
        _, rewards, terminated, truncated, _ = self.env.step(tensor)
        step_reward = getattr(self.env.reward_manager, "_step_reward", None)
        if step_reward is None or tuple(step_reward.shape) != (self.num_envs, len(self.component_names)):
            raise RuntimeError("Isaac Lab reward manager exposes no complete per-term step buffer")
        components = {
            name: step_reward[:, index].detach().cpu().tolist() for index, name in enumerate(self.component_names)
        }
        success = getattr(self.env, "_afb_step_success", None)
        joint_violations = getattr(self.env, "_afb_step_joint_limit_violation", None)
        if success is None or joint_violations is None:
            raise RuntimeError("generated terminations expose no pre-reset success and joint-limit snapshots")
        self._last_success = [bool(value) for value in success.detach().cpu().tolist()]
        for index, value in enumerate(joint_violations.detach().cpu().tolist()):
            self._pending_joint_violations[index] += int(bool(value))
        return StepResult(
            rewards=rewards.detach().cpu().tolist(),
            components=components,
            terminated=[bool(value) for value in terminated.detach().cpu().tolist()],
            truncated=[bool(value) for value in truncated.detach().cpu().tolist()],
            success=list(self._last_success),
        )

    def zero_actions(self) -> list[list[float]]:
        return [[0.0] * self._action_dim for _ in range(self.num_envs)]

    def random_actions(self, rng: random.Random) -> list[list[float]]:
        return [[rng.uniform(-1.0, 1.0) for _ in range(self._action_dim)] for _ in range(self.num_envs)]

    def select_grasp(self, grasp_id: str) -> None:
        try:
            self._selected_grasp = self._grasps[grasp_id]
        except KeyError as exc:
            raise ValueError(f"unknown grasp ID {grasp_id}") from exc
        self._lift_target_position = None
        self._lift_target_quaternion = None

    def oracle_actions(self, phase: str, step: int) -> list[list[float]]:
        if phase == "lift":
            if step == 0 or self._lift_target_position is None or self._lift_target_quaternion is None:
                target_position, target_quaternion = self._target_pose("close")
                target_position = target_position.clone()
                target_position[:, 2] += self._lift_distance_m
                self._lift_target_position = target_position
                self._lift_target_quaternion = target_quaternion.clone()
            target_position = self._lift_target_position
            target_quaternion = self._lift_target_quaternion
        else:
            target_position, target_quaternion = self._target_pose(phase)
        gripper = 1.0 if phase in {"approach", "descend"} else -1.0
        return self._ik_actions(target_position, target_quaternion, gripper)

    def gaming_actions(self, pattern: str, step: int) -> list[list[float]]:
        if pattern == "proximity_without_contact":
            position, quaternion = self._target_pose("approach")
            return self._ik_actions(position, quaternion, 1.0)
        actions = self.torch.zeros((self.num_envs, self._action_dim), device=self.device)
        arm_slice = self._action_slices["arm"]
        gripper_slice = self._action_slices["gripper"]
        if pattern == "velocity_without_displacement":
            actions[:, arm_slice] = 0.8 if step % 2 == 0 else -0.8
        elif pattern == "oscillation":
            actions[:, arm_slice] = 0.6 * math.sin(step / 3.0)
        elif pattern == "early_termination":
            actions[:, arm_slice] = 1.0 if step % 2 == 0 else -1.0
        else:
            raise ValueError(f"unsupported gaming pattern {pattern}")
        actions[:, gripper_slice] = 1.0
        return actions.detach().cpu().tolist()

    def object_positions(self) -> list[list[float]]:
        positions = self.env.scene["object"].data.root_pos_w - self.env.scene.env_origins
        return positions.detach().cpu().tolist()

    def object_speeds(self) -> list[float]:
        return self.env.scene["object"].data.root_lin_vel_w.norm(dim=1).detach().cpu().tolist()

    def penetration_depths(self) -> list[float]:
        sensor = self.env.scene.sensors["contact_forces"]
        _, _, _, separation, counts, starts = sensor.contact_physx_view.get_contact_data(dt=float(self.env.physics_dt))
        flat_counts = counts.reshape(-1).detach().cpu().tolist()
        flat_starts = starts.reshape(-1).detach().cpu().tolist()
        flat_separation = separation.reshape(-1).detach().cpu().tolist()
        filter_count = int(sensor.contact_physx_view.filter_count)
        if filter_count < 1 or int(sensor.num_bodies) != 1:
            raise RuntimeError("penetration measurement requires one rigid object body and at least one contact filter")
        expected_rows = self.num_envs * filter_count
        if len(flat_counts) != expected_rows or len(flat_starts) != expected_rows:
            raise RuntimeError("PhysX contact-separation rows do not cover every environment and filter")
        capacity = int(sensor.cfg.max_contact_data_count_per_prim) * int(sensor.num_instances)
        if sum(int(value) for value in flat_counts) >= capacity:
            raise RuntimeError("PhysX contact data reached its configured capacity; penetration is incomplete")
        per_environment: list[list[float]] = [[] for _ in range(self.num_envs)]
        for row, (count, start) in enumerate(zip(flat_counts, flat_starts, strict=True)):
            environment = row // filter_count
            count_int, start_int = int(count), int(start)
            if count_int < 0 or start_int < 0 or start_int + count_int > len(flat_separation):
                raise RuntimeError("PhysX contact data contains invalid count or start indices")
            per_environment[environment].extend(
                float(value) for value in flat_separation[start_int : start_int + count_int]
            )
        return penetration_depths_from_separations(per_environment)

    def joint_limit_violations(self) -> list[int]:
        result = list(self._pending_joint_violations)
        self._pending_joint_violations = [0] * self.num_envs
        return result

    def grasp_frame_reached(self) -> list[bool]:
        target_position, target_quaternion = self._target_pose("descend")
        robot = self.env.scene["robot"]
        state = robot.data.body_state_w[:, self._ee_body_id, 0:7]
        position_error = self.torch.linalg.vector_norm(state[:, :3] - target_position, dim=1)
        quaternion_dot = self.torch.abs(self.torch.sum(state[:, 3:7] * target_quaternion, dim=1)).clamp(0.0, 1.0)
        orientation_error = 2.0 * self.torch.acos(quaternion_dot)
        return (
            ((position_error <= self._position_tolerance) & (orientation_error <= self._orientation_tolerance))
            .detach()
            .cpu()
            .tolist()
        )

    def task_success(self) -> list[bool]:
        return list(self._last_success)

    def _target_pose(self, phase: str) -> tuple[Any, Any]:
        from isaaclab.utils.math import combine_frame_transforms, quat_apply

        asset = self.env.scene["object"]
        frame = self._selected_grasp["frame"]
        local_position = self.torch.tensor(frame[:3], dtype=self.torch.float32, device=self.device).repeat(
            self.num_envs, 1
        )
        local_quaternion = self.torch.tensor(frame[3:7], dtype=self.torch.float32, device=self.device).repeat(
            self.num_envs, 1
        )
        position, quaternion = combine_frame_transforms(
            asset.data.root_pos_w,
            asset.data.root_quat_w,
            local_position,
            local_quaternion,
        )
        approach_local = self.torch.tensor(
            self._selected_grasp["approach_vector"], dtype=self.torch.float32, device=self.device
        ).repeat(self.num_envs, 1)
        approach_world = quat_apply(asset.data.root_quat_w, approach_local)
        if phase == "approach":
            position = position - approach_world * 0.08
        elif phase not in {"descend", "close"}:
            raise ValueError(f"unsupported oracle phase {phase}")
        return position, quaternion

    def _ik_actions(self, target_position: Any, target_quaternion: Any, gripper: float) -> list[list[float]]:
        from isaaclab.utils.math import subtract_frame_transforms

        robot = self.env.scene["robot"]
        ee_pose_w = robot.data.body_state_w[:, self._ee_body_id, 0:7]
        root_pose_w = robot.data.root_state_w[:, 0:7]
        ee_position_b, ee_quaternion_b = subtract_frame_transforms(
            root_pose_w[:, :3], root_pose_w[:, 3:7], ee_pose_w[:, :3], ee_pose_w[:, 3:7]
        )
        target_position_b, target_quaternion_b = subtract_frame_transforms(
            root_pose_w[:, :3], root_pose_w[:, 3:7], target_position, target_quaternion
        )
        jacobian = robot.root_physx_view.get_jacobians()[:, self._jacobian_body_id, :, self._joint_ids]
        self._controller.set_command(self.torch.cat((target_position_b, target_quaternion_b), dim=1))
        joint_position = robot.data.joint_pos[:, self._joint_ids]
        joint_targets = self._controller.compute(ee_position_b, ee_quaternion_b, jacobian, joint_position)
        default = robot.data.default_joint_pos[:, self._joint_ids]
        arm = ((joint_targets - default) / self._action_scale).clamp(-1.0, 1.0)
        actions = self.torch.zeros((self.num_envs, self._action_dim), device=self.device)
        arm_slice = self._action_slices["arm"]
        if arm.shape[1] != arm_slice.stop - arm_slice.start:
            raise RuntimeError("IK arm dimension differs from the generated action term")
        actions[:, arm_slice] = arm
        actions[:, self._action_slices["gripper"]] = gripper
        return actions.detach().cpu().tolist()


def _load_env_package(project: Path, bindings: dict[str, Any]) -> Any:
    env_dir = project / "envs"
    package_name = f"_afb_env_{bindings['contract_sha256'][:16]}"
    spec = importlib.util.spec_from_file_location(
        package_name,
        env_dir / "__init__.py",
        submodule_search_locations=[str(env_dir)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the rendered environment package")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return module


def _write_report(output: Path, report: dict[str, Any], secret: bytes) -> dict[str, Any]:
    signed = {**report, "attestation": attest_report(report, secret)}
    schema_errors = validate_payload("rl-probe-evidence", signed)
    semantic_errors = verify_report(
        signed,
        PROBE_REPORT_ID,
        secret=secret,
        expected_backend="physx",
        expected_usd_sha256=report["usd_sha256"],
        expected_request_digest=report["request_digest"],
        expected_manifest_sha256=report["manifest_sha256"],
        expected_contract_sha256=report["contract_sha256"],
        expected_package_fingerprint=report["package_dependency_fingerprint"],
        expected_sim_dt=report["runtime"]["sim_dt"],
        expected_decimation=report["runtime"]["decimation"],
        expected_rendered_files=report["rendered_files"],
    )
    if schema_errors or semantic_errors:
        rendered = [issue.render() for issue in schema_errors] + semantic_errors
        raise RuntimeError("refusing to write invalid probe evidence: " + "; ".join(rendered))
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        write_attested_report(temporary, report, secret)
        durable_replace(temporary, output)
    finally:
        durable_unlink(temporary, missing_ok=True)
    return signed


def main() -> int:
    args = parse_args()
    project = Path(args.project).resolve(strict=True)
    try:
        secret = report_attestation_secret(PROBE_REPORT_ID)
        producer_files = _producer_files()
        bindings = load_bindings(project, args.manifest)
        output = _output_path(project, args.output)
    except (KeyError, OSError, ValueError) as exc:
        print(f"RL probe preflight failed: {exc}", file=sys.stderr)
        return 1

    started = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    started_clock = time.monotonic()
    probes: list[dict[str, Any]] = []
    errors: list[str] = []
    runtime_record = {
        "isaac_lab_available": False,
        "isaac_lab_version": "",
        "physics_backend": "physx",
        "sim_dt": float(bindings["runtime_contract"]["sim_dt"]),
        "decimation": int(bindings["runtime_contract"]["decimation"]),
        "source_report_sha256": bindings["runtime_source_report_sha256"],
    }
    env = None
    launcher = None
    try:
        installed_version = importlib.metadata.version("isaaclab")
        runtime_record["isaac_lab_version"] = installed_version
        if installed_version != bindings["isaac_lab_version"]:
            raise RuntimeError(
                f"Isaac Lab {installed_version} is installed; contract requires {bindings['isaac_lab_version']}"
            )
        from isaaclab.app import AppLauncher

        launcher = AppLauncher(headless=True)
        import torch
        from isaaclab.envs import ManagerBasedRLEnv

        module = _load_env_package(project, bindings)
        cfg = module.AssetFactoryEnvCfg()
        cfg.seed = int(bindings["runtime_contract"]["seeds"][0])
        env = ManagerBasedRLEnv(cfg=cfg)
        observed_dt = float(env.physics_dt)
        observed_decimation = int(env.cfg.decimation)
        if not math.isclose(
            observed_dt,
            float(bindings["runtime_contract"]["sim_dt"]),
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise RuntimeError("instantiated physics timestep differs from the RL contract")
        if observed_decimation != int(bindings["runtime_contract"]["decimation"]):
            raise RuntimeError("instantiated decimation differs from the RL contract")
        runtime_record.update({"isaac_lab_available": True, "sim_dt": observed_dt, "decimation": observed_decimation})
        adapter = IsaacLabProbeEnvironment(env, torch, bindings)
        selected = set(args.probes)
        seed = int(bindings["runtime_contract"]["seeds"][0])
        parameters = bindings["probe_parameters"]
        oracle_result: dict[str, Any] | None = None
        for probe_id in PROBE_IDS:
            if probe_id not in selected:
                continue
            if probe_id == "smoke":
                probes.append(run_smoke(adapter, seed, parameters["smoke_steps"]))
            elif probe_id == "oracle":
                oracle_result = run_oracle(adapter, seed, parameters["oracle_phase_steps"])
                dominance_ok, dominance_reason = component_dominance(
                    oracle_result["metrics"]["oracle_component_means"], bindings["dominance_bound"]
                )
                oracle_result["metrics"]["component_dominance_status"] = "pass" if dominance_ok else "blocked"
                oracle_result["metrics"]["component_dominance_reason"] = dominance_reason
                if not dominance_ok:
                    oracle_result["status"] = "blocked"
                    oracle_result["reason"] = "; ".join(
                        part for part in (str(oracle_result.get("reason") or ""), dominance_reason) if part
                    )
                    errors.append(dominance_reason)
                probes.append(oracle_result)
            elif probe_id == "reset":
                probes.append(
                    run_reset_feasibility(
                        adapter,
                        parameters["reset_seeds"],
                        bindings["settle_steps"],
                        bindings["settled_speed"],
                        parameters["penetration_tolerance_m"],
                    )
                )
            elif probe_id == "repeat":
                probes.append(
                    run_repeatability(
                        adapter,
                        seed,
                        parameters["repeat_steps"],
                        bindings["repeatability_tolerance"],
                    )
                )
            elif probe_id == "gaming":
                if oracle_result is None:
                    oracle_result = run_oracle(adapter, seed, parameters["oracle_phase_steps"])
                probes.append(
                    run_gaming_probes(
                        adapter,
                        seed,
                        parameters["gaming_steps"],
                        list(oracle_result["metrics"]["oracle_returns"]),
                        parameters["allowed_gaming_fraction"],
                    )
                )
    except Exception as exc:  # noqa: BLE001 - a signed blocked report retains the runtime failure
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        if env is not None:
            try:
                env.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"environment close failed: {exc}")
        if launcher is not None:
            try:
                launcher.app.close()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Isaac application close failed: {exc}")

    try:
        with workspace_lease(project, "rl-probe-producer"):
            output = _output_path(project, args.output)
            try:
                if load_bindings(project, args.manifest) != bindings:
                    errors.append("project bindings changed during probe execution")
            except Exception as exc:  # noqa: BLE001 - drift must produce signed blocked evidence
                errors.append(f"project binding revalidation failed: {type(exc).__name__}: {exc}")
            try:
                if _producer_files() != producer_files:
                    errors.append("probe producer files changed during execution")
            except Exception as exc:  # noqa: BLE001 - drift must produce signed blocked evidence
                errors.append(f"producer revalidation failed: {type(exc).__name__}: {exc}")
            execution = {
                "producer_id": "asset-factory.rl-probe",
                "producer_version": "1.0",
                "producer_sha256": producer_bundle_sha256(producer_files),
                "producer_files": producer_files,
                "started_at": started,
                "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "python_version": platform.python_version(),
                "platform": platform.platform(),
                "architecture": platform.machine(),
                "wall_seconds": time.monotonic() - started_clock,
            }
            report = assemble_probe_report(
                bindings={
                    "physics_backend": "physx",
                    "usd_sha256": bindings["usd_sha256"],
                    "request_digest": bindings["request_digest"],
                    "manifest_sha256": bindings["manifest_sha256"],
                    "manifest_path": bindings["manifest_path"],
                    "usd_path": bindings["usd_path"],
                    "contract_sha256": bindings["contract_sha256"],
                    "package_dependency_fingerprint": bindings["package_dependency_fingerprint"],
                    "rendered_files": bindings["rendered_files"],
                },
                runtime=runtime_record,
                execution=execution,
                probes=probes,
                errors=errors,
            )
            signed = _write_report(output, report, secret)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0 if signed["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
