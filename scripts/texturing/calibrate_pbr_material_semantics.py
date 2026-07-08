from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

MAP_KEYS = {
    "base_color": "base_color_path",
    "normal": "normal_path",
    "roughness": "roughness_path",
    "metallic": "metallic_path",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Calibrate PBR maps using semantic material rules.")
    parser.add_argument("--input-manifest", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--checksums", required=True)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--asset-id", default="asset")
    parser.add_argument("--dielectric-target", action="append", default=[], help="variant_id or variant_id:segment_id target for metallic=0.")
    parser.add_argument(
        "--roughness-floor",
        action="append",
        default=[],
        help="target=value where target is variant_id or variant_id:segment_id and value is 0-255.",
    )
    parser.add_argument(
        "--roughness-range",
        action="append",
        default=[],
        help="target=min:max where target is variant_id or variant_id:segment_id and min/max are 0-255.",
    )
    parser.add_argument(
        "--base-detail-strength",
        action="append",
        default=[],
        help="target=value where 0 means blurred base colour and 1 keeps source detail.",
    )
    parser.add_argument(
        "--base-colour-tint",
        action="append",
        default=[],
        help="target=#rrggbb:strength where strength is 0-1.",
    )
    parser.add_argument(
        "--normal-strength",
        action="append",
        default=[],
        help="target=value where 0 means flat normal and 1 keeps source normal detail.",
    )
    parser.add_argument(
        "--variant-rename",
        action="append",
        default=[],
        help="source_variant=output_variant. Rules still match the source variant id.",
    )
    return parser


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value).strip("_") or "texture"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def parse_roughness(values: list[str]) -> dict[str, int]:
    floors: dict[str, int] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"roughness floor must contain =: {value}")
        target, raw = value.split("=", 1)
        floors[target.strip()] = max(0, min(255, int(raw.strip())))
    return floors


def parse_float_rules(values: list[str], *, minimum: float = 0.0, maximum: float = 1.0) -> dict[str, float]:
    rules: dict[str, float] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"rule must contain =: {value}")
        target, raw = value.split("=", 1)
        rules[target.strip()] = max(minimum, min(maximum, float(raw.strip())))
    return rules


def parse_roughness_ranges(values: list[str]) -> dict[str, tuple[int, int]]:
    ranges: dict[str, tuple[int, int]] = {}
    for value in values:
        if "=" not in value or ":" not in value:
            raise ValueError(f"roughness range must be target=min:max: {value}")
        target, raw = value.split("=", 1)
        raw_min, raw_max = raw.split(":", 1)
        low = max(0, min(255, int(raw_min.strip())))
        high = max(0, min(255, int(raw_max.strip())))
        if high < low:
            low, high = high, low
        ranges[target.strip()] = (low, high)
    return ranges


def parse_tint_rules(values: list[str]) -> dict[str, tuple[tuple[int, int, int], float]]:
    rules: dict[str, tuple[tuple[int, int, int], float]] = {}
    for value in values:
        if "=" not in value or ":" not in value:
            raise ValueError(f"base colour tint must be target=#rrggbb:strength: {value}")
        target, raw = value.split("=", 1)
        colour_raw, strength_raw = raw.rsplit(":", 1)
        colour_raw = colour_raw.strip()
        if not colour_raw.startswith("#") or len(colour_raw) != 7:
            raise ValueError(f"base colour tint must use #rrggbb: {value}")
        colour = tuple(int(colour_raw[index : index + 2], 16) for index in (1, 3, 5))
        strength = max(0.0, min(1.0, float(strength_raw.strip())))
        rules[target.strip()] = (colour, strength)
    return rules


def parse_variant_renames(values: list[str]) -> dict[str, str]:
    renames: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"variant rename must contain =: {value}")
        source, target = value.split("=", 1)
        source_id = safe_name(source.strip())
        target_id = safe_name(target.strip())
        if source_id and target_id:
            renames[source_id] = target_id
    return renames


def rename_variant_value(value: str, variant_renames: dict[str, str]) -> str:
    safe_value = safe_name(value)
    if safe_value in variant_renames:
        return variant_renames[safe_value]
    for source, target in variant_renames.items():
        if safe_value.startswith(f"{source}_"):
            return f"{target}_{safe_value[len(source) + 1:]}"
    return value


