from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


VIEW_ORDER = ["front", "back", "left", "right"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hunyuan3d_multiview_to_glb")
    parser.add_argument("--backend-root", required=True)
    parser.add_argument("--images", nargs="+", required=True, help="view images in front, back, left, right order")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model-ref", default="tencent/Hunyuan3D-2mv")
    parser.add_argument("--output-name", default="asset.glb")
    parser.add_argument("--no-texture", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    backend_root = Path(args.backend_root).resolve()
    image_paths = [Path(item).resolve() for item in args.images]
    missing = [path.as_posix() for path in image_paths if not path.exists()]
    if missing:
        print(json.dumps({"status": "blocked", "error": "missing view images", "missing": missing}))
        return 1
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    from PIL import Image
    from hy3dgen.rembg import BackgroundRemover
    from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline

    remover = BackgroundRemover()
    views: dict[str, object] = {}
    for view_name, path in zip(VIEW_ORDER, image_paths, strict=False):
        image = Image.open(path)
        if image.mode == "RGB":
            image = remover(image)
        else:
            image = image.convert("RGBA")
        views[view_name] = image

    shape_pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(args.model_ref)
    mesh = shape_pipeline(image=views)[0]
    output_path = output_dir / args.output_name
    mesh.export(str(output_path))
    print(
        json.dumps(
            {
                "status": "proposal",
                "output_path": output_path.as_posix(),
                "conditioning_views": {name: path.as_posix() for name, path in zip(VIEW_ORDER, image_paths, strict=False)},
            },
            indent=2,
            sort_keys=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
