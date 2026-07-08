from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from pxr import Gf, Sdf, Usd, UsdGeom, Vt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Author a USD stage from PartCrafter GLB part meshes.")
    parser.add_argument("--parts-dir", required=True)
    parser.add_argument("--usd", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--checksums", required=True)
    parser.add_argument("--asset-id", default="asset")
    parser.add_argument("--min-component-area-ratio", type=float, default=0.002)
    parser.add_argument("--assembly-policy", default="", help="Optional JSON policy for role-aware cleanup and assembly transforms.")
    parser.add_argument("--max-part-faces", type=int, default=0, help="Optional post-split decimation target per authored part.")
    parser.add_argument("--decimation-aggression", type=int, default=7)
    parser.add_argument("--post-split-min-component-area-ratio", type=float, default=0.0)
    parser.add_argument("--post-split-max-components", type=int, default=0)
    parser.add_argument("--fill-holes", action="store_true", help="Try to cap simple mesh holes after pruning and splitting.")
    parser.add_argument("--fix-inversion", action="store_true", help="Try to repair inverted winding during mesh healing.")
    parser.add_argument("--cap-boundaries", action="store_true", help="Triangulate boundary loops left by semantic part splitting.")
    parser.add_argument("--max-cap-boundary-vertices", type=int, default=4096)
    return parser


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_mesh(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, force="scene")
    if isinstance(loaded, trimesh.Scene):
        geometries = [mesh for mesh in loaded.geometry.values() if len(mesh.vertices) and len(mesh.faces)]
        if not geometries:
            raise ValueError(f"no mesh geometry in {path}")
        mesh = trimesh.util.concatenate(tuple(geometries))
    else:
        mesh = loaded
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError(f"unsupported mesh payload in {path}")
    return mesh


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def boundary_edge_count(mesh: trimesh.Trimesh) -> int:
    if len(mesh.faces) == 0:
        return 0
    try:
        edge_use_count = np.bincount(mesh.edges_unique_inverse)
    except Exception:
        return 0
    return int((edge_use_count == 1).sum())


def cap_boundary_loops(mesh: trimesh.Trimesh, max_boundary_vertices: int = 4096) -> dict[str, Any]:
    before_boundary_edges = boundary_edge_count(mesh)
    if before_boundary_edges == 0 or len(mesh.faces) == 0:
        return {
            "status": "not_needed",
            "before_boundary_edges": before_boundary_edges,
            "after_boundary_edges": before_boundary_edges,
            "capped_loop_count": 0,
            "added_faces": 0,
        }
    try:
        edge_use_count = np.bincount(mesh.edges_unique_inverse)
        boundary_edges = np.asarray(mesh.edges_unique, dtype=np.int64)[edge_use_count == 1]
    except Exception as exc:
        return {
            "status": "failed",
            "reason": str(exc),
            "before_boundary_edges": before_boundary_edges,
            "after_boundary_edges": before_boundary_edges,
            "capped_loop_count": 0,
            "added_faces": 0,
        }
    adjacency: dict[int, set[int]] = {}
    for edge in boundary_edges:
        a, b = int(edge[0]), int(edge[1])
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    unvisited = set(adjacency)
    components: list[list[int]] = []
    while unvisited:
        start = unvisited.pop()
        stack = [start]
        component = [start]
        while stack:
            current = stack.pop()
            for neighbour in adjacency[current]:
                if neighbour in unvisited:
                    unvisited.remove(neighbour)
                    stack.append(neighbour)
                    component.append(neighbour)
        components.append(component)

    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    new_vertices: list[np.ndarray] = []
    new_faces: list[list[int]] = []
    skipped_components = 0
    for component in components:
        if len(component) < 3 or len(component) > max(3, int(max_boundary_vertices)):
            skipped_components += 1
            continue
        if any(len(adjacency[vertex]) != 2 for vertex in component):
            skipped_components += 1
            continue
        start = min(component)
        previous = -1
        current = start
        ordered: list[int] = []
        seen: set[int] = set()
        while True:
            ordered.append(current)
            seen.add(current)
            candidates = [item for item in sorted(adjacency[current]) if item != previous]
            if not candidates:
                break
            next_vertex = candidates[0]
            if next_vertex == start:
                break
            if next_vertex in seen:
                break
            previous, current = current, next_vertex
            if len(ordered) > len(component):
                break
        if len(ordered) != len(component):
            skipped_components += 1
            continue
        centre = vertices[ordered].mean(axis=0)
        centre_index = len(vertices) + len(new_vertices)
        new_vertices.append(centre)
        for index, vertex in enumerate(ordered):
            next_vertex = ordered[(index + 1) % len(ordered)]
            new_faces.append([int(vertex), int(next_vertex), int(centre_index)])

    if new_faces:
        mesh.vertices = np.vstack([vertices, np.asarray(new_vertices, dtype=np.float64)])
        mesh.faces = np.vstack([np.asarray(mesh.faces, dtype=np.int64), np.asarray(new_faces, dtype=np.int64)])
        mesh.remove_unreferenced_vertices()
        trimesh.repair.fix_normals(mesh, multibody=True)
    after_boundary_edges = boundary_edge_count(mesh)
    return {
        "status": "applied" if new_faces else "skipped",
        "before_boundary_edges": before_boundary_edges,
        "after_boundary_edges": after_boundary_edges,
        "boundary_component_count": len(components),
        "capped_loop_count": len(new_vertices),
        "skipped_component_count": skipped_components,
        "added_faces": len(new_faces),
    }


def heal_mesh(
    mesh: trimesh.Trimesh,
    *,
    fill_holes: bool = False,
    fix_inversion: bool = False,
    cap_boundaries: bool = False,
    max_cap_boundary_vertices: int = 4096,
) -> dict[str, Any]:
    actions: list[str] = []
    before_watertight = bool(mesh.is_watertight) if len(mesh.faces) else False
    before_boundary_edges = boundary_edge_count(mesh)
    mesh.remove_unreferenced_vertices()
    actions.append("remove_unreferenced_vertices")
    if hasattr(mesh, "merge_vertices"):
        mesh.merge_vertices()
        actions.append("merge_vertices")
    if hasattr(mesh, "nondegenerate_faces"):
        mesh.update_faces(mesh.nondegenerate_faces())
        actions.append("remove_degenerate_faces")
    if hasattr(mesh, "unique_faces"):
        mesh.update_faces(mesh.unique_faces())
        actions.append("remove_duplicate_faces")
    if fix_inversion and hasattr(trimesh.repair, "fix_inversion"):
        trimesh.repair.fix_inversion(mesh, multibody=True)
        actions.append("fix_inversion")
    trimesh.repair.fix_normals(mesh, multibody=True)
    actions.append("fix_normals")
    fill_holes_result = None
    if fill_holes:
        fill_holes_result = bool(trimesh.repair.fill_holes(mesh))
        actions.append("fill_holes")
        trimesh.repair.fix_normals(mesh, multibody=True)
        actions.append("fix_normals_after_fill_holes")
    boundary_cap_report = None
    if cap_boundaries:
        boundary_cap_report = cap_boundary_loops(mesh, max_cap_boundary_vertices)
        actions.append("cap_boundary_loops")
    mesh.remove_unreferenced_vertices()
    return {
        "actions": actions,
        "fill_holes_requested": bool(fill_holes),
        "fill_holes_result": fill_holes_result,
        "fix_inversion_requested": bool(fix_inversion),
        "cap_boundaries_requested": bool(cap_boundaries),
        "boundary_cap_report": boundary_cap_report,
        "before_watertight": before_watertight,
        "after_watertight": bool(mesh.is_watertight) if len(mesh.faces) else False,
        "before_boundary_edges": before_boundary_edges,
        "after_boundary_edges": boundary_edge_count(mesh),
    }


def prune_components(mesh: trimesh.Trimesh, min_area_ratio: float) -> tuple[trimesh.Trimesh, dict[str, Any]]:
    components = list(mesh.split(only_watertight=False))
    if len(components) <= 1:
        return mesh, {"component_count": len(components), "kept_count": len(components), "removed_count": 0}
    areas = np.array([component.area for component in components], dtype=np.float64)
    largest = float(areas.max(initial=0.0))
    threshold = largest * max(0.0, min_area_ratio)
    kept = [component for component, area in zip(components, areas) if float(area) >= threshold]
    if not kept:
        kept = [components[int(np.argmax(areas))]]
    pruned = trimesh.util.concatenate(tuple(kept)) if len(kept) > 1 else kept[0]
    return pruned, {
        "component_count": len(components),
        "kept_count": len(kept),
        "removed_count": len(components) - len(kept),
        "largest_area": largest,
        "area_threshold": threshold,
    }


def prune_components_by_policy(mesh: trimesh.Trimesh, policy: dict[str, Any]) -> tuple[trimesh.Trimesh, dict[str, Any]]:
    components = list(mesh.split(only_watertight=False))
    if len(components) <= 1:
        return mesh, {"component_count": len(components), "kept_count": len(components), "removed_count": 0, "policy": "unchanged"}
    areas = np.array([component.area for component in components], dtype=np.float64)
    order = np.argsort(areas)[::-1]
    largest = float(areas[order[0]]) if len(order) else 0.0
    ratio = policy.get("min_component_area_ratio")
    max_components = policy.get("max_components")
    if ratio is not None:
        threshold = largest * max(0.0, float(ratio))
        kept_indices = [int(index) for index in order if float(areas[index]) >= threshold]
    else:
        threshold = 0.0
        kept_indices = [int(index) for index in order]
    if max_components is not None:
        kept_indices = kept_indices[: max(1, int(max_components))]
    if not kept_indices:
        kept_indices = [int(order[0])]
    kept = [components[index] for index in kept_indices]
    pruned = trimesh.util.concatenate(tuple(kept)) if len(kept) > 1 else kept[0]
    return pruned, {
        "component_count": len(components),
        "kept_count": len(kept),
        "removed_count": len(components) - len(kept),
        "largest_area": largest,
        "area_threshold": threshold,
        "kept_component_indices": kept_indices,
        "policy": "role_aware_component_filter",
    }


def split_component_policy(min_component_area_ratio: float, max_components: int) -> dict[str, Any]:
    policy: dict[str, Any] = {}
    if min_component_area_ratio > 0.0:
        policy["min_component_area_ratio"] = float(min_component_area_ratio)
    if max_components > 0:
        policy["max_components"] = int(max_components)
    return policy


def prune_split_components(
    mesh: trimesh.Trimesh,
    min_component_area_ratio: float,
    max_components: int,
    *,
    fill_holes: bool = False,
    fix_inversion: bool = False,
    cap_boundaries: bool = False,
    max_cap_boundary_vertices: int = 4096,
) -> tuple[trimesh.Trimesh, dict[str, Any]]:
    policy = split_component_policy(min_component_area_ratio, max_components)
    if not policy:
        component_count = len(list(mesh.split(only_watertight=False)))
        healing = (
            heal_mesh(
                mesh,
                fill_holes=fill_holes,
                fix_inversion=fix_inversion,
                cap_boundaries=cap_boundaries,
                max_cap_boundary_vertices=max_cap_boundary_vertices,
            )
            if (fill_holes or fix_inversion or cap_boundaries)
            else None
        )
        return mesh, {
            "status": "not_requested",
            "component_count": component_count,
            "kept_count": component_count,
            "removed_count": 0,
            "min_component_area_ratio": float(min_component_area_ratio),
            "max_components": int(max_components),
            "healing": healing,
        }
    pruned, report = prune_components_by_policy(mesh, policy)
    healing = heal_mesh(
        pruned,
        fill_holes=fill_holes,
        fix_inversion=fix_inversion,
        cap_boundaries=cap_boundaries,
        max_cap_boundary_vertices=max_cap_boundary_vertices,
    )
    report["status"] = "applied" if int(report.get("removed_count", 0)) else "unchanged"
    report["min_component_area_ratio"] = float(min_component_area_ratio)
    report["max_components"] = int(max_components)
    report["healing"] = healing
    return pruned, report


def apply_assembly_policy(mesh: trimesh.Trimesh, part_id: str, policy: dict[str, Any]) -> tuple[trimesh.Trimesh | None, dict[str, Any]]:
    part_policy = policy.get("parts", {}).get(part_id, {})
    if not part_policy:
        return mesh, {"status": "not_requested"}
    if part_policy.get("action") == "drop":
        return None, {"status": "dropped", "reason": part_policy.get("reason", "")}
    actions: list[dict[str, Any]] = []
    if any(key in part_policy for key in ("min_component_area_ratio", "max_components")):
        mesh, pruning = prune_components_by_policy(mesh, part_policy)
        actions.append({"action": "component_filter", **pruning})
    translate = part_policy.get("translate")
    if translate:
        vector = np.array([float(value) for value in translate], dtype=np.float64)
        if vector.shape != (3,):
            raise ValueError(f"assembly policy translate for {part_id} must contain three values")
        mesh.apply_translation(vector)
        actions.append({"action": "translate", "vector": vector.tolist()})
    return mesh, {"status": "applied", "actions": actions, "reason": part_policy.get("reason", "")}


def cylindrical_uv(mesh: trimesh.Trimesh) -> np.ndarray:
    vertices = np.asarray(mesh.vertices, dtype=np.float32)
    min_y = float(vertices[:, 1].min(initial=0.0))
    height = max(float(vertices[:, 1].max(initial=0.0)) - min_y, 1e-6)
    u = (np.arctan2(vertices[:, 2], vertices[:, 0]) / math.tau + 0.5) % 1.0
    v = (vertices[:, 1] - min_y) / height
    return np.stack([u, v], axis=1).astype(np.float32)


def indexed_face_varying_uv(mesh: trimesh.Trimesh) -> tuple[np.ndarray, np.ndarray]:
    uv = cylindrical_uv(mesh)
    indices = np.asarray(mesh.faces, dtype=np.int32).reshape(-1)
    return uv, indices


def face_varying_uv(mesh: trimesh.Trimesh) -> np.ndarray:
    uv = cylindrical_uv(mesh)
    face_indices = np.asarray(mesh.faces, dtype=np.int32).reshape(-1)
    return uv[face_indices]


def set_extent(mesh_prim: UsdGeom.Mesh, bounds: np.ndarray) -> None:
    extent = Vt.Vec3fArray(
        [
            Gf.Vec3f(float(bounds[0][0]), float(bounds[0][1]), float(bounds[0][2])),
            Gf.Vec3f(float(bounds[1][0]), float(bounds[1][1]), float(bounds[1][2])),
        ]
    )
    mesh_prim.CreateExtentAttr(extent)


def author_mesh(stage: Usd.Stage, mesh: trimesh.Trimesh, prim_path: str, part_id: str, segment_id: str = "") -> None:
    mesh_prim = UsdGeom.Mesh.Define(stage, prim_path)
    vertices = np.asarray(mesh.vertices, dtype=np.float32)
    faces = np.asarray(mesh.faces, dtype=np.int32)
    face_counts = np.full((len(faces),), 3, dtype=np.int32)
    mesh_prim.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(vertices))
    mesh_prim.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(face_counts))
    mesh_prim.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(faces.reshape(-1)))
    mesh_prim.CreateSubdivisionSchemeAttr().Set(UsdGeom.Tokens.none)
    if len(mesh.vertex_normals) == len(mesh.vertices):
        normals = np.asarray(mesh.vertex_normals, dtype=np.float32)
        mesh_prim.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(normals))
        mesh_prim.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
    uv = face_varying_uv(mesh)
    primvar = UsdGeom.PrimvarsAPI(mesh_prim).CreatePrimvar(
        "st",
        Sdf.ValueTypeNames.TexCoord2fArray,
        UsdGeom.Tokens.faceVarying,
    )
    primvar.Set(Vt.Vec2fArray.FromNumpy(uv))
    set_extent(mesh_prim, np.asarray(mesh.bounds, dtype=np.float32))
    prim = mesh_prim.GetPrim()
    prim.CreateAttribute("assetFactory:partId", Sdf.ValueTypeNames.String).Set(part_id)
    if segment_id:
        prim.CreateAttribute("assetFactory:segmentId", Sdf.ValueTypeNames.String).Set(segment_id)


