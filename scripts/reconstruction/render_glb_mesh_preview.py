from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from PIL import Image, ImageOps
import trimesh


PART_COLOURS = np.array(
    [
        [0.23, 0.49, 0.83],
        [0.91, 0.44, 0.28],
        [0.40, 0.68, 0.34],
        [0.70, 0.44, 0.78],
        [0.92, 0.76, 0.26],
        [0.30, 0.74, 0.78],
    ],
    dtype=np.float64,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render software previews for PartCrafter GLB outputs.")
    parser.add_argument("--parts-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-image", default="")
    parser.add_argument("--conditioning-image", default="")
    parser.add_argument("--max-faces-per-part", type=int, default=30000)
    parser.add_argument("--azim", type=float, default=-55.0)
    parser.add_argument("--elev", type=float, default=18.0)
    parser.add_argument("--explosion", type=float, default=0.55)
    parser.add_argument(
        "--up-axis",
        choices=["y", "z"],
        default="y",
        help="up axis of the input meshes; glTF and GLB are Y-up, the plot is Z-up",
    )
    return parser.parse_args()


def load_mesh(path: Path, up_axis: str = "y") -> trimesh.Trimesh:
    loaded = trimesh.load(path, force="scene")
    if isinstance(loaded, trimesh.Scene):
        geometries = [mesh for mesh in loaded.geometry.values() if len(mesh.vertices) and len(mesh.faces)]
        if not geometries:
            raise ValueError(f"no mesh geometry in {path}")
        mesh = trimesh.util.concatenate(tuple(geometries))
    else:
        mesh = loaded
    if up_axis == "y":
        # stand Y-up assets upright in the Z-up plot so previews match the photo
        mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2.0, [1.0, 0.0, 0.0]))
    return mesh


def sample_faces(mesh: trimesh.Trimesh, max_faces: int, seed: int) -> np.ndarray:
    if len(mesh.faces) <= max_faces:
        return np.arange(len(mesh.faces))
    rng = np.random.default_rng(seed)
    return np.sort(rng.choice(len(mesh.faces), size=max_faces, replace=False))


def shaded_colours(mesh: trimesh.Trimesh, face_indices: np.ndarray, colour: np.ndarray) -> np.ndarray:
    vertices = mesh.vertices[mesh.faces[face_indices]]
    normals = np.cross(vertices[:, 1] - vertices[:, 0], vertices[:, 2] - vertices[:, 0])
    normal_lengths = np.linalg.norm(normals, axis=1)
    normals = normals / np.maximum(normal_lengths[:, None], 1e-9)
    light = np.array([-0.35, -0.45, 0.82], dtype=np.float64)
    light = light / np.linalg.norm(light)
    shade = 0.34 + 0.66 * np.clip(normals @ light, 0.0, 1.0)
    rgba = np.ones((len(face_indices), 4), dtype=np.float64)
    rgba[:, :3] = np.clip(colour[None, :] * shade[:, None], 0.0, 1.0)
    rgba[:, 3] = 1.0
    return rgba


def equalise_axes(ax: plt.Axes, bounds: np.ndarray) -> None:
    centre = bounds.mean(axis=0)
    span = float(np.max(bounds[1] - bounds[0]))
    radius = max(span * 0.58, 1e-3)
    ax.set_xlim(centre[0] - radius, centre[0] + radius)
    ax.set_ylim(centre[1] - radius, centre[1] + radius)
    ax.set_zlim(centre[2] - radius, centre[2] + radius)
    ax.set_box_aspect((1, 1, 1))
    ax.set_axis_off()


