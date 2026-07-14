"""Affordance-weighted collision fidelity.

Measures how far the collision shell departs from the visual surface in the
neighbourhood of each recorded affordance: surface points sampled on the visual
mesh within a radius of a grasp frame or articulated contact surface are measured
against the collision shell, and collision-shell points are measured back against
the visual surface to catch bloat. The metric follows the visual-to-collision shell
distance proposed for manipulation assets and reports p95 and maximum per region.

The metric implementation is independent of USD bindings; the evidence producer
extracts the bound meshes from the composed stage.
"""

from __future__ import annotations

import math
import random
from typing import Any

from asset_factory_blueprint.rl_evidence import FIDELITY_REPORT_ID, FIDELITY_REPORT_VERSION

Vec = tuple[float, float, float]
Mesh = tuple[list[Vec], list[tuple[int, int, int]]]


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a: Vec, s: float) -> Vec:
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a: Vec) -> float:
    return math.sqrt(_dot(a, a))


def triangle_area(a: Vec, b: Vec, c: Vec) -> float:
    return 0.5 * _norm(_cross(_sub(b, a), _sub(c, a)))


def closest_point_on_triangle(p: Vec, a: Vec, b: Vec, c: Vec) -> Vec:
    """Ericson, Real-Time Collision Detection, section 5.1.5."""

    ab, ac, ap = _sub(b, a), _sub(c, a), _sub(p, a)
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    if d1 <= 0.0 and d2 <= 0.0:
        return a
    bp = _sub(p, b)
    d3, d4 = _dot(ab, bp), _dot(ac, bp)
    if d3 >= 0.0 and d4 <= d3:
        return b
    vc = d1 * d4 - d3 * d2
    if vc <= 0.0 and d1 >= 0.0 and d3 <= 0.0:
        v = d1 / (d1 - d3)
        return _add(a, _scale(ab, v))
    cp = _sub(p, c)
    d5, d6 = _dot(ab, cp), _dot(ac, cp)
    if d6 >= 0.0 and d5 <= d6:
        return c
    vb = d5 * d2 - d1 * d6
    if vb <= 0.0 and d2 >= 0.0 and d6 <= 0.0:
        w = d2 / (d2 - d6)
        return _add(a, _scale(ac, w))
    va = d3 * d6 - d5 * d4
    if va <= 0.0 and (d4 - d3) >= 0.0 and (d5 - d6) >= 0.0:
        w = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        return _add(b, _scale(_sub(c, b), w))
    denom = 1.0 / (va + vb + vc)
    v, w = vb * denom, vc * denom
    return _add(a, _add(_scale(ab, v), _scale(ac, w)))


class MeshDistance:
    """Point-to-surface distance with a bounding-box prune; adequate for the sample counts used here."""

    def __init__(self, mesh: Mesh) -> None:
        vertices, faces = mesh
        self.triangles: list[tuple[Vec, Vec, Vec]] = []
        self.boxes: list[tuple[Vec, Vec]] = []
        for face in faces:
            tri = (vertices[face[0]], vertices[face[1]], vertices[face[2]])
            self.triangles.append(tri)
            lo = (min(v[0] for v in tri), min(v[1] for v in tri), min(v[2] for v in tri))
            hi = (max(v[0] for v in tri), max(v[1] for v in tri), max(v[2] for v in tri))
            self.boxes.append((lo, hi))

    def distance(self, p: Vec) -> float:
        best = math.inf
        for (lo, hi), tri in zip(self.boxes, self.triangles, strict=True):
            dx = max(lo[0] - p[0], 0.0, p[0] - hi[0])
            dy = max(lo[1] - p[1], 0.0, p[1] - hi[1])
            dz = max(lo[2] - p[2], 0.0, p[2] - hi[2])
            if dx * dx + dy * dy + dz * dz >= best * best:
                continue
            q = closest_point_on_triangle(p, *tri)
            best = min(best, _norm(_sub(p, q)))
        return best


def sample_surface(
    mesh: Mesh, count: int, rng: random.Random, centre: Vec | None = None, radius: float | None = None
) -> list[Vec]:
    """Area-weighted surface samples, optionally restricted to a ball around ``centre``."""

    vertices, faces = mesh
    weights: list[float] = []
    kept: list[tuple[int, int, int]] = []
    for face in faces:
        tri = (vertices[face[0]], vertices[face[1]], vertices[face[2]])
        if centre is not None and radius is not None:
            nearest = closest_point_on_triangle(centre, *tri)
            if _norm(_sub(nearest, centre)) > radius:
                continue
        area = triangle_area(*tri)
        if area <= 0.0:
            continue
        weights.append(area)
        kept.append(face)
    if not kept:
        return []
    samples: list[Vec] = []
    attempts = 0
    while len(samples) < count and attempts < count * 20:
        attempts += 1
        face = rng.choices(kept, weights=weights, k=1)[0]
        a, b, c = vertices[face[0]], vertices[face[1]], vertices[face[2]]
        r1, r2 = rng.random(), rng.random()
        s1 = math.sqrt(r1)
        point = _add(_scale(a, 1.0 - s1), _add(_scale(b, s1 * (1.0 - r2)), _scale(c, s1 * r2)))
        if centre is not None and radius is not None and _norm(_sub(point, centre)) > radius:
            continue
        samples.append(point)
    return samples


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(math.ceil(0.95 * len(ordered))) - 1)
    return ordered[max(index, 0)]


