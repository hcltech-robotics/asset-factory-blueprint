from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="triposg_image_to_glb")
    parser.add_argument("--backend-root", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model-ref", default="VAST-AI/TripoSG")
    parser.add_argument("--output-name", default="asset.glb")
    parser.add_argument("--faces", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    backend_root = Path(args.backend_root).resolve()
    image_path = Path(args.image).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / args.output_name

    command = [
        sys.executable,
        "-m",
        "scripts.inference_triposg",
        "--image-input",
        str(image_path),
        "--output-path",
        str(output_path),
    ]
    if args.faces:
        command.extend(["--faces", str(args.faces)])

    result = subprocess.run(
        command,
        cwd=backend_root,
        text=True,
        capture_output=True,
        check=False,
        timeout=None,
    )
    if result.returncode != 0:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "backend": "triposg",
                    "output_path": output_path.as_posix(),
                    "returncode": result.returncode,
                    "stdout_tail": result.stdout[-4000:],
                    "stderr_tail": result.stderr[-4000:],
                },
                indent=2,
                sort_keys=False,
            )
        )
        return result.returncode

    if not output_path.exists():
        candidates = sorted(backend_root.rglob("*.glb"), key=lambda path: path.stat().st_mtime, reverse=True)
        if candidates:
            shutil.copy2(candidates[0], output_path)

    status = "proposal" if output_path.exists() else "blocked"
    print(
        json.dumps(
            {
                "status": status,
                "backend": "triposg",
                "model_ref": args.model_ref,
                "output_path": output_path.as_posix() if output_path.exists() else "",
            },
            indent=2,
            sort_keys=False,
        )
    )
    return 0 if output_path.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
