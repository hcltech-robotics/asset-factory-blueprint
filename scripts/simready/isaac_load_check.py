from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

try:
    from asset_factory_blueprint.isaac_evidence import (
        ATTESTATION_ALGORITHM,
        ATTESTATION_SCHEMA_VERSION,
        PROTOCOL_ID,
        PROTOCOL_VERSION,
        REPORT_ID,
        REPORT_VERSION,
        attest_runtime_report,
        attestation_secret,
        canonical_report_bytes,
        producer_sha256_pin,
    )
except ModuleNotFoundError as exc:
    if exc.name != "asset_factory_blueprint":
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from asset_factory_blueprint.isaac_evidence import (  # type: ignore[no-redef]
        ATTESTATION_ALGORITHM,
        ATTESTATION_SCHEMA_VERSION,
        PROTOCOL_ID,
        PROTOCOL_VERSION,
        REPORT_ID,
        REPORT_VERSION,
        attest_runtime_report,
        attestation_secret,
        canonical_report_bytes,
        producer_sha256_pin,
    )


_NUMERIC_ARGUMENT_BOUNDS: dict[str, tuple[float, float, bool]] = {
    "width": (64, 4096, True),
    "height": (64, 4096, True),
    "min_seconds": (0.1, 300.0, False),
    "physics_dt": (0.0001, 0.1, False),
    "settle_steps": (1, 5000, True),
    "impulse_steps": (1, 5000, True),
    "minimum_drop_metres": (0.0, 10.0, False),
    "minimum_impulse_motion_metres": (0.0, 10.0, False),
    "settled_speed_metres_per_second": (0.0, 100.0, False),
    "repeatability_tolerance_metres": (0.0, 10.0, False),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load a generated Asset Factory USD in Isaac Sim and record structural and behavioural evidence."
    )
    parser.add_argument("--usd", required=True, help="USD file to load.")
    parser.add_argument("--output", required=True, help="JSON report path.")
    parser.add_argument("--profile-id", default="", help="Exact target SimReady Profile ID.")
    parser.add_argument("--profile-version", default="", help="Exact target SimReady Profile version.")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--min-seconds", type=float, default=3.0)
    parser.add_argument("--physics-dt", type=float, default=1.0 / 120.0)
    parser.add_argument("--settle-steps", type=int, default=240)
    parser.add_argument("--impulse-steps", type=int, default=60)
    parser.add_argument("--minimum-drop-metres", type=float, default=0.02)
    parser.add_argument("--minimum-impulse-motion-metres", type=float, default=0.01)
    parser.add_argument("--settled-speed-metres-per-second", type=float, default=0.25)
    parser.add_argument("--repeatability-tolerance-metres", type=float, default=0.05)
    return parser.parse_args()


def _numeric_argument_errors(args: argparse.Namespace) -> list[str]:
    errors: list[str] = []
    for name, (minimum, maximum, integer_only) in _NUMERIC_ARGUMENT_BOUNDS.items():
        value = getattr(args, name)
        if integer_only and (not isinstance(value, int) or isinstance(value, bool)):
            errors.append(f"--{name.replace('_', '-')} must be an integer")
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            errors.append(f"--{name.replace('_', '-')} must be finite")
            continue
        if float(value) < minimum or float(value) > maximum:
            errors.append(
                f"--{name.replace('_', '-')} must be between {minimum:g} and {maximum:g} inclusive"
            )
    return errors


def _portable_usd_identity(path: Path, output_path: Path, raw_value: str) -> tuple[str, str]:
    if output_path.parent.name == "reports":
        project_root = output_path.parent.parent.resolve(strict=False)
        try:
            relative = path.relative_to(project_root).as_posix()
        except ValueError:
            pass
        else:
            return relative, "project:///" + quote(relative, safe="/-._~")
    return raw_value.replace("\\", "/"), "package://" + quote(path.name, safe="-._~")


