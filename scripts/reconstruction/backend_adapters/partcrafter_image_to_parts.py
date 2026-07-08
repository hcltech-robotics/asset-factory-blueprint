from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


SUPPORTED_3D_SUFFIXES = {".glb", ".obj", ".ply"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="partcrafter_image_to_parts")
    parser.add_argument("--backend-root", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model-ref", default="wgsxm/PartCrafter")
    parser.add_argument("--output-name", default="asset.glb")
    parser.add_argument("--num-parts", type=int, default=4)
    parser.add_argument("--tag")
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--no-rmbg", action="store_true")
    parser.add_argument("--segment-prior-manifest")
    parser.add_argument("--use-segment-prior-image", action="store_true")
    parser.add_argument("--use-segment-prior-count", action="store_true")
    return parser


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_tag(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return cleaned[:80] or "asset"


def unique_copy(source: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if not target.exists():
        shutil.copy2(source, target)
        return target
    stem = source.stem
    suffix = source.suffix
    index = 1
    while True:
        candidate = target_dir / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            shutil.copy2(source, candidate)
            return candidate
        index += 1


def discover_outputs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_3D_SUFFIXES],
        key=lambda path: (path.suffix.lower() != ".glb", -path.stat().st_size, path.as_posix()),
    )


def choose_primary(copied: list[Path]) -> Path | None:
    glbs = [path for path in copied if path.suffix.lower() == ".glb"]
    candidates = glbs or copied
    if not candidates:
        return None
    preferred_tokens = ("object", "asset", "combined", "final", "mesh")
    for token in preferred_tokens:
        matches = [path for path in candidates if token in path.stem.lower()]
        if matches:
            return max(matches, key=lambda path: path.stat().st_size)
    return max(candidates, key=lambda path: path.stat().st_size)


def write_parts_manifest(
    manifest_path: Path,
    *,
    image_path: Path,
    backend_root: Path,
    model_ref: str,
    tag: str,
    num_parts: int,
    primary_asset: Path | None,
    part_assets: list[Path],
    segment_prior: dict | None = None,
    effective_image_path: Path | None = None,
) -> None:
    payload = {
        "id": f"{tag}_partcrafter_parts",
        "version": "1.0",
        "status": "proposal" if part_assets else "blocked",
        "backend": "partcrafter",
        "model_ref": model_ref,
        "backbone": "triposg",
        "source_image": image_path.as_posix(),
        "effective_image": effective_image_path.as_posix() if effective_image_path else image_path.as_posix(),
        "segment_prior_manifest": str(segment_prior.get("manifest_path", "")) if segment_prior else "",
        "segment_prior_status": str(segment_prior.get("status", "")) if segment_prior else "",
        "backend_root": backend_root.as_posix(),
        "tag": tag,
        "requested_num_parts": num_parts,
        "primary_asset": primary_asset.as_posix() if primary_asset else "",
        "parts": [
            {
                "part_id": f"part_{index:02d}",
                "asset_path": path.as_posix(),
                "kind": "part_mesh" if path != primary_asset else "primary_mesh",
                "checksum": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
            for index, path in enumerate(part_assets)
        ],
    }
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def load_segment_prior(path: str | None) -> dict | None:
    if not path:
        return None
    manifest_path = Path(path).resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["manifest_path"] = manifest_path.as_posix()
    return payload


def apply_segment_prior(
    image_path: Path,
    num_parts: int,
    segment_prior: dict | None,
    *,
    use_prior_image: bool,
    use_prior_count: bool,
) -> tuple[Path, int, list[str]]:
    warnings: list[str] = []
    effective_image = image_path
    effective_num_parts = num_parts
    if not segment_prior:
        return effective_image, effective_num_parts, warnings
    bias = segment_prior.get("partcrafter_bias", {}) if isinstance(segment_prior, dict) else {}
    if use_prior_image:
        candidate = Path(str(bias.get("image_path", "")))
        if candidate.exists():
            effective_image = candidate
        else:
            warnings.append("segment prior image path did not exist")
    if use_prior_count:
        try:
            suggested = int(bias.get("num_parts", 0))
        except (TypeError, ValueError):
            suggested = 0
        if suggested > 0:
            effective_num_parts = suggested
        else:
            warnings.append("segment prior did not include a positive part count")
    return effective_image, effective_num_parts, warnings


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    backend_root = Path(args.backend_root).resolve()
    image_path = Path(args.image).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    tag = safe_tag(args.tag or f"afb_{image_path.stem}")
    segment_prior = load_segment_prior(args.segment_prior_manifest)
    effective_image_path, effective_num_parts, prior_warnings = apply_segment_prior(
        image_path,
        args.num_parts,
        segment_prior,
        use_prior_image=args.use_segment_prior_image,
        use_prior_count=args.use_segment_prior_count,
    )

    command = [
        sys.executable,
        "scripts/inference_partcrafter.py",
        "--image_path",
        str(effective_image_path),
        "--num_parts",
        str(effective_num_parts),
        "--tag",
        tag,
    ]
    if not args.no_rmbg:
        command.append("--rmbg")
    if args.render:
        command.append("--render")

    result = subprocess.run(
        command,
        cwd=backend_root,
        text=True,
        capture_output=True,
        check=False,
        timeout=None,
    )
    result_root = backend_root / "results" / tag
    generated = discover_outputs(result_root)
    parts_dir = output_dir / "parts"
    copied = [unique_copy(path, parts_dir) for path in generated]
    primary = choose_primary(copied)
    primary_output = output_dir / args.output_name
    if primary and primary.suffix.lower() == primary_output.suffix.lower():
        shutil.copy2(primary, primary_output)
        primary = primary_output
        copied = [primary if path.name == primary.name else path for path in copied]

    manifest_path = output_dir / "parts-manifest.json"
    write_parts_manifest(
        manifest_path,
        image_path=image_path,
        backend_root=backend_root,
        model_ref=args.model_ref,
        tag=tag,
        num_parts=effective_num_parts,
        primary_asset=primary,
        part_assets=copied,
        segment_prior=segment_prior,
        effective_image_path=effective_image_path,
    )

    status = "proposal" if primary_output.exists() and copied else "blocked"
    print(
        json.dumps(
            {
                "status": status,
                "backend": "partcrafter",
                "model_ref": args.model_ref,
                "backbone": "triposg",
                "output_path": primary_output.as_posix() if primary_output.exists() else "",
                "parts_manifest": manifest_path.as_posix(),
                "part_asset_count": len(copied),
                "effective_image": effective_image_path.as_posix(),
                "effective_num_parts": effective_num_parts,
                "segment_prior_manifest": str(segment_prior.get("manifest_path", "")) if segment_prior else "",
                "warnings": prior_warnings,
                "returncode": result.returncode,
                "stdout_tail": result.stdout[-4000:],
                "stderr_tail": result.stderr[-4000:],
            },
            indent=2,
            sort_keys=False,
        )
    )
    return 0 if status == "proposal" and result.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
