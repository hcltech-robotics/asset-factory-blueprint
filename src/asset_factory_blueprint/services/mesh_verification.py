from __future__ import annotations

import gc
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
import matplotlib
import numpy as np
import trimesh

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from PIL import Image, ImageDraw, ImageOps

from asset_factory_blueprint.execution import atomic_write_json, resolve_within
from asset_factory_blueprint.manifests import load_schema
from asset_factory_blueprint.mesh_topology import (
    exact_mesh_metrics,
    failed_quality_findings,
    mesh_quality_checks,
    resolved_quality_policy,
)
from asset_factory_blueprint.services.vlm_review import governance_vlm_review
from asset_factory_blueprint.providers import complete_vision
from asset_factory_blueprint.skills.base import ToolResult
from asset_factory_blueprint.utils.checksums import sha256_file


MESH_SUFFIXES = {".glb", ".gltf", ".obj", ".ply", ".stl", ".usd", ".usda", ".usdc", ".usdz"}
LOCAL_FIX_TAGS = {
    "mesh_holes",
    "fragmented_parts",
    "lumpy_surface",
    "extra_geometry",
    "degenerate_faces",
    "invalid_normals",
    "non_manifold_geometry",
    "duplicate_faces",
    "interior_faces",
}
REGENERATE_TAGS = {"missing_parts", "wrong_proportions", "wrong_scale", "source_mismatch", "self_intersection"}
REVIEW_IMAGES = ("beauty-contact-sheet.png", "wireframe-contact-sheet.png", "normal-contact-sheet.png")
RENDER_FACE_LIMIT = 5_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _project_relative_uri(project_dir: Path, path: str | Path) -> str:
    resolved = resolve_within(project_dir, path, must_exist=True)
    return resolved.relative_to(project_dir.resolve()).as_posix()


def _authorised_candidate(project_dir: Path, raw_path: str | Path) -> Path | None:
    try:
        candidate = resolve_within(project_dir, raw_path, must_exist=True)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() and candidate.suffix.lower() in MESH_SUFFIXES else None


def discover_candidate(project_dir: str | Path, explicit_path: str | Path | None = None) -> Path | None:
    root = Path(project_dir).resolve()
    candidates: list[str | Path] = []
    if explicit_path:
        candidates.append(explicit_path)

    external = _read_json(root / "manifests" / "external-model-run-manifest.json")
    landing = external.get("project_landing", {}) if isinstance(external.get("project_landing"), dict) else {}
    if landing.get("mesh_path"):
        candidates.append(str(landing["mesh_path"]))

    candidates.extend(sorted((root / "assets").glob("*/asset.glb")))

    reconstruction = _read_json(root / "manifests" / "reconstruction-manifest.json")
    for key in ("candidate_geometry_path", "generated_asset", "usd_output_path"):
        if reconstruction.get(key):
            candidates.append(str(reconstruction[key]))
    for evidence in reconstruction.get("evidence", []):
        if isinstance(evidence, dict) and evidence.get("kind") in {
            "reconstructed_mesh",
            "usd_geometry",
            "candidate_geometry",
        }:
            candidates.append(str(evidence.get("uri") or ""))

    for raw in candidates:
        candidate = _authorised_candidate(root, raw)
        if candidate is not None:
            return candidate

    discovered = sorted(
        path for path in (root / "assets").glob("**/*") if path.is_file() and path.suffix.lower() in MESH_SUFFIXES
    )
    return discovered[0].resolve() if discovered else None


def _triangulate_faces(counts: list[int], indices: list[int]) -> np.ndarray:
    triangles: list[list[int]] = []
    offset = 0
    for count in counts:
        face = indices[offset : offset + count]
        offset += count
        if len(face) < 3:
            continue
        triangles.extend([[face[0], face[index], face[index + 1]] for index in range(1, len(face) - 1)])
    return np.asarray(triangles, dtype=np.int64)