def write_report(path: Path, payload: dict[str, Any], secret: bytes | None) -> None:
    payload["execution_identity"]["completed_at"] = datetime.now(timezone.utc).isoformat()
    payload.pop("attestation", None)
    if secret is None:
        canonical = canonical_report_bytes(payload)
        payload["attestation"] = {
            "schema_version": ATTESTATION_SCHEMA_VERSION,
            "status": "unsigned",
            "algorithm": ATTESTATION_ALGORITHM,
            "key_id": "",
            "payload_digest": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            "signature": "",
        }
    else:
        payload["attestation"] = attest_runtime_report(payload, secret)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=False, ensure_ascii=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _composition_fingerprint(stage: Any) -> str:
    layer_hashes = []
    for layer in stage.GetUsedLayers():
        real_path = Path(str(layer.realPath or ""))
        if real_path.is_file():
            layer_hashes.append(_sha256_file(real_path))
    if not layer_hashes:
        return ""
    digest = hashlib.sha256()
    for layer_hash in sorted(layer_hashes):
        digest.update(layer_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _package_inventory_fingerprint(package_root: Path) -> dict[str, Any]:
    records: list[dict[str, str]] = []
    blockers: list[str] = []
    seen: set[str] = set()
    root = package_root.resolve(strict=False)
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if candidate.is_symlink() or bool(getattr(candidate, "is_junction", lambda: False)()):
            blockers.append(f"package contains a link or junction: {candidate.relative_to(root).as_posix()}")
            continue
        if not candidate.is_file():
            continue
        resolved = candidate.resolve(strict=True)
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError:
            blockers.append(f"package file escapes the package root: {candidate.as_posix()}")
            continue
        folded = relative.casefold()
        if folded in seen:
            blockers.append(f"package contains a case-colliding path: {relative}")
            continue
        seen.add(folded)
        records.append({"path": relative, "sha256": _sha256_file(resolved)})
    if not records:
        blockers.append("package inventory contains no regular files")
    digest = hashlib.sha256()
    for record in records:
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\n")
    return {
        "status": "pass" if not blockers else "blocked",
        "fingerprint": f"sha256:{digest.hexdigest()}" if records and not blockers else "",
        "files": records,
        "blocked_reasons": blockers,
    }


def _test_result(
    test_id: str,
    status: str,
    applicable: bool,
    reason: str = "",
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "test_id": test_id,
        "status": status,
        "applicable": applicable,
        "reason": reason,
        "metrics": metrics or {},
    }


def _xyz(value: Any) -> list[float]:
    if hasattr(value, "x"):
        return [float(value.x), float(value.y), float(value.z)]
    return [float(value[0]), float(value[1]), float(value[2])]


def _distance(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(a, b, strict=True)))


def _horizontal_distance(a: list[float], b: list[float], up_index: int) -> float:
    return math.sqrt(sum((a[index] - b[index]) ** 2 for index in range(3) if index != up_index))


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))]


def _dynamic_control_adapter() -> tuple[Any | None, Any | None, str]:
    try:
        from omni.isaac.dynamic_control import _dynamic_control
    except Exception as exc:
        return None, None, f"dynamic-control state adapter unavailable: {exc}"
    try:
        return _dynamic_control.acquire_dynamic_control_interface(), _dynamic_control, ""
    except Exception as exc:
        return None, None, f"dynamic-control interface could not be acquired: {exc}"


def _body_state(dc: Any, dc_module: Any, path: str) -> tuple[Any, list[float], list[float]]:
    handle = dc.get_rigid_body(path)
    invalid = getattr(dc_module, "INVALID_HANDLE", 0)
    if handle is None or handle == invalid:
        raise RuntimeError(f"no runtime rigid-body handle for {path}")
    pose = dc.get_rigid_body_pose(handle)
    velocity = dc.get_rigid_body_linear_velocity(handle)
    return handle, _xyz(pose.p), _xyz(velocity)


def _step_many(simulation_context: Any, count: int, frame_times: list[float]) -> None:
    for _ in range(count):
        frame_start = time.perf_counter()
        simulation_context.step(render=True)
        frame_times.append((time.perf_counter() - frame_start) * 1000.0)


