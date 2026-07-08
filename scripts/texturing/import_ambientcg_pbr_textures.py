from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance

from asset_factory_blueprint import __version__
from asset_factory_blueprint.utils.checksums import sha256_file


MAP_PATH_KEYS = {
    "base_color": "base_color_path",
    "normal": "normal_path",
    "roughness": "roughness_path",
    "metallic": "metallic_path",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import CC0 ambientCG PBR material maps into an Asset Factory texture manifest.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--checksums", required=True)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--asset-id", default="asset")
    parser.add_argument("--attribute", default="1K-JPG")
    parser.add_argument("--query", action="append", default=[], help="Search ambientCG and print matching material ids.")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--variant",
        action="append",
        default=[],
        help="Variant mapping as variant_id=ambientcg_asset_id. Repeat for each variant.",
    )
    parser.add_argument(
        "--segment",
        action="append",
        default=[],
        help="Segment mapping as variant_id:segment_id=ambientcg_asset_id. Overrides the variant asset for that segment.",
    )
    parser.add_argument(
        "--tint",
        action="append",
        default=[],
        help="Optional albedo tint as variant_id[:segment_id]=R,G,B with 0-255 values.",
    )
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--user-agent", default=f"asset-factory-blueprint-texture-import/{__version__}")
    return parser


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def safe_slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "_" for char in value).strip("_") or "texture"


def parse_mapping(values: list[str], separator: str = "=") -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if separator not in value:
            raise ValueError(f"mapping must contain {separator}: {value}")
        key, raw_item = value.split(separator, 1)
        key = key.strip()
        raw_item = raw_item.strip()
        if not key or not raw_item:
            raise ValueError(f"mapping is incomplete: {value}")
        parsed[key] = raw_item
    return parsed


def parse_tints(values: list[str]) -> dict[str, tuple[int, int, int]]:
    parsed: dict[str, tuple[int, int, int]] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"tint must contain =: {value}")
        key, raw_colour = value.split("=", 1)
        channels = [int(item.strip()) for item in raw_colour.split(",")]
        if len(channels) != 3:
            raise ValueError(f"tint must have three channels: {value}")
        parsed[key.strip()] = tuple(max(0, min(255, channel)) for channel in channels)
    return parsed


def ambientcg_json(params: dict[str, str], user_agent: str) -> dict[str, Any]:
    url = "https://ambientCG.com/api/v2/full_json?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def search(query: str, limit: int, user_agent: str) -> dict[str, Any]:
    payload = ambientcg_json(
        {
            "q": query,
            "type": "Material",
            "sort": "Popular",
            "include": "displayData,tagData,mapData",
            "limit": str(max(1, min(25, limit))),
        },
        user_agent,
    )
    records = []
    for asset in payload.get("foundAssets", [])[:limit]:
        records.append(
            {
                "asset_id": asset.get("assetId", ""),
                "display_name": asset.get("displayName", ""),
                "creation_method": asset.get("creationMethodName", ""),
                "maps": asset.get("maps", []),
                "tags": asset.get("tags", [])[:16],
                "short_link": asset.get("shortLink", ""),
            }
        )
    return {"query": query, "result_count": payload.get("numberOfResults", 0), "results": records}


def asset_metadata(asset_id: str, user_agent: str) -> dict[str, Any]:
    payload = ambientcg_json(
        {
            "id": asset_id,
            "include": "downloadData,displayData,tagData,mapData,previewData",
        },
        user_agent,
    )
    assets = payload.get("foundAssets", [])
    if not assets:
        raise ValueError(f"ambientCG asset not found: {asset_id}")
    return assets[0]


def select_download(asset: dict[str, Any], attribute: str) -> dict[str, Any]:
    folders = asset.get("downloadFolders", {}) or {}
    downloads = folders.get("default", {}).get("downloadFiletypeCategories", {}).get("zip", {}).get("downloads", [])
    for item in downloads:
        if str(item.get("attribute", "")).lower() == attribute.lower():
            return item
    available = [str(item.get("attribute", "")) for item in downloads]
    raise ValueError(f"{asset.get('assetId', '')} does not expose {attribute}; available: {available}")


def download_zip(download: dict[str, Any], user_agent: str) -> bytes:
    request = urllib.request.Request(str(download["downloadLink"]), headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=180) as response:
        return response.read()


