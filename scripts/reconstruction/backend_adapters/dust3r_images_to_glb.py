from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dust3r_images_to_glb")
    parser.add_argument("--backend-root", required=True)
    parser.add_argument("--images", nargs="+", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model-ref", default="naver/DUSt3R_ViTLarge_BaseDecoder_512_dpt")
    parser.add_argument("--output-name", default="asset.glb")
    parser.add_argument("--image-size", type=int, default=512)
    parser.add_argument("--device", default="cuda")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    backend_root = Path(args.backend_root).resolve()
    image_paths = [Path(item).resolve() for item in args.images]
    missing = [path.as_posix() for path in image_paths if not path.exists()]
    if missing:
        print(json.dumps({"status": "blocked", "error": "missing view images", "missing": missing}))
        return 1
    if len(image_paths) < 2:
        print(json.dumps({"status": "blocked", "error": "multi-view geometry needs at least two images"}))
        return 1
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    import numpy as np
    import trimesh
    from dust3r.image_pairs import make_pairs
    from dust3r.inference import inference
    from dust3r.model import AsymmetricCroCo3DStereo
    from dust3r.utils.image import load_images
    from dust3r.cloud_opt import GlobalAlignerMode, global_aligner

    model = AsymmetricCroCo3DStereo.from_pretrained(args.model_ref).to(args.device)
    images = load_images([path.as_posix() for path in image_paths], size=args.image_size)
    pairs = make_pairs(images, scene_graph="complete", prefilter=None, symmetrize=True)
    output = inference(pairs, model, args.device, batch_size=1)
    scene = global_aligner(output, device=args.device, mode=GlobalAlignerMode.PointCloudOptimizer)
    scene.compute_global_alignment(init="mst", niter=300, schedule="cosine", lr=0.01)

    points = []
    colours = []
    for pts, mask, img in zip(scene.get_pts3d(), scene.get_masks(), scene.imgs, strict=False):
        pts_array = pts.detach().cpu().numpy()[mask.detach().cpu().numpy()]
        colour_array = np.asarray(img)[mask.detach().cpu().numpy()]
        points.append(pts_array.reshape(-1, 3))
        colours.append((colour_array.reshape(-1, 3) * 255).astype("uint8"))
    cloud = trimesh.PointCloud(np.concatenate(points), colors=np.concatenate(colours))
    ply_path = output_dir / "fused_points.ply"
    cloud.export(str(ply_path))

    output_path = output_dir / args.output_name
    cloud_scene = trimesh.Scene(cloud)
    cloud_scene.export(str(output_path))
    print(
        json.dumps(
            {
                "status": "proposal",
                "output_path": output_path.as_posix(),
                "fused_points": ply_path.as_posix(),
                "view_count": len(image_paths),
                "note": "fused point geometry; downstream meshing and repair run in the mesh conditioning tool",
            },
            indent=2,
            sort_keys=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