def _load_usd_mesh(path: Path) -> trimesh.Trimesh:
    from pxr import Usd, UsdGeom

    stage = Usd.Stage.Open(path.as_posix())
    if stage is None:
        raise ValueError("OpenUSD could not open the candidate")
    cache = UsdGeom.XformCache()
    meshes: list[trimesh.Trimesh] = []
    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.Mesh):
            continue
        mesh = UsdGeom.Mesh(prim)
        points = np.asarray(mesh.GetPointsAttr().Get() or [], dtype=np.float64)
        counts = [int(value) for value in (mesh.GetFaceVertexCountsAttr().Get() or [])]
        indices = [int(value) for value in (mesh.GetFaceVertexIndicesAttr().Get() or [])]
        faces = _triangulate_faces(counts, indices)
        if not len(points) or not len(faces):
            continue
        matrix = np.asarray(cache.GetLocalToWorldTransform(prim), dtype=np.float64)
        homogeneous = np.column_stack((points, np.ones(len(points), dtype=np.float64)))
        transformed = (homogeneous @ matrix)[:, :3]
        meshes.append(trimesh.Trimesh(vertices=transformed, faces=faces, process=False))
    if not meshes:
        raise ValueError("candidate USD contains no renderable mesh prims")
    return trimesh.util.concatenate(tuple(meshes))


def load_candidate_mesh(path: Path) -> trimesh.Trimesh:
    if path.suffix.lower() in {".usd", ".usda", ".usdc", ".usdz"}:
        return _load_usd_mesh(path)
    loaded = trimesh.load(path, force="scene", process=False)
    if isinstance(loaded, trimesh.Scene):
        geometries = [mesh for mesh in loaded.geometry.values() if len(mesh.vertices) and len(mesh.faces)]
        if not geometries:
            raise ValueError("candidate contains no mesh geometry")
        return trimesh.util.concatenate(tuple(geometries))
    if not isinstance(loaded, trimesh.Trimesh):
        raise ValueError(f"unsupported candidate geometry type: {type(loaded).__name__}")
    return loaded


def _format_validation(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    command: list[str] = []
    if suffix in {".glb", ".gltf"}:
        executable = shutil.which("gltf_validator") or shutil.which("gltf-validator")
        if executable:
            command = [executable, path.as_posix()]
    elif suffix in {".usd", ".usda", ".usdc", ".usdz"}:
        executable = shutil.which("usdchecker")
        if executable:
            command = [executable, path.as_posix()]
    if not command:
        return {"status": "not_available", "command": [], "returncode": None, "output": ""}
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "blocked", "command": command, "returncode": None, "output": str(exc)}
    output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()[-4000:]
    return {
        "status": "pass" if completed.returncode == 0 else "blocked",
        "command": command,
        "returncode": completed.returncode,
        "output": output,
    }


def inspect_mesh(
    path: Path,
    quality_policy: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], trimesh.Trimesh | None]:
    hard_failures: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {}
    policy = resolved_quality_policy(quality_policy)
    quality_checks: list[dict[str, Any]] = []
    quality_findings: list[dict[str, Any]] = []
    mesh: trimesh.Trimesh | None = None
    format_validation = _format_validation(path)
    if format_validation["status"] == "blocked":
        hard_failures.append("candidate failed its format validator")
    try:
        mesh = load_candidate_mesh(path)
    except Exception as exc:
        hard_failures.append(f"candidate mesh could not be loaded: {exc}")
    if mesh is not None:
        vertices = np.asarray(mesh.vertices)
        faces = np.asarray(mesh.faces)
        finite_vertices = bool(np.isfinite(vertices).all()) if vertices.size else False
        valid_indices = bool(faces.size and faces.min() >= 0 and faces.max() < len(vertices))
        bounds = np.asarray(mesh.bounds, dtype=np.float64) if len(vertices) else np.zeros((2, 3), dtype=np.float64)
        extents = bounds[1] - bounds[0]
        metrics = {
            "vertex_count": int(len(vertices)),
            "face_count": int(len(faces)),
            "topology_mode": "exact",
            "finite_vertices": finite_vertices,
            "valid_face_indices": valid_indices,
            "bounds_min": [float(value) for value in bounds[0]],
            "bounds_max": [float(value) for value in bounds[1]],
            "extents": [float(value) for value in extents],
        }
        if not len(vertices) or not len(faces):
            hard_failures.append("candidate mesh is empty")
        if not finite_vertices:
            hard_failures.append("candidate contains non-finite vertex coordinates")
        if not valid_indices:
            hard_failures.append("candidate contains invalid face indices")
        if not hard_failures:
            try:
                metrics.update(exact_mesh_metrics(mesh))
                metrics["euler_number"] = metrics["euler_characteristic"]
                metrics["self_intersection_checked"] = False
                metrics["self_intersecting_face_count"] = None
                quality_checks = mesh_quality_checks(metrics, policy)
                quality_findings = failed_quality_findings(quality_checks)
                warnings.append("self-intersection was not evaluated by the bounded early verifier")
            except (MemoryError, ValueError) as exc:
                hard_failures.append(f"exact topology analysis failed: {exc}")
    quality_failures = [str(item["description"]) for item in quality_findings]
    status = "blocked" if hard_failures or quality_failures else "warning" if warnings else "pass"
    return {
        "status": status,
        "tool": "exact-topology+trimesh+format-validator",
        "tool_versions": {"trimesh": trimesh.__version__, "numpy": np.__version__},
        "format_validation": format_validation,
        "metrics": metrics,
        "quality_policy": policy,
        "quality_checks": quality_checks,
        "quality_failures": quality_failures,
        "quality_findings": quality_findings,
        "hard_failures": hard_failures,
        "warnings": warnings,
    }, mesh


