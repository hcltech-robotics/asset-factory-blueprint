from __future__ import annotations

from collections import Counter
from typing import Any

import numpy as np
import trimesh


DEFAULT_MESH_QUALITY_POLICY: dict[str, Any] = {
    "profile": "simulation_closed_surface",
    "require_watertight": True,
    "require_winding_consistent": True,
    "require_genus_defined": True,
    "max_component_count": 64,
    "max_boundary_edge_count": 0,
    "max_non_manifold_edge_count": 0,
    "max_orientation_conflict_edge_count": 0,
    "max_degenerate_face_count": 0,
    "max_duplicate_face_count": 0,
    "max_interior_face_count": 0,
}

TOPOLOGY_INVARIANT_FIELDS = (
    "component_count",
    "euler_characteristic",
    "genus_total",
    "watertight",
    "winding_consistent",
    "boundary_edge_count",
    "boundary_loop_count",
    "boundary_non_loop_component_count",
    "non_manifold_edge_count",
    "orientation_conflict_edge_count",
)

QUALITY_INVARIANT_FIELDS = (
    "degenerate_face_count",
    "duplicate_face_count",
    "interior_face_count",
    "unreferenced_vertex_count",
    "coincident_vertex_count",
    "sliver_face_count",
)


def _directed_edges(faces: np.ndarray) -> np.ndarray:
    edges = np.empty((len(faces) * 3, 2), dtype=np.int64)
    edges[0::3] = faces[:, [0, 1]]
    edges[1::3] = faces[:, [1, 2]]
    edges[2::3] = faces[:, [2, 0]]
    return edges


def _boundary_metrics(
    unique_edges: np.ndarray,
    edge_use_count: np.ndarray,
    vertex_count: int,
) -> tuple[int, int, int]:
    boundary_edges = unique_edges[edge_use_count == 1]
    if not len(boundary_edges):
        return 0, 0, 0
    labels = trimesh.graph.connected_component_labels(boundary_edges, node_count=vertex_count)
    boundary_vertices = np.unique(boundary_edges.reshape(-1))
    compact_labels, compact = np.unique(labels[boundary_vertices], return_inverse=True)
    degree = np.bincount(boundary_edges.reshape(-1), minlength=vertex_count)[boundary_vertices]
    minimum_degree = np.full(len(compact_labels), np.iinfo(np.int64).max, dtype=np.int64)
    maximum_degree = np.zeros(len(compact_labels), dtype=np.int64)
    np.minimum.at(minimum_degree, compact, degree)
    np.maximum.at(maximum_degree, compact, degree)
    loops = (minimum_degree == 2) & (maximum_degree == 2)
    return int(len(boundary_edges)), int(loops.sum()), int(len(compact_labels) - loops.sum())


def _triangle_metrics(vertices: np.ndarray, faces: np.ndarray) -> dict[str, Any]:
    diagonal = float(np.linalg.norm(np.ptp(vertices, axis=0))) if len(vertices) else 0.0
    cross_epsilon = max(np.finfo(np.float64).eps * max(diagonal * diagonal, 1.0) * 16.0, 1e-30)
    degenerate_count = 0
    sliver_count = 0
    minimum_quality = 1.0
    chunk_size = 100_000
    for start in range(0, len(faces), chunk_size):
        triangles = vertices[faces[start : start + chunk_size]]
        edge_a = triangles[:, 1] - triangles[:, 0]
        edge_b = triangles[:, 2] - triangles[:, 1]
        edge_c = triangles[:, 0] - triangles[:, 2]
        cross_norm = np.linalg.norm(np.cross(edge_a, -edge_c), axis=1)
        edge_square_sum = np.einsum("ij,ij->i", edge_a, edge_a)
        edge_square_sum += np.einsum("ij,ij->i", edge_b, edge_b)
        edge_square_sum += np.einsum("ij,ij->i", edge_c, edge_c)
        quality = np.zeros(len(triangles), dtype=np.float64)
        nonzero = edge_square_sum > 0.0
        quality[nonzero] = 2.0 * np.sqrt(3.0) * cross_norm[nonzero] / edge_square_sum[nonzero]
        degenerate = cross_norm <= cross_epsilon
        degenerate_count += int(degenerate.sum())
        sliver_count += int(((quality < 1e-4) & ~degenerate).sum())
        if np.any(~degenerate):
            minimum_quality = min(minimum_quality, float(quality[~degenerate].min()))
    return {
        "degenerate_face_count": degenerate_count,
        "sliver_face_count": sliver_count,
        "minimum_triangle_quality": minimum_quality if len(faces) > degenerate_count else 0.0,
    }