def target_keys(variant_id: str, segment_id: str | None) -> list[str]:
    if segment_id:
        return [f"{variant_id}:{segment_id}", f"{variant_id}:*"]
    return [variant_id]


def matched_value(rules: dict[str, int], variant_id: str, segment_id: str | None) -> int | None:
    for key in target_keys(variant_id, segment_id):
        if key in rules:
            return rules[key]
    return None


def matched_rule(rules: dict[str, Any], variant_id: str, segment_id: str | None) -> Any:
    for key in target_keys(variant_id, segment_id):
        if key in rules:
            return rules[key]
    return None


def is_dielectric(targets: set[str], variant_id: str, segment_id: str | None) -> bool:
    return any(key in targets for key in target_keys(variant_id, segment_id))


def resolve_manifest_path(value: str, project_root: Path, manifest_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    root_candidate = project_root / value
    if root_candidate.exists():
        return root_candidate
    return manifest_dir / value


def relative_to_root(path: Path, project_root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(project_root).as_posix()
    except ValueError:
        return resolved.as_posix()


def copy_or_calibrate_map(
    *,
    source: Path,
    destination: Path,
    map_kind: str,
    variant_id: str,
    segment_id: str | None,
    dielectric_targets: set[str],
    roughness_floors: dict[str, int],
    roughness_ranges: dict[str, tuple[int, int]],
    base_detail_strengths: dict[str, float],
    base_colour_tints: dict[str, tuple[tuple[int, int, int], float]],
    normal_strengths: dict[str, float],
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "map_kind": map_kind,
        "source": source.as_posix(),
        "output": destination.as_posix(),
        "operation": "copied",
    }
    if map_kind == "metallic" and is_dielectric(dielectric_targets, variant_id, segment_id):
        image = Image.open(source)
        zero = Image.new("L", image.size, 0)
        zero.convert("RGB").save(destination)
        record["operation"] = "metallic_forced_dielectric"
        record["target_value"] = 0
        return record
    base_detail_strength = matched_rule(base_detail_strengths, variant_id, segment_id)
    base_colour_tint = matched_rule(base_colour_tints, variant_id, segment_id)
    if map_kind == "base_color" and (base_detail_strength is not None or base_colour_tint is not None):
        image = Image.open(source).convert("RGB")
        values = np.asarray(image, dtype=np.float32)
        operations = []
        if base_detail_strength is not None:
            strength = max(0.0, min(1.0, float(base_detail_strength)))
            blurred = np.asarray(image.filter(ImageFilter.GaussianBlur(radius=10.0)), dtype=np.float32)
            values = blurred * (1.0 - strength) + values * strength
            operations.append({"operation": "base_detail_strength_applied", "strength": strength})
        if base_colour_tint is not None:
            colour, strength = base_colour_tint
            tint = np.zeros_like(values)
            tint[:, :, 0] = colour[0]
            tint[:, :, 1] = colour[1]
            tint[:, :, 2] = colour[2]
            values = values * (1.0 - strength) + tint * strength
            operations.append({"operation": "base_colour_tint_applied", "colour": f"#{colour[0]:02x}{colour[1]:02x}{colour[2]:02x}", "strength": strength})
        Image.fromarray(np.clip(values, 0, 255).astype(np.uint8), mode="RGB").save(destination)
        record["operation"] = "base_colour_polish_calibrated"
        record["operations"] = operations
        return record
    roughness_floor = matched_value(roughness_floors, variant_id, segment_id)
    roughness_range = matched_rule(roughness_ranges, variant_id, segment_id)
    if map_kind == "roughness" and (roughness_floor is not None or roughness_range is not None):
        image = Image.open(source).convert("L")
        values = np.asarray(image, dtype=np.uint8)
        adjusted = values
        if roughness_floor is not None:
            adjusted = np.maximum(adjusted, np.uint8(roughness_floor))
            record["floor"] = int(roughness_floor)
        if roughness_range is not None:
            low, high = roughness_range
            adjusted = np.clip(adjusted, np.uint8(low), np.uint8(high))
            record["range"] = [int(low), int(high)]
        Image.fromarray(adjusted.astype(np.uint8), mode="L").convert("RGB").save(destination)
        record["operation"] = "roughness_range_applied" if roughness_range is not None else "roughness_floor_applied"
        record["before_mean"] = float(values.mean())
        record["after_mean"] = float(adjusted.mean())
        return record
    normal_strength = matched_rule(normal_strengths, variant_id, segment_id)
    if map_kind == "normal" and normal_strength is not None:
        strength = max(0.0, min(1.0, float(normal_strength)))
        image = Image.open(source).convert("RGB")
        values = np.asarray(image, dtype=np.float32)
        flat = np.zeros_like(values)
        flat[:, :, 0] = 128.0
        flat[:, :, 1] = 128.0
        flat[:, :, 2] = 255.0
        adjusted = flat * (1.0 - strength) + values * strength
        Image.fromarray(np.clip(adjusted, 0, 255).astype(np.uint8), mode="RGB").save(destination)
        record["operation"] = "normal_strength_applied"
        record["strength"] = strength
        return record
    shutil.copy2(source, destination)
    return record


def calibrate_record(
    *,
    record: dict[str, Any],
    variant_id: str,
    segment_id: str | None,
    output_variant_id: str | None,
    output_dir: Path,
    project_root: Path,
    manifest_dir: Path,
    dielectric_targets: set[str],
    roughness_floors: dict[str, int],
    roughness_ranges: dict[str, tuple[int, int]],
    base_detail_strengths: dict[str, float],
    base_colour_tints: dict[str, tuple[tuple[int, int, int], float]],
    normal_strengths: dict[str, float],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[Path]]:
    updated = dict(record)
    operations: list[dict[str, Any]] = []
    files: list[Path] = []
    prefix_parts = [safe_name(output_variant_id or variant_id)]
    if segment_id:
        prefix_parts.append(safe_name(segment_id))
    prefix = "_".join(prefix_parts)
    for map_kind, path_key in MAP_KEYS.items():
        value = str(record.get(path_key, ""))
        if not value:
            continue
        source = resolve_manifest_path(value, project_root, manifest_dir)
        destination = output_dir / f"{prefix}_{map_kind}.png"
        operation = copy_or_calibrate_map(
            source=source,
            destination=destination,
            map_kind=map_kind,
            variant_id=variant_id,
            segment_id=segment_id,
            dielectric_targets=dielectric_targets,
            roughness_floors=roughness_floors,
            roughness_ranges=roughness_ranges,
            base_detail_strengths=base_detail_strengths,
            base_colour_tints=base_colour_tints,
            normal_strengths=normal_strengths,
        )
        operations.append(operation)
        files.append(destination)
        updated[path_key] = relative_to_root(destination, project_root)
    updated.setdefault("material_semantics_calibrated", True)
    return updated, operations, files


def calibrate_manifest(args: argparse.Namespace) -> dict[str, Any]:
    project_root = Path(args.project_root).resolve()
    input_manifest = Path(args.input_manifest).resolve()
    manifest_dir = input_manifest.parent
    output_dir = Path(args.output_dir).resolve()
    source = json.loads(input_manifest.read_text(encoding="utf-8"))
    dielectric_targets = {str(item).strip() for item in args.dielectric_target if str(item).strip()}
    roughness_floors = parse_roughness([str(item) for item in args.roughness_floor])
    roughness_ranges = parse_roughness_ranges([str(item) for item in args.roughness_range])
    base_detail_strengths = parse_float_rules([str(item) for item in args.base_detail_strength])
    base_colour_tints = parse_tint_rules([str(item) for item in args.base_colour_tint])
    normal_strengths = parse_float_rules([str(item) for item in args.normal_strength])
    variant_renames = parse_variant_renames([str(item) for item in args.variant_rename])
    texture_outputs: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    output_files: list[Path] = []
    for item in source.get("texture_outputs", []):
        variant_id = safe_name(str(item.get("variant_id", "variant")))
        output_variant_id = variant_renames.get(variant_id, variant_id)
        updated, record_operations, files = calibrate_record(
            record=item,
            variant_id=variant_id,
            segment_id=None,
            output_variant_id=output_variant_id,
            output_dir=output_dir,
            project_root=project_root,
            manifest_dir=manifest_dir,
            dielectric_targets=dielectric_targets,
            roughness_floors=roughness_floors,
            roughness_ranges=roughness_ranges,
            base_detail_strengths=base_detail_strengths,
            base_colour_tints=base_colour_tints,
            normal_strengths=normal_strengths,
        )
        updated["variant_id"] = output_variant_id
        operations.extend(record_operations)
        output_files.extend(files)
        segment_records: list[dict[str, Any]] = []
        for segment in item.get("segment_materials", []):
            segment_id = safe_name(str(segment.get("segment_id", "segment")))
            updated_segment, segment_operations, segment_files = calibrate_record(
                record=segment,
                variant_id=variant_id,
                segment_id=segment_id,
                output_variant_id=output_variant_id,
                output_dir=output_dir,
                project_root=project_root,
                manifest_dir=manifest_dir,
                dielectric_targets=dielectric_targets,
                roughness_floors=roughness_floors,
                roughness_ranges=roughness_ranges,
                base_detail_strengths=base_detail_strengths,
                base_colour_tints=base_colour_tints,
                normal_strengths=normal_strengths,
            )
            updated_segment["segment_id"] = segment_id
            if "variant_id" in updated_segment:
                updated_segment["variant_id"] = f"{output_variant_id}_{segment_id}"
            segment_records.append(updated_segment)
            operations.extend(segment_operations)
            output_files.extend(segment_files)
        updated["segment_materials"] = segment_records
        texture_outputs.append(updated)
    calibrated = dict(source)
    calibrated["texture_outputs"] = texture_outputs
    calibrated["provider_trace"] = rename_trace_variants(source.get("provider_trace", []), variant_renames)
    calibrated["texture_map_policy_trace"] = rename_trace_variants(source.get("texture_map_policy_trace", []), variant_renames)
    calibrated["texture_generation_status"] = source.get("texture_generation_status", "generated")
    calibrated["material_semantics_calibration"] = {
        "status": "applied",
        "dielectric_targets": sorted(dielectric_targets),
        "roughness_floors": roughness_floors,
        "roughness_ranges": {key: list(value) for key, value in roughness_ranges.items()},
        "base_detail_strengths": base_detail_strengths,
        "base_colour_tints": {
            key: {"colour": f"#{value[0][0]:02x}{value[0][1]:02x}{value[0][2]:02x}", "strength": value[1]}
            for key, value in base_colour_tints.items()
        },
        "normal_strengths": normal_strengths,
        "variant_renames": variant_renames,
    }
    changed = [item for item in operations if item.get("operation") != "copied"]
    return {
        "manifest": calibrated,
        "report": {
            "id": f"{args.asset_id}_pbr_material_semantics_calibration_v1",
            "version": "1.0",
            "asset_id": args.asset_id,
            "status": "pass" if changed else "blocked",
            "input_manifest": input_manifest.as_posix(),
            "output_manifest": Path(args.manifest).resolve().as_posix(),
            "output_dir": output_dir.as_posix(),
            "operation_count": len(operations),
            "changed_operation_count": len(changed),
            "operations": operations,
        },
        "files": output_files,
    }


def rename_trace_variants(trace: Any, variant_renames: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(trace, list):
        return []
    renamed = []
    for item in trace:
        if not isinstance(item, dict):
            continue
        updated = dict(item)
        if "parent_variant_id" in updated:
            updated["parent_variant_id"] = rename_variant_value(str(updated["parent_variant_id"]), variant_renames)
        if "variant_id" in updated:
            updated["variant_id"] = rename_variant_value(str(updated["variant_id"]), variant_renames)
        renamed.append(updated)
    return renamed


def write_checksums(path: Path, files: list[Path]) -> None:
    write_json(
        path,
        {
            "files": [
                {
                    "path": item.as_posix(),
                    "sha256": sha256_file(item),
                    "size_bytes": item.stat().st_size,
                }
                for item in files
                if item.exists()
            ]
        },
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = calibrate_manifest(args)
    manifest_path = Path(args.manifest).resolve()
    report_path = Path(args.report).resolve()
    checksums_path = Path(args.checksums).resolve()
    write_json(manifest_path, result["manifest"])
    write_json(report_path, result["report"])
    write_checksums(checksums_path, [manifest_path, report_path, *result["files"]])
    print(
        json.dumps(
            {
                "status": result["report"]["status"],
                "manifest": manifest_path.as_posix(),
                "report": report_path.as_posix(),
                "changed_operation_count": result["report"]["changed_operation_count"],
            },
            indent=2,
            sort_keys=False,
        )
    )
    return 0 if result["report"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