def render_scene(
    meshes: list[trimesh.Trimesh],
    output_path: Path,
    *,
    colours: list[np.ndarray],
    bounds: np.ndarray,
    max_faces_per_part: int,
    azim: float,
    elev: float,
    explosion: float = 0.0,
) -> None:
    fig = plt.figure(figsize=(8, 8), dpi=150)
    ax = fig.add_subplot(111, projection="3d")
    centre = bounds.mean(axis=0)
    span = float(np.max(bounds[1] - bounds[0]))
    for index, mesh in enumerate(meshes):
        face_indices = sample_faces(mesh, max_faces_per_part, seed=17 + index)
        vertices = mesh.vertices.copy()
        if explosion:
            direction = mesh.centroid - centre
            norm = np.linalg.norm(direction)
            if norm < 1e-6:
                angle = (index / max(1, len(meshes))) * np.pi * 2.0
                direction = np.array([np.cos(angle), np.sin(angle), 0.25])
                norm = np.linalg.norm(direction)
            vertices = vertices + (direction / norm) * span * explosion
        triangles = vertices[mesh.faces[face_indices]]
        collection = Poly3DCollection(triangles, linewidths=0.0, antialiased=False)
        collection.set_facecolor(shaded_colours(mesh, face_indices, colours[index % len(colours)]))
        ax.add_collection3d(collection)
    equalise_axes(ax, bounds)
    ax.view_init(elev=elev, azim=azim)
    fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, facecolor="white")
    plt.close(fig)


def add_image_panel(ax: plt.Axes, path: str, title: str) -> None:
    ax.set_title(title, fontsize=10)
    ax.axis("off")
    if not path:
        return
    image_path = Path(path)
    if not image_path.exists():
        return
    image = Image.open(image_path).convert("RGB")
    image = ImageOps.contain(image, (900, 900))
    ax.imshow(image)


def make_contact_sheet(
    output_path: Path,
    panels: list[tuple[str, str]],
) -> None:
    fig, axes = plt.subplots(1, len(panels), figsize=(4 * len(panels), 4), dpi=150)
    if len(panels) == 1:
        axes = [axes]
    for ax, (path, title) in zip(axes, panels):
        add_image_panel(ax, path, title)
    fig.tight_layout(pad=0.8)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, facecolor="white")
    plt.close(fig)


def main() -> int:
    args = parse_args()
    parts_dir = Path(args.parts_dir)
    output_dir = Path(args.output_dir)
    part_paths = sorted(parts_dir.glob("part_*.glb")) or sorted(parts_dir.glob("*.glb"))
    if not part_paths:
        raise SystemExit(f"no GLB files found in {parts_dir}")
    meshes = [load_mesh(path, up_axis=args.up_axis) for path in part_paths]
    all_bounds = np.array([mesh.bounds for mesh in meshes])
    bounds = np.array([all_bounds[:, 0, :].min(axis=0), all_bounds[:, 1, :].max(axis=0)])

    mono_path = output_dir / "mesh-preview-mono.png"
    segments_path = output_dir / "mesh-preview-segments.png"
    exploded_path = output_dir / "mesh-preview-exploded.png"
    sheet_path = output_dir / "mesh-preview-contact-sheet.png"

    render_scene(
        meshes,
        mono_path,
        colours=[np.array([0.70, 0.72, 0.70])] * len(meshes),
        bounds=bounds,
        max_faces_per_part=args.max_faces_per_part,
        azim=args.azim,
        elev=args.elev,
    )
    render_scene(
        meshes,
        segments_path,
        colours=[PART_COLOURS[index % len(PART_COLOURS)] for index in range(len(meshes))],
        bounds=bounds,
        max_faces_per_part=args.max_faces_per_part,
        azim=args.azim,
        elev=args.elev,
    )
    render_scene(
        meshes,
        exploded_path,
        colours=[PART_COLOURS[index % len(PART_COLOURS)] for index in range(len(meshes))],
        bounds=bounds,
        max_faces_per_part=args.max_faces_per_part,
        azim=args.azim,
        elev=args.elev,
        explosion=args.explosion,
    )
    make_contact_sheet(
        sheet_path,
        [
            (args.source_image, "source"),
            (args.conditioning_image, "semantic conditioning"),
            (str(mono_path), "mono mesh"),
            (str(segments_path), "parts"),
            (str(exploded_path), "exploded"),
        ],
    )
    print(sheet_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