def _sample_faces(mesh: trimesh.Trimesh, limit: int = RENDER_FACE_LIMIT) -> np.ndarray:
    if len(mesh.faces) <= limit:
        return np.arange(len(mesh.faces))
    generator = np.random.default_rng(17)
    return np.sort(generator.choice(len(mesh.faces), size=limit, replace=False))


def _normal_colours(triangles: np.ndarray) -> np.ndarray:
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    normals = normals / np.maximum(lengths[:, None], 1e-12)
    colours = np.ones((len(triangles), 4), dtype=np.float64)
    colours[:, :3] = 0.15 + 0.85 * np.abs(normals)
    return colours


def _equalise_axes(axis: Any, bounds: np.ndarray) -> None:
    centre = bounds.mean(axis=0)
    radius = max(float(np.max(bounds[1] - bounds[0])) * 0.58, 1e-4)
    axis.set_xlim(centre[0] - radius, centre[0] + radius)
    axis.set_ylim(centre[1] - radius, centre[1] + radius)
    axis.set_zlim(centre[2] - radius, centre[2] + radius)
    axis.set_box_aspect((1, 1, 1))
    axis.set_axis_off()


def _render_contact_sheet(mesh: trimesh.Trimesh, target: Path, mode: str) -> None:
    face_indices = _sample_faces(mesh)
    triangles = np.asarray(mesh.vertices)[np.asarray(mesh.faces)[face_indices]]
    bounds = np.asarray(mesh.bounds, dtype=np.float64)
    views = [(-135, 20), (-90, 20), (-45, 20), (0, 20), (45, 20), (90, 20), (135, 20), (180, 20)]
    figure = plt.figure(figsize=(12, 6), dpi=120)
    for index, (azimuth, elevation) in enumerate(views, start=1):
        axis = figure.add_subplot(2, 4, index, projection="3d")
        collection = Poly3DCollection(triangles, antialiased=False)
        if mode == "wireframe":
            collection.set_facecolor((0.88, 0.90, 0.93, 1.0))
            collection.set_edgecolor((0.12, 0.17, 0.24, 0.8))
            collection.set_linewidth(0.12)
        elif mode == "normals":
            collection.set_facecolor(_normal_colours(triangles))
            collection.set_edgecolor((0.0, 0.0, 0.0, 0.0))
            collection.set_linewidth(0.0)
        else:
            collection.set_facecolor((0.56, 0.66, 0.80, 1.0))
            collection.set_edgecolor((0.0, 0.0, 0.0, 0.0))
            collection.set_linewidth(0.0)
        axis.add_collection3d(collection)
        _equalise_axes(axis, bounds)
        axis.view_init(elev=elevation, azim=azimuth)
        axis.set_title(f"az {azimuth}", fontsize=7)
    figure.tight_layout(pad=0.25)
    target.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(target, facecolor="white")
    plt.close(figure)


