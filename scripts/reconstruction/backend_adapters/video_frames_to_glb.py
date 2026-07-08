from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="video_frames_to_glb")
    parser.add_argument("--backend-root", required=True, help="root of the delegated multi-view backend checkout")
    parser.add_argument("--video", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model-ref", default="")
    parser.add_argument("--frames", type=int, default=8)
    parser.add_argument("--delegate", default="dust3r_images_to_glb", choices=["dust3r_images_to_glb", "hunyuan3d_multiview_to_glb"])
    parser.add_argument("--output-name", default="asset.glb")
    return parser


def _sample_frames(video_path: Path, frames_dir: Path, count: int) -> list[Path]:
    import imageio.v3 as iio

    frames_dir.mkdir(parents=True, exist_ok=True)
    props = iio.improps(video_path, plugin="pyav")
    total = int(props.shape[0]) if props.shape and props.shape[0] and props.shape[0] > 0 else 0
    written: list[Path] = []
    if total > 0:
        indices = [int(round(index * (total - 1) / max(1, count - 1))) for index in range(count)]
        for order, frame_index in enumerate(dict.fromkeys(indices)):
            frame = iio.imread(video_path, index=frame_index, plugin="pyav")
            target = frames_dir / f"frame_{order:03d}.png"
            iio.imwrite(target, frame)
            written.append(target)
    else:
        for order, frame in enumerate(iio.imiter(video_path, plugin="pyav")):
            if order % 10 == 0 and len(written) < count:
                target = frames_dir / f"frame_{len(written):03d}.png"
                iio.imwrite(target, frame)
                written.append(target)
            if len(written) >= count:
                break
    return written


def _load_delegate(name: str):
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load delegated adapter: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    video_path = Path(args.video).resolve()
    if not video_path.exists():
        print(json.dumps({"status": "blocked", "error": f"missing video: {video_path.as_posix()}"}))
        return 1
    output_dir = Path(args.output_dir).resolve()
    frames_dir = output_dir / "frames"
    frames = _sample_frames(video_path, frames_dir, max(2, args.frames))
    if len(frames) < 2:
        print(json.dumps({"status": "blocked", "error": "could not sample at least two frames from the video"}))
        return 1

    delegate = _load_delegate(args.delegate)
    delegate_args = [
        "--backend-root",
        args.backend_root,
        "--images",
        *[frame.as_posix() for frame in frames],
        "--output-dir",
        output_dir.as_posix(),
        "--output-name",
        args.output_name,
    ]
    if args.model_ref:
        delegate_args.extend(["--model-ref", args.model_ref])
    returncode = delegate.main(delegate_args)
    print(
        json.dumps(
            {
                "status": "proposal" if returncode == 0 else "blocked",
                "video": video_path.as_posix(),
                "sampled_frames": [frame.as_posix() for frame in frames],
                "delegate": args.delegate,
            },
            indent=2,
            sort_keys=False,
        )
    )
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
