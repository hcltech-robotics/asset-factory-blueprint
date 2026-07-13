from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trellis2_image_to_glb")
    parser.add_argument("--backend-root", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model-ref", default="microsoft/TRELLIS.2-4B")
    parser.add_argument("--output-name", default="asset.glb")
    parser.add_argument("--decimation-target", type=int, default=1000000)
    parser.add_argument("--texture-size", type=int, default=4096)
    parser.add_argument("--pipeline-type", choices=["512", "1024", "1024_cascade", "1536_cascade"])
    parser.add_argument("--max-num-tokens", type=int, default=49152)
    parser.add_argument("--dtype", choices=["float32", "float16", "bfloat16"], default="float32")
    parser.add_argument("--prune-unused-models", action="store_true")
    parser.add_argument("--cpu-image-cond", action="store_true")
    parser.add_argument("--skip-preprocess", action="store_true")
    parser.add_argument("--background-threshold", type=int, default=8)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--deterministic", action="store_true")
    return parser


def isolate_dark_background(image, threshold: int):
    import numpy as np
    from PIL import Image

    rgba = image.convert("RGBA")
    data = np.array(rgba)
    alpha = (data[:, :, :3].max(axis=2) > threshold).astype(np.uint8) * 255
    if not np.any(alpha):
        return image.convert("RGB")
    ys, xs = np.where(alpha > 0)
    margin = max(8, int(max(image.size) * 0.03))
    left = max(0, int(xs.min()) - margin)
    top = max(0, int(ys.min()) - margin)
    right = min(image.width, int(xs.max()) + margin)
    bottom = min(image.height, int(ys.max()) + margin)
    data[:, :, 3] = alpha
    cropped = Image.fromarray(data).crop((left, top, right, bottom))
    cropped_np = np.array(cropped).astype(np.float32) / 255.0
    rgb = (cropped_np[:, :, :3] * cropped_np[:, :, 3:4] * 255).astype(np.uint8)
    return Image.fromarray(rgb, mode="RGB")


def patch_dinov3_layer_alias() -> None:
    try:
        from transformers import DINOv3ViTModel
    except Exception:
        return
    if hasattr(DINOv3ViTModel, "layer"):
        return

    def layer(self):
        encoder = getattr(self, "model", None)
        return getattr(encoder, "layer")

    DINOv3ViTModel.layer = property(layer)


def cast_pipeline(pipeline, dtype_name: str) -> None:
    if dtype_name == "float32":
        return
    import torch

    dtype = {"float16": torch.float16, "bfloat16": torch.bfloat16}[dtype_name]
    for model in pipeline.models.values():
        model.to(dtype=dtype)
    image_model = getattr(getattr(pipeline, "image_cond_model", None), "model", None)
    if image_model is not None:
        image_model.to(dtype=dtype)


def patch_image_feature_extractor_device() -> None:
    import numpy as np
    import torch
    from PIL import Image
    from trellis2.modules import image_feature_extractor

    def call(self, image):
        if isinstance(image, torch.Tensor):
            if image.ndim != 4:
                raise ValueError("Image tensor should be batched (B, C, H, W)")
        elif isinstance(image, list):
            if not all(isinstance(item, Image.Image) for item in image):
                raise ValueError("Image list should be list of PIL images")
            image = [item.resize((self.image_size, self.image_size), Image.LANCZOS) for item in image]
            image = [np.array(item.convert("RGB")).astype(np.float32) / 255 for item in image]
            image = [torch.from_numpy(item).permute(2, 0, 1).float() for item in image]
            device = next(self.model.parameters()).device
            image = torch.stack(image).to(device)
        else:
            raise ValueError(f"Unsupported type of image: {type(image)}")
        device = next(self.model.parameters()).device
        image = self.transform(image).to(device)
        return self.extract_features(image)

    image_feature_extractor.DinoV3FeatureExtractor.__call__ = torch.no_grad()(call)


def use_cpu_image_conditioning(pipeline) -> None:
    import types
    import torch

    patch_image_feature_extractor_device()
    image_model = getattr(getattr(pipeline, "image_cond_model", None), "model", None)
    if image_model is not None:
        image_model.to(dtype=torch.float32)

    def get_cond(self, image, resolution: int, include_neg_cond: bool = True):
        self.image_cond_model.image_size = resolution
        self.image_cond_model.cpu()
        cond = self.image_cond_model(image).to(self.device)
        if not include_neg_cond:
            return {"cond": cond}
        return {"cond": cond, "neg_cond": torch.zeros_like(cond)}

    pipeline.get_cond = types.MethodType(get_cond, pipeline)


def prune_unused_models(pipeline, pipeline_type: str | None) -> None:
    if pipeline_type != "512":
        return
    for name in ["shape_slat_flow_model_1024", "tex_slat_flow_model_1024"]:
        if name in pipeline.models:
            del pipeline.models[name]


def configure_reproducibility(seed: int, deterministic: bool) -> None:
    """Set process-wide stochastic controls before any CUDA work begins."""
    if deterministic:
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    random.seed(seed)

    import numpy as np
    import torch

    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    if deterministic:
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        torch.use_deterministic_algorithms(True, warn_only=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    backend_root = Path(args.backend_root).resolve()
    image_path = Path(args.image).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))
    os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    configure_reproducibility(args.seed, args.deterministic)
    patch_dinov3_layer_alias()

    from PIL import Image
    import o_voxel
    from trellis2.pipelines import Trellis2ImageTo3DPipeline

    pipeline = Trellis2ImageTo3DPipeline.from_pretrained(args.model_ref)
    if args.prune_unused_models:
        prune_unused_models(pipeline, args.pipeline_type)
    cast_pipeline(pipeline, args.dtype)
    if args.cpu_image_cond:
        use_cpu_image_conditioning(pipeline)
    pipeline.cuda()
    image = Image.open(image_path).convert("RGB")
    run_kwargs = {"max_num_tokens": args.max_num_tokens}
    if args.pipeline_type:
        run_kwargs["pipeline_type"] = args.pipeline_type
    if args.skip_preprocess:
        image = isolate_dark_background(image, args.background_threshold)
        run_kwargs["preprocess_image"] = False
    if args.dtype == "float32":
        mesh = pipeline.run(image, **run_kwargs)[0]
    else:
        import torch

        dtype = {"float16": torch.float16, "bfloat16": torch.bfloat16}[args.dtype]
        with torch.autocast(device_type="cuda", dtype=dtype):
            mesh = pipeline.run(image, **run_kwargs)[0]
    if hasattr(mesh, "simplify"):
        mesh.simplify(16777216)
    glb = o_voxel.postprocess.to_glb(
        vertices=mesh.vertices,
        faces=mesh.faces,
        attr_volume=mesh.attrs,
        coords=mesh.coords,
        attr_layout=mesh.layout,
        voxel_size=mesh.voxel_size,
        aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
        decimation_target=args.decimation_target,
        texture_size=args.texture_size,
        remesh=True,
        remesh_band=1,
        remesh_project=0,
        verbose=True,
    )
    output_path = output_dir / args.output_name
    glb.export(str(output_path), extension_webp=True)
    print(
        json.dumps(
            {
                "status": "proposal",
                "output_path": output_path.as_posix(),
                "reproducibility": {"seed": args.seed, "deterministic": args.deterministic},
            },
            indent=2,
            sort_keys=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