def exact_mesh_metrics(mesh: trimesh.Trimesh) -> dict[str, Any]:
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    if not len(vertices) or not len(faces):
        raise ValueError("mesh must contain vertices and faces")
    if faces.min() < 0 or faces.max() >= len(vertices):
        raise ValueError("mesh contains invalid face indices")
    if not np.isfinite(vertices).all():
        raise ValueError("mesh contains non-finite vertex coordinates")

    referenced_vertices = np.unique(faces.reshape(-1))
    directed_edges = _directed_edges(faces)
    sorted_edges = np.sort(directed_edges, axis=1)
    unique_edges, edge_inverse, edge_use_count = np.unique(
        sorted_edges,
        axis=0,
        return_inverse=True,
        return_counts=True,
    )
    component_labels = trimesh.graph.connected_component_labels(unique_edges, node_count=len(vertices))
    component_label_values = np.unique(component_labels[referenced_vertices])
    label_to_component = np.full(len(vertices), -1, dtype=np.int64)
    label_to_component[component_label_values] = np.arange(len(component_label_values), dtype=np.int64)
    vertex_components = label_to_component[component_labels]
    component_count = int(len(component_label_values))
    component_vertex_count = np.bincount(
        vertex_components[referenced_vertices],
        minlength=component_count,
    )
    component_edge_count = np.bincount(
        vertex_components[unique_edges[:, 0]],
        minlength=component_count,
    )
    component_face_count = np.bincount(
        vertex_components[faces[:, 0]],
        minlength=component_count,
    )
    component_euler = component_vertex_count - component_edge_count + component_face_count
    euler_characteristic = int(component_euler.sum())

    orientation = np.where(directed_edges[:, 0] == sorted_edges[:, 0], 1, -1)
    orientation_balance = np.bincount(
        edge_inverse,
        weights=orientation,
        minlength=len(unique_edges),
    )
    orientation_conflicts = int(((edge_use_count == 2) & (orientation_balance != 0)).sum())
    boundary_edge_count, boundary_loop_count, boundary_non_loop_count = _boundary_metrics(
        unique_edges,
        edge_use_count,
        len(vertices),
    )
    non_manifold_edge_count = int((edge_use_count > 2).sum())
    watertight = boundary_edge_count == 0 and non_manifold_edge_count == 0
    winding_consistent = orientation_conflicts == 0

    genus_values: list[int] | None = None
    genus_total: int | None = None
    if watertight and winding_consistent:
        raw_genus = 2 - component_euler
        if np.all(raw_genus >= 0) and np.all(raw_genus % 2 == 0):
            genus_values = [int(value) for value in (raw_genus // 2)]
            genus_total = int(sum(genus_values))

    edge_use_by_face = edge_use_count[edge_inverse].reshape((-1, 3))
    interior_face_count = int(np.all(edge_use_by_face > 2, axis=1).sum())
    degree = np.bincount(unique_edges.reshape(-1), minlength=len(vertices))[referenced_vertices]
    unique_positions, position_inverse = np.unique(vertices, axis=0, return_inverse=True)
    canonical_faces = np.sort(position_inverse[faces], axis=1)
    duplicate_face_count = int(len(faces) - len(np.unique(canonical_faces, axis=0)))
    triangle_metrics = _triangle_metrics(vertices, faces)
    component_euler_histogram = Counter(int(value) for value in component_euler)
    component_genus_histogram = Counter(genus_values or []) if genus_values is not None else None

    return {
        "vertex_count": int(len(vertices)),
        "referenced_vertex_count": int(len(referenced_vertices)),
        "unreferenced_vertex_count": int(len(vertices) - len(referenced_vertices)),
        "coincident_vertex_count": int(len(vertices) - len(unique_positions)),
        "face_count": int(len(faces)),
        "unique_edge_count": int(len(unique_edges)),
        "component_count": component_count,
        "euler_characteristic": euler_characteristic,
        "component_euler_histogram": {
            str(key): int(value) for key, value in sorted(component_euler_histogram.items())
        },
        "genus_total": genus_total,
        "component_genus_histogram": (
            {str(key): int(value) for key, value in sorted(component_genus_histogram.items())}
            if component_genus_histogram is not None
            else None
        ),
        "genus_defined": genus_total is not None,
        "watertight": watertight,
        "winding_consistent": winding_consistent,
        "edge_manifold": non_manifold_edge_count == 0,
        "boundary_edge_count": boundary_edge_count,
        "boundary_loop_count": boundary_loop_count,
        "boundary_non_loop_component_count": boundary_non_loop_count,
        "non_manifold_edge_count": non_manifold_edge_count,
        "orientation_conflict_edge_count": orientation_conflicts,
        "interior_face_count": interior_face_count,
        "duplicate_face_count": duplicate_face_count,
        "maximum_vertex_valence": int(degree.max()) if len(degree) else 0,
        "six_plus_valence_vertex_count": int((degree >= 6).sum()),
        **triangle_metrics,
    }


def resolved_quality_policy(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = dict(DEFAULT_MESH_QUALITY_POLICY)
    if overrides:
        policy.update({key: value for key, value in overrides.items() if key in policy})
    return policy


def mesh_quality_checks(metrics: dict[str, Any], policy: dict[str, Any]) -> list[dict[str, Any]]:
    checks = [
        ("watertight", metrics["watertight"], True, bool(metrics["watertight"]), "revise_local"),
        (
            "winding_consistent",
            metrics["winding_consistent"],
            True,
            bool(metrics["winding_consistent"]),
            "revise_local",
        ),
        (
            "genus_defined",
            metrics["genus_defined"],
            True,
            bool(metrics["genus_defined"]),
            "revise_local",
        ),
        (
            "component_count",
            metrics["component_count"],
            {"maximum": int(policy["max_component_count"])},
            int(metrics["component_count"]) <= int(policy["max_component_count"]),
            "regenerate",
        ),
    ]
    for metric_name, policy_name in (
        ("boundary_edge_count", "max_boundary_edge_count"),
        ("non_manifold_edge_count", "max_non_manifold_edge_count"),
        ("orientation_conflict_edge_count", "max_orientation_conflict_edge_count"),
        ("degenerate_face_count", "max_degenerate_face_count"),
        ("duplicate_face_count", "max_duplicate_face_count"),
        ("interior_face_count", "max_interior_face_count"),
    ):
        maximum = int(policy[policy_name])
        checks.append(
            (
                metric_name,
                int(metrics[metric_name]),
                {"maximum": maximum},
                int(metrics[metric_name]) <= maximum,
                "revise_local",
            )
        )

    enabled = {
        "watertight": bool(policy["require_watertight"]),
        "winding_consistent": bool(policy["require_winding_consistent"]),
        "genus_defined": bool(policy["require_genus_defined"]),
    }
    records: list[dict[str, Any]] = []
    for check_id, actual, expected, passed, action in checks:
        if check_id in enabled and not enabled[check_id]:
            records.append(
                {
                    "id": check_id,
                    "status": "not_required",
                    "actual": actual,
                    "expected": expected,
                    "severity": "note",
                    "recommended_action": "none",
                }
            )
            continue
        records.append(
            {
                "id": check_id,
                "status": "pass" if passed else "fail",
                "actual": actual,
                "expected": expected,
                "severity": "major" if not passed else "note",
                "recommended_action": action if not passed else "none",
            }
        )
    return records


def failed_quality_findings(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tag_by_check = {
        "watertight": "mesh_holes",
        "winding_consistent": "invalid_normals",
        "genus_defined": "non_manifold_geometry",
        "component_count": "fragmented_parts",
        "boundary_edge_count": "mesh_holes",
        "non_manifold_edge_count": "non_manifold_geometry",
        "orientation_conflict_edge_count": "invalid_normals",
        "degenerate_face_count": "degenerate_faces",
        "duplicate_face_count": "duplicate_faces",
        "interior_face_count": "interior_faces",
    }
    fix_by_check = {
        "watertight": "heal_mesh_holes",
        "winding_consistent": "heal_mesh_holes",
        "genus_defined": "heal_mesh_holes",
        "component_count": "rerun_reconstruction_fallback_backend",
        "boundary_edge_count": "heal_mesh_holes",
        "non_manifold_edge_count": "heal_mesh_holes",
        "orientation_conflict_edge_count": "heal_mesh_holes",
        "degenerate_face_count": "heal_mesh_holes",
        "duplicate_face_count": "heal_mesh_holes",
        "interior_face_count": "heal_mesh_holes",
    }
    findings: list[dict[str, Any]] = []
    for check in checks:
        if check["status"] != "fail":
            continue
        findings.append(
            {
                "defect_tag": tag_by_check[str(check["id"])],
                "severity": str(check["severity"]),
                "description": (
                    f"deterministic quality check {check['id']} failed: "
                    f"expected {check['expected']}, actual {check['actual']}"
                ),
                "region": "whole_mesh",
                "suggested_fix_id": fix_by_check[str(check["id"])],
                "source": "deterministic_mesh_quality_gate",
                "recommended_action": str(check["recommended_action"]),
            }
        )
    return findings