def _joint_definitions(joints: list[Any]) -> tuple[bool, list[str]]:
    errors = []
    for joint in joints:
        path = str(joint.GetPath())
        schema = joint.GetTypeName()
        joint_schema = joint
        body0 = joint_schema.GetRelationship("physics:body0").GetTargets()
        body1 = joint_schema.GetRelationship("physics:body1").GetTargets()
        if not body0 or not body1:
            errors.append(f"{path} does not bind both joint bodies")
        if schema in {"PhysicsRevoluteJoint", "PhysicsPrismaticJoint"}:
            lower = joint.GetAttribute("physics:lowerLimit").Get()
            upper = joint.GetAttribute("physics:upperLimit").Get()
            if lower is None or upper is None or float(lower) > float(upper):
                errors.append(f"{path} has missing or invalid joint limits")
    return not errors, errors


def _run_joint_sweep(
    dc: Any,
    dc_module: Any,
    articulation_paths: list[str],
    step: Callable[[int], None],
) -> tuple[bool, dict[str, Any], str]:
    required_methods = [
        "get_articulation",
        "get_articulation_dof_count",
        "get_articulation_dof",
        "get_dof_properties",
        "get_dof_state",
        "set_dof_position_target",
    ]
    missing_methods = [name for name in required_methods if not hasattr(dc, name)]
    if missing_methods:
        return False, {}, "dynamic-control joint sweep API is unavailable: " + ", ".join(missing_methods)
    invalid = getattr(dc_module, "INVALID_HANDLE", 0)
    state_all = getattr(dc_module, "STATE_ALL", 0xF)
    records = []
    for path in articulation_paths:
        articulation = dc.get_articulation(path)
        if articulation is None or articulation == invalid:
            return False, {"articulation_path": path}, "runtime articulation handle is unavailable"
        dof_count = int(dc.get_articulation_dof_count(articulation))
        if dof_count <= 0:
            return False, {"articulation_path": path}, "articulation has no controllable degrees of freedom"
        for index in range(dof_count):
            dof = dc.get_articulation_dof(articulation, index)
            properties = dc.get_dof_properties(dof)
            lower = float(properties.lower)
            upper = float(properties.upper)
            if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
                return False, {"articulation_path": path, "dof_index": index}, "degree-of-freedom limits are invalid"
            target = (lower + upper) * 0.5
            dc.set_dof_position_target(dof, target)
            step(60)
            state = dc.get_dof_state(dof, state_all)
            position = float(state.pos)
            within_limits = lower - 1e-4 <= position <= upper + 1e-4
            records.append(
                {
                    "articulation_path": path,
                    "dof_index": index,
                    "lower_limit": lower,
                    "upper_limit": upper,
                    "target": target,
                    "observed_position": position,
                    "within_limits": within_limits,
                }
            )
            if not within_limits:
                return False, {"dofs": records}, "joint left its authored limits during the sweep"
    return True, {"dofs": records}, ""


