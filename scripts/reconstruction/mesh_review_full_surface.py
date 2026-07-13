from __future__ import annotations

import argparse
import gc
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


ANGLES = (-135, -90, -45, 0, 45, 90, 135, 180)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render full-surface mesh review contact sheets.")
    parser.add_argument("--backend-root", required=True)
    parser.add_argument("--mesh", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--resolution", type=int, default=384)
    return parser


def _sheet(frames: list[Image.Image], resolution: int) -> Image.Image:
    sheet = Image.new("RGB", (resolution * 4, resolution * 2), (245, 245, 245))
    for index, frame in enumerate(frames):
        sheet.paste(frame, ((index % 4) * resolution, (index // 4) * resolution))
    return sheet


def _label(image: Image.Image, angle: int) -> Image.Image:
    ImageDraw.Draw(image).text((12, 10), f"az {angle}", fill=(25, 30, 38))
    return image


def _normal_edges(normal: np.ndarray, depth: np.ndarray, mask: np.ndarray) -> np.ndarray:
    normal_gradient = np.zeros(mask.shape, dtype=np.float32)
    for channel in range(3):
        gy, gx = np.gradient(normal[:, :, channel])
        normal_gradient += np.abs(gx) + np.abs(gy)
    safe_depth = np.where(mask, depth, 0.0)
    gy, gx = np.gradient(safe_depth)
    depth_scale = max(float(np.ptp(safe_depth[mask])) if np.any(mask) else 0.0, 1e-6)
    depth_gradient = (np.abs(gx) + np.abs(gy)) / depth_scale
    mask_gradient = np.abs(np.gradient(mask.astype(np.float32), axis=0))
    mask_gradient += np.abs(np.gradient(mask.astype(np.float32), axis=1))
    return (normal_gradient > 0.18) | (depth_gradient > 0.025) | (mask_gradient > 0.0)


def main() -> int:
    args = build_parser().parse_args()
    backend_root = Path(args.backend_root).resolve()
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    import torch
    import trimesh
    from trellis2.renderers.mesh_renderer import MeshRenderer
    from trellis2.representations.mesh import Mesh
    from trellis2.utils.render_utils import yaw_pitch_r_fov_to_extrinsics_intrinsics

    source = Path(args.mesh).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    loaded = trimesh.load(source, force="scene", process=False)
    geometries = list(loaded.geometry.values()) if isinstance(loaded, trimesh.Scene) else [loaded]
    mesh = geometries[0] if len(geometries) == 1 else trimesh.util.concatenate(tuple(geometries))
    vertices = np.asarray(mesh.vertices, dtype=np.float32)
    faces = np.asarray(mesh.faces, dtype=np.int32)
    centre = (vertices.min(axis=0) + vertices.max(axis=0)) * 0.5
    scale = float(np.max(vertices.max(axis=0) - vertices.min(axis=0)))
    vertices = (vertices - centre) / max(scale, 1e-8) * 1.55

    render_mesh = Mesh(torch.from_numpy(vertices).cuda(), torch.from_numpy(faces).cuda())
    renderer = MeshRenderer(
        {"resolution": args.resolution, "near": 0.1, "far": 10.0, "ssaa": 1},
        device="cuda",
    )
    yaws = [math.radians(value) for value in ANGLES]
    pitches = [math.radians(20)] * len(yaws)
    extrinsics, intrinsics = yaw_pitch_r_fov_to_extrinsics_intrinsics(yaws, pitches, 2.8, 36)
    light = np.asarray([0.35, -0.25, 0.9], dtype=np.float32)
    light /= np.linalg.norm(light)
    colour = np.asarray([0.33, 0.52, 0.78], dtype=np.float32)
    beauty_frames: list[Image.Image] = []
    normal_frames: list[Image.Image] = []
    wireframe_frames: list[Image.Image] = []

    for angle, extrinsic, intrinsic in zip(ANGLES, extrinsics, intrinsics):
        rendered = renderer.render(
            render_mesh,
            extrinsic,
            intrinsic,
            return_types=["normal", "mask", "depth"],
        )
        normal_encoded = rendered.normal.detach().cpu().numpy().transpose(1, 2, 0)
        normal = normal_encoded * 2.0 - 1.0
        mask = rendered.mask.detach().cpu().numpy() > 0.01
        depth = rendered.depth.detach().cpu().numpy()
        shade = 0.35 + 0.65 * np.clip(np.sum(normal * light, axis=-1), 0.0, 1.0)
        beauty = np.ones((args.resolution, args.resolution, 3), dtype=np.float32) * 0.96
        beauty[mask] = shade[mask, None] * colour
        normal_rgb = np.ones_like(beauty) * 0.96
        normal_rgb[mask] = 0.12 + 0.78 * normal_encoded[mask]
        edges = _normal_edges(normal, depth, mask)
        wireframe = beauty * 0.55 + 0.42
        wireframe[edges] = np.asarray([0.06, 0.09, 0.14], dtype=np.float32)
        beauty_frames.append(
            _label(Image.fromarray(np.clip(beauty * 255.0, 0, 255).astype(np.uint8)), angle)
        )
        normal_frames.append(
            _label(Image.fromarray(np.clip(normal_rgb * 255.0, 0, 255).astype(np.uint8)), angle)
        )
        wireframe_frames.append(
            _label(Image.fromarray(np.clip(wireframe * 255.0, 0, 255).astype(np.uint8)), angle)
        )

    _sheet(beauty_frames, args.resolution).save(output_dir / "beauty-contact-sheet.png")
    _sheet(wireframe_frames, args.resolution).save(output_dir / "wireframe-contact-sheet.png")
    _sheet(normal_frames, args.resolution).save(output_dir / "normal-contact-sheet.png")
    del renderer, render_mesh, mesh, loaded, geometries, vertices, faces
    gc.collect()
    torch.cuda.empty_cache()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
