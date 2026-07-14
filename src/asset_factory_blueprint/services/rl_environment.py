"""Deterministic RL environment design for validated asset packages.

The service turns a validated asset package into an Isaac Lab environment contract
whose every term is traceable to upstream evidence. It reads manifests and reports,
never a runtime: probe evidence and collision fidelity arrive as
attested JSON reports produced elsewhere. Gates that lack their evidence are
``pending`` and the stage stays ``blocked``; a gate never passes by assumption.

Everything RL-specific is written under ``extensions.rl`` of the stage manifest,
mirroring the Isaac Lab manager-based environment configuration so that the record
can later be rendered to a ``ManagerBasedRLEnvCfg`` without interpretation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from asset_factory_blueprint.execution import WorkspaceBusyError, atomic_write_json, workspace_lease
from asset_factory_blueprint.manifests import validate_payload
from asset_factory_blueprint.rl_evidence import (
    FIDELITY_REPORT_ID,
    PROBE_REPORT_ID,
    environment_contract_sha256,
    environment_manifest_sha256,
    load_attested_report,
)
from asset_factory_blueprint.schemas.common import RunPlan, RunRequest
from asset_factory_blueprint.skills.base import ToolResult
from asset_factory_blueprint.utils.checksums import sha256_file, sha256_text
from asset_factory_blueprint.utils.package_fingerprint import package_inventory_fingerprint

_MANIFEST_ACCEPTED = frozenset({"validated", "released"})
_RECORD_ACCEPTED = frozenset({"validated", "released", "pass", "passed", "approved", "authored", "accepted"})
_DYNAMICS_OBSERVATIONS = frozenset({"joint_effort", "joint_torque", "force_torque", "contact", "contact_forces"})
_CAMERA_OBSERVATIONS = frozenset({"rgb", "depth", "camera", "segmentation", "thermal"})

BEHAVIOURS = ("pick",)
ADAPTATION_MODES = ("robust", "adaptive")
CURRICULUM_TIERS = ("none",)

GATE_IDS = (
    "rl-lineage",
    "rl-reconciliation",
    "rl-backend-binding",
    "rl-timestep-binding",
    "rl-embodiment",
    "rl-affordance-reachability",
    "rl-collision-fidelity",
    "rl-reset-feasibility",
    "rl-smoke",
    "rl-reward-probes",
    "rl-evidence-rule",
    "rl-randomisation-provenance",
    "rl-identifiability",
    "rl-evaluation-protocol",
    "rl-environment-card",
)

# Behaviour -> (human name, checks). Each check names the upstream record it needs.
EVIDENCE_MATRIX: dict[str, tuple[str, ...]] = {
    "pick": ("grasp_points", "mass", "friction"),
}

DEFAULT_EVALUATION = {
    "metrics": ["success_rate", "episodic_return", "episode_length"],
    "aggregation": "interquartile_mean_stratified_bootstrap_95",
    "minimum_seeds": 5,
}
ORACLE_PHASES = ("approach", "descend", "close", "lift")
SUPPORTED_ISAAC_LAB_VERSION = "2.3.1"
SUPPORTED_REWARDS = {
    "reach": {
        "functional_form": "1 - tanh(distance_to_grasp_frame / 0.1)",
        "shaping_class": "task",
    },
    "lift": {
        "functional_form": "clip(object_height - support_height, 0, 0.1) / 0.1",
        "shaping_class": "task",
    },
    "action_rate": {
        "functional_form": "-sum(square(action_t - action_t_minus_1))",
        "shaping_class": "penalty",
    },
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _status_of(record: Any) -> str:
    if not isinstance(record, dict):
        return ""
    for key in ("validation_status", "status", "review_status", "authoring_status"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _accepted(record: Any) -> bool:
    return _status_of(record) in _RECORD_ACCEPTED


def _accepted_manifest(record: Any) -> bool:
    return _status_of(record) in _MANIFEST_ACCEPTED


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _confined_regular_file(project_dir: Path, value: str, label: str) -> tuple[Path | None, str]:
    raw = Path(value)
    if not value or raw.is_absolute():
        return None, f"{label} must be a project-relative path"
    root = project_dir.resolve(strict=True)
    candidate = root / raw
    cursor = root
    for part in raw.parts:
        if part in {"", ".", ".."}:
            if part == "..":
                return None, f"{label} escapes the project workspace"
            continue
        cursor = cursor / part
        if cursor.is_symlink():
            return None, f"{label} must not traverse a symbolic link"
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None, f"{label} is missing or outside the project workspace"
    if not resolved.is_file():
        return None, f"{label} must identify a regular file"
    return resolved, ""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _argument_digest(command: str) -> str:
    return "sha256:" + _sha256_text(command)


def _interval(value: Any) -> list[float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(_finite(item) for item in value):
        low, high = float(value[0]), float(value[1])
        return [low, high] if low <= high else None
    if isinstance(value, dict):
        low, high = value.get("min"), value.get("max")
        if _finite(low) and _finite(high):
            low_number, high_number = float(low), float(high)
            return [low_number, high_number] if low_number <= high_number else None
    return None


def _intersect(a: list[float], b: list[float]) -> list[float] | None:
    low, high = max(a[0], b[0]), min(a[1], b[1])
    return [low, high] if low <= high else None


def _gate(gate_id: str, status: str, reasons: list[str] | None = None, evidence_ids: list[str] | None = None) -> dict:
    return {
        "gate_id": gate_id,
        "status": status,
        "reasons": list(reasons or []),
        "evidence_ids": list(evidence_ids or []),
    }


# ---------------------------------------------------------------------------
# upstream loading
# ---------------------------------------------------------------------------


def load_upstream(project_dir: Path, asset_validation: dict[str, Any] | None) -> dict[str, Any]:
    """Load every upstream record the lane consumes, with checksums."""

    manifests_dir = project_dir / "manifests"
    names = {
        "simready": "simready-asset-manifest.json",
        "physics": "physics-articulation-manifest.json",
        "material": "material-inference-manifest.json",
        "nonvisual": "nonvisual-material-manifest.json",
        "source": "source-asset-manifest.json",
        "segmentation": "segmentation-manifest.json",
        "layout": "asset-layout-manifest.json",
    }
    upstream: dict[str, Any] = {}
    for key, name in names.items():
        path = manifests_dir / name
        payload = _load_json(path) if path.exists() else None
        upstream[key] = {
            "path": path.relative_to(project_dir).as_posix() if path.exists() else "",
            "sha256": sha256_file(path) if path.exists() else "",
            "payload": payload,
        }
    runtime: dict[str, Any] = {"record": {}, "report": None, "path": "", "sha256": ""}
    conformance = (asset_validation or {}).get("simready_conformance") or {}
    record = conformance.get("runtime_validation") or {}
    runtime["record"] = record if isinstance(record, dict) else {}
    report_rel = str(runtime["record"].get("report_path") or "")
    report_path = project_dir / report_rel if report_rel else None
    if report_path is not None and report_path.exists():
        runtime["report"] = _load_json(report_path)
        runtime["path"] = report_rel
        runtime["sha256"] = sha256_file(report_path)
    upstream["runtime"] = runtime
    return upstream


def _rl_constraints(request: RunRequest) -> dict[str, Any]:
    constraints = request.constraints if isinstance(request.constraints, dict) else {}
    rl = constraints.get("rl")
    return dict(rl) if isinstance(rl, dict) else {}


# ---------------------------------------------------------------------------
# lineage and reconciliation
# ---------------------------------------------------------------------------


def check_lineage(upstream: dict[str, Any], asset_validation: dict[str, Any] | None) -> tuple[list[str], list[dict]]:
    """Every consumed manifest must exist, be accepted and match the recorded checksum."""

    reasons: list[str] = []
    lineage: list[dict[str, Any]] = []
    simready = upstream["simready"]["payload"]
    if not simready:
        reasons.append("simready-asset manifest is missing")
    elif not _accepted_manifest(simready):
        reasons.append(f"simready-asset manifest status is {_status_of(simready) or 'unknown'}")
    if (asset_validation or {}).get("status") != "validated":
        reasons.append("asset validation report is not validated")
    for key, label, id_field in (
        ("physics", "physics-articulation", "physics_articulation_manifest_id"),
        ("material", "material-inference", "material_manifest_id"),
    ):
        entry = upstream[key]
        if not entry["payload"]:
            reasons.append(f"{label} manifest is missing")
            continue
        if not _accepted_manifest(entry["payload"]):
            reasons.append(f"{label} manifest status is {_status_of(entry['payload']) or 'unknown'}")
        expected_id = (simready or {}).get(id_field)
        actual_id = entry["payload"].get("id")
        if not expected_id:
            reasons.append(f"simready-asset manifest does not bind {label} by id")
        elif not actual_id:
            reasons.append(f"{label} manifest has no id")
        elif expected_id != actual_id:
            reasons.append(f"{label} manifest id {actual_id} does not match the simready record {expected_id}")
        evidence = (simready or {}).get("evidence") or []
        declared = next(
            (item for item in evidence if isinstance(item, dict) and str(item.get("uri") or "") == entry["path"]),
            None,
        )
        expected_sha = str((declared or {}).get("checksum") or "").removeprefix("sha256:").lower()
        if not expected_sha:
            reasons.append(f"simready-asset manifest carries no checksum binding for {entry['path']}")
        elif expected_sha != entry["sha256"]:
            reasons.append(f"{label} manifest checksum differs from the simready lineage binding")
        lineage.append({"manifest": label, "id": actual_id, "path": entry["path"], "sha256": entry["sha256"]})
    if upstream["nonvisual"]["payload"]:
        lineage.append(
            {
                "manifest": "nonvisual-material",
                "id": upstream["nonvisual"]["payload"].get("id"),
                "path": upstream["nonvisual"]["path"],
                "sha256": upstream["nonvisual"]["sha256"],
            }
        )
    if simready:
        lineage.append(
            {
                "manifest": "simready-asset",
                "id": simready.get("id"),
                "path": upstream["simready"]["path"],
                "sha256": upstream["simready"]["sha256"],
            }
        )
    return reasons, lineage


def _grasp_points(physics: dict[str, Any] | None) -> list[dict[str, Any]]:
    affordances = (physics or {}).get("affordances") or {}
    points = affordances.get("grasp_points") or []
    return [point for point in points if isinstance(point, dict)]


def _accepted_grasp_points(physics: dict[str, Any] | None, threshold: float) -> list[dict[str, Any]]:
    return [
        point
        for point in _grasp_points(physics)
        if _accepted(point)
        and _finite(point.get("confidence"))
        and float(point["confidence"]) >= threshold
        and point.get("frame") is not None
    ]


def _contract_grasp_frames(physics: dict[str, Any] | None, threshold: float) -> tuple[list[dict[str, Any]], list[str]]:
    """Return the accepted asset-local grasp frames used by rendering and probes."""

    frames: list[dict[str, Any]] = []
    reasons: list[str] = []
    seen: set[str] = set()
    for index, point in enumerate(_accepted_grasp_points(physics, threshold)):
        grasp_id = str(point.get("grasp_id") or "")
        frame = point.get("frame")
        approach = point.get("approach_vector")
        width = point.get("gripper_width")
        if not grasp_id or grasp_id in seen:
            reasons.append(f"accepted grasp point {index} needs a unique id")
            continue
        seen.add(grasp_id)
        if not isinstance(frame, (list, tuple)) or len(frame) != 7 or not all(_finite(value) for value in frame):
            reasons.append(f"accepted grasp point {grasp_id} needs a finite seven-value pose")
            continue
        if (
            not isinstance(approach, (list, tuple))
            or len(approach) != 3
            or not all(_finite(value) for value in approach)
        ):
            reasons.append(f"accepted grasp point {grasp_id} needs a finite three-value approach vector")
            continue
        approach_norm = math.sqrt(sum(float(value) ** 2 for value in approach))
        if not math.isclose(approach_norm, 1.0, rel_tol=1e-6, abs_tol=1e-6):
            reasons.append(f"accepted grasp point {grasp_id} approach vector must be unit length")
            continue
        if not _finite(width) or float(width) <= 0:
            reasons.append(f"accepted grasp point {grasp_id} needs a finite positive gripper width")
            continue
        frame_space = str(point.get("frame_space") or "")
        if frame_space != "asset":
            reasons.append(f"accepted grasp point {grasp_id} must declare frame_space asset")
            continue
        if point.get("quaternion_order") != "wxyz":
            reasons.append(f"accepted grasp point {grasp_id} must declare quaternion_order wxyz")
            continue
        quaternion_norm = math.sqrt(sum(float(value) ** 2 for value in frame[3:7]))
        if not math.isclose(quaternion_norm, 1.0, rel_tol=1e-6, abs_tol=1e-6):
            reasons.append(f"accepted grasp point {grasp_id} quaternion must be unit length")
            continue
        frames.append(
            {
                "id": grasp_id,
                "frame_space": frame_space,
                "frame": [float(value) for value in frame],
                "quaternion_order": "wxyz",
                "approach_vector": [float(value) for value in approach],
                "gripper_width": float(width),
                "confidence": float(point["confidence"]),
            }
        )
    return frames, reasons


def _mass_record(physics: dict[str, Any] | None) -> dict[str, Any] | None:
    for record in (physics or {}).get("mass_properties") or []:
        if isinstance(record, dict) and _finite(record.get("mass")) and _accepted(record):
            return record
    return None


def _friction_interval(
    physics: dict[str, Any] | None, material: dict[str, Any] | None
) -> tuple[list[float] | None, str]:
    for record in (physics or {}).get("physics_materials") or []:
        if not isinstance(record, dict) or not _accepted(record):
            continue
        static, dynamic = record.get("static_friction"), record.get("dynamic_friction")
        if _finite(static) and _finite(dynamic):
            return [
                min(float(static), float(dynamic)),
                max(float(static), float(dynamic)),
            ], "physics-articulation-manifest:physics_materials"
    for proposal in _physical_proposals(material):
        if (
            proposal["accepted"]
            and proposal["property"] in {"friction", "static_friction", "coefficient_of_friction"}
            and proposal["interval"]
        ):
            return proposal["interval"], f"material-inference-manifest:physical_property_proposals[{proposal['index']}]"
    return None, ""


def _physical_proposals(material: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalise physical property proposals into a uniform shape."""

    proposals: list[dict[str, Any]] = []
    for index, raw in enumerate((material or {}).get("physical_property_proposals") or []):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("property") or raw.get("name") or raw.get("property_id") or "").strip().lower()
        if not name:
            continue
        interval = _interval(raw.get("range")) or _interval({"min": raw.get("min"), "max": raw.get("max")})
        value = raw.get("value")
        if interval is None and _finite(value) and _finite(raw.get("uncertainty")):
            interval = [float(value) - float(raw["uncertainty"]), float(value) + float(raw["uncertainty"])]
        proposals.append(
            {
                "index": index,
                "property": name,
                "prim_path": raw.get("prim_path") or raw.get("component") or "",
                "value": float(value) if _finite(value) else None,
                "unit": raw.get("unit") or "",
                "interval": interval,
                "distribution": raw.get("distribution") or ("uniform" if interval else ""),
                "confidence": float(raw["confidence"]) if _finite(raw.get("confidence")) else None,
                "accepted": _accepted(raw),
                "status": _status_of(raw),
            }
        )
    return proposals