def map_kind_for_member(name: str) -> str:
    lowered = Path(name).stem.lower()
    if "color" in lowered or "diffuse" in lowered or "albedo" in lowered:
        return "base_color"
    if "normalgl" in lowered or "normalogl" in lowered or "normal" in lowered:
        return "normal"
    if "roughness" in lowered or "rough" in lowered:
        return "roughness"
    if "metalness" in lowered or "metallic" in lowered:
        return "metallic"
    return ""


def sort_member_priority(name: str) -> int:
    lowered = Path(name).stem.lower()
    if "normalgl" in lowered or "normalogl" in lowered:
        return 0
    if "normaldx" in lowered:
        return 1
    return 2


def read_map_images(zip_bytes: bytes) -> dict[str, Image.Image]:
    maps: dict[str, Image.Image] = {}
    with zipfile.ZipFile(BytesIO(zip_bytes)) as archive:
        members = sorted(
            [item for item in archive.namelist() if not item.endswith("/")],
            key=sort_member_priority,
        )
        for member in members:
            kind = map_kind_for_member(member)
            if not kind or kind in maps:
                continue
            with archive.open(member) as fp:
                maps[kind] = Image.open(fp).convert("RGB").copy()
    missing = [kind for kind in MAP_PATH_KEYS if kind not in maps]
    if missing:
        raise ValueError(f"downloaded archive is missing required maps: {missing}")
    return maps


def apply_tint(image: Image.Image, tint: tuple[int, int, int] | None) -> Image.Image:
    if tint is None:
        return image
    overlay = Image.new("RGB", image.size, tint)
    desaturated = ImageEnhance.Color(image).enhance(0.35)
    tinted = Image.blend(desaturated, overlay, 0.62)
    return ImageEnhance.Contrast(tinted).enhance(1.08)


def save_maps(
    *,
    maps: dict[str, Image.Image],
    output_dir: Path,
    prefix: str,
    size: int,
    tint: tuple[int, int, int] | None,
) -> dict[str, str]:
    output: dict[str, str] = {}
    output_dir.mkdir(parents=True, exist_ok=True)
    for kind, path_key in MAP_PATH_KEYS.items():
        image = maps[kind]
        if kind == "base_color":
            image = apply_tint(image, tint)
        resized = image.resize((size, size), Image.Resampling.LANCZOS)
        if kind in {"roughness", "metallic"}:
            resized = resized.convert("L").convert("RGB")
        path = output_dir / f"{prefix}_{kind}.png"
        resized.save(path)
        output[path_key] = path.as_posix()
    return output


def source_record(asset: dict[str, Any], download: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": "ambientCG",
        "asset_id": asset.get("assetId", ""),
        "display_name": asset.get("displayName", ""),
        "short_link": asset.get("shortLink", ""),
        "creation_method": asset.get("creationMethodName", ""),
        "download_link": download.get("downloadLink", ""),
        "file_name": download.get("fileName", ""),
        "attribute": download.get("attribute", ""),
        "license": "CC0",
    }


def manifest_paths_to_relative(record: dict[str, Any], project_root: Path) -> dict[str, Any]:
    updated = dict(record)
    for path_key in MAP_PATH_KEYS.values():
        value = str(updated.get(path_key, ""))
        if not value:
            continue
        path = Path(value).resolve()
        try:
            updated[path_key] = path.relative_to(project_root).as_posix()
        except ValueError:
            updated[path_key] = path.as_posix()
    return updated


