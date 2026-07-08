from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from asset_factory_blueprint.services.live_textures import build_live_texture_request_plan, generate_live_texture_sets
from asset_factory_blueprint.utils.checksums import sha256_file


MAP_KINDS = ("base_color", "normal", "roughness", "metallic")
MAP_PATH_KEYS = tuple(f"{kind}_path" for kind in MAP_KINDS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run live per-segment texture generation from an existing texture manifest.")
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--asset-id", default="asset")
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--model", default="")
    parser.add_argument("--texture-size", type=int, default=1024)
    parser.add_argument("--quality", default="medium")
    parser.add_argument("--object-prompt", default="")
    return parser


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def sanitise_error(value: str) -> str:
    redacted = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-redacted", value)
    redacted = re.sub(r"nvapi-[A-Za-z0-9_-]+", "nvapi-redacted", redacted)
    redacted = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer redacted", redacted, flags=re.IGNORECASE)
    return redacted


def relative_output_dir(output_dir: Path, project_root: Path) -> Path:
    resolved_output = output_dir.resolve()
    resolved_root = project_root.resolve()
    try:
        return resolved_output.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("output_dir must be inside project_root") from exc


def live_map_paths(output_rel: Path, prefix: str) -> dict[str, str]:
    return {f"{kind}_path": (output_rel / f"{prefix}_{kind}.png").as_posix() for kind in MAP_KINDS}


def safe_slug(value: str) -> str:
    slug = "".join(char.lower() if char.isalnum() else "_" for char in value).strip("_")
    return slug or "texture"


def build_live_texture_records(source_manifest: dict[str, Any], output_rel: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for source in source_manifest.get("texture_outputs", []):
        if not isinstance(source, dict):
            continue
        variant_id = safe_slug(str(source.get("variant_id") or "variant"))
        record = {
            "variant_id": variant_id,
            "material_name": str(source.get("material_name") or variant_id),
            "texture_intent": str(source.get("texture_intent") or source.get("prompt") or variant_id.replace("_", " ")),
            "prompt": str(source.get("prompt") or source.get("texture_intent") or variant_id.replace("_", " ")),
            "negative_prompt": str(
                source.get("negative_prompt")
                or "object silhouette, labels, logos, baked lighting, cast shadows, perspective"
            ),
            "provider_role": "texture_generator",
            "segment_materials": [],
            **live_map_paths(output_rel, variant_id),
        }
        for segment in source.get("segment_materials", []):
            if not isinstance(segment, dict):
                continue
            segment_id = safe_slug(str(segment.get("segment_id") or "segment"))
            segment_prefix = f"{variant_id}_{segment_id}"
            segment_record = {
                "variant_id": segment_prefix,
                "segment_id": segment_id,
                "material_name": str(segment.get("material_name") or f"{variant_id} {segment_id}"),
                "texture_intent": str(segment.get("texture_intent") or segment.get("prompt") or record["texture_intent"]),
                "prompt": str(segment.get("prompt") or segment.get("texture_intent") or record["prompt"]),
                "negative_prompt": str(segment.get("negative_prompt") or record["negative_prompt"]),
                "provider_role": "texture_generator",
                **live_map_paths(output_rel, segment_prefix),
            }
            record["segment_materials"].append(segment_record)
        records.append(record)
    return records


def manifest_payload(
    *,
    asset_id: str,
    status: str,
    backend: str,
    records: list[dict[str, Any]],
    provider_trace: list[dict[str, Any]],
    map_policy_trace: list[dict[str, Any]],
    texture_prompt_plan: list[dict[str, Any]],
    blocked_reasons: list[str],
) -> dict[str, Any]:
    return {
        "id": f"{asset_id}_live_texture_generation",
        "version": "1.0",
        "asset_id": asset_id,
        "texture_generation_status": status,
        "texture_generation_backend": backend,
        "texture_blocked_reasons": blocked_reasons,
        "provider_trace": provider_trace,
        "texture_map_policy_trace": map_policy_trace,
        "texture_prompt_plan": texture_prompt_plan,
        "texture_outputs": records,
        "texture_variants": records,
        "validation_status": "proposal" if status == "generated" else "blocked",
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = Path(args.project_root).resolve()
    source_manifest_path = Path(args.source_manifest).resolve()
    output_dir = Path(args.output_dir).resolve()
    manifest_path = Path(args.manifest).resolve()
    report_path = Path(args.report).resolve()
    source_manifest = load_json(source_manifest_path)
    output_rel = relative_output_dir(output_dir, project_root)
    records = build_live_texture_records(source_manifest, output_rel)
    constraints = {
        "asset_id": args.asset_id,
        "object_prompt": args.object_prompt,
        "texture_provider": args.provider,
        "texture_model": args.model or None,
        "texture_size": int(args.texture_size),
        "texture_quality": args.quality,
    }
    files: list[Path] = []
    blocked_reasons: list[str] = []
    provider_trace: list[dict[str, Any]] = []
    map_policy_trace: list[dict[str, Any]] = []
    texture_prompt_plan = build_live_texture_request_plan(project_root, records, constraints)["texture_prompt_plan"]
    status = "blocked"
    backend = f"{args.provider}_images_api"
    try:
        result = generate_live_texture_sets(project_root, records, constraints)
        status = str(result.get("status") or "blocked")
        records = list(result.get("texture_variants", []))
        provider_trace = list(result.get("provider_trace", []))
        map_policy_trace = list(result.get("map_policy_trace", []))
        texture_prompt_plan = list(result.get("texture_prompt_plan", texture_prompt_plan))
        files = [Path(item) for item in result.get("files", [])]
        blocked_reasons = [str(item) for item in result.get("blocked_reasons", [])]
        backend = str(result.get("backend") or backend)
    except Exception as exc:
        blocked_reasons = [sanitise_error(str(exc))]
        if output_dir.exists():
            files = sorted(path for path in output_dir.glob("*.png") if path.is_file())

    manifest = manifest_payload(
        asset_id=args.asset_id,
        status=status,
        backend=backend,
        records=records,
        provider_trace=provider_trace,
        map_policy_trace=map_policy_trace,
        texture_prompt_plan=texture_prompt_plan,
        blocked_reasons=blocked_reasons,
    )
    write_json(manifest_path, manifest)
    checksum_records = [
        {"path": path.as_posix(), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
        for path in files
        if path.exists()
    ]
    resolved_model = next(
        (
            str(item.get("model"))
            for item in texture_prompt_plan
            if isinstance(item, dict) and item.get("role") == "texture_generator" and item.get("model")
        ),
        args.model or "provider_default",
    )
    report = {
        "status": status,
        "asset_id": args.asset_id,
        "source_manifest": source_manifest_path.as_posix(),
        "texture_manifest": manifest_path.as_posix(),
        "output_dir": output_dir.as_posix(),
        "provider": args.provider,
        "model": resolved_model,
        "provider_trace_count": len(provider_trace),
        "texture_prompt_plan_count": len(texture_prompt_plan),
        "partial_output_count": len(files) if status != "generated" else 0,
        "blocked_reasons": blocked_reasons,
        "files": checksum_records,
    }
    write_json(report_path, report)
    print(
        json.dumps(
            {
                "status": status,
                "manifest": manifest_path.as_posix(),
                "report": report_path.as_posix(),
                "blocked_reasons": blocked_reasons,
                "provider_trace_count": len(provider_trace),
            },
            indent=2,
            sort_keys=False,
        )
    )
    return 0 if status == "generated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
