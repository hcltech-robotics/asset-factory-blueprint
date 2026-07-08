from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hunyuan3d_image_to_glb")
    parser.add_argument("--backend-root", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model-ref", default="tencent/Hunyuan3D-2")
    parser.add_argument("--output-name", default="asset.glb")
    parser.add_argument("--no-texture", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    backend_root = Path(args.backend_root).resolve()
    image_path = Path(args.image).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    from PIL import Image
    from hy3dgen.rembg import BackgroundRemover
    from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline

    image = Image.open(image_path)
    if image.mode == "RGB":
        image = BackgroundRemover()(image)
    else:
        image = image.convert("RGBA")
    shape_pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(args.model_ref)
    mesh = shape_pipeline(image=image)[0]
    if not args.no_texture:
        # texgen needs the compiled rasteriser, so it must not be imported on shape-only runs
        from hy3dgen.texgen import Hunyuan3DPaintPipeline

        paint_pipeline = Hunyuan3DPaintPipeline.from_pretrained(args.model_ref)
        mesh = paint_pipeline(mesh, image=image)
    output_path = output_dir / args.output_name
    mesh.export(str(output_path))
    print(json.dumps({"status": "proposal", "output_path": output_path.as_posix()}, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