def generate_manifest(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_dir = Path(args.output_dir).resolve()
    manifest_path = Path(args.manifest).resolve()
    report_path = Path(args.report).resolve()
    checksums_path = Path(args.checksums).resolve()
    variants = parse_mapping(args.variant)
    segment_assets = parse_mapping(args.segment)
    tints = parse_tints(args.tint)
    if not variants:
        raise ValueError("at least one --variant mapping is required")

    asset_cache: dict[str, dict[str, Any]] = {}
    map_cache: dict[str, dict[str, Image.Image]] = {}
    download_cache: dict[str, dict[str, Any]] = {}
    source_records: dict[str, dict[str, Any]] = {}

    def load_maps(asset_id: str) -> dict[str, Image.Image]:
        if asset_id not in map_cache:
            asset = asset_metadata(asset_id, args.user_agent)
            download = select_download(asset, args.attribute)
            zip_bytes = download_zip(download, args.user_agent)
            asset_cache[asset_id] = asset
            download_cache[asset_id] = download
            map_cache[asset_id] = read_map_images(zip_bytes)
            source_records[asset_id] = source_record(asset, download)
        return map_cache[asset_id]

    texture_outputs = []
    all_files: list[Path] = []
    segment_ids = sorted({key.split(":", 1)[1] for key in segment_assets if ":" in key})
    for variant_id, asset_id in variants.items():
        variant_slug = safe_slug(variant_id)
        maps = load_maps(asset_id)
        variant_paths = save_maps(
            maps=maps,
            output_dir=output_dir,
            prefix=variant_slug,
            size=int(args.size),
            tint=tints.get(variant_id),
        )
        all_files.extend(Path(value) for value in variant_paths.values())
        variant_record: dict[str, Any] = {
            "variant_id": variant_slug,
            "material_name": f"ambientcg_{asset_id}",
            "texture_intent": f"ambientCG PBR material {asset_id}",
            "prompt": f"ambientCG PBR material {asset_id}",
            "provider_role": "pbr_material_library",
            "resolution": f"{int(args.size)}x{int(args.size)} PBR map set",
            "tileable": True,
            "generation_method": "ambientcg_cc0_pbr_material_import",
            "is_generated_texture": True,
            "generated_map_kinds": list(MAP_PATH_KEYS),
            "policy_map_kinds": [],
            "status": "generated",
            "source_material": source_records[asset_id],
            **variant_paths,
            "segment_materials": [],
        }
        for segment_id in segment_ids:
            segment_key = f"{variant_id}:{segment_id}"
            segment_asset = segment_assets.get(segment_key, asset_id)
            segment_maps = load_maps(segment_asset)
            segment_prefix = f"{variant_slug}_{safe_slug(segment_id)}"
            segment_paths = save_maps(
                maps=segment_maps,
                output_dir=output_dir,
                prefix=segment_prefix,
                size=int(args.size),
                tint=tints.get(segment_key) or tints.get(variant_id),
            )
            all_files.extend(Path(value) for value in segment_paths.values())
            variant_record["segment_materials"].append(
                {
                    "segment_id": safe_slug(segment_id),
                    "material_name": f"ambientcg_{segment_asset}_{safe_slug(segment_id)}",
                    "generation_method": "ambientcg_cc0_segment_pbr_material_import",
                    "is_generated_texture": True,
                    "generated_map_kinds": list(MAP_PATH_KEYS),
                    "policy_map_kinds": [],
                    "status": "generated",
                    "source_material": source_records[segment_asset],
                    **segment_paths,
                }
            )
        texture_outputs.append(manifest_paths_to_relative(variant_record, project_root))
        texture_outputs[-1]["segment_materials"] = [
            manifest_paths_to_relative(segment, project_root)
            for segment in texture_outputs[-1]["segment_materials"]
        ]

    manifest = {
        "id": f"{args.asset_id}_ambientcg_pbr_textures",
        "version": "1.0",
        "asset_id": args.asset_id,
        "texture_generation_status": "generated",
        "texture_generation_backend": "ambientcg_cc0_pbr_material_import",
        "texture_blocked_reasons": [],
        "provider_trace": [
            {
                "provider": "ambientCG",
                "role": "pbr_material_library",
                "asset_id": record["asset_id"],
                "display_name": record["display_name"],
                "short_link": record["short_link"],
                "download_attribute": record["attribute"],
                "license": record["license"],
            }
            for record in source_records.values()
        ],
        "texture_map_policy_trace": [],
        "texture_outputs": texture_outputs,
        "texture_variants": texture_outputs,
        "validation_status": "proposal",
    }
    write_json(manifest_path, manifest)
    report = {
        "status": "generated",
        "asset_id": args.asset_id,
        "texture_manifest": manifest_path.as_posix(),
        "output_dir": output_dir.as_posix(),
        "source_material_count": len(source_records),
        "texture_output_count": len(texture_outputs),
        "segment_material_count": sum(len(item.get("segment_materials", [])) for item in texture_outputs),
        "attribute": args.attribute,
        "size": int(args.size),
        "source_materials": list(source_records.values()),
    }
    write_json(report_path, report)
    checksum_files = [manifest_path, report_path, *all_files]
    write_json(
        checksums_path,
        {
            "files": [
                {"path": path.as_posix(), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
                for path in checksum_files
                if path.exists()
            ]
        },
    )
    print(json.dumps({"status": "generated", "manifest": manifest_path.as_posix(), "report": report_path.as_posix()}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.query:
        results = [search(query, args.limit, args.user_agent) for query in args.query]
        print(json.dumps({"status": "searched", "queries": results}, indent=2, sort_keys=False))
        return 0
    return generate_manifest(args)


if __name__ == "__main__":
    raise SystemExit(main())
