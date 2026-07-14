"""Measure contract-bound collision fidelity from the composed USD package."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asset_factory_blueprint.rl_evidence as rl_evidence_module
import asset_factory_blueprint.rl_fidelity as rl_fidelity_module
import asset_factory_blueprint.rl_render as rl_render_module
from asset_factory_blueprint.execution import durable_replace, durable_unlink, workspace_lease
from asset_factory_blueprint.manifests import validate_payload
from asset_factory_blueprint.rl_evidence import (
    FIDELITY_REPORT_ID,
    FIDELITY_REPORT_VERSION,
    environment_contract_sha256,
    environment_manifest_sha256,
    producer_bundle_sha256,
    report_attestation_secret,
    verify_report,
    write_attested_report,
)
from asset_factory_blueprint.rl_fidelity import Mesh, convex_hull, evaluate_fidelity
from asset_factory_blueprint.rl_render import verify_rendered_package_sources
from asset_factory_blueprint.schemas.common import RunPlan, RunRequest
from asset_factory_blueprint.utils.checksums import sha256_file, sha256_text
from asset_factory_blueprint.utils.package_fingerprint import package_inventory_fingerprint

SUPPORTED_APPROXIMATIONS = {"none", "convexHull"}
RENDERED_FILES = ("__init__.py", "rl_env_cfg.py", "custom_mdp.py", "rl_env_cfg.render.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure affordance-weighted collision fidelity")
    parser.add_argument("--project", required=True)
    parser.add_argument("--manifest", default="manifests/rl-environment-manifest.json")
    parser.add_argument("--output", default="reports/incoming/rl-collision-fidelity.json")
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


def _producer_files() -> list[dict[str, str]]:
    paths = {
        "src/asset_factory_blueprint/rl_collision_fidelity.py": Path(__file__).resolve(),
        "src/asset_factory_blueprint/rl_fidelity.py": Path(rl_fidelity_module.__file__).resolve(),
        "src/asset_factory_blueprint/rl_evidence.py": Path(rl_evidence_module.__file__).resolve(),
        "src/asset_factory_blueprint/rl_render.py": Path(rl_render_module.__file__).resolve(),
    }
    return [{"path": label, "sha256": sha256_file(path)} for label, path in sorted(paths.items())]


def _rendered_files(project: Path, manifest: dict[str, Any]) -> dict[str, str]:
    return verify_rendered_package_sources(project, manifest)


def load_bindings(project: Path, manifest_relative: str) -> dict[str, Any]:
    manifest_path = _project_file(project, manifest_relative, "RL environment manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_errors = validate_payload("rl-environment-manifest", manifest)
    if manifest_errors:
        raise ValueError("RL environment manifest schema: " + "; ".join(issue.render() for issue in manifest_errors))
    block = (manifest.get("extensions") or {}).get("rl") or {}
    if environment_contract_sha256(block) != block.get("contract_sha256"):
        raise ValueError("RL environment contract SHA-256 is stale")
    if (block.get("runtime") or {}).get("physics_backend") != "physx":
        raise ValueError("collision fidelity supports the PhysX contract only")
    if (block.get("task") or {}).get("behaviour") != "pick":
        raise ValueError("collision fidelity supports the pick contract only")
    usd_relative = str((block.get("scene") or {}).get("composed_root") or "")
    usd_path = _project_file(project, usd_relative, "composed USD")
    runtime = block["runtime"]
    package = package_inventory_fingerprint(usd_path.parent)
    if package["status"] != "pass":
        raise ValueError("composed USD package inventory cannot be verified")
    if package["fingerprint"] != runtime.get("validated_package_dependency_fingerprint"):
        raise ValueError("composed USD package fingerprint differs from the RL contract")
    if sha256_file(usd_path) != runtime.get("validated_usd_sha256"):
        raise ValueError("composed USD checksum differs from the RL contract")
    runtime_report = _project_file(project, "reports/isaac-load-check.json", "Isaac runtime report")
    if sha256_file(runtime_report) != (runtime.get("runtime_evidence") or {}).get("sha256"):
        raise ValueError("Isaac runtime report checksum differs from the RL contract")
    protocol_ref = block.get("task_fitness_protocol") or {}
    protocol_path = _project_file(project, str(protocol_ref.get("path") or ""), "task-fitness protocol")
    if sha256_file(protocol_path) != protocol_ref.get("sha256"):
        raise ValueError("task-fitness protocol checksum differs from the RL contract")
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    protocol_errors = validate_payload("task-fitness-protocol", protocol)
    if protocol_errors:
        raise ValueError("task-fitness protocol schema: " + "; ".join(issue.render() for issue in protocol_errors))
    if protocol.get("protocol_id") != protocol_ref.get("protocol_id"):
        raise ValueError("task-fitness protocol identity differs from the RL contract")
    protocol_extensions = protocol.get("extensions") if isinstance(protocol.get("extensions"), dict) else {}
    rl_extensions = protocol_extensions.get("rl") if isinstance(protocol_extensions.get("rl"), dict) else {}
    fidelity = (
        rl_extensions.get("collision_fidelity") if isinstance(rl_extensions.get("collision_fidelity"), dict) else {}
    )
    required = ("tolerance_m", "region_radius_m", "samples_per_region", "seed")
    if any(field not in fidelity for field in required):
        raise ValueError("task-fitness protocol has no complete collision-fidelity parameters")
    regions = []
    for item in (block.get("task") or {}).get("grasp_frames") or []:
        if not isinstance(item, dict) or item.get("frame_space") != "asset":
            raise ValueError("task grasp frames must be asset-local")
        regions.append(
            {
                "id": str(item["id"]),
                "kind": "grasp_point",
                "centre": [float(value) for value in item["frame"][:3]],
                "radius_m": float(fidelity["region_radius_m"]),
            }
        )
    if not regions:
        raise ValueError("RL contract has no accepted grasp regions")
    request = RunRequest.model_validate_json(
        _project_file(project, "run-request.json", "run request").read_text(encoding="utf-8")
    )
    plan = RunPlan.model_validate_json(_project_file(project, "run-plan.json", "run plan").read_text(encoding="utf-8"))
    current_request_digest = "sha256:" + sha256_text(request.model_dump_json())
    if plan.request_digest != current_request_digest:
        raise ValueError("run plan digest does not identify the persisted run request")
    if plan.request_id != request.id or plan.asset_id != request.id:
        raise ValueError("run plan identity does not match the persisted run request")
    if rl_extensions.get("request_digest") != current_request_digest:
        raise ValueError("task-fitness protocol belongs to another run request")
    rendered = _rendered_files(project, manifest)
    record = json.loads((project / "envs" / "rl_env_cfg.render.json").read_text(encoding="utf-8"))
    stable_manifest_sha256 = environment_manifest_sha256(manifest)
    if record.get("manifest_sha256") != stable_manifest_sha256:
        raise ValueError("rendered environment was produced from another manifest")
    if record.get("contract_sha256") != block.get("contract_sha256"):
        raise ValueError("rendered environment was produced from another contract")
    return {
        "manifest_path": manifest_relative,
        "manifest_sha256": stable_manifest_sha256,
        "contract_sha256": block["contract_sha256"],
        "usd_path": usd_relative,
        "usd_file": usd_path,
        "usd_sha256": sha256_file(usd_path),
        "request_digest": current_request_digest,
        "package_dependency_fingerprint": package["fingerprint"],
        "runtime": {
            "physics_backend": "physx",
            "sim_dt": runtime["sim_dt"],
            "decimation": runtime["decimation"],
            "source_report_sha256": sha256_file(runtime_report),
        },
        "rendered_files": rendered,
        "regions": regions,
        "fidelity": fidelity,
    }


def meshes_from_usd(usd_path: Path) -> tuple[Mesh, Mesh, list[str]]:
    """Extract visual triangles and supported collision meshes from a composed USD stage."""

    from pxr import Usd, UsdGeom, UsdPhysics  # type: ignore[import-not-found]

    stage = Usd.Stage.Open(str(usd_path))
    if stage is None:
        raise RuntimeError(f"cannot open {usd_path}")
    default_prim = stage.GetDefaultPrim()
    if not default_prim or not default_prim.IsValid():
        raise RuntimeError("composed stage has no default asset prim")
    metres_per_unit = float(UsdGeom.GetStageMetersPerUnit(stage))
    if not 0 < metres_per_unit < float("inf"):
        raise RuntimeError("composed stage has no finite positive metres-per-unit value")
    cache = UsdGeom.XformCache()
    world_to_asset = cache.GetLocalToWorldTransform(default_prim).GetInverse()
    visual_vertices: list[tuple[float, float, float]] = []
    visual_faces: list[tuple[int, int, int]] = []
    collision_vertices: list[tuple[float, float, float]] = []
    collision_faces: list[tuple[int, int, int]] = []
    problems: list[str] = []

    def triangulate(mesh: Any) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
        matrix = cache.GetLocalToWorldTransform(mesh.GetPrim())
        points = [world_to_asset.Transform(matrix.Transform(point)) for point in mesh.GetPointsAttr().Get() or []]
        vertices = [(float(point[0]), float(point[1]), float(point[2])) for point in points]
        vertices = [(x * metres_per_unit, y * metres_per_unit, z * metres_per_unit) for x, y, z in vertices]
        counts = list(mesh.GetFaceVertexCountsAttr().Get() or [])
        indices = list(mesh.GetFaceVertexIndicesAttr().Get() or [])
        faces: list[tuple[int, int, int]] = []
        cursor = 0
        for count in counts:
            polygon = indices[cursor : cursor + count]
            cursor += count
            for index in range(1, len(polygon) - 1):
                faces.append((int(polygon[0]), int(polygon[index]), int(polygon[index + 1])))
        return vertices, faces

    for prim in Usd.PrimRange(default_prim):
        if prim.HasAPI(UsdPhysics.CollisionAPI) and not prim.IsA(UsdGeom.Mesh):
            problems.append(f"{prim.GetPath()} is a non-mesh collider; collision fidelity cannot measure it")
            continue
        if not prim.IsA(UsdGeom.Mesh):
            continue
        mesh = UsdGeom.Mesh(prim)
        purpose = UsdGeom.Imageable(prim).ComputePurpose()
        has_collision = prim.HasAPI(UsdPhysics.CollisionAPI)
        if purpose in {UsdGeom.Tokens.default_, UsdGeom.Tokens.render} and not has_collision:
            vertices, faces = triangulate(mesh)
            offset = len(visual_vertices)
            visual_vertices.extend(vertices)
            visual_faces.extend((a + offset, b + offset, c + offset) for a, b, c in faces)
        if not has_collision:
            continue
        if purpose != UsdGeom.Tokens.proxy:
            problems.append(
                f"{prim.GetPath()} collision mesh must use purpose proxy and remain separate from render geometry"
            )
            continue
        approximation = "none"
        if prim.HasAPI(UsdPhysics.MeshCollisionAPI):
            attribute = UsdPhysics.MeshCollisionAPI(prim).GetApproximationAttr()
            approximation = str(attribute.Get() or "none") if attribute else "none"
        if approximation not in SUPPORTED_APPROXIMATIONS:
            problems.append(f"{prim.GetPath()} uses unsupported collision approximation {approximation}")
            continue
        vertices, faces = triangulate(mesh)
        if approximation == "convexHull":
            try:
                vertices, faces = convex_hull(vertices)
            except RuntimeError as exc:
                problems.append(f"{prim.GetPath()} convex hull failed: {exc}")
                continue
        offset = len(collision_vertices)
        collision_vertices.extend(vertices)
        collision_faces.extend((a + offset, b + offset, c + offset) for a, b, c in faces)
    if not visual_faces:
        problems.append("composed stage contains no visual mesh triangles")
    if not collision_faces:
        problems.append("composed stage contains no supported collision mesh triangles")
    return (visual_vertices, visual_faces), (collision_vertices, collision_faces), problems


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
            "fidelity output must be inside reports/incoming so evidence passes through the importer"
        ) from exc
    if resolved.is_symlink():
        raise ValueError("output must not be a symbolic link")
    return resolved


def main() -> int:
    args = parse_args()
    project = Path(args.project).resolve(strict=True)
    try:
        secret = report_attestation_secret(FIDELITY_REPORT_ID)
        producer_files = _producer_files()
        bindings = load_bindings(project, args.manifest)
        output = _output_path(project, args.output)
    except (KeyError, OSError, ValueError) as exc:
        print(f"collision fidelity preflight failed: {exc}", file=sys.stderr)
        return 1

    started = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    errors: list[str] = []
    result: dict[str, Any]
    try:
        visual, collision, extraction_errors = meshes_from_usd(bindings["usd_file"])
        errors.extend(extraction_errors)
        if errors:
            result = {
                "report_identity": {"id": FIDELITY_REPORT_ID, "version": FIDELITY_REPORT_VERSION},
                "status": "blocked",
                "tolerance_m": float(bindings["fidelity"]["tolerance_m"]),
                "samples_per_region": int(bindings["fidelity"]["samples_per_region"]),
                "seed": int(bindings["fidelity"]["seed"]),
                "regions": [],
                "errors": list(errors),
            }
        else:
            result = evaluate_fidelity(
                visual,
                collision,
                bindings["regions"],
                float(bindings["fidelity"]["tolerance_m"]),
                int(bindings["fidelity"]["samples_per_region"]),
                int(bindings["fidelity"]["seed"]),
            )
            errors.extend(str(item) for item in result.get("errors") or [])
    except Exception as exc:  # noqa: BLE001 - the signed report preserves the runtime failure
        errors.append(f"{type(exc).__name__}: {exc}")
        result = {
            "report_identity": {"id": FIDELITY_REPORT_ID, "version": FIDELITY_REPORT_VERSION},
            "status": "blocked",
            "tolerance_m": float(bindings["fidelity"]["tolerance_m"]),
            "samples_per_region": int(bindings["fidelity"]["samples_per_region"]),
            "seed": int(bindings["fidelity"]["seed"]),
            "regions": [],
            "errors": list(errors),
        }

    try:
        with workspace_lease(project, "rl-collision-fidelity-producer"):
            output = _output_path(project, args.output)
            try:
                if load_bindings(project, args.manifest) != bindings:
                    errors.append("project bindings changed during collision-fidelity measurement")
            except Exception as exc:  # noqa: BLE001 - drift must produce signed blocked evidence
                errors.append(f"project binding revalidation failed: {type(exc).__name__}: {exc}")
            try:
                if _producer_files() != producer_files:
                    errors.append("collision-fidelity producer files changed during measurement")
            except Exception as exc:  # noqa: BLE001 - drift must produce signed blocked evidence
                errors.append(f"producer revalidation failed: {type(exc).__name__}: {exc}")
            report = {
                **result,
                "physics_backend": "physx",
                "usd_sha256": bindings["usd_sha256"],
                "request_digest": bindings["request_digest"],
                "manifest_sha256": bindings["manifest_sha256"],
                "manifest_path": bindings["manifest_path"],
                "usd_path": bindings["usd_path"],
                "contract_sha256": bindings["contract_sha256"],
                "package_dependency_fingerprint": bindings["package_dependency_fingerprint"],
                "runtime": bindings["runtime"],
                "rendered_files": bindings["rendered_files"],
                "execution_identity": {
                    "producer_id": "asset-factory.rl-collision-fidelity",
                    "producer_version": "1.0",
                    "producer_sha256": producer_bundle_sha256(producer_files),
                    "producer_files": producer_files,
                    "started_at": started,
                    "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "python_version": platform.python_version(),
                    "platform": platform.platform(),
                    "architecture": platform.machine(),
                },
                "errors": list(dict.fromkeys(errors)),
            }
            if report["errors"]:
                report["status"] = "blocked"
            signed = {**report, "attestation": rl_evidence_module.attest_report(report, secret)}
            schema_errors = validate_payload("rl-collision-fidelity", signed)
            semantic_errors = verify_report(
                signed,
                FIDELITY_REPORT_ID,
                secret=secret,
                expected_backend="physx",
                expected_usd_sha256=bindings["usd_sha256"],
                expected_request_digest=bindings["request_digest"],
                expected_manifest_sha256=bindings["manifest_sha256"],
                expected_contract_sha256=bindings["contract_sha256"],
                expected_package_fingerprint=bindings["package_dependency_fingerprint"],
                expected_sim_dt=bindings["runtime"]["sim_dt"],
                expected_decimation=bindings["runtime"]["decimation"],
                expected_rendered_files=bindings["rendered_files"],
            )
            if schema_errors or semantic_errors:
                rendered = [issue.render() for issue in schema_errors] + semantic_errors
                print(
                    "refusing to write invalid collision-fidelity evidence: " + "; ".join(rendered),
                    file=sys.stderr,
                )
                return 1
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
            try:
                write_attested_report(temporary, report, secret)
                durable_replace(temporary, output)
            finally:
                durable_unlink(temporary, missing_ok=True)
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