def main() -> int:
    args = parse_args()
    usd_path = Path(args.usd).resolve(strict=False)
    output_path = Path(args.output).resolve(strict=False)
    package_root = usd_path.parent
    portable_usd_path, portable_usd_label = _portable_usd_identity(usd_path, output_path, args.usd)
    producer_sha256 = _sha256_file(Path(__file__).resolve()).lower()
    package_evidence: dict[str, Any] = {
        "status": "blocked",
        "fingerprint": "",
        "files": [],
        "blocked_reasons": ["package inventory has not been evaluated"],
    }
    report: dict[str, Any] = {
        "report_identity": {"id": REPORT_ID, "version": REPORT_VERSION},
        "protocol_identity": {"id": PROTOCOL_ID, "version": PROTOCOL_VERSION},
        "execution_identity": {
            "producer_id": "asset-factory-blueprint.isaac-load-check",
            "producer_version": REPORT_VERSION,
            "producer_sha256": f"sha256:{producer_sha256}",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": "",
            "python_version": platform.python_version(),
            "platform": platform.system(),
            "architecture": platform.machine(),
        },
        "runtime_identity": {
            "id": "nvidia-isaac-sim",
            "version": "",
            "renderer": "RayTracedLighting",
            "physics_backend": "PhysX",
            "headless": True,
        },
        "status": "blocked",
        "usd_path": portable_usd_path,
        "usd_label": portable_usd_label,
        "usd_sha256": _sha256_file(usd_path) if usd_path.exists() else "",
        "package_dependency_fingerprint": "",
        "package_inventory": [],
        "profile_id": args.profile_id,
        "profile_version": args.profile_version,
        "loaded": False,
        "renderer": "RayTracedLighting",
        "width": args.width,
        "height": args.height,
        "min_seconds": args.min_seconds,
        "physics_dt": args.physics_dt,
        "validation_parameters": {
            name: getattr(args, name)
            for name in _NUMERIC_ARGUMENT_BOUNDS
        },
        "default_prim": "",
        "prim_count": 0,
        "schema_inventory": {},
        "behavioural_tests": [],
        "performance": {},
        "lights": {},
        "runtime_availability": {"isaac_sim": False, "dynamic_control": False},
        "errors": [],
    }
    secret: bytes | None = None
    report["errors"].extend(_numeric_argument_errors(args))
    try:
        secret = attestation_secret()
    except ValueError as exc:
        report["errors"].append(str(exc))
    try:
        pinned_producer_sha256 = producer_sha256_pin()
    except ValueError as exc:
        report["errors"].append(str(exc))
    else:
        if producer_sha256 != pinned_producer_sha256:
            report["errors"].append("Isaac runtime producer digest does not match AFB_ISAAC_PRODUCER_SHA256")
    simulation_app: Any | None = None
    exit_code = 1
    try:
        if report["errors"]:
            return exit_code
        if not usd_path.is_file():
            report["errors"].append("USD file does not exist")
            return exit_code
        package_evidence = _package_inventory_fingerprint(package_root)
        report["package_dependency_fingerprint"] = package_evidence["fingerprint"]
        report["package_inventory"] = package_evidence["files"]
        try:
            output_path.relative_to(package_root)
        except ValueError:
            pass
        else:
            report["errors"].append("runtime report path must be outside the immutable package directory")
            return exit_code
        if package_evidence["status"] != "pass":
            report["errors"].extend(package_evidence["blocked_reasons"])
            return exit_code
        try:
            import isaacsim
            from isaacsim import SimulationApp
        except Exception as exc:
            report["errors"].append(f"Isaac Sim runtime unavailable: {exc}")
            return exit_code

        simulation_app = SimulationApp(
            {"headless": True, "width": args.width, "height": args.height, "renderer": "RayTracedLighting"}
        )
        report["runtime_availability"]["isaac_sim"] = True
        report["runtime_availability"]["isaac_sim_version"] = str(getattr(isaacsim, "__version__", "unreported"))
        report["runtime_identity"]["version"] = report["runtime_availability"]["isaac_sim_version"]

        import carb
        import omni.timeline
        import omni.usd
        from isaacsim.core.api.simulation_context import SimulationContext
        from pxr import Gf, Usd, UsdGeom, UsdLux, UsdPhysics

        settings = carb.settings.get_settings()
        settings.set("/rtx/rendermode", "RayTracedLighting")
        settings.set("/rtx/post/tonemap/op", 4)
        settings.set("/rtx/post/tonemap/filmIso", 200)

        context = omni.usd.get_context()
        open_result = context.open_stage(str(usd_path))
        for _ in range(30):
            simulation_app.update()
        stage = context.get_stage()
        if stage is None:
            report["errors"].append(f"open_stage returned {open_result!r} and no stage is available")
            return exit_code
        default_prim = stage.GetDefaultPrim()
        if not default_prim:
            report["errors"].append("loaded stage has no default prim")
            return exit_code
        report["composition_fingerprint"] = _composition_fingerprint(stage)

        original_prims = list(stage.Traverse())
        rigid_bodies = [prim for prim in original_prims if prim.HasAPI(UsdPhysics.RigidBodyAPI)]
        colliders = [prim for prim in original_prims if prim.HasAPI(UsdPhysics.CollisionAPI)]
        mass_prims = [prim for prim in original_prims if prim.HasAPI(UsdPhysics.MassAPI)]
        articulation_roots = [prim for prim in original_prims if prim.HasAPI(UsdPhysics.ArticulationRootAPI)]
        joints = [prim for prim in original_prims if prim.IsA(UsdPhysics.Joint)]
        report["schema_inventory"] = {
            "rigid_body_paths": [str(prim.GetPath()) for prim in rigid_bodies],
            "collider_paths": [str(prim.GetPath()) for prim in colliders],
            "mass_api_paths": [str(prim.GetPath()) for prim in mass_prims],
            "articulation_root_paths": [str(prim.GetPath()) for prim in articulation_roots],
            "joint_paths": [str(prim.GetPath()) for prim in joints],
        }

        stage.SetEditTarget(stage.GetSessionLayer())
        dome = UsdLux.DomeLight.Define(stage, "/Validation/DomeLight")
        dome.CreateIntensityAttr(150.0)
        distant = UsdLux.DistantLight.Define(stage, "/Validation/DistantLight")
        distant.CreateIntensityAttr(600.0)
        report["lights"] = {
            "DomeLight": {"path": "/Validation/DomeLight", "intensity": 150.0},
            "DistantLight": {"path": "/Validation/DistantLight", "intensity": 600.0},
        }

        up_axis = str(UsdGeom.GetStageUpAxis(stage)).upper()
        up_index = 2 if up_axis == "Z" else 1
        gravity = [0.0, 0.0, 0.0]
        gravity[up_index] = -1.0
        physics_scenes = [prim for prim in original_prims if prim.IsA(UsdPhysics.Scene)]
        physics_scene = UsdPhysics.Scene(physics_scenes[0]) if physics_scenes else UsdPhysics.Scene.Define(stage, "/Validation/PhysicsScene")
        physics_scene.CreateGravityDirectionAttr().Set(Gf.Vec3f(*gravity))
        physics_scene.CreateGravityMagnitudeAttr().Set(9.81)

        bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
        aligned_box = bbox_cache.ComputeWorldBound(default_prim).ComputeAlignedBox()
        bbox_min = _xyz(aligned_box.GetMin())
        bbox_max = _xyz(aligned_box.GetMax())
        ground_top = bbox_min[up_index] - 0.25
        centre = [(low + high) * 0.5 for low, high in zip(bbox_min, bbox_max, strict=True)]
        span = [max(high - low, 0.1) for low, high in zip(bbox_min, bbox_max, strict=True)]
        ground = UsdGeom.Cube.Define(stage, "/Validation/Ground")
        ground.CreateSizeAttr(2.0)
        scale = [max(value, 1.0) for value in span]
        scale[up_index] = 0.05
        translation = list(centre)
        translation[up_index] = ground_top - scale[up_index]
        UsdGeom.XformCommonAPI(ground).SetScale(Gf.Vec3f(*scale))
        UsdGeom.XformCommonAPI(ground).SetTranslate(Gf.Vec3d(*translation))
        UsdPhysics.CollisionAPI.Apply(ground.GetPrim()).CreateCollisionEnabledAttr().Set(True)

        simulation_context = SimulationContext(
            physics_dt=args.physics_dt,
            rendering_dt=args.physics_dt,
            stage_units_in_meters=float(UsdGeom.GetStageMetersPerUnit(stage)),
        )
        simulation_context.initialize_physics()
        timeline = omni.timeline.get_timeline_interface()
        timeline.play()
        frame_times: list[float] = []
        _step_many(simulation_context, 5, frame_times)

        dc, dc_module, dc_error = _dynamic_control_adapter()
        report["runtime_availability"]["dynamic_control"] = dc is not None
        rigid_paths = [str(prim.GetPath()) for prim in rigid_bodies]
        if not rigid_paths:
            for test_id in [
                "rigid_body_drop_and_settle",
                "rigid_body_impulse_response",
                "rigid_body_reset_repeatability",
            ]:
                report["behavioural_tests"].append(
                    _test_result(test_id, "skipped", False, "asset has no applied RigidBodyAPI")
                )
        elif not colliders:
            for test_id in [
                "rigid_body_drop_and_settle",
                "rigid_body_impulse_response",
                "rigid_body_reset_repeatability",
            ]:
                report["behavioural_tests"].append(
                    _test_result(test_id, "blocked", True, "rigid asset has no applied CollisionAPI")
                )
        elif dc is None:
            for test_id in [
                "rigid_body_drop_and_settle",
                "rigid_body_impulse_response",
                "rigid_body_reset_repeatability",
            ]:
                report["behavioural_tests"].append(_test_result(test_id, "blocked", True, dc_error))
        else:
            primary_path = rigid_paths[0]
            handle, initial_position, _ = _body_state(dc, dc_module, primary_path)
            bottom_offset = bbox_min[up_index] - initial_position[up_index]
            _step_many(simulation_context, args.settle_steps, frame_times)
            _, settled_position, settled_velocity = _body_state(dc, dc_module, primary_path)
            drop_distance = initial_position[up_index] - settled_position[up_index]
            settled_speed = math.sqrt(sum(value * value for value in settled_velocity))
            estimated_bottom = settled_position[up_index] + bottom_offset
            clearance = estimated_bottom - ground_top
            finite_state = all(math.isfinite(value) for value in [*settled_position, *settled_velocity])
            drop_pass = (
                finite_state
                and drop_distance >= args.minimum_drop_metres
                and settled_speed <= args.settled_speed_metres_per_second
                and clearance >= -0.05
            )
            report["behavioural_tests"].append(
                _test_result(
                    "rigid_body_drop_and_settle",
                    "pass" if drop_pass else "blocked",
                    True,
                    "" if drop_pass else "asset did not fall, settle and remain above the validation ground within thresholds",
                    {
                        "rigid_body_path": primary_path,
                        "drop_distance_metres": drop_distance,
                        "settled_speed_metres_per_second": settled_speed,
                        "estimated_ground_clearance_metres": clearance,
                    },
                )
            )

            horizontal_axis = 0 if up_index != 0 else 1
            impulse_velocity = [0.0, 0.0, 0.0]
            impulse_velocity[horizontal_axis] = 1.0
            dc.set_rigid_body_linear_velocity(handle, carb.Float3(*impulse_velocity))
            impulse_start = list(settled_position)
            _step_many(simulation_context, args.impulse_steps, frame_times)
            _, impulse_end, impulse_end_velocity = _body_state(dc, dc_module, primary_path)
            impulse_motion = _horizontal_distance(impulse_start, impulse_end, up_index)
            impulse_pass = impulse_motion >= args.minimum_impulse_motion_metres and all(
                math.isfinite(value) for value in [*impulse_end, *impulse_end_velocity]
            )
            report["behavioural_tests"].append(
                _test_result(
                    "rigid_body_impulse_response",
                    "pass" if impulse_pass else "blocked",
                    True,
                    "" if impulse_pass else "rigid body did not produce a finite measurable response to an applied velocity",
                    {"horizontal_motion_metres": impulse_motion, "applied_velocity_metres_per_second": 1.0},
                )
            )

            try:
                simulation_context.reset()
                timeline.play()
                _step_many(simulation_context, args.settle_steps, frame_times)
                _, repeated_position, repeated_velocity = _body_state(dc, dc_module, primary_path)
                repeatability_error = _distance(settled_position, repeated_position)
                repeatability_pass = repeatability_error <= args.repeatability_tolerance_metres and all(
                    math.isfinite(value) for value in [*repeated_position, *repeated_velocity]
                )
                repeatability_reason = "" if repeatability_pass else "reset-and-settle result exceeded the repeatability tolerance"
            except Exception as exc:
                repeatability_error = None
                repeatability_pass = False
                repeatability_reason = f"reset repeatability could not be measured: {exc}"
            report["behavioural_tests"].append(
                _test_result(
                    "rigid_body_reset_repeatability",
                    "pass" if repeatability_pass else "blocked",
                    True,
                    repeatability_reason,
                    {
                        "position_error_metres": repeatability_error,
                        "tolerance_metres": args.repeatability_tolerance_metres,
                    },
                )
            )

        joint_definitions_pass, joint_errors = _joint_definitions(joints)
        articulation_paths = [str(prim.GetPath()) for prim in articulation_roots]
        if not joints and not articulation_paths:
            report["behavioural_tests"].append(
                _test_result("articulation_runtime_stability", "skipped", False, "asset is not articulated")
            )
            report["behavioural_tests"].append(
                _test_result("articulation_joint_sweep", "skipped", False, "asset is not articulated")
            )
        elif not articulation_paths:
            report["behavioural_tests"].append(
                _test_result(
                    "articulation_runtime_stability",
                    "blocked",
                    True,
                    "joint prims exist but no ArticulationRootAPI is applied",
                    {"joint_definition_errors": joint_errors},
                )
            )
            report["behavioural_tests"].append(
                _test_result("articulation_joint_sweep", "blocked", True, "no runtime articulation root is available")
            )
        elif dc is None:
            report["behavioural_tests"].append(
                _test_result("articulation_runtime_stability", "blocked", True, dc_error)
            )
            report["behavioural_tests"].append(_test_result("articulation_joint_sweep", "blocked", True, dc_error))
        else:
            _step_many(simulation_context, 60, frame_times)
            runtime_states = []
            finite_runtime = True
            for path in rigid_paths:
                try:
                    _, position, velocity = _body_state(dc, dc_module, path)
                    runtime_states.append({"path": path, "position": position, "velocity": velocity})
                    finite_runtime = finite_runtime and all(math.isfinite(value) for value in [*position, *velocity])
                except Exception as exc:
                    finite_runtime = False
                    runtime_states.append({"path": path, "error": str(exc)})
            stability_pass = finite_runtime and joint_definitions_pass
            report["behavioural_tests"].append(
                _test_result(
                    "articulation_runtime_stability",
                    "pass" if stability_pass else "blocked",
                    True,
                    "" if stability_pass else "articulation state or joint definitions were invalid",
                    {"runtime_states": runtime_states, "joint_definition_errors": joint_errors},
                )
            )
            sweep_pass, sweep_metrics, sweep_reason = _run_joint_sweep(
                dc,
                dc_module,
                articulation_paths,
                lambda count: _step_many(simulation_context, count, frame_times),
            )
            report["behavioural_tests"].append(
                _test_result(
                    "articulation_joint_sweep",
                    "pass" if sweep_pass else "blocked",
                    True,
                    sweep_reason,
                    sweep_metrics,
                )
            )

        start = time.perf_counter()
        frames_before = len(frame_times)
        while time.perf_counter() - start < args.min_seconds:
            _step_many(simulation_context, 1, frame_times)
        elapsed = time.perf_counter() - start
        measured_frames = len(frame_times) - frames_before
        simulation_context.stop()

        applicable_tests = [item for item in report["behavioural_tests"] if item["applicable"]]
        behaviour_passed = all(item["status"] == "pass" for item in applicable_tests)
        report.update(
            {
                "status": "pass" if behaviour_passed else "blocked",
                "loaded": True,
                "default_prim": str(default_prim.GetPath()),
                "prim_count": len(original_prims),
                "elapsed_seconds": elapsed,
                "frames": measured_frames,
                "render_mode": "RayTracedLighting",
                "tonemap": {"op": 4, "filmIso": 200},
                "performance": {
                    "frame_time_ms_p50": sorted(frame_times)[len(frame_times) // 2] if frame_times else 0.0,
                    "frame_time_ms_p95": _p95(frame_times),
                    "measured_frame_count": len(frame_times),
                    "real_time_factor": (len(frame_times) * args.physics_dt) / max(sum(frame_times) / 1000.0, 1e-9),
                },
            }
        )
        final_package_evidence = _package_inventory_fingerprint(package_root)
        if (
            final_package_evidence["status"] != "pass"
            or final_package_evidence["fingerprint"] != package_evidence["fingerprint"]
            or final_package_evidence["files"] != package_evidence["files"]
        ):
            report["status"] = "blocked"
            report["errors"].append("package inventory changed during runtime validation")
        exit_code = 0 if report["status"] == "pass" else 1
        return exit_code
    except Exception as exc:
        report["errors"].append(str(exc))
        return exit_code
    finally:
        if simulation_app is not None:
            try:
                simulation_app.close()
            except Exception as exc:
                report["status"] = "blocked"
                report["errors"].append(f"Isaac Sim shutdown failed: {exc}")
        write_report(output_path, report, secret)


if __name__ == "__main__":
    raise SystemExit(main())