def evaluate_region(
    visual: Mesh, collision: Mesh, centre: Vec, radius: float, samples: int, rng: random.Random
) -> dict[str, Any]:
    visual_points = sample_surface(visual, samples, rng, centre, radius)
    collision_points = sample_surface(collision, samples, rng, centre, radius)
    to_collision = MeshDistance(collision)
    to_visual = MeshDistance(visual)
    gaps = [to_collision.distance(p) for p in visual_points]
    bloat = [to_visual.distance(p) for p in collision_points]
    return {
        "visual_samples": len(visual_points),
        "collision_samples": len(collision_points),
        "visual_to_collision_p95_m": _p95(gaps),
        "visual_to_collision_max_m": max(gaps) if gaps else 0.0,
        "collision_to_visual_p95_m": _p95(bloat),
        "collision_to_visual_max_m": max(bloat) if bloat else 0.0,
    }


def evaluate_fidelity(
    visual: Mesh,
    collision: Mesh,
    regions: list[dict[str, Any]],
    tolerance_m: float,
    samples_per_region: int = 256,
    seed: int = 0,
) -> dict[str, Any]:
    """Evaluate every region; a region passes when both p95 distances are within tolerance."""

    if not math.isfinite(tolerance_m) or tolerance_m <= 0:
        raise ValueError("tolerance_m must be finite and greater than zero")
    if not isinstance(samples_per_region, int) or isinstance(samples_per_region, bool) or samples_per_region < 1:
        raise ValueError("samples_per_region must be a positive integer")
    rng = random.Random(seed)
    results: list[dict[str, Any]] = []
    problems: list[str] = []
    for region in regions:
        centre = tuple(float(x) for x in region["centre"])
        radius = float(region.get("radius_m", 0.05))
        if not math.isfinite(radius) or radius <= 0:
            raise ValueError(f"region {region.get('id', '?')} radius must be finite and greater than zero")
        measured = evaluate_region(visual, collision, centre, radius, samples_per_region, rng)
        shell_p95 = max(measured["visual_to_collision_p95_m"], measured["collision_to_visual_p95_m"])
        status = "pass"
        reason = ""
        if measured["visual_samples"] != samples_per_region:
            status, reason = (
                "blocked",
                f"sampled {measured['visual_samples']} of {samples_per_region} required visual-surface points",
            )
        elif measured["collision_samples"] != samples_per_region:
            status, reason = (
                "blocked",
                f"sampled {measured['collision_samples']} of {samples_per_region} required collision-surface points",
            )
        elif not math.isfinite(shell_p95):
            status, reason = "blocked", "shell distance is not finite"
        elif shell_p95 > tolerance_m:
            status, reason = "blocked", f"shell distance p95 {shell_p95:.4f} m exceeds tolerance {tolerance_m} m"
        if status == "blocked":
            problems.append(f"region {region.get('id', '?')}: {reason}")
        results.append(
            {
                "id": str(region.get("id", "")),
                "kind": str(region.get("kind", "grasp_point")),
                "centre": list(centre),
                "radius_m": radius,
                "shell_distance_p95_m": shell_p95,
                "status": status,
                "reason": reason,
                **measured,
            }
        )
    return {
        "report_identity": {"id": FIDELITY_REPORT_ID, "version": FIDELITY_REPORT_VERSION},
        "status": "pass" if not problems and results else "blocked",
        "tolerance_m": tolerance_m,
        "samples_per_region": samples_per_region,
        "seed": seed,
        "regions": results,
        "errors": problems if results else ["no affordance regions were supplied"],
    }


def convex_hull(vertices: list[Vec]) -> Mesh:
    """Convex hull via scipy when available; the collision shell for ``convexHull`` approximations."""

    try:
        from scipy.spatial import ConvexHull  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only without the mesh extra
        raise RuntimeError("scipy is required for convexHull collision shells (install the mesh extra)") from exc
    hull = ConvexHull([list(v) for v in vertices])
    faces = [(int(a), int(b), int(c)) for a, b, c in hull.simplices]
    return list(vertices), faces


def parse_obj(text: str) -> Mesh:
    """Minimal OBJ reader for exported visual or collision meshes; fans polygons into triangles."""

    vertices: list[Vec] = []
    faces: list[tuple[int, int, int]] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "v" and len(parts) >= 4:
            vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif parts[0] == "f" and len(parts) >= 4:
            indices = [int(token.split("/")[0]) - 1 for token in parts[1:]]
            for i in range(1, len(indices) - 1):
                faces.append((indices[0], indices[i], indices[i + 1]))
    return vertices, faces