def _render_bundle(
    mesh: trimesh.Trimesh | None,
    output_dir: Path,
    candidate: Path | None = None,
    full_surface_backend_root: str | Path | None = None,
) -> dict[str, Any]:
    if mesh is None:
        return {"status": "blocked", "images": [], "blocked_reason": "candidate could not be rendered"}
    fallback_reason = ""
    if candidate is not None and full_surface_backend_root:
        script = Path(__file__).resolve().parents[3] / "scripts" / "reconstruction" / "mesh_review_full_surface.py"
        command = [
            sys.executable,
            script.as_posix(),
            "--backend-root",
            Path(full_surface_backend_root).resolve().as_posix(),
            "--mesh",
            candidate.as_posix(),
            "--output-dir",
            output_dir.as_posix(),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
            if completed.returncode == 0 and all((output_dir / name).is_file() for name in REVIEW_IMAGES):
                images = [
                    {"kind": kind, "uri": (output_dir / name).as_posix(), "checksum": sha256_file(output_dir / name)}
                    for kind, name in zip(("beauty", "wireframe", "normals"), REVIEW_IMAGES)
                ]
                return {
                    "status": "generated",
                    "images": images,
                    "camera_policy": "fixed-eight-view-full-surface-v1",
                    "renderer": "nvdiffrast-full-surface",
                    "seed": 17,
                }
            fallback_reason = ((completed.stderr or "") + "\n" + (completed.stdout or "")).strip()[-2000:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            fallback_reason = str(exc)
    modes = (("beauty", REVIEW_IMAGES[0]), ("wireframe", REVIEW_IMAGES[1]), ("normals", REVIEW_IMAGES[2]))
    images: list[dict[str, str]] = []
    try:
        for mode, name in modes:
            target = output_dir / name
            _render_contact_sheet(mesh, target, mode)
            images.append({"kind": mode, "uri": target.as_posix(), "checksum": sha256_file(target)})
            gc.collect()
    except Exception as exc:
        return {"status": "blocked", "images": images, "blocked_reason": str(exc)}
    return {
        "status": "generated",
        "images": images,
        "camera_policy": "fixed-eight-view-sampled-v1",
        "renderer": "matplotlib-bounded-sample",
        "fallback_reason": fallback_reason,
        "seed": 17,
    }


def _source_images(project_dir: Path, limit: int = 2) -> list[Path]:
    paths: list[Path] = []
    for pattern in (
        "source-assets/**/*.png",
        "source-assets/**/*.jpg",
        "source-assets/**/*.jpeg",
        "source-assets/**/*.webp",
    ):
        paths.extend(path for path in sorted(project_dir.glob(pattern)) if path.is_file())
    return paths[:limit]


def _selected_source_images(project_dir: Path, raw_paths: Any, limit: int = 2) -> list[Path]:
    selected: list[Path] = []
    if isinstance(raw_paths, list):
        for raw_path in raw_paths:
            try:
                path = resolve_within(project_dir, str(raw_path), must_exist=True)
            except (OSError, ValueError):
                continue
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                selected.append(path)
    return selected[:limit] or _source_images(project_dir, limit)


def _source_candidate_comparison(
    render_bundle: dict[str, Any],
    source_images: list[Path],
    output_dir: Path,
    asset_intent: str,
) -> Path | None:
    if not source_images:
        return None
    beauty_uri = next(
        (str(item.get("uri") or "") for item in render_bundle.get("images", []) if item.get("kind") == "beauty"),
        "",
    )
    beauty_path = Path(beauty_uri)
    if not beauty_path.is_file():
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "source-candidate-comparison.png"
    source_panel_size = (1560, 700)
    candidate_panel_size = (1560, 780)
    canvas = Image.new("RGB", (1600, 1650), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((20, 14), f"Intended foreground asset: {asset_intent}", fill="black")
    draw.text((20, 42), "SOURCE PHOTO: identify the named foreground object and exclude its surroundings", fill="black")
    draw.text((20, 820), "CANDIDATE MESH: eight full-surface views", fill="black")

    for position, panel_size, path in (
        ((20, 76), source_panel_size, source_images[0]),
        ((20, 850), candidate_panel_size, beauty_path),
    ):
        with Image.open(path) as raw:
            image = ImageOps.exif_transpose(raw).convert("RGB")
            image.thumbnail(panel_size, Image.Resampling.LANCZOS)
            panel = Image.new("RGB", panel_size, (238, 238, 238))
            panel.paste(image, ((panel.width - image.width) // 2, (panel.height - image.height) // 2))
            canvas.paste(panel, position)
    canvas.save(target, format="PNG", optimize=True)
    return target


def _json_object(content: str) -> dict[str, Any] | None:
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _blind_identity_check(
    provider: str,
    model: str,
    beauty_path: Path,
    expected_aliases: list[str],
    seed: int | None = None,
) -> dict[str, Any]:
    prompt = (
        "Look only at these eight views of an untextured 3D mesh. Do not infer an intended label. "
        "Name the dominant enclosing geometric object in plain terms. Return only one JSON object "
        "with keys dominant_object, encloses_another_object and reason."
    )
    last_error = "reviewer response was not valid JSON"
    completion = None
    payload: dict[str, Any] | None = None
    for _ in range(2):
        try:
            completion = complete_vision(
                provider,
                prompt,
                [beauty_path],
                model=model,
                max_tokens=256,
                seed=seed,
            )
        except Exception as error:
            last_error = str(error)
            continue
        payload = _json_object(completion.content)
        if payload and payload.get("dominant_object"):
            break
        prompt += " Return the JSON object now."
    if completion is None or payload is None or not payload.get("dominant_object"):
        return {
            "status": "unavailable",
            "reason": last_error,
            "expected_aliases": expected_aliases,
        }
    dominant_object = str(payload["dominant_object"]).strip()
    normalised = dominant_object.casefold()
    aliases = [str(alias).strip().casefold() for alias in expected_aliases if str(alias).strip()]
    matches = any(alias in normalised for alias in aliases)
    return {
        "status": "validated",
        "provider": completion.provider,
        "model": completion.model,
        "dominant_object": dominant_object,
        "encloses_another_object": bool(payload.get("encloses_another_object", False)),
        "reason": str(payload.get("reason") or ""),
        "expected_aliases": aliases,
        "matches_expected_identity": matches,
    }


def _attempt_counts(project_dir: Path, candidate_checksum: str, current_decision: str) -> dict[str, int]:
    history = project_dir / "reports" / "mesh-verification-history.jsonl"
    records: list[dict[str, Any]] = []
    if history.exists():
        for line in history.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    checksums = [str(item.get("candidate", {}).get("checksum") or "") for item in records]
    if candidate_checksum not in checksums:
        checksums.append(candidate_checksum)
    fixes = _read_json(project_dir / "reports" / "fix-attempts.json").get("attempts", [])
    local_fixes = [
        item
        for item in fixes
        if item.get("stage_id") == "mesh-verification"
        and item.get("action_kind") == "tool"
        and item.get("status") == "applied"
    ]
    resubmissions = [
        item
        for item in fixes
        if item.get("stage_id") == "mesh-verification"
        and item.get("action_kind") == "capability_fallback"
        and item.get("status") in {"completed", "applied", "executed"}
    ]
    previous_rejections = sum(1 for item in records if item.get("decision") != "approve")
    return {
        "inference_attempt": max(1, len(dict.fromkeys(checksums))),
        "review_attempt": len(records) + 1,
        "local_fix_count": len(local_fixes),
        "mesh_rejection_count": previous_rejections + (0 if current_decision == "approve" else 1),
        "inference_resubmission_count": len(resubmissions),
    }


def _base_record(
    project_dir: Path,
    asset_id: str,
    project_id: str,
    candidate: Path | None,
    diagnostics: dict[str, Any],
    render_bundle: dict[str, Any],
) -> dict[str, Any]:
    checksum = sha256_file(candidate) if candidate else "0" * 64
    policy_checksum = hashlib.sha256(
        json.dumps(diagnostics.get("quality_policy", {}), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "id": f"{asset_id or 'asset'}_mesh-verification",
        "version": "1.0",
        "status": "blocked",
        "asset_id": asset_id,
        "project_id": project_id,
        "stage_id": "mesh-verification",
        "candidate": {
            "path": candidate.as_posix() if candidate else "",
            "checksum": checksum,
            "format": candidate.suffix.lower().lstrip(".") if candidate else "unknown",
        },
        "diagnostics": diagnostics,
        "quality_policy_checksum": policy_checksum,
        "render_bundle": render_bundle,
        "decision": "review_required",
        "decision_reason": "mandatory mesh-verification agent approval is required",
        "review_status": "review_required",
        "findings": [],
        "reviewer": {"provider": "", "model": "", "role": "mesh_verifier"},
        "rubric_checksum": "",
        "provider_trace": [],
        "attempts": _attempt_counts(project_dir, checksum, "review_required"),
        "actions": [],
        "evidence": [],
        "promotion": {
            "approved": False,
            "candidate_checksum": checksum,
            "canonical_geometry_path": "",
            "canonical_geometry_checksum": "",
        },
        "raw_secrets_recorded": False,
        "extensions": {},
    }


def prepare_mesh_verification(
    project_dir: str | Path,
    asset_id: str = "",
    project_id: str = "",
    candidate_path: str | Path | None = None,
    quality_policy: dict[str, Any] | None = None,
    full_surface_backend_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(project_dir).resolve()
    output_dir = root / "reports" / "mesh-verification"
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate = discover_candidate(root, candidate_path)
    if candidate is None:
        policy = resolved_quality_policy(quality_policy)
        diagnostics = {
            "status": "blocked",
            "tool": "candidate-discovery",
            "metrics": {},
            "quality_policy": policy,
            "quality_checks": [],
            "quality_failures": [],
            "quality_findings": [],
            "hard_failures": ["candidate geometry was not found inside the project workspace"],
            "warnings": [],
        }
        mesh = None
    else:
        diagnostics, mesh = inspect_mesh(candidate, quality_policy)
    diagnostics_path = atomic_write_json(output_dir / "diagnostics.json", diagnostics)
    render_bundle = _render_bundle(mesh, output_dir, candidate, full_surface_backend_root)
    record = _base_record(root, asset_id, project_id, candidate, diagnostics, render_bundle)
    if candidate is not None:
        record["evidence"].append(
            {
                "evidence_id": "candidate_geometry",
                "kind": "candidate_geometry",
                "uri": _project_relative_uri(root, candidate),
                "checksum": sha256_file(candidate),
            }
        )
    record["evidence"].append(
        {
            "evidence_id": "mesh_diagnostics",
            "kind": "mesh_diagnostics",
            "uri": _project_relative_uri(root, diagnostics_path),
            "checksum": sha256_file(diagnostics_path),
        }
    )
    for index, image in enumerate(render_bundle.get("images", [])):
        record["evidence"].append(
            {
                "evidence_id": f"mesh_review_render_{index}",
                "kind": str(image["kind"]),
                "uri": _project_relative_uri(root, str(image["uri"])),
                "checksum": str(image["checksum"]),
            }
        )

    existing = _read_json(root / "manifests" / "mesh-verification-record.json")
    candidate_checksum = record["candidate"]["checksum"]
    approval_matches = (
        existing.get("decision") == "approve"
        and existing.get("review_status") == "approved"
        and existing.get("candidate", {}).get("checksum") == candidate_checksum
        and existing.get("promotion", {}).get("candidate_checksum") == candidate_checksum
        and existing.get("promotion", {}).get("approved") is True
        and existing.get("quality_policy_checksum") == record.get("quality_policy_checksum")
    )
    blocked_reasons = list(diagnostics.get("hard_failures", []))
    blocked_reasons.extend(str(reason) for reason in diagnostics.get("quality_failures", []))
    if render_bundle.get("status") != "generated":
        blocked_reasons.append(str(render_bundle.get("blocked_reason") or "diagnostic render bundle was not generated"))
    if not approval_matches:
        blocked_reasons.append(
            "mandatory mesh-verification agent approval is missing or stale for this candidate checksum"
        )
    if approval_matches and not blocked_reasons:
        record = existing
        record["gate_status"] = "pass"
        record["blocked_reasons"] = []
    else:
        record["gate_status"] = "blocked"
        record["blocked_reasons"] = list(dict.fromkeys(blocked_reasons))
    record["diagnostics_path"] = diagnostics_path.as_posix()
    return record


def _decision_from_review(
    review: dict[str, Any],
    hard_failures: list[str],
    quality_findings: list[dict[str, Any]],
) -> str:
    if hard_failures:
        return "regenerate" if review.get("verdict") == "revise" else "blocked"
    if quality_findings:
        actions = {str(item.get("recommended_action") or "") for item in quality_findings}
        return "regenerate" if "regenerate" in actions else "revise_local"
    verdict = str(review.get("verdict") or "skipped")
    requested = str(review.get("action") or "")
    tags = {str(item.get("defect_tag") or "") for item in review.get("findings", [])}
    if verdict == "approve":
        return "approve"
    if verdict == "skipped":
        return "blocked"
    if tags.intersection(REGENERATE_TAGS):
        return "regenerate"
    if verdict == "blocked":
        return "blocked"
    if requested in {"revise_local", "regenerate"}:
        return requested
    return "revise_local" if tags.intersection(LOCAL_FIX_TAGS) else "blocked"


def _write_verification_record(project_dir: Path, record: dict[str, Any]) -> tuple[Path, list[str]]:
    schema = load_schema("mesh-verification-record")
    errors = [error.message for error in jsonschema.Draft202012Validator(schema).iter_errors(record)]
    target = atomic_write_json(project_dir / "manifests" / "mesh-verification-record.json", record)
    history = project_dir / "reports" / "mesh-verification-history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n")
    return target, errors


def governance_mesh_verify(params: dict[str, Any]) -> ToolResult:
    project_raw = params.get("project")
    if not project_raw:
        return ToolResult(success=False, error="project is required", validation_status="blocked")
    root = Path(str(project_raw)).resolve()
    asset_id = str(params.get("asset_id") or "")
    project_id = str(params.get("project_id") or "")
    prepared = prepare_mesh_verification(
        root,
        asset_id,
        project_id,
        params.get("candidate_path"),
        params.get("quality_policy"),
        params.get("full_surface_backend_root"),
    )
    diagnostics = prepared["diagnostics"]
    hard_failures = list(diagnostics.get("hard_failures", []))
    quality_failures = list(diagnostics.get("quality_failures", []))
    quality_findings = list(diagnostics.get("quality_findings", []))
    source_images = _selected_source_images(root, params.get("source_image_paths"))
    asset_intent = str(params.get("asset_intent") or asset_id)
    comparison_path = _source_candidate_comparison(
        prepared["render_bundle"],
        source_images,
        root / "reports" / "mesh-verification",
        asset_intent,
    )
    if comparison_path:
        image_paths = [comparison_path.as_posix()]
    else:
        image_paths = [str(item["uri"]) for item in prepared["render_bundle"].get("images", [])]
        image_paths.extend(path.as_posix() for path in source_images)
    dry_run = bool(params.get("dry_run", True))
    blind_identity: dict[str, Any] = {}
    blind_identity_path: Path | None = None

    if dry_run or hard_failures or prepared["render_bundle"].get("status") != "generated":
        review: dict[str, Any] = {
            "verdict": "blocked",
            "verdict_reason": (
                "dry run cannot satisfy mandatory mesh verification"
                if dry_run
                else "; ".join(hard_failures) or "diagnostic render bundle was not generated"
            ),
            "findings": [],
            "reviewer": {"provider": "", "model": "", "role": "mesh_verifier"},
            "rubric_checksum": "",
            "provider_trace": [],
        }
    else:
        context = json.dumps(
            {
                "candidate": prepared["candidate"],
                "asset_intent": asset_intent,
                "deterministic_gate": {
                    "hard_failures": hard_failures,
                    "quality_failures": quality_failures,
                    "quality_check_statuses": {
                        str(item.get("id") or "unknown"): str(item.get("status") or "unknown")
                        for item in diagnostics.get("quality_checks", [])
                    },
                },
                "render_bundle": prepared["render_bundle"],
                "instruction": (
                    "Tool-reported hard failures and deterministic quality failures cannot be overridden. "
                    "First compare the intended foreground asset in the labelled source-candidate sheet. "
                    "A mesh dominated by source surroundings is a source_mismatch and must be regenerated. "
                    "Choose revise_local or regenerate for a failed quality gate and use local repair only "
                    "for structure-preserving changes."
                ),
            },
            indent=2,
            sort_keys=True,
        )
        result = governance_vlm_review(
            {
                "project": root.as_posix(),
                "stage_id": "mesh-verification",
                "asset_id": asset_id,
                "project_id": project_id,
                "dry_run": False,
                "attempt": int(params.get("attempt") or 0),
                "image_paths": image_paths,
                "stage_context": context,
                "provider": params.get("provider"),
                "model": params.get("model"),
                "temperature": params.get("temperature", 0.0),
                "seed": params.get("seed"),
            }
        )
        review = result.data
        expected_aliases = [str(item) for item in params.get("asset_aliases") or [] if str(item).strip()]
        if review.get("verdict") == "approve" and expected_aliases:
            beauty_path = next(
                (
                    Path(str(item["uri"]))
                    for item in prepared["render_bundle"].get("images", [])
                    if item.get("kind") == "beauty"
                ),
                None,
            )
            if beauty_path is None or not beauty_path.is_file():
                blind_identity = {
                    "status": "unavailable",
                    "reason": "blind identity evidence is missing",
                    "expected_aliases": expected_aliases,
                }
            else:
                blind_identity = _blind_identity_check(
                    str(params.get("provider") or "nvidia_nim"),
                    str(params.get("model") or ""),
                    beauty_path,
                    expected_aliases,
                    seed=params.get("seed"),
                )
            blind_identity_path = atomic_write_json(
                root / "reports" / "mesh-verification-blind-identity.json",
                blind_identity,
            )
            if blind_identity.get("status") != "validated":
                review["verdict"] = "skipped"
                review["action"] = "blocked"
                review["verdict_reason"] = "vision provider call failed: blind identity check unavailable"
            elif not blind_identity.get("matches_expected_identity"):
                review["verdict"] = "revise"
                review["action"] = "regenerate"
                review["verdict_reason"] = (
                    "blind identity check found dominant object "
                    f"'{blind_identity.get('dominant_object', 'unknown')}' instead of the intended asset"
                )
                review.setdefault("findings", []).append(
                    {
                        "finding_id": "blind_identity_source_mismatch",
                        "defect_tag": "source_mismatch",
                        "severity": "blocker",
                        "description": review["verdict_reason"],
                        "region": "entire candidate",
                        "suggested_fix_id": "",
                        "tag_in_vocabulary": True,
                    }
                )

    decision = _decision_from_review(review, hard_failures, quality_findings)
    checksum = prepared["candidate"]["checksum"]
    approved = decision == "approve" and not hard_failures and not quality_failures
    findings = [*quality_findings, *list(review.get("findings", []))]
    decision_reason = str(review.get("verdict_reason") or prepared.get("decision_reason") or "")
    if quality_failures:
        decision_reason = "deterministic mesh quality gate rejected candidate: " + "; ".join(quality_failures)
    record = prepared
    record.update(
        {
            "status": "validated" if approved else "blocked" if decision == "blocked" else "review_required",
            "decision": decision,
            "decision_reason": decision_reason,
            "review_status": "approved"
            if approved
            else "rejected"
            if decision in {"revise_local", "regenerate"}
            else "review_required",
            "findings": findings,
            "reviewer": {
                "provider": str(review.get("reviewer", {}).get("provider") or ""),
                "model": str(review.get("reviewer", {}).get("model") or ""),
                "role": "mesh_verifier",
            },
            "rubric_checksum": str(review.get("rubric_checksum") or ""),
            "provider_trace": list(review.get("provider_trace", [])),
            "extensions": {
                **dict(prepared.get("extensions", {})),
                **({"blind_identity_check": blind_identity} if blind_identity else {}),
            },
            "attempts": _attempt_counts(root, checksum, decision),
            "actions": [
                {
                    "recorded_at": _now(),
                    "decision": decision,
                    "candidate_checksum": checksum,
                    "defect_tags": sorted(
                        {
                            str(item.get("defect_tag") or "")
                            for item in findings
                            if item.get("defect_tag")
                        }
                    ),
                }
            ],
            "promotion": {
                "approved": approved,
                "candidate_checksum": checksum,
                "canonical_geometry_path": prepared["candidate"]["path"] if approved else "",
                "canonical_geometry_checksum": checksum if approved else "",
                "approved_at": _now() if approved else "",
            },
            "gate_status": "pass" if approved else "blocked",
            "blocked_reasons": []
            if approved
            else [decision_reason or f"mesh verifier requested {decision}"],
        }
    )
    target, errors = _write_verification_record(root, record)
    warnings = list(diagnostics.get("warnings", []))
    warnings.extend(
        f"{item.get('defect_tag', 'finding')}: {item.get('description', '')}" for item in record["findings"]
    )
    return ToolResult(
        success=approved and not errors,
        data=record,
        error="; ".join(errors) if errors else None,
        warnings=warnings,
        artefacts=[
            target.as_posix(),
            prepared["diagnostics_path"],
            *image_paths[:3],
            *([blind_identity_path.as_posix()] if blind_identity_path else []),
        ],
        proposals=[record],
        validation_status="validated"
        if approved and not errors
        else "blocked"
        if decision == "blocked" or errors
        else "review_required",
    )