def split_mesh_regions(mesh: trimesh.Trimesh, part_id: str, part_policy: dict[str, Any]) -> list[dict[str, Any]]:
    regions = part_policy.get("split_regions", [])
    if not regions:
        return [
            {
                "part_id": part_id,
                "segment_id": part_policy.get("segment_id", ""),
                "quality_exemptions": list(part_policy.get("quality_exemptions", [])),
                "mesh": mesh,
                "split_rule": "none",
                "post_split_min_component_area_ratio": part_policy.get("post_split_min_component_area_ratio"),
                "post_split_max_components": part_policy.get("post_split_max_components"),
                "max_part_faces": part_policy.get("max_part_faces"),
                "decimation_aggression": part_policy.get("decimation_aggression"),
            }
        ]
    centres = np.asarray(mesh.triangles_center, dtype=np.float64)
    if len(centres) == 0:
        return []
    centre_xz = part_policy.get("centre_xz")
    if centre_xz:
        cx, cz = float(centre_xz[0]), float(centre_xz[1])
    else:
        cx, cz = float(np.median(centres[:, 0])), float(np.median(centres[:, 2]))
    radial = np.sqrt((centres[:, 0] - cx) ** 2 + (centres[:, 2] - cz) ** 2)
    assigned = np.zeros(len(centres), dtype=bool)
    outputs: list[dict[str, Any]] = []
    for region in regions:
        rule = str(region.get("rule", "remaining"))
        if rule == "remaining":
            mask = ~assigned
        elif rule == "radial_gt":
            mask = radial > float(region.get("radius", 0.0))
            if "y_min" in region:
                mask &= centres[:, 1] >= float(region["y_min"])
            if "y_max" in region:
                mask &= centres[:, 1] <= float(region["y_max"])
            mask &= ~assigned
        elif rule == "y_outside":
            mask = (centres[:, 1] < float(region.get("y_min", -float("inf")))) | (
                centres[:, 1] > float(region.get("y_max", float("inf")))
            )
            mask &= ~assigned
        else:
            raise ValueError(f"unsupported split rule for {part_id}: {rule}")
        face_indices = np.flatnonzero(mask)
        if len(face_indices) == 0:
            continue
        submesh = mesh.submesh([face_indices], append=True, repair=False)
        if not isinstance(submesh, trimesh.Trimesh) or len(submesh.faces) == 0:
            continue
        suffix = str(region.get("suffix", region.get("segment_id", "region"))).strip() or "region"
        child_part_id = f"{part_id}_{suffix}"
        outputs.append(
            {
                "part_id": child_part_id,
                "source_part_id": part_id,
                "segment_id": str(region.get("segment_id", "")),
                "quality_exemptions": list(region.get("quality_exemptions", part_policy.get("quality_exemptions", []))),
                "mesh": submesh,
                "split_rule": rule,
                "face_count": int(len(face_indices)),
                "centre_xz": [cx, cz],
                "post_split_min_component_area_ratio": region.get(
                    "post_split_min_component_area_ratio",
                    part_policy.get("post_split_min_component_area_ratio"),
                ),
                "post_split_max_components": region.get(
                    "post_split_max_components",
                    part_policy.get("post_split_max_components"),
                ),
                "max_part_faces": region.get("max_part_faces", part_policy.get("max_part_faces")),
                "decimation_aggression": region.get("decimation_aggression", part_policy.get("decimation_aggression")),
            }
        )
        assigned[face_indices] = True
    return outputs