def reconcile_behaviour(
    behaviour: str,
    upstream: dict[str, Any],
    rl: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    """Check the requested behaviour against the evidence the asset carries."""

    reasons: list[str] = []
    physics = upstream["physics"]["payload"]
    material = upstream["material"]["payload"]
    findings: dict[str, Any] = {"behaviour": behaviour, "checks": []}
    if behaviour not in BEHAVIOURS:
        reasons.append(f"behaviour {behaviour!r} is unsupported; the implemented runtime contract is pick")
        return reasons, findings
    if behaviour in {"pick", "place"}:
        if any(_accepted(item) for item in (physics or {}).get("articulation_roots") or [] if isinstance(item, dict)):
            reasons.append("the implemented pick renderer supports rigid objects, not articulated object assets")
        success = rl.get("success") if isinstance(rl.get("success"), dict) else {}
        if success.get("type") != "object_height_above_support":
            reasons.append("pick and place require success.type object_height_above_support")
        if not _finite(success.get("height_m")) or float(success.get("height_m") or 0) <= 0:
            reasons.append("pick and place require a finite positive success.height_m")
        if not _finite(success.get("hold_seconds")) or float(success.get("hold_seconds") or 0) <= 0:
            reasons.append("pick and place require a finite positive success.hold_seconds")
        scene = rl.get("scene") if isinstance(rl.get("scene"), dict) else {}
        position = scene.get("object_initial_position")
        if (
            not isinstance(position, (list, tuple))
            or len(position) != 3
            or not all(_finite(value) for value in position)
        ):
            reasons.append("pick and place require scene.object_initial_position as three finite coordinates")
        if not _finite(scene.get("support_height_m")):
            reasons.append("pick and place require a finite scene.support_height_m")
        elif not math.isclose(float(scene["support_height_m"]), 0.0, rel_tol=0.0, abs_tol=1e-9):
            reasons.append("the implemented ground-supported pick scene requires scene.support_height_m to be zero")
        if (
            isinstance(position, (list, tuple))
            and len(position) == 3
            and all(_finite(value) for value in position)
            and _finite(scene.get("support_height_m"))
            and not math.isclose(float(position[2]), float(scene["support_height_m"]), rel_tol=0.0, abs_tol=1e-9)
        ):
            reasons.append(
                "scene.object_initial_position z must equal scene.support_height_m for a bottom-origin asset"
            )
        if not _finite(scene.get("env_spacing")) or float(scene.get("env_spacing") or 0) <= 0:
            reasons.append("scene.env_spacing must be a finite positive number")
        grasp_threshold = rl.get("grasp_confidence_threshold")
        _, grasp_reasons = _contract_grasp_frames(physics, float(grasp_threshold) if _finite(grasp_threshold) else 0.5)
        reasons.extend(grasp_reasons)
        fidelity = rl.get("fidelity") if isinstance(rl.get("fidelity"), dict) else {}
        for name in ("collision_shell_tolerance_m", "region_radius_m"):
            if not _finite(fidelity.get(name)) or float(fidelity.get(name) or 0) <= 0:
                reasons.append(f"fidelity.{name} must be a finite positive number")
        samples = fidelity.get("samples_per_region")
        seed = fidelity.get("seed")
        if not isinstance(samples, int) or isinstance(samples, bool) or samples < 32:
            reasons.append("fidelity.samples_per_region must be an integer of at least 32")
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            reasons.append("fidelity.seed must be a non-negative integer")
        probe_parameters = rl.get("probes") if isinstance(rl.get("probes"), dict) else {}
        for name in ("smoke_steps", "repeat_steps", "gaming_steps"):
            value = probe_parameters.get(name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                reasons.append(f"probes.{name} must be a positive integer")
        reset_seeds = probe_parameters.get("reset_seeds")
        if (
            not isinstance(reset_seeds, list)
            or not reset_seeds
            or any(not isinstance(seed, int) or isinstance(seed, bool) or seed < 0 for seed in reset_seeds)
            or len(reset_seeds) != len(set(reset_seeds))
        ):
            reasons.append("probes.reset_seeds must contain unique non-negative integers")
        for name in (
            "penetration_tolerance_m",
            "grasp_position_tolerance_m",
            "grasp_orientation_tolerance_rad",
        ):
            value = probe_parameters.get(name)
            if not _finite(value) or float(value) <= 0:
                reasons.append(f"probes.{name} must be a finite positive number")
        orientation_tolerance = probe_parameters.get("grasp_orientation_tolerance_rad")
        if _finite(orientation_tolerance) and float(orientation_tolerance) > math.pi:
            reasons.append("probes.grasp_orientation_tolerance_rad must not exceed pi")
        gaming_fraction = probe_parameters.get("allowed_gaming_fraction")
        if not _finite(gaming_fraction) or not 0 <= float(gaming_fraction) < 1:
            reasons.append("probes.allowed_gaming_fraction must be at least zero and less than one")
        oracle_steps = (
            probe_parameters.get("oracle_phase_steps")
            if isinstance(probe_parameters.get("oracle_phase_steps"), dict)
            else {}
        )
        if set(oracle_steps) != set(ORACLE_PHASES):
            reasons.append(f"probes.oracle_phase_steps must contain exactly {', '.join(ORACLE_PHASES)}")
        else:
            valid_oracle_horizon = True
            for phase in ORACLE_PHASES:
                value = oracle_steps.get(phase)
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    reasons.append(f"probes.oracle_phase_steps.{phase} must be a positive integer")
                    valid_oracle_horizon = False
            gaming_steps = probe_parameters.get("gaming_steps")
            if (
                valid_oracle_horizon
                and isinstance(gaming_steps, int)
                and not isinstance(gaming_steps, bool)
                and gaming_steps > 0
                and gaming_steps != sum(int(oracle_steps[phase]) for phase in ORACLE_PHASES)
            ):
                reasons.append("probes.gaming_steps must equal the complete scripted-oracle horizon")
    safety = rl.get("safety") if isinstance(rl.get("safety"), dict) else {}
    if not _finite(safety.get("contact_force_ceiling_n")) or float(safety.get("contact_force_ceiling_n") or 0) <= 0:
        reasons.append("safety.contact_force_ceiling_n must be a finite positive number")
    adaptation_mode = str(rl.get("adaptation_mode") or "")
    if adaptation_mode not in ADAPTATION_MODES:
        reasons.append(f"adaptation_mode must be one of {', '.join(ADAPTATION_MODES)}")
    threshold_raw = rl.get("grasp_confidence_threshold")
    if not _finite(threshold_raw) or not 0 <= float(threshold_raw) <= 1:
        reasons.append("grasp_confidence_threshold must be between zero and one")
        threshold = 0.5
    else:
        threshold = float(threshold_raw)
    resets = rl.get("resets") if isinstance(rl.get("resets"), dict) else {}
    robot_offset = _interval(resets.get("robot_joint_offset_rad"))
    if robot_offset is None:
        reasons.append("resets.robot_joint_offset_rad must be a finite interval")
    object_pose = resets.get("object_pose") if isinstance(resets.get("object_pose"), dict) else {}
    for axis in ("x_m", "y_m", "yaw_rad"):
        if _interval(object_pose.get(axis)) is None:
            reasons.append(f"resets.object_pose.{axis} must be a finite interval")
    curriculum = rl.get("curriculum") if isinstance(rl.get("curriculum"), dict) else {}
    tier = str(curriculum.get("tier") or "")
    if tier not in CURRICULUM_TIERS:
        reasons.append("curriculum.tier must be none; curricula are not implemented by the PhysX pick renderer")
    for check in EVIDENCE_MATRIX[behaviour]:
        ok, detail = True, ""
        if check == "grasp_points":
            points = _accepted_grasp_points(physics, threshold)
            ok = bool(points)
            detail = f"{len(points)} accepted grasp points at confidence >= {threshold}"
            if not ok:
                status = _status_of((physics or {}).get("affordances") or {})
                detail = f"physics-articulation-manifest:affordances.grasp_points has no accepted point (status {status or 'unknown'})"
        elif check == "mass":
            ok = _mass_record(physics) is not None
            detail = "physics-articulation-manifest:mass_properties" + (
                "" if ok else " has no accepted mass with a finite value"
            )
        elif check == "friction":
            interval, source = _friction_interval(physics, material)
            ok = interval is not None
            detail = source if ok else "no accepted friction record in physics_materials or physical_property_proposals"
        elif check == "contact_fidelity":
            ok = True
            detail = "collision fidelity is checked by the rl-collision-fidelity gate"
        elif check == "joints":
            joints = [
                j for j in (physics or {}).get("joints") or [] if isinstance(j, dict) and j.get("joint_type") != "fixed"
            ]
            limited = [
                j for j in joints if _finite(j.get("lower_limit")) and _finite(j.get("upper_limit")) and _accepted(j)
            ]
            ok = bool(limited)
            detail = (
                f"{len(limited)} accepted joints with finite limits"
                if ok
                else "physics-articulation-manifest:joints has no accepted joint with finite limits"
            )
        elif check == "drives":
            drives = [
                item for item in (physics or {}).get("drives") or [] if isinstance(item, dict) and _accepted(item)
            ]
            ok = bool(drives)
            detail = "physics-articulation-manifest:drives" + ("" if ok else " has no accepted drive")
        elif check == "articulation_root":
            roots = [r for r in (physics or {}).get("articulation_roots") or [] if isinstance(r, dict) and _accepted(r)]
            ok = bool(roots)
            detail = "physics-articulation-manifest:articulation_roots" + ("" if ok else " has no authored root")
        elif check == "moving_part_labels":
            labels = [str(x) for x in ((physics or {}).get("affordances") or {}).get("affordance_labels") or []]
            ok = any(label not in {"static_asset"} for label in labels)
            detail = f"affordance labels {labels}" if ok else "affordance_labels carries no moving-part label"
        elif check == "materials":
            components = [c for c in (material or {}).get("component_materials") or [] if isinstance(c, dict)]
            ok = any(c.get("selected_material") and _status_of(c) in _RECORD_ACCEPTED for c in components)
            detail = "material-inference-manifest:component_materials" + ("" if ok else " has no validated selection")
        elif check == "semantic_labels":
            segmentation = upstream["segmentation"]["payload"]
            ok = bool(segmentation) and _accepted_manifest(segmentation)
            detail = "segmentation-manifest" + ("" if ok else " is missing or not accepted")
        elif check == "sensor_contract":
            ok = bool(rl.get("sensor_contract"))
            detail = "constraints.rl.sensor_contract" + ("" if ok else " is empty")
        elif check == "layout":
            layout = upstream["layout"]["payload"]
            ok = bool(layout) and bool(layout.get("placements"))
            detail = "asset-layout-manifest" + ("" if ok else " is missing or has no placements")
        elif check == "collision_closure":
            simready = upstream["simready"]["payload"] or {}
            ok = bool(simready.get("usd_layer_stack"))
            detail = "simready-asset-manifest:usd_layer_stack" + ("" if ok else " is empty")
        findings["checks"].append({"check": check, "status": "pass" if ok else "blocked", "detail": detail})
        if not ok:
            reasons.append(f"{behaviour} requires {check}: {detail}")
    return reasons, findings


# ---------------------------------------------------------------------------
# runtime identity and embodiment
# ---------------------------------------------------------------------------


def bind_runtime(rl: dict[str, Any], runtime: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """Bind backend, timestep, episode timing and seeds to the validated runtime record."""

    backend_reasons: list[str] = []
    timestep_reasons: list[str] = []
    requested = dict(rl.get("runtime") or {})
    record, report = runtime.get("record") or {}, runtime.get("report") or {}
    identity = (report.get("runtime_identity") or {}) if isinstance(report, dict) else {}
    validated_backend = str(identity.get("physics_backend") or "").lower()
    validated_dt = report.get("physics_dt") if isinstance(report, dict) else None
    backend = str(requested.get("physics_backend") or "").lower()
    if not backend:
        backend_reasons.append("runtime physics_backend is required")
    isaac_lab_version = str(requested.get("isaac_lab_version") or "")
    if isaac_lab_version != SUPPORTED_ISAAC_LAB_VERSION:
        backend_reasons.append(
            f"runtime isaac_lab_version must be exactly {SUPPORTED_ISAAC_LAB_VERSION} for the implemented adapter"
        )
    if record.get("status") != "pass":
        backend_reasons.append(
            f"runtime validation status is {record.get('status') or 'missing'}; isaac-load evidence is required"
        )
    expected_report_sha = str(record.get("report_sha256") or "").removeprefix("sha256:").lower()
    if not expected_report_sha:
        backend_reasons.append("runtime validation record has no report_sha256 binding")
    elif expected_report_sha != str(runtime.get("sha256") or "").lower():
        backend_reasons.append("runtime evidence checksum differs from the runtime validation binding")
    if not validated_backend:
        backend_reasons.append("runtime evidence does not name a physics backend")
    elif validated_backend != "physx":
        backend_reasons.append(f"runtime backend {validated_backend} is unsupported; the implemented adapter is PhysX")
    elif backend != validated_backend:
        backend_reasons.append(f"requested backend {backend} differs from the validated backend {validated_backend}")
    validated_package_fingerprint = str(record.get("validated_package_dependency_fingerprint") or "")
    if not validated_package_fingerprint:
        backend_reasons.append("runtime validation record has no package dependency fingerprint")
    validated_usd_sha256 = str(record.get("validated_usd_sha256") or "")
    if not validated_usd_sha256:
        backend_reasons.append("runtime validation record has no validated USD SHA-256")
    sim_dt = requested.get("sim_dt")
    if sim_dt is None:
        sim_dt = validated_dt
    if not _finite(sim_dt):
        timestep_reasons.append("no sim_dt was requested and runtime evidence records no physics_dt")
    elif _finite(validated_dt) and not math.isclose(float(sim_dt), float(validated_dt), rel_tol=1e-6, abs_tol=1e-9):
        timestep_reasons.append(
            f"sim_dt {float(sim_dt):.6g} differs from the validated physics_dt {float(validated_dt):.6g}; import matching runtime evidence"
        )
    seeds_raw = requested.get("seeds")
    if (
        not isinstance(seeds_raw, list)
        or not seeds_raw
        or any(not isinstance(seed, int) or isinstance(seed, bool) or seed < 0 for seed in seeds_raw)
        or len(seeds_raw) != len(set(seeds_raw))
    ):
        timestep_reasons.append("runtime seeds must contain unique non-negative integers")
        seeds: list[int] = []
    else:
        seeds = list(seeds_raw)
    decimation_raw = requested.get("decimation")
    if not isinstance(decimation_raw, int) or isinstance(decimation_raw, bool) or decimation_raw < 1:
        timestep_reasons.append("runtime decimation must be a positive integer")
        decimation = 0
    else:
        decimation = decimation_raw
    episode_raw = requested.get("episode_length_s")
    if not _finite(episode_raw) or float(episode_raw) <= 0:
        timestep_reasons.append("runtime episode_length_s must be a finite positive number")
        episode_length = 0.0
    else:
        episode_length = float(episode_raw)
    num_envs_raw = requested.get("num_envs")
    if not isinstance(num_envs_raw, int) or isinstance(num_envs_raw, bool) or num_envs_raw < 1:
        timestep_reasons.append("runtime num_envs must be a positive integer")
        num_envs = 0
    else:
        num_envs = num_envs_raw
    render_interval_raw = requested.get("render_interval", decimation)
    if not isinstance(render_interval_raw, int) or isinstance(render_interval_raw, bool) or render_interval_raw < 1:
        timestep_reasons.append("runtime render_interval must be a positive integer when supplied")
        render_interval = 0
    else:
        render_interval = render_interval_raw
    bound = {
        "physics_backend": backend,
        "validated_physics_backend": validated_backend,
        "runtime_id": record.get("runtime_id") or identity.get("id") or "",
        "runtime_version": record.get("runtime_version") or identity.get("version") or "",
        "isaac_lab_version": isaac_lab_version,
        "sim_dt": float(sim_dt) if _finite(sim_dt) else None,
        "validated_physics_dt": float(validated_dt) if _finite(validated_dt) else None,
        "decimation": decimation,
        "control_dt": float(sim_dt) * decimation if _finite(sim_dt) and decimation > 0 else None,
        "episode_length_s": episode_length,
        "render_interval": render_interval,
        "num_envs": num_envs,
        "seeds": seeds,
        "settle_parameters": dict((report.get("validation_parameters") or {}) if isinstance(report, dict) else {}),
        "runtime_evidence": {"path": runtime.get("path", ""), "sha256": runtime.get("sha256", "")},
        "validated_package_dependency_fingerprint": validated_package_fingerprint,
        "validated_usd_sha256": validated_usd_sha256,
    }
    return bound, backend_reasons, timestep_reasons


def parse_urdf_embodiment(path: Path) -> dict[str, Any]:
    """Read joint limits and actuator ceilings from a URDF with the standard library."""

    tree = ET.parse(path)
    root = tree.getroot()
    link_names = [str(link.get("name") or "") for link in root.iter("link") if link.get("name")]
    joints: list[dict[str, Any]] = []
    for joint in root.iter("joint"):
        joint_type = joint.get("type", "")
        limit = joint.find("limit")
        parent = joint.find("parent")
        child = joint.find("child")
        axis = joint.find("axis")
        record: dict[str, Any] = {
            "name": joint.get("name", ""),
            "type": joint_type,
            "lower": None,
            "upper": None,
            "effort": None,
            "velocity": None,
            "parent_link": str(parent.get("link") or "") if parent is not None else "",
            "child_link": str(child.get("link") or "") if child is not None else "",
            "axis": [],
        }
        if axis is not None:
            raw_axis = str(axis.get("xyz") or "").split()
            if len(raw_axis) == 3:
                try:
                    record["axis"] = [float(value) for value in raw_axis]
                except ValueError:
                    pass
        if limit is not None:
            for key in ("lower", "upper", "effort", "velocity"):
                raw = limit.get(key)
                if raw is not None:
                    try:
                        record[key] = float(raw)
                    except ValueError:
                        pass
        if joint_type == "continuous":
            record["lower"], record["upper"] = -math.pi, math.pi
        joints.append(record)
    actuated = [j for j in joints if j["type"] in {"revolute", "prismatic", "continuous"}]
    return {
        "format": "urdf",
        "robot_name": root.get("name", ""),
        "joint_count": len(joints),
        "link_names": link_names,
        "actuated_joints": actuated,
        "effort_ceiling": max((j["effort"] for j in actuated if _finite(j["effort"])), default=None),
        "velocity_ceiling": max((j["velocity"] for j in actuated if _finite(j["velocity"])), default=None),
    }


def resolve_embodiment(
    project_dir: Path, request: RunRequest, rl: dict[str, Any], upstream: dict[str, Any]
) -> tuple[dict, list[str]]:
    """Find the robot description among the run request sources and read what it declares."""

    reasons: list[str] = []
    requested = rl.get("embodiment")
    if not isinstance(requested, dict):
        reasons.append("constraints.rl.embodiment must be an object")
        requested = {}
    requested_source = str(requested.get("source") or "")
    sources = [str(item) for item in request.sources]
    embodiment: dict[str, Any] = {
        "source": "",
        "project_path": "",
        "sha256": "",
        "format": "",
        "robot_name": "",
        "actuated_joints": [],
        "link_names": [],
        "end_effector_body": "",
        "gripper_joints": [],
        "action_scale": None,
        "maximum_gripper_aperture_m": None,
        "gripper_open_positions": {},
        "gripper_closed_positions": {},
        "fixed_base": None,
        "actuator": {},
        "effort_ceiling": None,
        "velocity_ceiling": None,
        "status": "blocked",
    }
    if not requested_source:
        reasons.append("constraints.rl.embodiment.source is required")
        return embodiment, reasons
    if requested_source not in sources:
        reasons.append(f"constraints.rl.embodiment names {requested_source}, which is not a run request source")
        return embodiment, reasons
    chosen = requested_source
    source_manifest = upstream["source"]["payload"] or {}
    matching_assets = [
        asset
        for asset in source_manifest.get("source_assets") or []
        if isinstance(asset, dict)
        and chosen in {str(asset.get("source_path") or ""), str(asset.get("source_uri") or "")}
    ]
    if len(matching_assets) != 1:
        reasons.append(f"robot description {chosen} must resolve to exactly one source-asset record")
        return embodiment, reasons
    source_record = matching_assets[0]
    if source_record.get("status") != "copied":
        reasons.append(f"robot description {chosen} source-asset status is not copied")
    materialised = str(source_record.get("project_copy_path") or "")
    local_path, local_error = _confined_regular_file(project_dir, materialised, "robot description")
    if local_path is None:
        reasons.append(local_error or f"robot description {chosen} has no local copy in the source-asset manifest")
        return embodiment, reasons
    expected_copy_sha256 = str(source_record.get("copy_sha256") or "").removeprefix("sha256:").lower()
    actual_copy_sha256 = sha256_file(local_path)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_copy_sha256):
        reasons.append(f"robot description {chosen} source-asset record has no valid copy SHA-256")
    elif expected_copy_sha256 != actual_copy_sha256:
        reasons.append(f"robot description {chosen} differs from its source-asset checksum")
    embodiment["source"] = chosen
    embodiment["project_path"] = local_path.resolve().relative_to(project_dir.resolve()).as_posix()
    embodiment["sha256"] = actual_copy_sha256
    if local_path.suffix.lower() == ".urdf":
        try:
            embodiment.update(parse_urdf_embodiment(local_path))
        except ET.ParseError as exc:
            reasons.append(f"robot description {chosen} is not well-formed XML: {exc}")
            return embodiment, reasons
        if not embodiment["actuated_joints"]:
            reasons.append(f"robot description {chosen} declares no actuated joints")
        else:
            actuated_records = [item for item in embodiment["actuated_joints"] if isinstance(item, dict)]
            actuated_names = [str(item.get("name") or "") for item in actuated_records]
            if not all(actuated_names) or len(actuated_names) != len(set(actuated_names)):
                reasons.append(f"robot description {chosen} needs unique non-empty actuated joint names")
            for joint in actuated_records:
                name = str(joint.get("name") or "<unnamed>")
                lower, upper = joint.get("lower"), joint.get("upper")
                if not _finite(lower) or not _finite(upper) or float(lower) > float(upper):
                    reasons.append(f"actuated joint {name} needs finite ordered position limits")
                for field in ("effort", "velocity"):
                    value = joint.get(field)
                    if not _finite(value) or float(value) <= 0:
                        reasons.append(f"actuated joint {name} needs a finite positive {field} limit")
    else:
        embodiment["format"] = local_path.suffix.lower().lstrip(".")
        reasons.append(f"robot description {chosen} is {embodiment['format']}; the embodiment parser accepts URDF only")
    requested_record = requested
    end_effector = str(requested_record.get("end_effector_body") or "")
    if not end_effector:
        reasons.append("constraints.rl.embodiment.end_effector_body is required")
    elif end_effector not in embodiment.get("link_names", []):
        reasons.append(f"end-effector body {end_effector} is not a link in {chosen}")
    embodiment["end_effector_body"] = end_effector

    gripper_joints = [str(item) for item in requested_record.get("gripper_joints") or []]
    joint_names = {str(item.get("name") or "") for item in embodiment.get("actuated_joints") or []}
    unknown_gripper = sorted(set(gripper_joints) - joint_names)
    if str(rl.get("behaviour") or "").lower() in {"pick", "place"} and not gripper_joints:
        reasons.append("pick and place embodiments require explicit gripper_joints")
    if unknown_gripper:
        reasons.append(f"gripper joints {unknown_gripper} are not actuated joints in {chosen}")
    embodiment["gripper_joints"] = gripper_joints
    gripper_records = [
        item for item in embodiment.get("actuated_joints") or [] if str(item.get("name") or "") in gripper_joints
    ]
    unsupported_gripper = [str(item.get("name") or "") for item in gripper_records if item.get("type") != "prismatic"]
    if unsupported_gripper:
        reasons.append(
            f"gripper joints {unsupported_gripper} are not prismatic; the implemented aperture contract supports prismatic fingers only"
        )
    arm_records = [
        item
        for item in embodiment.get("actuated_joints") or []
        if str(item.get("name") or "") not in set(gripper_joints)
    ]
    unsupported_arm = [
        str(item.get("name") or "") for item in arm_records if item.get("type") not in {"revolute", "continuous"}
    ]
    if unsupported_arm:
        reasons.append(
            f"arm joints {unsupported_arm} are not revolute or continuous; reset offsets use angular position units"
        )
    arm_joint_count = len(arm_records)
    if arm_joint_count < 6:
        reasons.append("the full-pose scripted oracle requires at least six non-gripper actuated joints")

    maximum_aperture = requested_record.get("maximum_gripper_aperture_m")
    oversized: list[str] = []
    if not _finite(maximum_aperture) or float(maximum_aperture) <= 0:
        reasons.append("constraints.rl.embodiment.maximum_gripper_aperture_m must be a finite positive number")
    else:
        embodiment["maximum_gripper_aperture_m"] = float(maximum_aperture)
        threshold_raw = rl.get("grasp_confidence_threshold", 0.5)
        threshold = float(threshold_raw) if _finite(threshold_raw) else 0.5
        grasp_frames, _ = _contract_grasp_frames(upstream["physics"]["payload"], threshold)
        oversized = [str(item["id"]) for item in grasp_frames if float(item["gripper_width"]) > float(maximum_aperture)]
    if oversized:
        reasons.append(f"accepted grasp frames {oversized} exceed maximum_gripper_aperture_m")

    gripper_by_name = {str(item.get("name") or ""): item for item in gripper_records}
    for field in ("gripper_open_positions", "gripper_closed_positions"):
        positions = requested_record.get(field) if isinstance(requested_record.get(field), dict) else {}
        if set(positions) != set(gripper_joints):
            reasons.append(f"constraints.rl.embodiment.{field} must name every gripper joint exactly once")
            continue
        valid_positions: dict[str, float] = {}
        for name, value in positions.items():
            joint = gripper_by_name.get(str(name), {})
            if not _finite(value):
                reasons.append(f"constraints.rl.embodiment.{field}.{name} must be finite")
                continue
            lower, upper = joint.get("lower"), joint.get("upper")
            if not _finite(lower) or not _finite(upper) or not float(lower) <= float(value) <= float(upper):
                reasons.append(f"constraints.rl.embodiment.{field}.{name} lies outside the URDF joint limits")
                continue
            valid_positions[str(name)] = float(value)
        embodiment[field] = valid_positions
    if (
        embodiment["gripper_open_positions"]
        and embodiment["gripper_open_positions"] == embodiment["gripper_closed_positions"]
    ):
        reasons.append("gripper open and closed position maps must differ")

    action_scale = requested_record.get("action_scale")
    if not _finite(action_scale) or float(action_scale) <= 0:
        reasons.append("constraints.rl.embodiment.action_scale must be a finite positive number")
    else:
        embodiment["action_scale"] = float(action_scale)

    fixed_base = requested_record.get("fixed_base")
    if not isinstance(fixed_base, bool):
        reasons.append("constraints.rl.embodiment.fixed_base must be explicit")
    elif fixed_base is not True:
        reasons.append("the implemented pick adapter requires a fixed-base embodiment")
    else:
        embodiment["fixed_base"] = fixed_base

    actuator = requested_record.get("actuator") if isinstance(requested_record.get("actuator"), dict) else {}
    stiffness, damping = actuator.get("stiffness"), actuator.get("damping")
    if not _finite(stiffness) or float(stiffness) < 0 or not _finite(damping) or float(damping) < 0:
        reasons.append("constraints.rl.embodiment.actuator needs finite non-negative stiffness and damping")
    else:
        embodiment["actuator"] = {"stiffness": float(stiffness), "damping": float(damping)}
    embodiment["status"] = "validated" if not reasons else "blocked"
    return embodiment, reasons


# ---------------------------------------------------------------------------
# context prior, observations, actions, resets, terminations
# ---------------------------------------------------------------------------


def build_context_prior(upstream: dict[str, Any]) -> list[dict[str, Any]]:
    """Every recorded range, distribution and uncertainty becomes a context prior entry."""

    physics = upstream["physics"]["payload"] or {}
    material = upstream["material"]["payload"] or {}
    physics_ref = {"manifest": "physics-articulation-manifest", "sha256": upstream["physics"]["sha256"]}
    material_ref = {"manifest": "material-inference-manifest", "sha256": upstream["material"]["sha256"]}
    prior: list[dict[str, Any]] = []
    for index, record in enumerate(physics.get("mass_properties") or []):
        if not isinstance(record, dict) or not _finite(record.get("mass")) or not _accepted(record):
            continue
        mass = float(record["mass"])
        uncertainty = record.get("uncertainty") if isinstance(record.get("uncertainty"), dict) else {}
        spread = uncertainty.get("mass") if isinstance(uncertainty, dict) else None
        interval = [mass - float(spread), mass + float(spread)] if _finite(spread) else None
        prior.append(
            {
                "parameter": "mass",
                "category": "dynamics",
                "prim_path": record.get("prim_path", ""),
                **physics_ref,
                "field": f"mass_properties[{index}].mass",
                "value": mass,
                "unit": "kg",
                "interval": interval,
                "distribution": "uniform" if interval else "",
                "confidence": float(record["confidence"]) if _finite(record.get("confidence")) else None,
            }
        )
    for index, record in enumerate(physics.get("physics_materials") or []):
        if not isinstance(record, dict) or not _accepted(record):
            continue
        static, dynamic = record.get("static_friction"), record.get("dynamic_friction")
        if _finite(static) and _finite(dynamic):
            prior.append(
                {
                    "parameter": "friction",
                    "category": "dynamics",
                    "prim_path": record.get("prim_path", ""),
                    **physics_ref,
                    "field": f"physics_materials[{index}]",
                    "value": (float(static) + float(dynamic)) / 2.0,
                    "unit": "dimensionless",
                    "interval": [min(float(static), float(dynamic)), max(float(static), float(dynamic))],
                    "distribution": "uniform",
                    "confidence": None,
                }
            )
    for proposal in _physical_proposals(material):
        if not proposal["accepted"]:
            continue
        category = "visual" if proposal["property"] in {"albedo", "roughness", "metallic", "texture"} else "dynamics"
        prior.append(
            {
                "parameter": proposal["property"],
                "category": category,
                "prim_path": proposal["prim_path"],
                **material_ref,
                "field": f"physical_property_proposals[{proposal['index']}]",
                "value": proposal["value"],
                "unit": proposal["unit"],
                "interval": proposal["interval"],
                "distribution": proposal["distribution"],
                "confidence": proposal["confidence"],
            }
        )
    for index, joint in enumerate(physics.get("drives") or []):
        if not isinstance(joint, dict):
            continue
        for key in ("damping", "stiffness"):
            value = joint.get(key)
            if _finite(value):
                prior.append(
                    {
                        "parameter": f"drive_{key}",
                        "category": "dynamics",
                        "prim_path": joint.get("joint_name", ""),
                        **physics_ref,
                        "field": f"drives[{index}].{key}",
                        "value": float(value),
                        "unit": "",
                        "interval": None,
                        "distribution": "",
                        "confidence": None,
                    }
                )
    return prior


def build_observations(
    rl: dict[str, Any], upstream: dict[str, Any], embodiment: dict[str, Any]
) -> tuple[dict, list[dict], list[str]]:
    """Policy and critic groups plus the sensor contract, with evidence checks."""

    reasons: list[str] = []
    requested = rl.get("observations") if isinstance(rl.get("observations"), dict) else {}
    sensor_contract_in = rl.get("sensor_contract") if isinstance(rl.get("sensor_contract"), list) else []
    nonvisual = upstream["nonvisual"]["payload"] or {}
    nonvisual_evidence = {
        str(item.get("evidence_id")) for item in nonvisual.get("evidence") or [] if isinstance(item, dict)
    }
    all_evidence = {
        str(item.get("evidence_id"))
        for entry in upstream.values()
        if isinstance(entry, dict) and isinstance(entry.get("payload"), dict)
        for item in entry["payload"].get("evidence") or []
        if isinstance(item, dict) and item.get("evidence_id")
    }
    sensor_contract: list[dict[str, Any]] = []
    policy_terms: list[dict[str, Any]] = [
        {"term": "joint_pos", "source": "proprioception", "noise": None, "rate_hz": None},
        {"term": "joint_vel", "source": "proprioception", "noise": None, "rate_hz": None},
    ]
    if embodiment.get("effort_ceiling") is not None:
        policy_terms.append({"term": "joint_effort", "source": "proprioception", "noise": None, "rate_hz": None})
    for channel in sensor_contract_in:
        if not isinstance(channel, dict) or not channel.get("channel"):
            reasons.append("sensor contract entries need a channel name")
            continue
        name = str(channel["channel"]).lower()
        entry = {
            "channel": name,
            "sensor_model": str(channel.get("sensor_model") or ""),
            "noise_model": channel.get("noise_model") if isinstance(channel.get("noise_model"), dict) else {},
            "update_rate_hz": channel.get("update_rate_hz"),
            "evidence_ids": [str(x) for x in channel.get("evidence_ids") or []],
            "calibration": channel.get("calibration") if isinstance(channel.get("calibration"), dict) else {},
            "status": "validated",
        }
        if not entry["evidence_ids"]:
            entry["status"] = "blocked"
            reasons.append(f"sensor channel {name} cites no calibration or characterisation evidence")
        unknown_evidence = sorted(set(entry["evidence_ids"]) - all_evidence)
        if unknown_evidence:
            entry["status"] = "blocked"
            reasons.append(f"sensor channel {name} cites unknown evidence {unknown_evidence}")
        if name in _CAMERA_OBSERVATIONS and not entry["calibration"]:
            entry["status"] = "blocked"
            reasons.append(f"sensor channel {name} declares no camera calibration")
        if not _finite(entry["update_rate_hz"]) or float(entry["update_rate_hz"]) <= 0:
            entry["status"] = "blocked"
            reasons.append(f"sensor channel {name} needs a finite positive update_rate_hz")
        if name in {"thermal", "acoustic", "electrical"}:
            if not nonvisual or not _accepted_manifest(nonvisual):
                entry["status"] = "blocked"
                reasons.append(f"sensor channel {name} needs a validated nonvisual-material manifest")
            elif not set(entry["evidence_ids"]) & nonvisual_evidence:
                entry["status"] = "blocked"
                reasons.append(f"sensor channel {name} cites no evidence recorded in the nonvisual-material manifest")
        if not entry["sensor_model"]:
            entry["status"] = "blocked"
            reasons.append(f"sensor channel {name} declares no sensor model")
        sensor_contract.append(entry)
        if entry["status"] == "validated":
            policy_terms.append(
                {"term": name, "source": "sensor", "noise": entry["noise_model"], "rate_hz": entry["update_rate_hz"]}
            )
    for term in requested.get("policy") or []:
        if isinstance(term, dict) and term.get("term"):
            name = str(term["term"])
            source = str(term.get("source") or "")
            manifest_path = str(term.get("manifest_path") or "")
            resolved, status = resolve_manifest_path(manifest_path, upstream) if manifest_path else (None, "")
            if source == "sensor":
                valid_channels = {item["channel"] for item in sensor_contract if item["status"] == "validated"}
                if name not in valid_channels:
                    reasons.append(f"policy term {name} has no validated sensor-contract entry")
            elif source == "proprioception":
                if name not in {"joint_pos", "joint_vel", "joint_effort"}:
                    reasons.append(f"policy term {name} is not a supported proprioceptive term")
            elif source == "control":
                if name not in {"actions", "last_action"}:
                    reasons.append(f"policy term {name} is not a supported control observation")
            elif source == "simulator":
                if name != "object_position":
                    reasons.append(f"policy term {name} has no implemented simulator observation")
                if resolved is None:
                    reasons.append(
                        f"policy term {name} cites {manifest_path or 'no manifest path'}, which does not resolve"
                    )
                elif status not in _RECORD_ACCEPTED:
                    reasons.append(
                        f"policy term {name} depends on unvalidated evidence {manifest_path} ({status or 'missing status'})"
                    )
            else:
                reasons.append(f"policy term {name} has unsupported or missing source {source or '(empty)'}")
            policy_terms.append(
                {
                    "term": name,
                    "source": source,
                    "manifest_path": manifest_path,
                    "record_status": status,
                    "noise": term.get("noise"),
                    "rate_hz": term.get("rate_hz"),
                }
            )
    critic_terms: list[dict[str, Any]] = []
    for term in requested.get("critic") or []:
        if not isinstance(term, dict) or not term.get("term"):
            continue
        manifest_path = str(term.get("manifest_path") or "")
        resolved, status = resolve_manifest_path(manifest_path, upstream)
        entry = {
            "term": str(term["term"]),
            "manifest_path": manifest_path,
            "resolved": resolved is not None,
            "record_status": status,
        }
        if entry["term"] not in {
            "joint_pos",
            "joint_vel",
            "joint_effort",
            "object_position",
            "object_velocity",
            "last_action",
        }:
            reasons.append(f"critic term {entry['term']} has no implemented observation")
        if resolved is None:
            reasons.append(
                f"critic term {term['term']} cites {manifest_path or 'no manifest path'}, which does not resolve"
            )
        elif status not in _RECORD_ACCEPTED:
            reasons.append(
                f"critic term {term['term']} depends on unvalidated evidence {manifest_path} ({status or 'missing status'})"
            )
        critic_terms.append(entry)
    deduplicated_policy: list[dict[str, Any]] = []
    seen_policy: set[str] = set()
    for term in policy_terms:
        if term["term"] not in seen_policy:
            seen_policy.add(term["term"])
            deduplicated_policy.append(term)
    observations = {"policy": deduplicated_policy, "critic": critic_terms}
    return observations, sensor_contract, reasons


def resolve_manifest_path(reference: str, upstream: dict[str, Any]) -> tuple[Any, str]:
    """Resolve ``manifest-name:dotted.path[index]`` against the loaded manifests.

    Returns the value and the status of the nearest enclosing record that carries one.
    """

    if ":" not in reference:
        return None, ""
    name, _, path = reference.partition(":")
    key = {
        "physics-articulation-manifest": "physics",
        "material-inference-manifest": "material",
        "nonvisual-material-manifest": "nonvisual",
        "simready-asset-manifest": "simready",
        "segmentation-manifest": "segmentation",
        "asset-layout-manifest": "layout",
    }.get(name)
    if key is None:
        return None, ""
    node: Any = upstream[key]["payload"]
    if node is None:
        return None, ""
    status = _status_of(node)
    for part in [p for p in path.replace("]", "").replace("[", ".").split(".") if p]:
        if isinstance(node, dict):
            if part not in node:
                return None, ""
            node = node[part]
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None, ""
        else:
            return None, ""
        if _status_of(node):
            status = _status_of(node)
    return node, status


def build_actions(upstream: dict[str, Any], embodiment: dict[str, Any], behaviour: str) -> list[dict[str, Any]]:
    physics = upstream["physics"]["payload"] or {}
    actions: list[dict[str, Any]] = []
    actuated = [item for item in embodiment.get("actuated_joints") or [] if item.get("name")]
    gripper_names = set(embodiment.get("gripper_joints") or [])
    arm_joints = [item for item in actuated if item["name"] not in gripper_names]
    if arm_joints:
        actions.append(
            {
                "term": "arm_joint_position",
                "target": "embodiment",
                "joints": [j["name"] for j in arm_joints],
                "mode": "position",
                "scale": embodiment.get("action_scale"),
                "limits_source": f"embodiment:{embodiment.get('source', '')}",
            }
        )
    if gripper_names:
        actions.append(
            {
                "term": "gripper_joint_position",
                "target": "embodiment",
                "joints": [item["name"] for item in actuated if item["name"] in gripper_names],
                "mode": "binary_position",
                "open_command": dict(embodiment.get("gripper_open_positions") or {}),
                "close_command": dict(embodiment.get("gripper_closed_positions") or {}),
                "limits_source": f"embodiment:{embodiment.get('source', '')}",
            }
        )
    if behaviour in {"open", "close"}:
        joints = [j for j in physics.get("joints") or [] if isinstance(j, dict) and j.get("joint_type") != "fixed"]
        actions.append(
            {
                "term": "asset_joint_interaction",
                "target": "asset",
                "joints": [j.get("joint_name") for j in joints],
                "mode": "contact",
                "scale": 1.0,
                "limits_source": "physics-articulation-manifest:limits",
            }
        )
    return actions


def build_resets(
    upstream: dict[str, Any], rl: dict[str, Any], behaviour: str, runtime: dict[str, Any]
) -> list[dict[str, Any]]:
    physics = upstream["physics"]["payload"] or {}
    threshold_raw = rl.get("grasp_confidence_threshold", 0.5)
    threshold = float(threshold_raw) if _finite(threshold_raw) else 0.5
    settle = runtime.get("settle_parameters") or {}
    requested = rl.get("resets") if isinstance(rl.get("resets"), dict) else {}
    robot_offset = _interval(requested.get("robot_joint_offset_rad"))
    resets: list[dict[str, Any]] = [
        {
            "entity": "embodiment",
            "distribution": "uniform_offset_within_joint_limits",
            "bounds": {"position_offset_rad": robot_offset, "velocity": [0.0, 0.0]},
            "feasibility_evidence": "rl-probe-evidence:reset_feasibility",
        }
    ]
    if behaviour in {"pick", "place"}:
        points = _accepted_grasp_points(physics, threshold)
        pose = requested.get("object_pose") if isinstance(requested.get("object_pose"), dict) else {}
        resets.append(
            {
                "entity": "asset",
                "distribution": "pose_jitter_around_support",
                "bounds": {
                    "x_m": _interval(pose.get("x_m")),
                    "y_m": _interval(pose.get("y_m")),
                    "yaw_rad": _interval(pose.get("yaw_rad")),
                },
                "seed_from_grasp_points": [str(p.get("grasp_id") or index) for index, p in enumerate(points)],
                "feasibility_evidence": "rl-probe-evidence:reset_feasibility",
            }
        )
    if behaviour in {"open", "close"}:
        resets.append(
            {
                "entity": "asset_joints",
                "distribution": "uniform_within_limits",
                "bounds": "physics-articulation-manifest:limits",
                "feasibility_evidence": "rl-probe-evidence:reset_feasibility",
            }
        )
    resets.append(
        {
            "entity": "settle",
            "distribution": "n/a",
            "bounds": {
                "settle_steps": settle.get("settle_steps"),
                "settled_speed_metres_per_second": settle.get("settled_speed_metres_per_second"),
            },
            "feasibility_evidence": "isaac-runtime-evidence:validation_parameters",
        }
    )
    return resets


def build_terminations(behaviour: str, rl: dict[str, Any]) -> list[dict[str, Any]]:
    safety = rl.get("safety") if isinstance(rl.get("safety"), dict) else {}
    success = rl.get("success") if isinstance(rl.get("success"), dict) else {}
    terminations = [
        {"term": "time_out", "condition": "episode_length_s elapsed", "time_out": True},
        {
            "term": "success",
            "condition": str(rl.get("success_definition") or ""),
            "type": str(success.get("type") or ""),
            "height_m": success.get("height_m"),
            "hold_seconds": success.get("hold_seconds"),
            "time_out": False,
        },
        {
            "term": "illegal_contact",
            "condition": "contact force above the safety ceiling on the protected object",
            "force_ceiling_n": safety.get("contact_force_ceiling_n"),
            "time_out": False,
        },
        {
            "term": "joint_limit_violation",
            "condition": "embodiment joint outside its recorded limit",
            "time_out": False,
        },
    ]
    if safety.get("workspace_bounds"):
        terminations.append(
            {"term": "workspace_exit", "condition": "end effector outside workspace_bounds", "time_out": False}
        )
    return terminations


# ---------------------------------------------------------------------------
# rewards
# ---------------------------------------------------------------------------


def static_reward_probes(
    rewards: list[dict[str, Any]], upstream: dict[str, Any], dominance_bound: Any
) -> tuple[list[dict], list[str], list[str]]:
    """Units, shaping class, weights and the evidence rule. Dynamic probes come from the probe report."""

    audit_reasons: list[str] = []
    evidence_reasons: list[str] = []
    components: list[dict[str, Any]] = []
    if not rewards:
        audit_reasons.append("no reward components were declared")
    valid_dominance = _finite(dominance_bound) and 0 < float(dominance_bound) <= 1
    if not valid_dominance:
        audit_reasons.append("dominance_bound must be greater than zero and no greater than one")
    for index, raw in enumerate(rewards):
        if not isinstance(raw, dict) or not raw.get("component"):
            audit_reasons.append(f"rewards[{index}] needs a component name")
            continue
        name = str(raw["component"])
        units = str(raw.get("units") or "")
        shaping = str(raw.get("shaping_class") or "")
        weight = raw.get("weight")
        problems: list[str] = []
        if not units:
            problems.append("declares no units (use dimensionless when appropriate)")
        if not str(raw.get("functional_form") or "").strip():
            problems.append("declares no functional_form")
        if shaping not in {"task", "potential_based", "penalty", "other"}:
            problems.append("shaping_class must be task, potential_based, penalty or other")
        if not _finite(weight):
            problems.append("weight must be a finite number")
        if shaping == "potential_based" and not raw.get("potential"):
            problems.append("potential_based components record their potential function")
        implementation = SUPPORTED_REWARDS.get(name)
        if implementation is None:
            problems.append("has no implemented reward function")
        else:
            if str(raw.get("functional_form") or "") != implementation["functional_form"]:
                problems.append("functional_form does not match the implemented reward function")
            if shaping != implementation["shaping_class"]:
                problems.append(
                    f"shaping_class must be {implementation['shaping_class']} for the implemented {name} reward"
                )
        depends_on = [str(x) for x in raw.get("depends_on") or []]
        unresolved: list[str] = []
        unvalidated: list[str] = []
        for reference in depends_on:
            value, status = resolve_manifest_path(reference, upstream)
            if value is None:
                unresolved.append(reference)
            elif status and status not in _RECORD_ACCEPTED:
                unvalidated.append(f"{reference} ({status})")
        if unresolved:
            evidence_reasons.append(f"reward {name} depends on unresolved evidence {', '.join(unresolved)}")
        if unvalidated:
            evidence_reasons.append(f"reward {name} depends on unvalidated evidence {', '.join(unvalidated)}")
        for problem in problems:
            audit_reasons.append(f"reward {name} {problem}")
        components.append(
            {
                "component": name,
                "functional_form": str(raw.get("functional_form") or ""),
                "weight": float(weight) if _finite(weight) else None,
                "units": units,
                "shaping_class": shaping,
                "potential": raw.get("potential"),
                "depends_on_evidence": depends_on,
                "dominance_bound": float(dominance_bound) if valid_dominance else None,
                "static_probe_status": "pass"
                if valid_dominance and not problems and not unresolved and not unvalidated
                else "blocked",
                "probes_passed": ["component_audit"] if not problems else [],
            }
        )
    return components, audit_reasons, evidence_reasons


# ---------------------------------------------------------------------------
# randomisation
# ---------------------------------------------------------------------------


def derive_randomisation(
    context_prior: list[dict[str, Any]],
    rl: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Bind each supported axis to accepted upstream evidence."""

    config = rl.get("randomisation") if isinstance(rl.get("randomisation"), dict) else {}
    selected_value = config.get("axes")
    if not isinstance(selected_value, list):
        return [], [], ["randomisation.axes must be an array"]
    selected_raw = [str(item) for item in selected_value if str(item)]
    selected = {str(item) for item in selected_raw if str(item)}
    axes: list[dict[str, Any]] = []
    blocked: list[str] = []
    if len(selected_raw) != len(selected):
        blocked.append("randomisation.axes contains duplicate entries")
    unsupported = sorted(selected - {"mass", "friction"})
    blocked.extend(
        f"randomisation axis {parameter} is unsupported by the PhysX pick renderer" for parameter in unsupported
    )
    for parameter in sorted(selected - set(unsupported)):
        candidates = [
            entry
            for entry in context_prior
            if entry.get("parameter") == parameter and _interval(entry.get("interval")) is not None
        ]
        if not candidates:
            blocked.append(f"randomisation axis {parameter} has no accepted upstream evidence interval")
            continue
        if parameter == "mass" and len(candidates) != 1:
            blocked.append("mass randomisation requires exactly one accepted rigid-object mass interval")
            continue
        interval = _interval(candidates[0]["interval"])
        if interval is None:
            blocked.append(f"randomisation axis {parameter} has no finite interval")
            continue
        for candidate in candidates[1:]:
            candidate_interval = _interval(candidate["interval"])
            if candidate_interval is None:
                continue
            merged = _intersect(interval, candidate_interval)
            if merged is None:
                blocked.append(f"randomisation axis {parameter} has conflicting upstream evidence intervals")
                interval = None
                break
            interval = merged
        if interval is None:
            continue
        if interval[0] < 0 or (parameter == "mass" and interval[0] <= 0):
            blocked.append(f"randomisation axis {parameter} has a non-physical evidence interval {interval}")
            continue
        axis: dict[str, Any] = {
            "axis": parameter,
            "category": "dynamics",
            "prim_path": candidates[0].get("prim_path", ""),
            "interval": interval,
            "distribution": "uniform",
            "provenance": "evidence",
            "sources": [
                {
                    "kind": "evidence",
                    "interval": list(candidate["interval"]),
                    "manifest": candidate["manifest"],
                    "field": candidate["field"],
                    "sha256": candidate["sha256"],
                }
                for candidate in candidates
            ],
            "review_status": "validated",
            "notes": [],
        }
        axes.append(axis)
    return axes, [], blocked


def check_affordance_invariance(rl: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Reject variant switching until the renderer and probes implement it."""

    config = rl.get("randomisation") if isinstance(rl.get("randomisation"), dict) else {}
    variants = config.get("variants")
    if not isinstance(variants, list):
        return [], ["randomisation.variants must be an empty array"]
    if variants:
        return [], ["variant switching is not implemented by the PhysX pick renderer"]
    return [], []


def identifiability_audit(
    axes: list[dict[str, Any]],
    observations: dict[str, Any],
    behaviour: str,
    mode: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Which randomised parameters can the policy tell apart from what it observes?"""

    policy_terms = {str(t.get("term")).lower() for t in observations.get("policy") or []}
    has_dynamics_sensing = bool(policy_terms & _DYNAMICS_OBSERVATIONS)
    has_camera = bool(policy_terms & _CAMERA_OBSERVATIONS)
    interactive = behaviour in {"pick", "place", "push", "slide", "open", "close"}
    audit: list[dict[str, Any]] = []
    reasons: list[str] = []
    for axis in axes:
        category = axis.get("category", "dynamics")
        if category == "visual":
            identifiable, observable_from = has_camera, sorted(policy_terms & _CAMERA_OBSERVATIONS)
        else:
            identifiable = has_dynamics_sensing and interactive
            observable_from = sorted(policy_terms & _DYNAMICS_OBSERVATIONS) if interactive else []
        handling = "adaptive" if (mode == "adaptive" and identifiable) else "robust"
        audit.append(
            {
                "parameter": axis["axis"],
                "category": category,
                "identifiable": identifiable,
                "observable_from": observable_from,
                "handling": handling,
            }
        )
        if mode == "adaptive" and not identifiable:
            reasons.append(
                f"parameter {axis['axis']} is randomised but not identifiable from the policy observations; handled robustly"
            )
    return audit, reasons


# ---------------------------------------------------------------------------
# evaluation, protocol, contracts, card
# ---------------------------------------------------------------------------


def build_evaluation_protocol(rl: dict[str, Any], variants: list[dict[str, Any]]) -> tuple[dict, list[str]]:
    requested = rl.get("evaluation") if isinstance(rl.get("evaluation"), dict) else {}
    seeds_raw = requested.get("seeds")
    reasons: list[str] = []
    if not isinstance(seeds_raw, list) or any(
        not isinstance(seed, int) or isinstance(seed, bool) or seed < 0 for seed in seeds_raw
    ):
        reasons.append("evaluation seeds must be an array of non-negative integers")
        seeds: list[int] = []
    else:
        seeds = list(seeds_raw)
    held_out = [str(v) for v in requested.get("held_out_variants") or []]
    proxy = [str(v) for v in requested.get("proxy_audit_variants") or []]
    admitted = {v["variant_id"] for v in variants if v.get("affordance_invariant")}
    if len(set(seeds)) < DEFAULT_EVALUATION["minimum_seeds"]:
        reasons.append(
            f"evaluation declares {len(set(seeds))} unique seeds; at least {DEFAULT_EVALUATION['minimum_seeds']} are required"
        )
    if len(seeds) != len(set(seeds)):
        reasons.append("evaluation seeds must be unique")
    if variants and not held_out:
        reasons.append("variants are declared but no held-out variant is reserved for evaluation")
    if variants and not proxy:
        reasons.append("variants are declared but no held-out proxy-audit variant is reserved")
    unknown = sorted((set(held_out) | set(proxy)) - admitted)
    if unknown:
        reasons.append(f"evaluation variants {unknown} are not admitted affordance-invariant variants")
    non_held_out_proxy = sorted(set(proxy) - set(held_out))
    if non_held_out_proxy:
        reasons.append(f"proxy-audit variants {non_held_out_proxy} are not held out from training")
    protocol = {
        "seeds": seeds,
        "held_out_variants": held_out,
        "proxy_audit_variants": proxy,
        "metrics": list(requested.get("metrics") or DEFAULT_EVALUATION["metrics"]),
        "aggregation": str(requested.get("aggregation") or DEFAULT_EVALUATION["aggregation"]),
        "report_per_axis": True,
        "warnings": [],
    }
    return protocol, reasons


def build_task_fitness_protocol(
    request: RunRequest,
    plan: RunPlan,
    rl: dict[str, Any],
    behaviour: str,
    runtime: dict[str, Any],
) -> dict[str, Any]:
    """Build the review-controlled task-fitness protocol for this contract."""

    authority = str(rl.get("protocol_authority") or "")
    fidelity = rl.get("fidelity") if isinstance(rl.get("fidelity"), dict) else {}
    tolerance_raw = fidelity.get("collision_shell_tolerance_m")
    radius_raw = fidelity.get("region_radius_m")
    samples_raw = fidelity.get("samples_per_region")
    seed_raw = fidelity.get("seed")
    tolerance = float(tolerance_raw) if _finite(tolerance_raw) else 0.0
    radius = float(radius_raw) if _finite(radius_raw) else 0.0
    samples = samples_raw if isinstance(samples_raw, int) and not isinstance(samples_raw, bool) else 0
    fidelity_seed = seed_raw if isinstance(seed_raw, int) and not isinstance(seed_raw, bool) else 0
    repeat_tolerance_raw = (runtime.get("settle_parameters") or {}).get("repeatability_tolerance_metres")
    repeat_tol = float(repeat_tolerance_raw) if _finite(repeat_tolerance_raw) else 0.0
    requested_probes = rl.get("probes") if isinstance(rl.get("probes"), dict) else {}
    phase_steps = requested_probes.get("oracle_phase_steps")
    if not isinstance(phase_steps, dict):
        phase_steps = {}
    probe_parameters = {
        "smoke_steps": requested_probes.get("smoke_steps"),
        "repeat_steps": requested_probes.get("repeat_steps"),
        "gaming_steps": requested_probes.get("gaming_steps"),
        "reset_seeds": list(requested_probes.get("reset_seeds") or []),
        "penetration_tolerance_m": requested_probes.get("penetration_tolerance_m"),
        "allowed_gaming_fraction": requested_probes.get("allowed_gaming_fraction"),
        "grasp_position_tolerance_m": requested_probes.get("grasp_position_tolerance_m"),
        "grasp_orientation_tolerance_rad": requested_probes.get("grasp_orientation_tolerance_rad"),
        "oracle_phase_steps": {phase: phase_steps.get(phase) for phase in ORACLE_PHASES},
    }
    tests = [
        {
            "test_id": "joint_task_fidelity",
            "scenario": f"{behaviour} task completes under the recorded joint limits and drives",
            "metrics": [
                {
                    "metric_id": "oracle_success_rate",
                    "unit": "ratio",
                    "expected_min": 1.0,
                    "expected_max": 1.0,
                    "tolerance": 0.0,
                },
                {
                    "metric_id": "limit_violations",
                    "unit": "count",
                    "expected_min": 0,
                    "expected_max": 0,
                    "tolerance": 0,
                },
            ],
        },
        {
            "test_id": "affordance_reachability",
            "scenario": "scripted oracle reaches every used grasp frame along its approach vector within joint limits",
            "metrics": [
                {
                    "metric_id": "reachable_grasp_fraction",
                    "unit": "ratio",
                    "expected_min": 1.0,
                    "expected_max": 1.0,
                    "tolerance": 0.0,
                },
            ],
        },
        {
            "test_id": "collision_fidelity",
            "scenario": "visual-to-collision shell distance near each accepted grasp region",
            "metrics": [
                {
                    "metric_id": "shell_distance_p95_m",
                    "unit": "m",
                    "expected_min": 0.0,
                    "expected_max": tolerance,
                    "tolerance": 0.0,
                },
            ],
        },
        {
            "test_id": "reset_feasibility",
            "scenario": "sampled resets settle without interpenetration within settle_steps",
            "metrics": [
                {
                    "metric_id": "infeasible_reset_fraction",
                    "unit": "ratio",
                    "expected_min": 0.0,
                    "expected_max": 0.0,
                    "tolerance": 0.0,
                },
            ],
        },
        {
            "test_id": "rollout_repeatability",
            "scenario": "seeded rollouts repeat within the runtime repeatability tolerance",
            "metrics": [
                {
                    "metric_id": "max_trajectory_deviation_m",
                    "unit": "m",
                    "expected_min": 0.0,
                    "expected_max": repeat_tol,
                    "tolerance": 0.0,
                },
            ],
        },
    ]
    core = {
        "schema_version": "1.0.0",
        "protocol_version": "1.0.0",
        "status": "draft",
        "scope": "rigid_body_manipulation",
        "tests": tests,
        "extensions": {
            "rl": {
                "run_id": plan.run_id,
                "request_digest": plan.request_digest,
                "asset_id": request.id,
                "collision_fidelity": {
                    "tolerance_m": tolerance,
                    "region_radius_m": radius,
                    "samples_per_region": samples,
                    "seed": fidelity_seed,
                },
                "probe_parameters": probe_parameters,
            }
        },
    }
    if authority:
        core["authority"] = authority
    protocol_id = "rl-rigid-body-manipulation-" + _sha256_text(json.dumps(core, sort_keys=True))[:24]
    return {"protocol_id": protocol_id, **core}


def build_command_contracts(project_dir: Path, runtime: dict[str, Any], behaviour: str) -> dict[str, dict[str, Any]]:
    manifest = "manifests/rl-environment-manifest.json"
    commands = {
        "render": ["afb", "rl", "render-env-cfg", "--project", ".", "--manifest", manifest, "--output", "envs"],
        "probe": [
            "afb",
            "rl",
            "probe",
            "smoke",
            "oracle",
            "reset",
            "repeat",
            "gaming",
            "--project",
            ".",
            "--manifest",
            manifest,
            "--output",
            "reports/incoming/rl-probe-evidence.json",
        ],
        "fidelity": [
            "afb",
            "rl",
            "fidelity",
            "--project",
            ".",
            "--manifest",
            manifest,
            "--output",
            "reports/incoming/rl-collision-fidelity.json",
        ],
    }
    return {
        name: {
            "argv": argv,
            "working_directory": ".",
            "available": True,
            "argument_digest": _argument_digest(json.dumps(argv, separators=(",", ":"))),
        }
        for name, argv in commands.items()
    }


def render_environment_card(rl_block: dict[str, Any], request: RunRequest) -> str:
    runtime = rl_block["runtime"]
    lines = [
        f"# Environment card: {request.id}",
        "",
        f"Behaviour: {rl_block['task']['behaviour']}. Embodiment: {rl_block['task']['embodiment'].get('robot_name') or rl_block['task']['embodiment'].get('source') or 'unbound'}.",
        f"Status: {rl_block['status']}.",
        "",
        "## Runtime identity",
        "",
        f"- physics backend: {runtime.get('physics_backend')} (validated: {runtime.get('validated_physics_backend') or 'none'})",
        f"- sim_dt: {runtime.get('sim_dt')} (validated physics_dt: {runtime.get('validated_physics_dt')}), decimation {runtime.get('decimation')}, episode {runtime.get('episode_length_s')} s",
        f"- seeds: {runtime.get('seeds')}, environments: {runtime.get('num_envs')}",
        "",
        "## Intended use",
        "",
        f"Generation and runtime probing of the {rl_block['task']['behaviour']} environment for the asset package named in the lineage record, in the declared backend and timestep only.",
        "",
        "## Out of scope",
        "",
        "- any other physics backend or timestep without fresh runtime evidence",
        "- behaviours the asset carries no evidence for",
        "- variants that fail the affordance-invariance check",
        "",
        "## Randomisation coverage",
        "",
    ]
    for axis in rl_block["randomisation"]:
        lines.append(f"- {axis['axis']}: {axis['interval']} ({axis['provenance']}, {axis['review_status']})")
    if not rl_block["randomisation"]:
        lines.append("- none")
    lines += ["", "## Reward report", ""]
    for component in rl_block["rewards"]:
        lines.append(
            f"- {component['component']}: weight {component['weight']}, {component['units']}, {component['shaping_class']}, static probes {component['static_probe_status']}"
        )
    if not rl_block["rewards"]:
        lines.append("- no reward components declared")
    lines += ["", "## Gates", ""]
    for gate in rl_block["gates"]:
        suffix = "" if not gate["reasons"] else ": " + "; ".join(gate["reasons"])
        lines.append(f"- {gate['gate_id']}: {gate['status']}{suffix}")
    lines += ["", "## Evaluation protocol", ""]
    protocol = rl_block["evaluation_protocol"]
    lines.append(f"- seeds {protocol['seeds']}, aggregation {protocol['aggregation']}")
    lines.append(
        f"- held-out variants: {protocol['held_out_variants'] or 'none'}; proxy-audit variants: {protocol['proxy_audit_variants'] or 'none'}"
    )
    lines += ["", "## Known gaps", ""]
    for note in rl_block["known_gaps"]:
        lines.append(f"- {note}")
    if not rl_block["known_gaps"]:
        lines.append("- none recorded")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# probe and fidelity evidence
# ---------------------------------------------------------------------------


def _probe_result(report: dict[str, Any] | None, probe: str) -> dict[str, Any] | None:
    if not report:
        return None
    for item in report.get("probes") or []:
        if isinstance(item, dict) and item.get("probe") == probe:
            return item
    return None


def _evidence_gate(
    gate_id: str, report: dict[str, Any] | None, load_errors: list[str], probe: str, evidence_id: str, missing: str
) -> dict:
    if load_errors:
        return _gate(gate_id, "blocked", load_errors)
    result = _probe_result(report, probe)
    if result is None:
        return _gate(gate_id, "pending", [missing])
    status = str(result.get("status") or "")
    if status == "pass":
        return _gate(gate_id, "pass", [], [evidence_id])
    return _gate(
        gate_id,
        "blocked",
        [f"{probe} probe status is {status or 'unknown'}: {result.get('reason') or 'no reason recorded'}"],
        [evidence_id],
    )


def _reward_evidence_gate(
    report: dict[str, Any] | None,
    load_errors: list[str],
    evidence_id: str,
    audit_reasons: list[str],
) -> dict[str, Any]:
    reasons = list(audit_reasons)
    if load_errors:
        reasons.extend(load_errors)
        return _gate("rl-reward-probes", "blocked", reasons)
    if report is None:
        if reasons:
            return _gate("rl-reward-probes", "blocked", reasons)
        return _gate(
            "rl-reward-probes",
            "pending",
            ["random baseline, oracle reward, repeatability and specification-gaming probes have not been imported"],
        )
    if report.get("status") != "pass":
        reasons.append(f"probe report status is {report.get('status') or 'unknown'}")
        reasons.extend(str(item) for item in report.get("errors") or [])
    for probe in ("smoke", "oracle", "repeat", "gaming"):
        result = _probe_result(report, probe)
        if result is None:
            reasons.append(f"probe report has no {probe} result")
        elif result.get("status") != "pass":
            reasons.append(
                f"{probe} reward probe is {result.get('status') or 'unknown'}: {result.get('reason') or 'no reason recorded'}"
            )
    return _gate(
        "rl-reward-probes",
        "blocked" if reasons else "pass",
        reasons,
        [evidence_id] if report else [],
    )


def _fidelity_gate(
    report: dict[str, Any] | None,
    load_errors: list[str],
    protocol: dict[str, Any],
    grasp_frames: list[dict[str, Any]],
) -> dict[str, Any]:
    evidence_id = "rl_collision_fidelity"
    if load_errors:
        return _gate("rl-collision-fidelity", "blocked", load_errors)
    if report is None:
        return _gate("rl-collision-fidelity", "pending", ["collision fidelity evidence has not been imported"])
    reasons: list[str] = []
    protocol_extensions = protocol.get("extensions") if isinstance(protocol.get("extensions"), dict) else {}
    rl_extensions = protocol_extensions.get("rl") if isinstance(protocol_extensions.get("rl"), dict) else {}
    expected = (
        rl_extensions.get("collision_fidelity") if isinstance(rl_extensions.get("collision_fidelity"), dict) else {}
    )
    comparisons = (
        ("tolerance_m", report.get("tolerance_m"), expected.get("tolerance_m")),
        ("samples_per_region", report.get("samples_per_region"), expected.get("samples_per_region")),
        ("seed", report.get("seed"), expected.get("seed")),
    )
    for field, actual, wanted in comparisons:
        if actual != wanted:
            reasons.append(f"collision fidelity {field} {actual!r} does not match protocol value {wanted!r}")
    region_map = {
        str(item.get("id") or ""): item
        for item in report.get("regions") or []
        if isinstance(item, dict) and item.get("id")
    }
    expected_ids = {str(item["id"]) for item in grasp_frames}
    if set(region_map) != expected_ids:
        reasons.append(
            f"collision fidelity regions {sorted(region_map)} do not exactly cover accepted grasp regions {sorted(expected_ids)}"
        )
    radius = expected.get("region_radius_m")
    for region_id, item in region_map.items():
        if item.get("radius_m") != radius:
            reasons.append(f"collision fidelity region {region_id} radius does not match the protocol")
    if report.get("status") != "pass":
        reasons.append(f"collision fidelity status is {report.get('status') or 'unknown'}")
        reasons.extend(str(item) for item in report.get("errors") or [])
    return _gate(
        "rl-collision-fidelity",
        "blocked" if reasons else "pass",
        reasons,
        [evidence_id],
    )


def _live_report_bindings(
    project_dir: Path,
    manifest: dict[str, Any] | None,
    rl_core: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Recompute the mutable artefact bindings consumed by canonical RL reports."""

    bindings: dict[str, Any] = {}
    errors: list[str] = []
    if manifest is None:
        errors.append("current RL environment manifest is unavailable for rendered-source verification")
    else:
        try:
            from asset_factory_blueprint.rl_render import verify_rendered_package_sources

            bindings["rendered_files"] = verify_rendered_package_sources(project_dir, manifest)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            errors.append(f"current rendered environment package cannot be verified: {exc}")

    scene = rl_core.get("scene") if isinstance(rl_core.get("scene"), dict) else {}
    usd_path, usd_error = _confined_regular_file(
        project_dir,
        str(scene.get("composed_root") or ""),
        "current composed USD",
    )
    if usd_path is None:
        errors.append(usd_error)
    else:
        try:
            package = package_inventory_fingerprint(usd_path.parent)
        except (OSError, ValueError) as exc:
            errors.append(f"current composed USD package inventory cannot be recomputed: {exc}")
        else:
            if package.get("status") != "pass":
                reasons = "; ".join(str(item) for item in package.get("blocked_reasons") or [])
                errors.append(
                    "current composed USD package inventory cannot be recomputed" + (f": {reasons}" if reasons else "")
                )
            else:
                bindings["package_fingerprint"] = str(package["fingerprint"])

    runtime = rl_core.get("runtime") if isinstance(rl_core.get("runtime"), dict) else {}
    runtime_evidence = runtime.get("runtime_evidence") if isinstance(runtime.get("runtime_evidence"), dict) else {}
    runtime_path, runtime_error = _confined_regular_file(
        project_dir,
        str(runtime_evidence.get("path") or ""),
        "current Isaac runtime evidence",
    )
    if runtime_path is None:
        errors.append(runtime_error)
    else:
        bindings["runtime_source_report_sha256"] = sha256_file(runtime_path)
    return bindings, errors


def _apply_runtime_source_binding(
    report: dict[str, Any] | None,
    errors: list[str],
    expected_sha256: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Bind one loaded report to the current Isaac runtime evidence bytes."""

    if report is not None and expected_sha256 is not None:
        runtime = report.get("runtime") if isinstance(report.get("runtime"), dict) else {}
        if runtime.get("source_report_sha256") != expected_sha256:
            errors.append("rl evidence runtime source_report_sha256 does not match current Isaac runtime evidence")
    return (None if errors else report), list(dict.fromkeys(errors))


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def design_environment(
    project_dir: str | Path,
    request: RunRequest,
    plan: RunPlan,
    asset_package: dict[str, Any],
    asset_validation: dict[str, Any] | None,
) -> dict[str, Any]:
    """Design the evidence-bound PhysX pick environment contract."""

    root = Path(project_dir).resolve()
    current_request_digest = "sha256:" + sha256_text(request.model_dump_json())
    if plan.request_digest != current_request_digest:
        raise ValueError("run plan digest does not identify the persisted run request")
    if plan.request_id != request.id or plan.asset_id != request.id:
        raise ValueError("run plan identity does not match the persisted run request")
    reports_dir = root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    rl = _rl_constraints(request)
    upstream = load_upstream(root, asset_validation)
    gates: list[dict[str, Any]] = []
    known_gaps: list[str] = []

    lineage_reasons, lineage = check_lineage(upstream, asset_validation)
    gates.append(_gate("rl-lineage", "blocked" if lineage_reasons else "pass", lineage_reasons))

    behaviour = str(rl.get("behaviour") or "").lower()
    reconciliation_reasons, reconciliation = reconcile_behaviour(behaviour, upstream, rl)
    requested_scope = str(request.constraints.get("release_scope") or "")
    if requested_scope and requested_scope != "rigid_body_manipulation":
        reconciliation_reasons.append("the implemented RL pick lane requires release_scope rigid_body_manipulation")
    gates.append(_gate("rl-reconciliation", "blocked" if reconciliation_reasons else "pass", reconciliation_reasons))

    runtime, backend_reasons, timestep_reasons = bind_runtime(rl, upstream["runtime"])
    gates.append(_gate("rl-backend-binding", "blocked" if backend_reasons else "pass", backend_reasons))
    gates.append(_gate("rl-timestep-binding", "blocked" if timestep_reasons else "pass", timestep_reasons))

    embodiment, embodiment_reasons = resolve_embodiment(root, request, rl, upstream)
    gates.append(_gate("rl-embodiment", "blocked" if embodiment_reasons else "pass", embodiment_reasons))

    threshold_raw = rl.get("grasp_confidence_threshold", 0.5)
    threshold = float(threshold_raw) if _finite(threshold_raw) else 0.5
    grasp_frames, _ = _contract_grasp_frames(upstream["physics"]["payload"], threshold)
    context_prior = build_context_prior(upstream)
    observations, sensor_contract, observation_reasons = build_observations(rl, upstream, embodiment)
    if sensor_contract:
        observation_reasons.append("sensor_contract is unsupported by the current PhysX pick renderer")
    actions = build_actions(upstream, embodiment, behaviour)
    resets = build_resets(upstream, rl, behaviour, runtime)
    terminations = build_terminations(behaviour, rl)

    dominance = rl.get("dominance_bound")
    rewards, reward_audit_reasons, reward_evidence_reasons = static_reward_probes(
        list(rl.get("rewards") or []), upstream, dominance
    )

    posterior_path = str(rl.get("posterior_evidence") or "")
    posterior_reasons = (
        ["posterior randomisation evidence is unsupported until it has an attested importer"] if posterior_path else []
    )
    axes, randomisation_review, randomisation_blocked = derive_randomisation(context_prior, rl)
    variants, variant_rejections = check_affordance_invariance(rl)
    provenance_reasons = posterior_reasons + randomisation_blocked + variant_rejections
    gates.append(
        _gate(
            "rl-randomisation-provenance",
            "blocked" if provenance_reasons else ("review_required" if randomisation_review else "pass"),
            provenance_reasons + randomisation_review,
        )
    )

    mode = str(rl.get("adaptation_mode") or "").lower()
    identifiability, identifiability_reasons = identifiability_audit(axes, observations, behaviour, mode)
    gates.append(
        _gate(
            "rl-identifiability",
            "blocked" if mode == "adaptive" and identifiability_reasons else "pass",
            identifiability_reasons if mode == "adaptive" else [],
        )
    )
    if mode == "robust":
        known_gaps.extend(identifiability_reasons)

    evaluation_protocol, evaluation_reasons = build_evaluation_protocol(rl, variants)
    gates.append(_gate("rl-evaluation-protocol", "blocked" if evaluation_reasons else "pass", evaluation_reasons))

    protocol = build_task_fitness_protocol(request, plan, rl, behaviour, runtime)
    protocol_path = reports_dir / "rl-task-fitness-protocol.json"
    if protocol_path.is_file():
        existing_protocol = _load_json(protocol_path)
        if existing_protocol.get("status") == "approved":
            approval_fields = {"status", "authority", "approval_id", "approved_at"}
            candidate_core = {key: value for key, value in protocol.items() if key not in approval_fields}
            existing_core = {key: value for key, value in existing_protocol.items() if key not in approval_fields}
            schema_errors = validate_payload("task-fitness-protocol", existing_protocol)
            if schema_errors:
                rendered = "; ".join(issue.render() for issue in schema_errors)
                raise ValueError(f"approved RL task-fitness protocol is invalid: {rendered}")
            if existing_core != candidate_core:
                raise ValueError("approved RL task-fitness protocol differs from the current environment contract")
            protocol = existing_protocol
    protocol = json.loads(json.dumps(protocol, sort_keys=True))
    atomic_write_json(protocol_path, protocol)
    protocol_ref = {
        "protocol_id": protocol["protocol_id"],
        "path": protocol_path.relative_to(root).as_posix(),
        "sha256": sha256_file(protocol_path),
        "status": protocol["status"],
    }
    contracts = build_command_contracts(root, runtime, behaviour)
    scene_request = rl.get("scene") if isinstance(rl.get("scene"), dict) else {}
    success = rl.get("success") if isinstance(rl.get("success"), dict) else {}
    physics_payload = upstream["physics"]["payload"] or {}
    object_asset_type = (
        "articulation"
        if any(_accepted(item) for item in physics_payload.get("articulation_roots") or [] if isinstance(item, dict))
        else "rigid_object"
    )
    rl_core: dict[str, Any] = {
        "schema_version": "0.1.0",
        "lineage": lineage,
        "runtime": runtime,
        "task": {
            "behaviour": behaviour,
            "embodiment": embodiment,
            "success": {
                "type": str(success.get("type") or ""),
                "height_m": success.get("height_m"),
                "hold_seconds": success.get("hold_seconds"),
            },
            "success_definition": str(rl.get("success_definition") or ""),
            "grasp_frames": grasp_frames,
            "reconciliation": reconciliation,
        },
        "scene": {
            "composed_root": (upstream["simready"]["payload"] or {}).get("usd_root_path")
            or asset_package.get("usd_root_path", ""),
            "layout_manifest": upstream["layout"]["path"],
            "env_spacing": scene_request.get("env_spacing"),
            "object_initial_position": scene_request.get("object_initial_position"),
            "support_height_m": scene_request.get("support_height_m"),
            "object_asset_type": object_asset_type,
        },
        "context_prior": context_prior,
        "adaptation_mode": mode,
        "identifiability": identifiability,
        "observations": observations,
        "sensor_contract": sensor_contract,
        "actions": actions,
        "commands": [{"term": "pick_grasp", "grasp_ids": [frame["id"] for frame in grasp_frames]}],
        "rewards": rewards,
        "terminations": terminations,
        "resets": resets,
        "randomisation": axes,
        "variants": variants,
        "curriculum": _curriculum(rl),
        "safety": _safety(rl, embodiment),
        "evaluation_protocol": evaluation_protocol,
        "task_fitness_protocol": protocol_ref,
        "command_contracts": contracts,
    }
    contract_sha256 = environment_contract_sha256(rl_core)
    rl_core["contract_sha256"] = contract_sha256

    source_manifest = _load_json(root / "manifests" / "rl-environment-manifest.json")
    expected_manifest_sha256: str | None = None
    if source_manifest is not None:
        source_extensions = source_manifest.setdefault("extensions", {})
        source_extensions["rl"] = rl_core
        expected_manifest_sha256 = environment_manifest_sha256(source_manifest)

    validated_usd = runtime.get("validated_usd_sha256") or None
    recorded_package_fingerprint = runtime.get("validated_package_dependency_fingerprint") or None
    expected_backend = runtime.get("physics_backend") or None
    expected_dt = runtime.get("sim_dt") if _finite(runtime.get("sim_dt")) else None
    expected_decimation = runtime.get("decimation") if isinstance(runtime.get("decimation"), int) else None
    probe_path = root / "reports" / "rl-probe-evidence.json"
    fidelity_path = root / "reports" / "rl-collision-fidelity.json"
    canonical_reports_exist = probe_path.exists() or fidelity_path.exists()
    live_bindings: dict[str, Any] = {}
    live_binding_errors: list[str] = []
    if canonical_reports_exist:
        live_bindings, live_binding_errors = _live_report_bindings(root, source_manifest, rl_core)

    probe_report, probe_errors = load_attested_report(
        root,
        "reports/rl-probe-evidence.json",
        PROBE_REPORT_ID,
        expected_backend=expected_backend,
        expected_usd_sha256=validated_usd,
        expected_request_digest=plan.request_digest or None,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_contract_sha256=contract_sha256,
        expected_package_fingerprint=(
            live_bindings.get("package_fingerprint") if probe_path.exists() else recorded_package_fingerprint
        ),
        expected_sim_dt=expected_dt,
        expected_decimation=expected_decimation,
        expected_rendered_files=live_bindings.get("rendered_files") if probe_path.exists() else None,
        require_import_receipt=True,
    )
    if probe_path.exists():
        probe_errors.extend(live_binding_errors)
        probe_report, probe_errors = _apply_runtime_source_binding(
            probe_report,
            probe_errors,
            live_bindings.get("runtime_source_report_sha256"),
        )
    fidelity_report, fidelity_errors = load_attested_report(
        root,
        "reports/rl-collision-fidelity.json",
        FIDELITY_REPORT_ID,
        expected_backend=expected_backend,
        expected_usd_sha256=validated_usd,
        expected_request_digest=plan.request_digest or None,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_contract_sha256=contract_sha256,
        expected_package_fingerprint=(
            live_bindings.get("package_fingerprint") if fidelity_path.exists() else recorded_package_fingerprint
        ),
        expected_sim_dt=expected_dt,
        expected_decimation=expected_decimation,
        expected_rendered_files=live_bindings.get("rendered_files") if fidelity_path.exists() else None,
        require_import_receipt=True,
    )
    if fidelity_path.exists():
        fidelity_errors.extend(live_binding_errors)
        fidelity_report, fidelity_errors = _apply_runtime_source_binding(
            fidelity_report,
            fidelity_errors,
            live_bindings.get("runtime_source_report_sha256"),
        )
    probe_evidence_id = "rl_probe_evidence"
    gates.append(
        _evidence_gate(
            "rl-smoke",
            probe_report,
            probe_errors,
            "smoke",
            probe_evidence_id,
            "zero-action and random-action smoke rollouts have not been imported",
        )
    )
    gates.append(
        _evidence_gate(
            "rl-affordance-reachability",
            probe_report,
            probe_errors,
            "oracle",
            probe_evidence_id,
            "scripted-oracle reachability has not been imported",
        )
    )
    gates.append(
        _evidence_gate(
            "rl-reset-feasibility",
            probe_report,
            probe_errors,
            "reset",
            probe_evidence_id,
            "reset feasibility has not been imported",
        )
    )
    gates.append(_reward_evidence_gate(probe_report, probe_errors, probe_evidence_id, reward_audit_reasons))
    evidence_reasons = reward_evidence_reasons + observation_reasons
    gates.append(_gate("rl-evidence-rule", "blocked" if evidence_reasons else "pass", evidence_reasons))
    gates.append(_fidelity_gate(fidelity_report, fidelity_errors, protocol, grasp_frames))

    gates.append(_gate("rl-environment-card", "pass"))
    blocked = [reason for gate in gates if gate["status"] == "blocked" for reason in gate["reasons"]]
    pending = [reason for gate in gates if gate["status"] == "pending" for reason in gate["reasons"]]
    review = [reason for gate in gates if gate["status"] == "review_required" for reason in gate["reasons"]]
    status = "blocked" if blocked or pending else "review_required"
    rl_block: dict[str, Any] = {
        **rl_core,
        "status": status,
        "gates": gates,
        "blocked_reasons": blocked + pending,
        "review_reasons": review,
        "known_gaps": known_gaps,
    }
    card_text = render_environment_card(rl_block, request)
    card_path = reports_dir / "environment-card.md"
    card_path.write_text(card_text, encoding="utf-8")
    rl_block["environment_card"] = {"path": card_path.relative_to(root).as_posix(), "sha256": sha256_file(card_path)}
    report = {
        "asset_id": request.id,
        "run_id": plan.run_id,
        "status": status,
        "gates": gates,
        "blocked_reasons": blocked + pending,
        "review_reasons": review,
        "rl": rl_block,
    }
    report_path = reports_dir / "rl-environment-design-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")

    evidence = [
        {
            "evidence_id": "rl_environment_design_report",
            "kind": "rl_environment_design_report",
            "uri": report_path.relative_to(root).as_posix(),
            "checksum": sha256_file(report_path),
        },
        {
            "evidence_id": "rl_environment_card",
            "kind": "environment_card",
            "uri": rl_block["environment_card"]["path"],
            "checksum": rl_block["environment_card"]["sha256"],
        },
        {
            "evidence_id": "rl_task_fitness_protocol",
            "kind": "task_fitness_protocol",
            "uri": rl_block["task_fitness_protocol"]["path"],
            "checksum": rl_block["task_fitness_protocol"]["sha256"],
        },
    ]
    for item in lineage:
        if item.get("path") and item.get("sha256"):
            evidence.append(
                {
                    "evidence_id": f"lineage_{item['manifest']}",
                    "kind": "upstream_manifest",
                    "uri": item["path"],
                    "checksum": item["sha256"],
                }
            )
    if upstream["runtime"]["path"]:
        evidence.append(
            {
                "evidence_id": "isaac_runtime_evidence",
                "kind": "isaac_runtime_report",
                "uri": upstream["runtime"]["path"],
                "checksum": upstream["runtime"]["sha256"],
            }
        )
    if probe_report is not None and not probe_errors:
        evidence.append(
            {
                "evidence_id": probe_evidence_id,
                "kind": "rl_probe_evidence",
                "uri": "reports/rl-probe-evidence.json",
                "checksum": sha256_file(root / "reports" / "rl-probe-evidence.json"),
            }
        )
        evidence.append(
            {
                "evidence_id": "rl_probe_import_receipt",
                "kind": "rl_evidence_import_receipt",
                "uri": "reports/rl-probe-evidence.import.json",
                "checksum": sha256_file(root / "reports" / "rl-probe-evidence.import.json"),
            }
        )
    if fidelity_report is not None and not fidelity_errors:
        evidence.append(
            {
                "evidence_id": "rl_collision_fidelity",
                "kind": "rl_collision_fidelity",
                "uri": "reports/rl-collision-fidelity.json",
                "checksum": sha256_file(root / "reports" / "rl-collision-fidelity.json"),
            }
        )
        evidence.append(
            {
                "evidence_id": "rl_fidelity_import_receipt",
                "kind": "rl_evidence_import_receipt",
                "uri": "reports/rl-collision-fidelity.import.json",
                "checksum": sha256_file(root / "reports" / "rl-collision-fidelity.import.json"),
            }
        )
    return {
        "status": status,
        "blocked_reasons": blocked + pending,
        "review_reasons": review,
        "gates": gates,
        "report_path": report_path.relative_to(root).as_posix(),
        "report_sha256": sha256_file(report_path),
        "extensions_rl": rl_block,
        "evidence": evidence,
    }


def _curriculum(rl: dict[str, Any]) -> dict[str, Any]:
    requested = rl.get("curriculum") if isinstance(rl.get("curriculum"), dict) else {}
    tier = str(requested.get("tier") or "").lower()
    return {"tier": tier}


def _safety(rl: dict[str, Any], embodiment: dict[str, Any]) -> dict[str, Any]:
    requested = rl.get("safety") if isinstance(rl.get("safety"), dict) else {}
    hard_terminations = ["illegal_contact", "joint_limit_violation"]
    if requested.get("workspace_bounds"):
        hard_terminations.append("workspace_exit")
    return {
        "costs": list(requested.get("costs") or []),
        "hard_terminations": hard_terminations,
        "workspace_bounds": requested.get("workspace_bounds"),
        "actuator_ceilings": {
            "effort": embodiment.get("effort_ceiling"),
            "velocity": embodiment.get("velocity_ceiling"),
            "source": f"embodiment:{embodiment.get('source', '')}" if embodiment.get("source") else "",
        },
        "contact_force_ceiling_n": requested.get("contact_force_ceiling_n"),
    }


# ---------------------------------------------------------------------------
# tool surface
# ---------------------------------------------------------------------------


def _rl_route_locked(root: Path) -> ToolResult:
    try:
        request = RunRequest.model_validate_json((root / "run-request.json").read_text(encoding="utf-8"))
        plan = RunPlan.model_validate_json((root / "run-plan.json").read_text(encoding="utf-8"))
        stage_report = json.loads((root / "reports" / "simready-verification-report.json").read_text(encoding="utf-8"))
        validation_path = root / "reports" / "generated-asset-validation-report.json"
        asset_validation = json.loads(validation_path.read_text(encoding="utf-8"))
        asset_validation["report_sha256"] = sha256_file(validation_path)
    except (OSError, ValueError) as exc:
        return ToolResult(success=False, error=f"project workspace is incomplete: {exc}", validation_status="blocked")
    asset_package = stage_report.get("generated_asset") or {}
    if not asset_package:
        return ToolResult(
            success=False, error="simready-verification report carries no generated asset", validation_status="blocked"
        )
    result = design_environment(root, request, plan, asset_package, asset_validation)
    manifest_path = root / "manifests" / "rl-environment-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        merge_design_into_manifest(manifest, result)
        schema_errors = validate_payload("rl-environment-manifest", manifest)
        if schema_errors:
            rendered = "; ".join(issue.render() for issue in schema_errors)
            return ToolResult(
                success=False,
                error=f"refreshed RL environment manifest is invalid: {rendered}",
                validation_status="blocked",
            )
        atomic_write_json(manifest_path, manifest)
    except (OSError, ValueError) as exc:
        return ToolResult(
            success=False,
            error=f"RL environment manifest could not be refreshed: {exc}",
            validation_status="blocked",
        )
    summary = {key: value for key, value in result.items() if key != "extensions_rl"}
    return ToolResult(
        success=result["status"] != "blocked",
        data=summary,
        warnings=list(result.get("blocked_reasons", [])) + list(result.get("review_reasons", [])),
        artefacts=[result["report_path"], "manifests/rl-environment-manifest.json"]
        + [
            item["uri"]
            for item in result.get("evidence", [])
            if item["kind"] in {"environment_card", "task_fitness_protocol"}
        ],
        proposals=[
            {"kind": "reward_component", **component} for component in result["extensions_rl"].get("rewards", [])
        ],
        validation_status=result["status"],
    )


def rl_route(params: dict[str, Any]) -> ToolResult:
    """Design the environment contract for an existing project workspace.

    The project must already carry a run request, a run plan, the simready-verification
    stage report and the generated-asset validation report.
    """

    project = params.get("project") or params.get("project_dir")
    if not project:
        return ToolResult(success=False, error="project is required", validation_status="blocked")
    root = Path(str(project)).resolve()
    if not root.is_dir():
        return ToolResult(success=False, error="project must be an existing directory", validation_status="blocked")
    try:
        with workspace_lease(root, "rl-environment-design"):
            return _rl_route_locked(root)
    except WorkspaceBusyError as exc:
        return ToolResult(success=False, error=str(exc), validation_status="blocked")


def merge_design_into_manifest(payload: dict[str, Any], design: dict[str, Any]) -> dict[str, Any]:
    """Write a design result into a stage manifest payload; shared by the workflow and the evidence importer."""

    payload.setdefault("extensions", {})["rl"] = design["extensions_rl"]
    payload["status"] = design["status"]
    payload["validation_status"] = design["status"]
    if design["status"] == "review_required":
        payload["review_status"] = "review_required"
    else:
        payload.pop("review_status", None)
    payload["blocked_reasons"] = list(design.get("blocked_reasons", []))
    payload["review_reasons"] = list(design.get("review_reasons", []))
    managed_ids = {
        "rl_environment_design_report",
        "rl_environment_card",
        "rl_task_fitness_protocol",
        "isaac_runtime_evidence",
        "rl_probe_evidence",
        "rl_probe_import_receipt",
        "rl_collision_fidelity",
        "rl_fidelity_import_receipt",
    }
    retained_evidence = [
        item
        for item in payload.get("evidence", [])
        if isinstance(item, dict)
        and item.get("evidence_id") not in managed_ids
        and not str(item.get("evidence_id") or "").startswith("lineage_")
    ]
    payload["evidence"] = retained_evidence + list(design.get("evidence", []))
    kept = [gate for gate in payload.get("validation_gates", []) if not str(gate.get("gate_id", "")).startswith("rl-")]
    payload["validation_gates"] = kept + [
        {
            "gate_id": gate["gate_id"],
            "status": gate["status"],
            "reasons": list(gate.get("reasons", [])),
            "evidence_ids": list(gate.get("evidence_ids", [])),
        }
        for gate in design.get("gates", [])
    ]
    return payload
