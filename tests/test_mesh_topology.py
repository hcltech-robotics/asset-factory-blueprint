from __future__ import annotations

import trimesh

from asset_factory_blueprint.mesh_topology import (
    exact_mesh_metrics,
    failed_quality_findings,
    mesh_quality_checks,
    resolved_quality_policy,
)


def test_closed_box_has_exact_topology_invariants() -> None:
    metrics = exact_mesh_metrics(trimesh.creation.box())

    assert metrics["component_count"] == 1
    assert metrics["euler_characteristic"] == 2
    assert metrics["genus_total"] == 0
    assert metrics["watertight"] is True
    assert metrics["winding_consistent"] is True
    assert metrics["boundary_edge_count"] == 0
    assert metrics["boundary_loop_count"] == 0
    assert metrics["non_manifold_edge_count"] == 0


def test_open_box_has_boundary_loop_and_undefined_genus() -> None:
    mesh = trimesh.creation.box()
    mesh.update_faces([False, *([True] * (len(mesh.faces) - 1))])
    mesh.remove_unreferenced_vertices()

    metrics = exact_mesh_metrics(mesh)

    assert metrics["watertight"] is False
    assert metrics["genus_total"] is None
    assert metrics["boundary_edge_count"] == 3
    assert metrics["boundary_loop_count"] == 1
    assert metrics["boundary_non_loop_component_count"] == 0


def test_disconnected_bodies_preserve_per_component_euler() -> None:
    first = trimesh.creation.box()
    second = trimesh.creation.box()
    second.apply_translation((3.0, 0.0, 0.0))

    metrics = exact_mesh_metrics(trimesh.util.concatenate((first, second)))

    assert metrics["component_count"] == 2
    assert metrics["euler_characteristic"] == 4
    assert metrics["component_euler_histogram"] == {"2": 2}
    assert metrics["component_genus_histogram"] == {"0": 2}


def test_quality_policy_turns_open_and_fragmented_geometry_into_failures() -> None:
    bodies = []
    for index in range(65):
        body = trimesh.creation.box()
        body.apply_translation((index * 2.0, 0.0, 0.0))
        bodies.append(body)
    mesh = trimesh.util.concatenate(tuple(bodies))
    mesh.update_faces([False, *([True] * (len(mesh.faces) - 1))])
    mesh.remove_unreferenced_vertices()

    checks = mesh_quality_checks(exact_mesh_metrics(mesh), resolved_quality_policy())
    findings = failed_quality_findings(checks)

    assert any(item["id"] == "watertight" and item["status"] == "fail" for item in checks)
    assert any(item["id"] == "component_count" and item["status"] == "fail" for item in checks)
    assert any(item["recommended_action"] == "regenerate" for item in findings)
