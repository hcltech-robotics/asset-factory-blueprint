from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from pxr import Usd, UsdGeom, Vt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Condition selected semantic mesh segments in a USD stage.")
    parser.add_argument("--usd", required=True)
    parser.add_argument("--output-usd", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--checksums", required=True)
    parser.add_argument("--asset-id", default="asset")
    parser.add_argument("--selector-segment", action="append", default=[])
    parser.add_argument("--selector-prim", action="append", default=[])
    parser.add_argument("--smooth-iterations", type=int, default=0)
    parser.add_argument("--smooth-lambda", type=float, default=0.25)
    parser.add_argument("--max-displacement-m", type=float, default=0.0)
    parser.add_argument("--radial-regularise-segment", action="append", default=[])
    parser.add_argument("--radial-strength", type=float, default=0.0)
    parser.add_argument("--radial-bins", type=int, default=24)
    parser.add_argument("--flatten-top-segment", action="append", default=[])
    parser.add_argument("--flatten-top-strength", type=float, default=0.0)
    parser.add_argument("--flatten-top-quantile", type=float, default=0.82)
    parser.add_argument("--prune-components-segment", action="append", default=[])
    parser.add_argument("--prune-components-prim", action="append", default=[])
    parser.add_argument("--prune-max-components", type=int, default=0)
    parser.add_argument("--prune-min-component-area-ratio", type=float, default=0.0)
    parser.add_argument("--prune-long-edge-segment", action="append", default=[])
    parser.add_argument("--prune-long-edge-prim", action="append", default=[])
    parser.add_argument("--prune-max-edge-m", type=float, default=0.0)
    parser.add_argument("--prune-max-aspect-ratio", type=float, default=0.0)
    return parser


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def segment_id(prim: Usd.Prim) -> str:
    attr = prim.GetAttribute("assetFactory:segmentId")
    if attr:
        value = attr.Get()
        if value:
            return str(value)
    return ""


def selected(prim: Usd.Prim, selectors: set[str], segments: set[str]) -> bool:
    return str(prim.GetPath()) in selectors or segment_id(prim) in segments


def mesh_from_prim(prim: Usd.Prim) -> tuple[trimesh.Trimesh, np.ndarray]:
    mesh = UsdGeom.Mesh(prim)
    points = np.asarray(mesh.GetPointsAttr().Get(), dtype=np.float64)
    counts = np.asarray(mesh.GetFaceVertexCountsAttr().Get(), dtype=np.int64)
    indices = np.asarray(mesh.GetFaceVertexIndicesAttr().Get(), dtype=np.int64)
    if len(points) == 0 or len(counts) == 0 or len(indices) == 0:
        return trimesh.Trimesh(vertices=points, faces=np.empty((0, 3), dtype=np.int64), process=False), counts
    faces: list[list[int]] = []
    cursor = 0
    for count in counts:
        face = indices[cursor : cursor + int(count)].tolist()
        cursor += int(count)
        if len(face) == 3:
            faces.append(face)
        elif len(face) > 3:
            first = face[0]
            for offset in range(1, len(face) - 1):
                faces.append([first, face[offset], face[offset + 1]])
    return trimesh.Trimesh(vertices=points.copy(), faces=np.asarray(faces, dtype=np.int64), process=False), counts


def clamp_displacement(original: np.ndarray, conditioned: np.ndarray, max_displacement: float) -> np.ndarray:
    if max_displacement <= 0.0:
        return conditioned
    displacement = conditioned - original
    lengths = np.linalg.norm(displacement, axis=1)
    scale = np.ones(len(lengths), dtype=np.float64)
    mask = lengths > max_displacement
    scale[mask] = max_displacement / np.maximum(lengths[mask], 1e-12)
    return original + displacement * scale[:, None]


def apply_smooth(mesh: trimesh.Trimesh, iterations: int, lamb: float, max_displacement: float) -> dict[str, Any]:
    if iterations <= 0 or len(mesh.vertices) == 0 or len(mesh.faces) == 0:
        return {"status": "not_requested"}
    original = mesh.vertices.copy()
    trimesh.smoothing.filter_laplacian(
        mesh,
        lamb=float(lamb),
        iterations=max(1, int(iterations)),
        volume_constraint=False,
    )
    mesh.vertices = clamp_displacement(original, mesh.vertices, float(max_displacement))
    displacement = np.linalg.norm(mesh.vertices - original, axis=1)
    return {
        "status": "applied",
        "iterations": max(1, int(iterations)),
        "lambda": float(lamb),
        "max_displacement_limit_m": float(max_displacement),
        "max_displacement_m": float(displacement.max(initial=0.0)),
        "mean_displacement_m": float(displacement.mean() if len(displacement) else 0.0),
    }


def apply_radial_regularise(mesh: trimesh.Trimesh, strength: float, bins: int, max_displacement: float) -> dict[str, Any]:
    strength = max(0.0, min(1.0, float(strength)))
    if strength <= 0.0 or len(mesh.vertices) == 0:
        return {"status": "not_requested"}
    original = mesh.vertices.copy()
    vertices = original.copy()
    centre_xz = np.median(vertices[:, [0, 2]], axis=0)
    y_values = vertices[:, 1]
    y_min = float(y_values.min(initial=0.0))
    y_max = float(y_values.max(initial=0.0))
    if y_max <= y_min:
        return {"status": "skipped", "reason": "zero height segment"}
    bin_count = max(1, int(bins))
    bin_index = np.clip(((y_values - y_min) / max(y_max - y_min, 1e-12) * bin_count).astype(int), 0, bin_count - 1)
    changed_vertices = 0
    for index in range(bin_count):
        mask = bin_index == index
        if int(mask.sum()) < 8:
            continue
        offset = vertices[mask][:, [0, 2]] - centre_xz
        radius = np.linalg.norm(offset, axis=1)
        valid = radius > 1e-8
        if int(valid.sum()) < 8:
            continue
        target = float(np.median(radius[valid]))
        scale = ((1.0 - strength) * radius + strength * target) / np.maximum(radius, 1e-12)
        updated = centre_xz + offset * scale[:, None]
        vertices[mask, 0] = updated[:, 0]
        vertices[mask, 2] = updated[:, 1]
        changed_vertices += int(mask.sum())
    vertices = clamp_displacement(original, vertices, float(max_displacement))
    mesh.vertices = vertices
    displacement = np.linalg.norm(mesh.vertices - original, axis=1)
    return {
        "status": "applied" if changed_vertices else "skipped",
        "strength": strength,
        "bins": bin_count,
        "changed_vertices": changed_vertices,
        "max_displacement_limit_m": float(max_displacement),
        "max_displacement_m": float(displacement.max(initial=0.0)),
        "mean_displacement_m": float(displacement.mean() if len(displacement) else 0.0),
    }


def apply_flatten_top(mesh: trimesh.Trimesh, strength: float, quantile: float, max_displacement: float) -> dict[str, Any]:
    strength = max(0.0, min(1.0, float(strength)))
    if strength <= 0.0 or len(mesh.vertices) == 0:
        return {"status": "not_requested"}
    quantile = max(0.0, min(0.99, float(quantile)))
    original = mesh.vertices.copy()
    vertices = original.copy()
    y_values = vertices[:, 1]
    threshold = float(np.quantile(y_values, quantile))
    mask = y_values >= threshold
    if int(mask.sum()) < 8:
        return {"status": "skipped", "reason": "not enough top vertices", "threshold_y": threshold}
    target_y = float(np.median(y_values[mask]))
    vertices[mask, 1] = (1.0 - strength) * vertices[mask, 1] + strength * target_y
    vertices = clamp_displacement(original, vertices, float(max_displacement))
    mesh.vertices = vertices
    displacement = np.linalg.norm(mesh.vertices - original, axis=1)
    before_range = float(np.ptp(original[mask, 1]))
    after_range = float(np.ptp(mesh.vertices[mask, 1]))
    return {
        "status": "applied",
        "strength": strength,
        "quantile": quantile,
        "threshold_y": threshold,
        "target_y": target_y,
        "changed_vertices": int(mask.sum()),
        "before_top_y_range_m": before_range,
        "after_top_y_range_m": after_range,
        "max_displacement_limit_m": float(max_displacement),
        "max_displacement_m": float(displacement.max(initial=0.0)),
        "mean_displacement_m": float(displacement.mean() if len(displacement) else 0.0),
    }


def apply_component_prune(mesh: trimesh.Trimesh, max_components: int, min_component_area_ratio: float) -> dict[str, Any]:
    if len(mesh.vertices) == 0 or len(mesh.faces) == 0:
        return {"status": "not_requested"}
    max_components = max(0, int(max_components))
    min_component_area_ratio = max(0.0, float(min_component_area_ratio))
    if max_components <= 0 and min_component_area_ratio <= 0.0:
        return {"status": "not_requested"}
    components = list(mesh.split(only_watertight=False))
    if len(components) <= 1:
        return {
            "status": "skipped",
            "reason": "single component",
            "before_component_count": len(components),
            "after_component_count": len(components),
        }
    components = sorted(components, key=lambda item: float(item.area), reverse=True)
    largest_area = float(components[0].area)
    kept = []
    removed = []
    for index, component in enumerate(components):
        ratio = float(component.area) / max(largest_area, 1e-12)
        keep_for_count = max_components <= 0 or index < max_components
        keep_for_area = min_component_area_ratio <= 0.0 or ratio >= min_component_area_ratio
        if keep_for_count and keep_for_area:
            kept.append(component)
        else:
            removed.append({"component_index": index, "surface_area": float(component.area), "area_ratio": ratio})
    if not kept:
        kept = [components[0]]
        removed = [
            {"component_index": index, "surface_area": float(component.area), "area_ratio": float(component.area) / max(largest_area, 1e-12)}
            for index, component in enumerate(components[1:], start=1)
        ]
    if len(kept) == len(components):
        return {
            "status": "unchanged",
            "before_component_count": len(components),
            "after_component_count": len(kept),
            "largest_area": largest_area,
            "min_component_area_ratio": min_component_area_ratio,
            "max_components": max_components,
        }
    pruned = trimesh.util.concatenate(tuple(kept)) if len(kept) > 1 else kept[0].copy()
    pruned.remove_unreferenced_vertices()
    mesh.vertices = np.asarray(pruned.vertices, dtype=np.float64)
    mesh.faces = np.asarray(pruned.faces, dtype=np.int64)
    return {
        "status": "applied",
        "before_component_count": len(components),
        "after_component_count": len(kept),
        "removed_component_count": len(removed),
        "largest_area": largest_area,
        "min_component_area_ratio": min_component_area_ratio,
        "max_components": max_components,
        "removed_components": removed[:12],
    }


def apply_long_edge_prune(mesh: trimesh.Trimesh, max_edge_m: float, max_aspect_ratio: float) -> dict[str, Any]:
    max_edge_m = max(0.0, float(max_edge_m))
    max_aspect_ratio = max(0.0, float(max_aspect_ratio))
    if len(mesh.vertices) == 0 or len(mesh.faces) == 0:
        return {"status": "not_requested"}
    if max_edge_m <= 0.0 and max_aspect_ratio <= 0.0:
        return {"status": "not_requested"}

    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    triangles = vertices[faces]
    edge_lengths = np.stack(
        [
            np.linalg.norm(triangles[:, 0] - triangles[:, 1], axis=1),
            np.linalg.norm(triangles[:, 1] - triangles[:, 2], axis=1),
            np.linalg.norm(triangles[:, 2] - triangles[:, 0], axis=1),
        ],
        axis=1,
    )
    max_edges = edge_lengths.max(axis=1)
    min_edges = np.maximum(edge_lengths.min(axis=1), 1e-12)
    aspect_ratios = max_edges / min_edges
    keep = np.ones(len(faces), dtype=bool)
    if max_edge_m > 0.0:
        keep &= max_edges <= max_edge_m
    if max_aspect_ratio > 0.0:
        keep &= aspect_ratios <= max_aspect_ratio

    removed_count = int((~keep).sum())
    if removed_count == 0:
        return {
            "status": "unchanged",
            "before_faces": int(len(faces)),
            "after_faces": int(len(faces)),
            "max_edge_m": float(max_edges.max(initial=0.0)),
            "max_aspect_ratio": float(aspect_ratios.max(initial=0.0)),
            "threshold_max_edge_m": max_edge_m,
            "threshold_max_aspect_ratio": max_aspect_ratio,
        }
    if int(keep.sum()) == 0:
        return {
            "status": "skipped",
            "reason": "all faces would be removed",
            "before_faces": int(len(faces)),
            "removed_face_count": removed_count,
            "threshold_max_edge_m": max_edge_m,
            "threshold_max_aspect_ratio": max_aspect_ratio,
        }

    mesh.faces = faces[keep]
    mesh.remove_unreferenced_vertices()
    kept_max_edges = max_edges[keep]
    kept_aspect_ratios = aspect_ratios[keep]
    removed_max_edges = max_edges[~keep]
    removed_aspect_ratios = aspect_ratios[~keep]
    return {
        "status": "applied",
        "before_faces": int(len(faces)),
        "after_faces": int(len(mesh.faces)),
        "removed_face_count": removed_count,
        "threshold_max_edge_m": max_edge_m,
        "threshold_max_aspect_ratio": max_aspect_ratio,
        "max_removed_edge_m": float(removed_max_edges.max(initial=0.0)),
        "max_removed_aspect_ratio": float(removed_aspect_ratios.max(initial=0.0)),
        "max_kept_edge_m": float(kept_max_edges.max(initial=0.0)),
        "max_kept_aspect_ratio": float(kept_aspect_ratios.max(initial=0.0)),
    }


def clear_topology_dependent_attributes(prim: Usd.Prim) -> list[str]:
    removed = []
    for name in ("normals", "primvars:normal", "primvars:normals", "primvars:st", "primvars:st:indices"):
        if prim.HasAttribute(name):
            prim.RemoveProperty(name)
            removed.append(name)
    return removed


def write_mesh(prim: Usd.Prim, vertices: np.ndarray, faces: np.ndarray) -> list[str]:
    mesh = UsdGeom.Mesh(prim)
    removed_attributes = clear_topology_dependent_attributes(prim)
    mesh.GetPointsAttr().Set(Vt.Vec3fArray.FromNumpy(vertices.astype(np.float32)))
    if len(faces):
        mesh.GetFaceVertexCountsAttr().Set(Vt.IntArray([3] * int(len(faces))))
        mesh.GetFaceVertexIndicesAttr().Set(Vt.IntArray([int(item) for item in np.asarray(faces, dtype=np.int64).reshape(-1).tolist()]))
    return removed_attributes


def condition_stage(args: argparse.Namespace) -> dict[str, Any]:
    input_usd = Path(args.usd).resolve()
    output_usd = Path(args.output_usd).resolve()
    stage = Usd.Stage.Open(str(input_usd))
    if stage is None:
        raise RuntimeError(f"could not open USD stage: {input_usd}")
    selectors = {str(item) for item in args.selector_prim if str(item).strip()}
    segments = {str(item) for item in args.selector_segment if str(item).strip()}
    radial_segments = {str(item) for item in args.radial_regularise_segment if str(item).strip()}
    flatten_top_segments = {str(item) for item in args.flatten_top_segment if str(item).strip()}
    prune_segments = {str(item) for item in args.prune_components_segment if str(item).strip()}
    prune_prims = {str(item) for item in args.prune_components_prim if str(item).strip()}
    long_edge_segments = {str(item) for item in args.prune_long_edge_segment if str(item).strip()}
    long_edge_prims = {str(item) for item in args.prune_long_edge_prim if str(item).strip()}
    records: list[dict[str, Any]] = []
    for prim in stage.Traverse():
        if prim.GetTypeName() != "Mesh":
            continue
        prim_path = str(prim.GetPath())
        seg = segment_id(prim)
        is_selected = selected(prim, selectors, segments)
        prune_selected = seg in prune_segments or prim_path in prune_prims
        long_edge_selected = seg in long_edge_segments or prim_path in long_edge_prims
        if not is_selected and seg not in radial_segments and seg not in flatten_top_segments and not prune_selected and not long_edge_selected:
            records.append({"prim_path": prim_path, "segment_id": seg, "status": "unchanged"})
            continue
        mesh, counts = mesh_from_prim(prim)
        before_vertices = int(len(mesh.vertices))
        before_faces = int(len(mesh.faces))
        long_edge_prune = apply_long_edge_prune(
            mesh,
            float(args.prune_max_edge_m) if long_edge_selected else 0.0,
            float(args.prune_max_aspect_ratio) if long_edge_selected else 0.0,
        )
        component_prune = apply_component_prune(
            mesh,
            int(args.prune_max_components) if prune_selected else 0,
            float(args.prune_min_component_area_ratio) if prune_selected else 0.0,
        )
        smooth = apply_smooth(
            mesh,
            int(args.smooth_iterations) if is_selected else 0,
            float(args.smooth_lambda),
            float(args.max_displacement_m),
        )
        radial = apply_radial_regularise(
            mesh,
            float(args.radial_strength) if seg in radial_segments else 0.0,
            int(args.radial_bins),
            float(args.max_displacement_m),
        )
        flatten_top = apply_flatten_top(
            mesh,
            float(args.flatten_top_strength) if seg in flatten_top_segments else 0.0,
            float(args.flatten_top_quantile),
            float(args.max_displacement_m),
        )
        removed_topology_attributes = write_mesh(prim, np.asarray(mesh.vertices, dtype=np.float64), np.asarray(mesh.faces, dtype=np.int64))
        records.append(
            {
                "prim_path": prim_path,
                "segment_id": seg,
                "status": "conditioned",
                "before_vertices": before_vertices,
                "before_faces": before_faces,
                "face_vertex_count_kinds": sorted({int(item) for item in counts.tolist()}),
                "removed_topology_dependent_attributes": removed_topology_attributes,
                "long_edge_prune": long_edge_prune,
                "component_prune": component_prune,
                "smooth": smooth,
                "radial_regularise": radial,
                "flatten_top": flatten_top,
            }
        )
    output_usd.parent.mkdir(parents=True, exist_ok=True)
    stage.GetRootLayer().Export(str(output_usd))
    conditioned_count = len([item for item in records if item.get("status") == "conditioned"])
    return {
        "id": f"{args.asset_id}_usd_mesh_conditioning_v1",
        "version": "1.0",
        "asset_id": args.asset_id,
        "status": "pass" if conditioned_count else "blocked",
        "input_usd": input_usd.as_posix(),
        "output_usd": output_usd.as_posix(),
        "selector_segments": sorted(segments),
        "selector_prims": sorted(selectors),
        "radial_regularise_segments": sorted(radial_segments),
        "flatten_top_segments": sorted(flatten_top_segments),
        "smooth_iterations": int(args.smooth_iterations),
        "smooth_lambda": float(args.smooth_lambda),
        "max_displacement_m": float(args.max_displacement_m),
        "flatten_top_strength": float(args.flatten_top_strength),
        "flatten_top_quantile": float(args.flatten_top_quantile),
        "prune_components_segments": sorted(prune_segments),
        "prune_components_prims": sorted(prune_prims),
        "prune_max_components": int(args.prune_max_components),
        "prune_min_component_area_ratio": float(args.prune_min_component_area_ratio),
        "prune_long_edge_segments": sorted(long_edge_segments),
        "prune_long_edge_prims": sorted(long_edge_prims),
        "prune_max_edge_m": float(args.prune_max_edge_m),
        "prune_max_aspect_ratio": float(args.prune_max_aspect_ratio),
        "conditioned_mesh_count": conditioned_count,
        "mesh_records": records,
    }


def write_checksums(path: Path, files: list[Path]) -> None:
    write_json(
        path,
        {
            "files": [
                {"path": item.as_posix(), "sha256": sha256_file(item), "size_bytes": item.stat().st_size}
                for item in files
                if item.exists()
            ]
        },
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = condition_stage(args)
    report_path = Path(args.report).resolve()
    checksums_path = Path(args.checksums).resolve()
    output_usd = Path(args.output_usd).resolve()
    input_usd = Path(args.usd).resolve()
    write_json(report_path, report)
    write_checksums(checksums_path, [input_usd, output_usd, report_path])
    print(
        json.dumps(
            {
                "status": report["status"],
                "output_usd": report["output_usd"],
                "report": report_path.as_posix(),
                "conditioned_mesh_count": report["conditioned_mesh_count"],
            },
            indent=2,
            sort_keys=False,
        )
    )
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