def split_pruning_controls(
    split_record: dict[str, Any],
    default_min_component_area_ratio: float,
    default_max_components: int,
) -> tuple[float, int]:
    min_component_area_ratio = split_record.get("post_split_min_component_area_ratio")
    max_components = split_record.get("post_split_max_components")
    if min_component_area_ratio is None:
        min_component_area_ratio = default_min_component_area_ratio
    if max_components is None:
        max_components = default_max_components
    return float(min_component_area_ratio), int(max_components)


def split_decimation_controls(
    split_record: dict[str, Any],
    default_max_part_faces: int,
    default_decimation_aggression: int,
) -> tuple[int, int]:
    max_part_faces = split_record.get("max_part_faces")
    decimation_aggression = split_record.get("decimation_aggression")
    if max_part_faces is None:
        max_part_faces = default_max_part_faces
    if decimation_aggression is None:
        decimation_aggression = default_decimation_aggression
    return int(max_part_faces), int(decimation_aggression)


def decimate_mesh(
    mesh: trimesh.Trimesh,
    max_faces: int,
    aggression: int,
    *,
    fill_holes: bool = False,
    fix_inversion: bool = False,
    cap_boundaries: bool = False,
    max_cap_boundary_vertices: int = 4096,
) -> tuple[trimesh.Trimesh, dict[str, Any]]:
    target_faces = max(0, int(max_faces))
    before_faces = int(len(mesh.faces))
    if target_faces <= 0 or before_faces <= target_faces:
        return mesh, {"status": "not_requested", "before_faces": before_faces, "after_faces": before_faces}
    try:
        decimated = mesh.simplify_quadric_decimation(face_count=target_faces, aggression=max(0, int(aggression)))
    except Exception as exc:
        return mesh, {
            "status": "failed",
            "before_faces": before_faces,
            "after_faces": before_faces,
            "target_faces": target_faces,
            "error": str(exc),
        }
    if not isinstance(decimated, trimesh.Trimesh) or len(decimated.faces) == 0:
        return mesh, {
            "status": "failed",
            "before_faces": before_faces,
            "after_faces": before_faces,
            "target_faces": target_faces,
            "error": "decimation returned no mesh",
        }
    healing = heal_mesh(
        decimated,
        fill_holes=fill_holes,
        fix_inversion=fix_inversion,
        cap_boundaries=cap_boundaries,
        max_cap_boundary_vertices=max_cap_boundary_vertices,
    )
    return decimated, {
        "status": "applied",
        "before_faces": before_faces,
        "after_faces": int(len(decimated.faces)),
        "target_faces": target_faces,
        "aggression": max(0, int(aggression)),
        "healing": healing,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    parts_dir = Path(args.parts_dir).resolve()
    usd_path = Path(args.usd).resolve()
    manifest_path = Path(args.manifest).resolve()
    report_path = Path(args.report).resolve()
    checksums_path = Path(args.checksums).resolve()
    assembly_policy_path = Path(args.assembly_policy).resolve() if args.assembly_policy else None
    if assembly_policy_path and not assembly_policy_path.exists():
        raise SystemExit(f"assembly policy does not exist: {assembly_policy_path}")
    assembly_policy = load_json(assembly_policy_path) if assembly_policy_path and assembly_policy_path.exists() else {}
    part_paths = sorted(parts_dir.glob("part_*.glb"))
    if not part_paths:
        raise SystemExit(f"no part_*.glb files found in {parts_dir}")

    usd_path.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(usd_path))
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    UsdGeom.Xform.Define(stage, "/World/Geometry")

    part_records: list[dict[str, Any]] = []
    for path in part_paths:
        part_id = path.stem
        mesh = load_mesh(path)
        before = {
            "vertices": int(len(mesh.vertices)),
            "faces": int(len(mesh.faces)),
            "surface_area": float(mesh.area),
            "bounds": np.asarray(mesh.bounds).tolist(),
        }
        healing = heal_mesh(mesh, fill_holes=bool(args.fill_holes), fix_inversion=bool(args.fix_inversion))
        mesh, pruning = prune_components(mesh, float(args.min_component_area_ratio))
        mesh, assembly = apply_assembly_policy(mesh, part_id, assembly_policy)
        if mesh is None:
            part_records.append(
                {
                    "part_id": part_id,
                    "asset_path": path.as_posix(),
                    "kind": "part_mesh",
                    "prim_path": "",
                    "before": before,
                    "after": {"vertices": 0, "faces": 0, "surface_area": 0.0, "bounds": []},
                    "healing": healing,
                    "component_pruning": pruning,
                    "assembly_policy": assembly,
                    "status": "dropped",
                }
            )
            continue
        post_assembly_healing = heal_mesh(mesh, fill_holes=bool(args.fill_holes), fix_inversion=bool(args.fix_inversion))
        split_records = split_mesh_regions(mesh, part_id, assembly_policy.get("parts", {}).get(part_id, {}))
        for split_record in split_records:
            split_mesh = split_record["mesh"]
            split_min_area_ratio, split_max_components = split_pruning_controls(
                split_record,
                float(args.post_split_min_component_area_ratio),
                int(args.post_split_max_components),
            )
            split_max_part_faces, split_decimation_aggression = split_decimation_controls(
                split_record,
                int(args.max_part_faces),
                int(args.decimation_aggression),
            )
            split_mesh, post_split_pruning = prune_split_components(
                split_mesh,
                split_min_area_ratio,
                split_max_components,
                fill_holes=bool(args.fill_holes),
                fix_inversion=bool(args.fix_inversion),
                cap_boundaries=bool(args.cap_boundaries),
                max_cap_boundary_vertices=int(args.max_cap_boundary_vertices),
            )
            split_mesh, decimation = decimate_mesh(
                split_mesh,
                split_max_part_faces,
                split_decimation_aggression,
                fill_holes=bool(args.fill_holes),
                fix_inversion=bool(args.fix_inversion),
                cap_boundaries=bool(args.cap_boundaries),
                max_cap_boundary_vertices=int(args.max_cap_boundary_vertices),
            )
            split_mesh, post_decimation_pruning = prune_split_components(
                split_mesh,
                split_min_area_ratio,
                split_max_components,
                fill_holes=bool(args.fill_holes),
                fix_inversion=bool(args.fix_inversion),
                cap_boundaries=bool(args.cap_boundaries),
                max_cap_boundary_vertices=int(args.max_cap_boundary_vertices),
            )
            split_part_id = split_record["part_id"]
            prim_path = f"/World/Geometry/{split_part_id}"
            segment_id = str(split_record.get("segment_id", ""))
            author_mesh(stage, split_mesh, prim_path, split_part_id, segment_id)
            part_records.append(
                {
                    "part_id": split_part_id,
                    "source_part_id": split_record.get("source_part_id", part_id),
                    "segment_id": segment_id,
                    "quality_exemptions": list(split_record.get("quality_exemptions", [])),
                    "asset_path": path.as_posix(),
                    "kind": "part_mesh",
                    "prim_path": prim_path,
                    "before": before,
                    "after": {
                        "vertices": int(len(split_mesh.vertices)),
                        "faces": int(len(split_mesh.faces)),
                        "surface_area": float(split_mesh.area),
                        "bounds": np.asarray(split_mesh.bounds).tolist(),
                    },
                    "healing": {"source": healing, "post_assembly": post_assembly_healing},
                    "component_pruning": pruning,
                    "assembly_policy": assembly,
                    "split": {
                        "rule": split_record.get("split_rule", "none"),
                        "face_count": split_record.get("face_count", int(decimation.get("before_faces", len(split_mesh.faces)))),
                        "centre_xz": split_record.get("centre_xz", []),
                    },
                    "decimation": decimation,
                    "decimation_controls": {
                        "max_part_faces": split_max_part_faces,
                        "decimation_aggression": split_decimation_aggression,
                    },
                    "post_split_component_pruning": post_split_pruning,
                    "post_decimation_component_pruning": post_decimation_pruning,
                    "status": "authored",
                }
            )

    stage.GetRootLayer().Save()
    manifest = {
        "id": f"{args.asset_id}_usd_parts_manifest",
        "version": "1.0",
        "status": "proposal",
        "asset_id": args.asset_id,
        "usd": usd_path.as_posix(),
        "part_root": "/World/Geometry",
        "assembly_policy": assembly_policy_path.as_posix() if assembly_policy_path else "",
        "parts": part_records,
    }
    report = {
        "id": f"{args.asset_id}_usd_authoring_report",
        "status": "pass",
        "asset_id": args.asset_id,
        "source_parts_dir": parts_dir.as_posix(),
        "usd": usd_path.as_posix(),
        "assembly_policy": assembly_policy_path.as_posix() if assembly_policy_path else "",
        "part_count": len([item for item in part_records if item.get("status") != "dropped"]),
        "dropped_part_count": len([item for item in part_records if item.get("status") == "dropped"]),
        "total_faces": sum(item["after"]["faces"] for item in part_records),
        "total_vertices": sum(item["after"]["vertices"] for item in part_records),
        "max_part_faces": int(args.max_part_faces),
        "decimation_aggression": int(args.decimation_aggression),
        "post_split_min_component_area_ratio": float(args.post_split_min_component_area_ratio),
        "post_split_max_components": int(args.post_split_max_components),
        "fill_holes": bool(args.fill_holes),
        "fix_inversion": bool(args.fix_inversion),
        "cap_boundaries": bool(args.cap_boundaries),
        "max_cap_boundary_vertices": int(args.max_cap_boundary_vertices),
        "parts": part_records,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    checksums_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    report_path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    checksum_files = [usd_path, manifest_path, report_path]
    if assembly_policy_path:
        checksum_files.append(assembly_policy_path)
    checksums_path.write_text(
        json.dumps(
            {
                "files": [
                    {"path": path.as_posix(), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
                    for path in checksum_files
                ]
            },
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "pass",
                "usd": usd_path.as_posix(),
                "manifest": manifest_path.as_posix(),
                "part_count": report["part_count"],
                "dropped_part_count": report["dropped_part_count"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
