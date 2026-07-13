from __future__ import annotations

import copy
import json
import math
import random
import re
import shutil
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from asset_factory_blueprint.physics_evidence import (
    physics_evidence_secret_from_environment,
    verify_physics_evidence_attestation,
)
from asset_factory_blueprint.services.live_textures import build_live_texture_request_plan, generate_live_texture_sets
from asset_factory_blueprint.utils.checksums import sha256_file
from asset_factory_blueprint.utils.ids import slugify


USD_REFERENCE = re.compile(r"@([^@]+)@")
USD_XFORM_OPINION = re.compile(
    r"\b(?:double3|float3|matrix4d)\s+xformOp:(?:translate|rotateXYZ|rotateX|rotateY|rotateZ|scale|transform)\s*="
)
USD_SUFFIXES = {".usd", ".usda", ".usdc"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
SCAN_SUFFIXES = {".ply", ".obj", ".stl", ".glb", ".gltf", ".las", ".laz", ".e57"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv"}

TEXTURE_VARIANT_PROFILES = [
    {
        "variant_id": "clean_satin",
        "material_name": "painted_metal",
        "base_color": (156, 164, 170),
        "normal": (128, 128, 255),
        "roughness": (138, 138, 138),
        "metallic": (0, 0, 0),
        "texture_intent": "clean satin finish from the visible source image",
        "prompt": "clean satin painted metal, even base colour, physically plausible roughness, no baked lighting",
        "negative_prompt": "cast shadows, text, logos, watermarks, implausible wear",
        "seed": 11,
    },
    {
        "variant_id": "worn_edges",
        "material_name": "painted_metal",
        "base_color": (126, 132, 136),
        "normal": (126, 128, 255),
        "roughness": (178, 178, 178),
        "metallic": (0, 0, 0),
        "texture_intent": "subtle worn-edge finish while preserving the source silhouette",
        "prompt": "painted metal with subtle worn edges, small scuffs, consistent UV scale, no baked lighting",
        "negative_prompt": "large damage, text, logos, watermarks, changed shape",
        "seed": 23,
    },
    {
        "variant_id": "rough_speckled",
        "material_name": "painted_metal",
        "base_color": (112, 120, 118),
        "normal": (130, 130, 255),
        "roughness": (214, 214, 214),
        "metallic": (0, 0, 0),
        "texture_intent": "rough speckled finish for domain variety",
        "prompt": "rough speckled painted surface, fine material noise, tileable PBR maps, no baked lighting",
        "negative_prompt": "deep geometry edits, text, logos, watermarks, cast shadows",
        "seed": 37,
    },
]

MESH_DEFORMATION_PROFILES = [
    {
        "variant_id": "small_dents",
        "deformation_kind": "dent",
        "description": "small concave dents constrained to visible broad faces",
        "amplitude_m": -0.012,
        "radius_m": 0.08,
        "count": 6,
    },
    {
        "variant_id": "raised_bumps",
        "deformation_kind": "bump",
        "description": "small convex bumps constrained to non-contact visual surfaces",
        "amplitude_m": 0.01,
        "radius_m": 0.06,
        "count": 8,
    },
]

DRINKWARE_TERMS = {"mug", "cup", "tumbler", "stein", "thermos", "bottle"}
OIL_CAN_TERMS = {"oil", "dripper", "oiler", "oilcan", "can"}

GENERIC_APPEARANCE_SEGMENTS = [
    {
        "segment_id": "body",
        "label": "body",
        "semantic_class": "asset_body",
        "material_name": "painted_metal",
        "material_family": "painted_metal",
        "mask_kind": "body",
        "preview_colour": (78, 110, 88),
        "confidence": 0.55,
    },
    {
        "segment_id": "trim",
        "label": "trim",
        "semantic_class": "asset_trim",
        "material_name": "brushed_metal",
        "material_family": "metal",
        "mask_kind": "rims",
        "preview_colour": (176, 170, 158),
        "confidence": 0.45,
    },
    {
        "segment_id": "feature",
        "label": "secondary feature",
        "semantic_class": "asset_feature",
        "material_name": "painted_metal",
        "material_family": "painted_metal",
        "mask_kind": "handle",
        "preview_colour": (48, 87, 68),
        "confidence": 0.35,
    },
]

DRINKWARE_APPEARANCE_SEGMENTS = [
    {
        "segment_id": "body",
        "label": "body",
        "semantic_class": "container_body",
        "material_name": "painted_metal",
        "material_family": "painted_metal",
        "mask_kind": "body",
        "preview_colour": (83, 111, 88),
        "confidence": 0.72,
    },
    {
        "segment_id": "handle",
        "label": "handle",
        "semantic_class": "handle",
        "material_name": "painted_metal",
        "material_family": "painted_metal",
        "mask_kind": "handle",
        "preview_colour": (34, 85, 68),
        "confidence": 0.68,
    },
    {
        "segment_id": "rims",
        "label": "rims",
        "semantic_class": "metal_rim",
        "material_name": "brushed_metal",
        "material_family": "metal",
        "mask_kind": "rims",
        "preview_colour": (180, 169, 154),
        "confidence": 0.7,
    },
]

OIL_CAN_APPEARANCE_SEGMENTS = [
    {
        "segment_id": "body",
        "label": "body",
        "semantic_class": "reservoir_body",
        "material_name": "painted_metal",
        "material_family": "painted_metal",
        "mask_kind": "body",
        "preview_colour": (164, 32, 28),
        "confidence": 0.72,
    },
    {
        "segment_id": "handle",
        "label": "handle",
        "semantic_class": "handle",
        "material_name": "painted_metal",
        "material_family": "painted_metal",
        "mask_kind": "handle",
        "preview_colour": (38, 38, 38),
        "confidence": 0.66,
    },
    {
        "segment_id": "spout",
        "label": "spout",
        "semantic_class": "spout",
        "material_name": "brushed_metal",
        "material_family": "metal",
        "mask_kind": "spout",
        "preview_colour": (48, 48, 48),
        "confidence": 0.62,
    },
]


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _usda_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _layer_header(default_prim: str | None = None, meters_per_unit: float = 1.0, up_axis: str = "Z") -> str:
    metadata = ["#usda 1.0", "("]
    if default_prim:
        metadata.append(f'    defaultPrim = "{default_prim}"')
    metadata.append(f"    metersPerUnit = {meters_per_unit}")
    metadata.append(f'    upAxis = "{up_axis}"')
    metadata.append(")")
    return "\n".join(metadata) + "\n\n"


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def _write_png(path: Path, rgb: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    width = 2
    height = 2
    row = b"\x00" + bytes(rgb) * width
    raw = row * height
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(payload)
    return path


def _write_texture_image(path: Path, rgb: tuple[int, int, int], seed: int, size: int = 512, noise: int = 18) -> Path:
    rng = random.Random(seed)
    image = Image.new("RGB", (size, size), rgb)
    pixels = image.load()
    for y in range(size):
        band = int(10 * ((y % 41) / 40.0))
        for x in range(size):
            jitter = rng.randint(-noise, noise)
            fine = rng.randint(-4, 4)
            pixels[x, y] = tuple(max(0, min(255, channel + jitter + fine + band)) for channel in rgb)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _write_scalar_texture(path: Path, value: int, seed: int, size: int = 512, noise: int = 12) -> Path:
    return _write_texture_image(path, (value, value, value), seed=seed, size=size, noise=noise)


def _write_normal_texture(path: Path, seed: int, size: int = 512, strength: int = 10) -> Path:
    rng = random.Random(seed)
    image = Image.new("RGB", (size, size), (128, 128, 255))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            pixels[x, y] = (
                max(0, min(255, 128 + rng.randint(-strength, strength))),
                max(0, min(255, 128 + rng.randint(-strength, strength))),
                255,
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _write_segment_mask(path: Path, mask_kind: str, size: int = 512) -> Path:
    image = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(image)
    if mask_kind == "body":
        draw.rounded_rectangle((118, 64, 366, 456), radius=44, fill=255)
    elif mask_kind == "handle":
        draw.rounded_rectangle((336, 148, 460, 384), radius=42, fill=255)
        draw.rounded_rectangle((374, 188, 426, 344), radius=26, fill=0)
    elif mask_kind == "rims":
        draw.rounded_rectangle((96, 48, 390, 98), radius=26, fill=255)
        draw.rounded_rectangle((112, 426, 374, 472), radius=24, fill=255)
    elif mask_kind == "spout":
        draw.line((246, 86, 454, 34), fill=255, width=28)
        draw.line((446, 34, 496, 42), fill=255, width=14)
        draw.ellipse((212, 70, 282, 128), fill=255)
    else:
        draw.rounded_rectangle((120, 96, 392, 416), radius=36, fill=255)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def _usd_identifier(value: str, suffix: str = "") -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]
    if not parts:
        parts = ["material"]
    name = "".join(part[:1].upper() + part[1:] for part in parts)
    if name[:1].isdigit():
        name = "M" + name
    return name + suffix


def _appearance_segment_presets(asset_id: str, primary_source: dict[str, Any]) -> list[dict[str, Any]]:
    haystack = " ".join(
        [
            asset_id,
            str(primary_source.get("project_copy_path", "")),
            str(primary_source.get("source_path", "")),
        ]
    ).lower()
    if "oil dripper" in haystack or "oil_dripper" in haystack or "oil can" in haystack:
        presets = OIL_CAN_APPEARANCE_SEGMENTS
    elif any(term in haystack for term in DRINKWARE_TERMS):
        presets = DRINKWARE_APPEARANCE_SEGMENTS
    elif "oil" in haystack and "can" in haystack:
        presets = OIL_CAN_APPEARANCE_SEGMENTS
    else:
        presets = GENERIC_APPEARANCE_SEGMENTS
    return [dict(item) for item in presets]


def _style_profile_from_prompt(variant_id: str, prompt: str, index: int) -> dict[str, Any]:
    text = f"{variant_id} {prompt}".lower()
    seed = 500 + index * 23
    profile = {
        "variant_id": variant_id,
        "material_name": "painted_metal",
        "base_color": (116, 124, 116),
        "normal": (128, 128, 255),
        "roughness": (156, 156, 156),
        "metallic": (60, 60, 60),
        "texture_intent": prompt or variant_id.replace("_", " "),
        "prompt": prompt or f"{variant_id.replace('_', ' ')} PBR material",
        "negative_prompt": "text, logos, watermarks, baked lighting, changed silhouette",
        "seed": seed,
    }
    if "red" in text:
        profile["base_color"] = (178, 22, 20)
        profile["material_name"] = "red_painted_metal"
    if "shiny" in text or "gloss" in text or "polished" in text:
        profile["roughness"] = (46, 46, 46)
        profile["metallic"] = (76, 76, 76)
        profile["negative_prompt"] = "rust, dirt, scratches, text, logos, watermarks, baked lighting"
    if "rust" in text or "oxid" in text:
        profile["base_color"] = (132, 78, 42)
        profile["roughness"] = (222, 222, 222)
        profile["metallic"] = (92, 92, 92)
        profile["material_name"] = "rusty_metal"
        profile["negative_prompt"] = "glossy paint, clean enamel, text, logos, watermarks, baked lighting"
    if "metal" in text and "rust" not in text:
        profile["metallic"] = (160, 160, 160)
    return profile


def _normalise_requested_texture_profiles(raw: Any, objective: str = "") -> list[dict[str, Any]]:
    if not raw:
        return [dict(item) for item in TEXTURE_VARIANT_PROFILES]
    variants = raw if isinstance(raw, list) else [raw]
    profiles: list[dict[str, Any]] = []
    for index, item in enumerate(variants):
        if isinstance(item, dict):
            raw_id = str(item.get("variant_id") or item.get("id") or item.get("style") or f"variant_{index + 1}")
            variant_id = slugify(raw_id)
            style_prompt = str(item.get("prompt") or item.get("texture_intent") or item.get("style") or raw_id)
            prompt = (
                f"{objective}, {style_prompt}".strip(", ")
                if objective and objective.lower() not in style_prompt.lower()
                else style_prompt
            )
            profile = _style_profile_from_prompt(variant_id, prompt, index)
            profile.update(
                {
                    "texture_intent": str(item.get("texture_intent") or style_prompt),
                    "prompt": prompt,
                    "negative_prompt": str(item.get("negative_prompt") or profile["negative_prompt"]),
                    "seed": int(item.get("seed") or profile["seed"]),
                    "reference_image": str(item.get("reference_image") or ""),
                    "reference_role": str(item.get("reference_role") or "texture_reference"),
                }
            )
            profiles.append(profile)
            continue
        raw_id = str(item)
        variant_id = slugify(raw_id)
        prompt = f"{objective}, {raw_id}".strip(", ")
        profiles.append(_style_profile_from_prompt(variant_id, prompt, index))
    return profiles


def _recorded_external_reconstruction(project_dir: Path, asset_id: str) -> dict[str, Any]:
    """Lineage of a completed external reconstruction run recorded against the
    project: the run manifest plus the generated mesh, both present and readable."""
    manifest_path = project_dir / "manifests" / "external-model-run-manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if manifest.get("execution_status") != "completed":
        return {}
    mesh_path = project_dir / "assets" / asset_id / "asset.glb"
    if not mesh_path.exists():
        return {}
    return {
        "run_id": str(manifest.get("run_id") or manifest.get("id") or ""),
        "model_id": str(manifest.get("model_id") or ""),
        "backend_id": str(manifest.get("backend", {}).get("backend_id") or ""),
        "manifest_path": manifest_path.relative_to(project_dir).as_posix(),
        "mesh_path": mesh_path.relative_to(project_dir).as_posix(),
        "mesh_sha256": sha256_file(mesh_path),
    }


def _approved_canonical_geometry(project_dir: Path) -> Path | None:
    """Return the checksum-bound geometry promoted by mandatory mesh verification."""
    record_path = project_dir / "manifests" / "mesh-verification-record.json"
    if not record_path.exists():
        return None
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    promotion = record.get("promotion", {})
    if record.get("decision") != "approve" or promotion.get("approved") is not True:
        return None
    raw_path = str(promotion.get("canonical_geometry_path") or "")
    candidate_checksum = str(record.get("candidate", {}).get("checksum") or "")
    promoted_checksum = str(promotion.get("canonical_geometry_checksum") or "")
    if not raw_path or not candidate_checksum or candidate_checksum != promoted_checksum:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = project_dir / candidate
    try:
        candidate = candidate.resolve(strict=True)
        root = project_dir.resolve(strict=True)
    except OSError:
        return None
    if candidate != root and root not in candidate.parents:
        return None
    if not candidate.is_file() or sha256_file(candidate) != candidate_checksum:
        return None
    return candidate


def _segmentation_prior_segments(project_dir: Path) -> list[dict[str, Any]]:
    """Segments from a real segmentation prior run, when one exists in the workspace."""
    manifest_path = project_dir / "segmentation-prior" / "segmentation-prior-manifest.json"
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    segments = []
    for item in manifest.get("segments", []):
        if isinstance(item, dict) and item.get("mask_path") and Path(str(item["mask_path"])).exists():
            segments.append(item)
    return segments


def _write_appearance_segments(
    asset_dir: Path, asset_id: str, primary_source: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[Path]]:
    records: list[dict[str, Any]] = []
    files: list[Path] = []
    segments_dir = asset_dir / "textures" / "segments"
    if segments_dir.exists():
        # masks are regenerated in full; stale ones from an earlier segment set
        # would otherwise ride along as review evidence
        for stale in segments_dir.glob("*_mask.png"):
            stale.unlink()
    prior_segments = _segmentation_prior_segments(asset_dir.parent.parent)
    for index, item in enumerate(prior_segments):
        segment_id = slugify(str(item["segment_id"]))
        mask_path = asset_dir / "textures" / "segments" / f"{segment_id}_mask.png"
        mask_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(str(item["mask_path"])), mask_path)
        files.append(mask_path)
        material_family = str(item.get("material_family") or "painted_metal")
        material_name = _usd_identifier(segment_id, "SegmentMaterial")
        records.append(
            {
                "segment_id": segment_id,
                "label": str(item.get("label") or segment_id),
                "semantic_class": f"asset_{segment_id}",
                "semantic_label": segment_id,
                "prim_path": f"/{asset_id}/SemanticSegments/{segment_id}",
                "mask_path": mask_path.relative_to(asset_dir).as_posix(),
                "material_name": material_family,
                "material_family": material_family,
                "material_prim_path": f"/{asset_id}/Materials/{material_name}",
                "preview_colour": [int(value) for value in item.get("preview_colour") or (128, 128, 128)],
                "confidence": float(item.get("confidence") or 0.5),
                "selection_status": "proposal",
                "source_evidence_ids": ["segmentation_prior"],
                "sort_order": index,
            }
        )
    if records:
        return records, files
    for index, preset in enumerate(_appearance_segment_presets(asset_id, primary_source)):
        segment_id = str(preset["segment_id"])
        mask_path = asset_dir / "textures" / "segments" / f"{segment_id}_mask.png"
        files.append(_write_segment_mask(mask_path, str(preset["mask_kind"])))
        material_name = _usd_identifier(segment_id, "SegmentMaterial")
        records.append(
            {
                "segment_id": segment_id,
                "label": str(preset["label"]),
                "semantic_class": str(preset["semantic_class"]),
                "semantic_label": segment_id,
                "prim_path": f"/{asset_id}/SemanticSegments/{segment_id}",
                "mask_path": mask_path.relative_to(asset_dir).as_posix(),
                "material_name": str(preset["material_name"]),
                "material_family": str(preset["material_family"]),
                "material_prim_path": f"/{asset_id}/Materials/{material_name}",
                "preview_colour": list(preset["preview_colour"]),
                "confidence": float(preset["confidence"]),
                "selection_status": "proposal",
                "source_evidence_ids": ["source_copy_0"],
                "sort_order": index,
            }
        )
    return records, files


def _copy_or_reference_source(source_record: dict[str, Any], project_dir: Path, asset_dir: Path) -> Path:
    source_path = Path(str(source_record["project_copy_path"]))
    copied_source = asset_dir / "source" / source_path.name
    copied_source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project_dir / source_path, copied_source)
    return copied_source


def _source_reference_issues(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    issues = []
    for ref in USD_REFERENCE.findall(text):
        if ref.startswith(("http://", "https://", "omniverse://")):
            issues.append(f"external dependency is not localised: {ref}")
            continue
        if ref.startswith("#"):
            continue
        target = (path.parent / ref).resolve()
        if not target.exists():
            issues.append(f"unresolved dependency: {ref}")
    return issues


def _source_has_transform_opinion(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    return bool(USD_XFORM_OPINION.search(text))


def _inspect_usd_source(path: Path, allow_project_copy_baking: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source_path": path.as_posix(),
        "default_prim": "",
        "root_prims": [],
        "root_transform_identity": True,
        "source_meters_per_unit": None,
        "source_up_axis": "",
        "geometry_prim_paths": [],
        "unresolved_external_dependencies": _source_reference_issues(path),
        "blocked_reasons": [],
        "inspection_status": "not_usd_source" if path.suffix.lower() not in USD_SUFFIXES else "inspected",
    }
    if path.suffix.lower() not in USD_SUFFIXES:
        return result
    has_transform_opinion = _source_has_transform_opinion(path)
    try:
        from pxr import Gf, Usd, UsdGeom
    except Exception:
        result["inspection_status"] = "usd_runtime_unavailable"
        result["blocked_reasons"].append("OpenUSD runtime unavailable for source-root inspection")
        if has_transform_opinion and not allow_project_copy_baking:
            result["root_transform_identity"] = False
            result["blocked_reasons"].append(
                "non-identity root transform requires explicit project-copy baking approval"
            )
        return result

    stage = Usd.Stage.Open(str(path))
    if stage is None:
        result["inspection_status"] = "blocked"
        result["blocked_reasons"].append("source USD could not be opened")
        return result
    default_prim = stage.GetDefaultPrim()
    result["source_meters_per_unit"] = float(UsdGeom.GetStageMetersPerUnit(stage))
    result["source_up_axis"] = str(UsdGeom.GetStageUpAxis(stage)).upper()
    roots = list(stage.GetPseudoRoot().GetChildren())
    result["default_prim"] = str(default_prim.GetPath()) if default_prim else ""
    result["root_prims"] = [str(prim.GetPath()) for prim in roots]
    if default_prim:
        default_path = str(default_prim.GetPath()).rstrip("/")
        result["geometry_prim_paths"] = [
            str(prim.GetPath())[len(default_path) :].lstrip("/")
            for prim in stage.Traverse()
            if prim.IsA(UsdGeom.Gprim) and str(prim.GetPath()).startswith(default_path)
        ]
    if not default_prim or len(roots) != 1:
        result["blocked_reasons"].append("ambiguous USD root; declare an assembly root before authoring")
    for prim in roots:
        xformable = UsdGeom.Xformable(prim)
        if not xformable:
            continue
        try:
            matrix = xformable.GetLocalTransformation()
            if isinstance(matrix, tuple):
                matrix = matrix[0]
            if matrix != Gf.Matrix4d(1.0):
                result["root_transform_identity"] = False
        except Exception:
            if xformable.GetOrderedXformOps():
                result["root_transform_identity"] = False
    if not result["root_transform_identity"] and not allow_project_copy_baking:
        result["blocked_reasons"].append("non-identity root transform requires explicit project-copy baking approval")
    result["blocked_reasons"].extend(result["unresolved_external_dependencies"])
    if result["blocked_reasons"]:
        result["inspection_status"] = "blocked"
    return result


def _requested(requested_outputs: list[str] | tuple[str, ...], name: str) -> bool:
    return name in " ".join(item.lower() for item in requested_outputs)


def _requested_any(requested_outputs: list[str] | tuple[str, ...], names: tuple[str, ...]) -> bool:
    requested = " ".join(item.lower() for item in requested_outputs)
    return any(name in requested for name in names)


def _texture_variants_requested(requested_outputs: list[str] | tuple[str, ...], constraints: dict[str, Any]) -> bool:
    return bool(constraints.get("texture_variants")) or _requested_any(
        requested_outputs,
        ("texture", "variant", "variety", "varieties"),
    )


def _mesh_deformations_requested(requested_outputs: list[str] | tuple[str, ...], constraints: dict[str, Any]) -> bool:
    return bool(constraints.get("mesh_deformations") or constraints.get("deformation_variants")) or _requested_any(
        requested_outputs,
        ("deformation", "deform", "dent", "bump", "geometry variation", "mesh variation"),
    )


def _texture_paths(asset_dir: Path) -> list[Path]:
    texture_dir = asset_dir / "textures"
    return [
        texture_dir / "default_base_color.png",
        texture_dir / "default_normal.png",
        texture_dir / "default_roughness.png",
        texture_dir / "default_metallic.png",
    ]


def _write_texture_set(paths: list[Path]) -> list[Path]:
    if not paths:
        return []
    # Preview scaffolds keep USD/material wiring inspectable during dry runs.
    # They are not production texture synthesis outputs.
    return [
        _write_texture_image(paths[0], (92, 118, 96), seed=101, noise=20),
        _write_normal_texture(paths[1], seed=102, strength=8),
        _write_scalar_texture(paths[2], 148, seed=103, noise=16),
        _write_scalar_texture(paths[3], 42, seed=104, noise=8),
    ]


def _texture_variant_path_set(asset_dir: Path, variant_id: str) -> dict[str, Path]:
    root = asset_dir / "textures" / "variants"
    return {
        "base_color_path": root / f"{variant_id}_base_color.png",
        "normal_path": root / f"{variant_id}_normal.png",
        "roughness_path": root / f"{variant_id}_roughness.png",
        "metallic_path": root / f"{variant_id}_metallic.png",
    }


def _default_texture_variant_record(asset_dir: Path) -> dict[str, Any]:
    paths = _texture_paths(asset_dir)
    return {
        "variant_id": "default",
        "material_name": "painted_metal",
        "texture_intent": "neutral default PBR maps aligned with material proposal",
        "prompt": "neutral painted metal base colour with no baked lighting",
        "negative_prompt": "cast shadows, baked highlights, text, logos, watermarks",
        "provider_role": "texture_generator",
        "seed": 0,
        "resolution": "512x512 PBR proposal maps",
        "tileable": True,
        "base_color_path": paths[0].relative_to(asset_dir).as_posix(),
        "normal_path": paths[1].relative_to(asset_dir).as_posix(),
        "roughness_path": paths[2].relative_to(asset_dir).as_posix(),
        "metallic_path": paths[3].relative_to(asset_dir).as_posix(),
        "height_or_displacement_path": "",
        "status": "preview_scaffold",
        "generation_method": "local_preview_scaffold",
        "is_generated_texture": False,
    }


def _write_texture_variant_sets(
    asset_dir: Path, raw_variants: Any = None, objective: str = ""
) -> tuple[list[dict[str, Any]], list[Path]]:
    records = [_default_texture_variant_record(asset_dir)]
    files: list[Path] = []
    profiles = _normalise_requested_texture_profiles(raw_variants, objective)
    for index, profile in enumerate(profiles):
        paths = _texture_variant_path_set(asset_dir, profile["variant_id"])
        written = [
            _write_texture_image(paths["base_color_path"], profile["base_color"], seed=200 + index * 10, noise=22),
            _write_normal_texture(paths["normal_path"], seed=201 + index * 10, strength=10),
            _write_scalar_texture(paths["roughness_path"], profile["roughness"][0], seed=202 + index * 10, noise=15),
            _write_scalar_texture(paths["metallic_path"], profile["metallic"][0], seed=203 + index * 10, noise=6),
        ]
        files.extend(written)
        records.append(
            {
                "variant_id": profile["variant_id"],
                "material_name": profile["material_name"],
                "texture_intent": profile["texture_intent"],
                "prompt": profile["prompt"],
                "negative_prompt": profile["negative_prompt"],
                "provider_role": "texture_generator",
                "seed": profile["seed"],
                "resolution": "512x512 PBR proposal maps",
                "tileable": True,
                "base_color_path": paths["base_color_path"].relative_to(asset_dir).as_posix(),
                "normal_path": paths["normal_path"].relative_to(asset_dir).as_posix(),
                "roughness_path": paths["roughness_path"].relative_to(asset_dir).as_posix(),
                "metallic_path": paths["metallic_path"].relative_to(asset_dir).as_posix(),
                "height_or_displacement_path": "",
                "status": "preview_scaffold",
                "generation_method": "local_preview_scaffold",
                "is_generated_texture": False,
                "reference_image": profile.get("reference_image", ""),
                "reference_role": profile.get("reference_role", ""),
            }
        )
    return records, files


def _write_deformation_plan(asset_dir: Path, source_image_path: str) -> tuple[list[dict[str, Any]], list[Path]]:
    records: list[dict[str, Any]] = []
    files: list[Path] = []
    deformation_dir = asset_dir / "deformations"
    for index, profile in enumerate(MESH_DEFORMATION_PROFILES):
        height_path = deformation_dir / f"{profile['variant_id']}_height.png"
        grey = 112 if profile["deformation_kind"] == "dent" else 174
        files.append(_write_png(height_path, (grey, grey, grey)))
        records.append(
            {
                "variant_id": profile["variant_id"],
                "deformation_kind": profile["deformation_kind"],
                "description": profile["description"],
                "amplitude_m": profile["amplitude_m"],
                "radius_m": profile["radius_m"],
                "count": profile["count"],
                "provider_role": "texture_generator",
                "source_image_path": source_image_path,
                "height_or_displacement_path": height_path.relative_to(asset_dir).as_posix(),
                "mask_policy": "review visible source image before applying to mesh",
                "status": "proposal",
                "seed": 101 + index,
            }
        )
    plan = _write_json(
        deformation_dir / "deformation-plan.json",
        {
            "source_image_path": source_image_path,
            "source_assets_mutated": False,
            "deformation_requests": records,
            "usd_layer": "deform.usda",
            "variant_set": "geometryDeformation",
            "validation_status": "review_required",
        },
    )
    files.append(plan)
    return records, files


def _usda_number(value: Any) -> str:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("mesh contains a non-finite coordinate")
    return f"{number:.9g}"


def _usda_vec(values: Any, width: int) -> str:
    parts = [_usda_number(value) for value in values]
    if len(parts) != width:
        raise ValueError(f"expected a {width}-component vector")
    return "(" + ", ".join(parts) + ")"


def _author_conditioned_mesh_layer(source_path: Path, output_path: Path) -> tuple[Path, dict[str, Any]]:
    import trimesh

    loaded = trimesh.load(str(source_path), force="scene", process=False)
    source_units = getattr(loaded, "units", None)
    if source_path.suffix.lower() in {".glb", ".gltf"} and not source_units:
        source_units = "m"
    if source_units:
        try:
            loaded = loaded.convert_units("m")
        except (ValueError, KeyError) as exc:
            raise ValueError(f"could not convert mesh units {source_units!r} to metres: {exc}") from exc
    mesh = loaded.to_geometry() if isinstance(loaded, trimesh.Scene) else loaded
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
        raise ValueError("reconstruction did not contain a non-empty triangle mesh")
    if len(mesh.faces) == 0 or getattr(mesh.faces, "shape", (0, 0))[1] != 3:
        raise ValueError("reconstruction mesh is not triangulated")
    mesh.remove_unreferenced_vertices()

    vertices = [_usda_vec(vertex, 3) for vertex in mesh.vertices]
    faces = [str(int(index)) for face in mesh.faces for index in face]
    normals = [_usda_vec(normal, 3) for normal in mesh.vertex_normals]
    uv_values: list[str] = []
    uv = getattr(mesh.visual, "uv", None)
    if uv is not None and len(uv) == len(mesh.vertices):
        uv_values = [_usda_vec(value, 2) for value in uv]

    mesh_body = [
        _layer_header(default_prim="World"),
        'def Xform "World"\n',
        "{\n",
        '    def Mesh "ReconstructionMesh"\n',
        "    {\n",
        '        uniform token subdivisionScheme = "none"\n',
        "        int[] faceVertexCounts = [" + ", ".join("3" for _ in mesh.faces) + "]\n",
        "        int[] faceVertexIndices = [" + ", ".join(faces) + "]\n",
        "        point3f[] points = [" + ", ".join(vertices) + "]\n",
        "        normal3f[] normals = [" + ", ".join(normals) + "]\n",
        '        uniform token normals:interpolation = "vertex"\n',
    ]
    if uv_values:
        mesh_body.extend(
            [
                "        texCoord2f[] primvars:st = [" + ", ".join(uv_values) + "]\n",
                '        uniform token primvars:st:interpolation = "vertex"\n',
            ]
        )
    mesh_body.extend(
        [
            f'        custom string reconstruction_source = "{_usda_escape(source_path.name)}"\n',
            '        custom string reconstruction_status = "conditioned_mesh_composed"\n',
            '        custom string geometry_role = "canonical_reconstruction_geometry"\n',
            "    }\n",
            '    custom string normalisation_status = "mesh_conditioned_to_metres_z_up"\n',
            '    custom string units_policy = "meters_per_unit_1_up_axis_z"\n',
            '    custom string root_transform_policy = "trimesh_scene_transforms_baked"\n',
            "}\n",
        ]
    )
    output = _write_text(output_path, "".join(mesh_body))
    bounds = mesh.bounds.tolist()
    return output, {
        "status": "conditioned",
        "source_path": source_path.as_posix(),
        "source_units": source_units or "unknown",
        "unit_status": "known" if source_units else "unknown",
        "axis_policy": "z_up_after_trimesh_scene_composition",
        "vertex_count": int(len(mesh.vertices)),
        "face_count": int(len(mesh.faces)),
        "has_vertex_normals": len(normals) == len(vertices),
        "has_uvs": bool(uv_values),
        "bounds_m": bounds,
        "collision_prim_paths": ["Geometry/ReconstructionMesh"],
    }


def _blocked_geometry_layer(output_path: Path, source_copy: Path, reason: str) -> tuple[Path, dict[str, Any]]:
    output = _write_text(
        output_path,
        _layer_header(default_prim="World")
        + 'def Xform "World"\n'
        + "{\n"
        + '    def Scope "MissingReconstruction"\n'
        + "    {\n"
        + f'        custom string source_file = "./{_usda_escape(source_copy.name)}"\n'
        + f'        custom string blocked_reason = "{_usda_escape(reason)}"\n'
        + '        custom string geometry_role = "no_proxy_geometry_authored"\n'
        + "    }\n"
        + '    custom string normalisation_status = "blocked_missing_conditioned_geometry"\n'
        + '    custom string root_transform_policy = "no_silent_geometry_substitution"\n'
        + "}\n",
    )
    return output, {"status": "blocked", "blocked_reason": reason, "collision_prim_paths": []}


def _normalised_source_layer(
    asset_dir: Path,
    source_copy: Path,
    source_inspection: dict[str, Any],
    geometry_source: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    output_path = asset_dir / "source" / "normalised.usda"
    if source_copy.suffix.lower() not in USD_SUFFIXES:
        if geometry_source is None:
            return _blocked_geometry_layer(output_path, source_copy, "validated reconstruction mesh is unavailable")
        try:
            return _author_conditioned_mesh_layer(geometry_source, output_path)
        except Exception as exc:
            return _blocked_geometry_layer(output_path, source_copy, f"mesh conditioning failed: {exc}")

    source_root = source_inspection.get("default_prim") or "/World"
    if source_root.startswith("/"):
        source_root = source_root[1:]
    source_up_axis = str(source_inspection.get("source_up_axis") or "").upper()
    source_meters = source_inspection.get("source_meters_per_unit")
    transform_opinions = ""
    transform_order: list[str] = []
    if source_meters is not None and not math.isclose(float(source_meters), 1.0):
        scale = _usda_number(float(source_meters))
        transform_opinions += f"    double3 xformOp:scale = ({scale}, {scale}, {scale})\n"
        transform_order.append("xformOp:scale")
    if source_up_axis == "Y":
        transform_opinions += "    double3 xformOp:rotateXYZ = (90, 0, 0)\n"
        transform_order.append("xformOp:rotateXYZ")
    if transform_order:
        quoted = ", ".join(f'"{item}"' for item in transform_order)
        transform_opinions += f"    uniform token[] xformOpOrder = [{quoted}]\n"
    collision_paths = [
        "Geometry/" + str(item).strip("/")
        for item in source_inspection.get("geometry_prim_paths", [])
        if str(item).strip("/")
    ]
    output = _write_text(
        output_path,
        _layer_header(default_prim="World")
        + 'def Xform "World" (\n'
        + f"    prepend references = @./{source_copy.name}@</{_usda_escape(source_root)}>\n"
        + ")\n"
        + "{\n"
        + transform_opinions
        + '    custom string normalisation_status = "project_copy_reference_normalised_to_metres_z_up"\n'
        + '    custom string units_policy = "meters_per_unit_1_up_axis_z"\n'
        + '    custom string root_transform_policy = "identity_required_or_project_copy_bake"\n'
        + "}\n",
    )
    return output, {
        "status": "conditioned" if source_up_axis and source_meters is not None else "blocked",
        "source_units": source_meters,
        "source_up_axis": source_up_axis,
        "axis_policy": "z_up",
        "collision_prim_paths": collision_paths,
        "blocked_reason": ""
        if source_up_axis and source_meters is not None
        else "source units or up axis were not inspected",
    }


def _texture_map_paths(record: dict[str, Any]) -> dict[str, str]:
    return {
        "base_color": str(record.get("base_color_path", "")),
        "normal": str(record.get("normal_path", "")),
        "roughness": str(record.get("roughness_path", "")),
        "metallic": str(record.get("metallic_path", "")),
    }


def _material_block(
    asset_id: str,
    material_name: str,
    texture_paths: dict[str, str],
    diffuse_colour: tuple[float, float, float],
    roughness: float,
    metallic: float,
) -> str:
    has_textures = any(texture_paths.values())
    shaders = (
        '            def Shader "PrimvarReader_st"\n'
        "            {\n"
        '                uniform token info:id = "UsdPrimvarReader_float2"\n'
        '                string inputs:varname = "st"\n'
        "                float2 outputs:result\n"
        "            }\n"
        if has_textures
        else ""
    )
    diffuse_value = (
        f"color3f inputs:diffuseColor = ({diffuse_colour[0]:.3f}, {diffuse_colour[1]:.3f}, {diffuse_colour[2]:.3f})"
    )
    roughness_value = f"float inputs:roughness = {roughness:.3f}"
    metallic_value = f"float inputs:metallic = {metallic:.3f}"
    normal_value = ""
    if texture_paths.get("base_color"):
        diffuse_value = f"color3f inputs:diffuseColor.connect = </{asset_id}/Materials/{material_name}/BaseColorTexture.outputs:rgb>"
        shaders += (
            '            def Shader "BaseColorTexture"\n'
            "            {\n"
            '                uniform token info:id = "UsdUVTexture"\n'
            f"                asset inputs:file = @./{_usda_escape(texture_paths['base_color'])}@\n"
            f"                float2 inputs:st.connect = </{asset_id}/Materials/{material_name}/PrimvarReader_st.outputs:result>\n"
            '                token inputs:sourceColorSpace = "sRGB"\n'
            "                float4 inputs:fallback = (0.18, 0.18, 0.18, 1)\n"
            "                float3 outputs:rgb\n"
            "            }\n"
        )
    if texture_paths.get("roughness"):
        roughness_value = (
            f"float inputs:roughness.connect = </{asset_id}/Materials/{material_name}/RoughnessTexture.outputs:r>"
        )
        shaders += (
            '            def Shader "RoughnessTexture"\n'
            "            {\n"
            '                uniform token info:id = "UsdUVTexture"\n'
            f"                asset inputs:file = @./{_usda_escape(texture_paths['roughness'])}@\n"
            f"                float2 inputs:st.connect = </{asset_id}/Materials/{material_name}/PrimvarReader_st.outputs:result>\n"
            '                token inputs:sourceColorSpace = "raw"\n'
            "                float4 inputs:fallback = (0.5, 0.5, 0.5, 1)\n"
            "                float outputs:r\n"
            "            }\n"
        )
    if texture_paths.get("metallic"):
        metallic_value = (
            f"float inputs:metallic.connect = </{asset_id}/Materials/{material_name}/MetallicTexture.outputs:r>"
        )
        shaders += (
            '            def Shader "MetallicTexture"\n'
            "            {\n"
            '                uniform token info:id = "UsdUVTexture"\n'
            f"                asset inputs:file = @./{_usda_escape(texture_paths['metallic'])}@\n"
            f"                float2 inputs:st.connect = </{asset_id}/Materials/{material_name}/PrimvarReader_st.outputs:result>\n"
            '                token inputs:sourceColorSpace = "raw"\n'
            "                float4 inputs:fallback = (0, 0, 0, 1)\n"
            "                float outputs:r\n"
            "            }\n"
        )
    if texture_paths.get("normal"):
        normal_value = f"                normal3f inputs:normal.connect = </{asset_id}/Materials/{material_name}/NormalTexture.outputs:rgb>\n"
        shaders += (
            '            def Shader "NormalTexture"\n'
            "            {\n"
            '                uniform token info:id = "UsdUVTexture"\n'
            f"                asset inputs:file = @./{_usda_escape(texture_paths['normal'])}@\n"
            f"                float2 inputs:st.connect = </{asset_id}/Materials/{material_name}/PrimvarReader_st.outputs:result>\n"
            '                token inputs:sourceColorSpace = "raw"\n'
            "                float4 inputs:fallback = (0.5, 0.5, 1, 1)\n"
            "                float4 inputs:scale = (2, 2, 2, 1)\n"
            "                float4 inputs:bias = (-1, -1, -1, 0)\n"
            "                float3 outputs:rgb\n"
            "            }\n"
        )
    return (
        f'        def Material "{material_name}"\n'
        + "        {\n"
        + f"            token outputs:surface.connect = </{asset_id}/Materials/{material_name}/PreviewSurface.outputs:surface>\n"
        + shaders
        + '            def Shader "PreviewSurface"\n'
        + "            {\n"
        + '                uniform token info:id = "UsdPreviewSurface"\n'
        + f"                {diffuse_value}\n"
        + f"                {roughness_value}\n"
        + f"                {metallic_value}\n"
        + normal_value
        + "                token outputs:surface\n"
        + "            }\n"
        + "        }\n"
    )


def _material_layer(
    asset_dir: Path,
    asset_id: str,
    texture_files: list[Path],
    texture_variants: list[dict[str, Any]],
    appearance_segments: list[dict[str, Any]],
) -> Path:
    material_blocks: list[str] = []
    default_maps = {
        "base_color": texture_files[0].relative_to(asset_dir).as_posix() if len(texture_files) > 0 else "",
        "normal": texture_files[1].relative_to(asset_dir).as_posix() if len(texture_files) > 1 else "",
        "roughness": texture_files[2].relative_to(asset_dir).as_posix() if len(texture_files) > 2 else "",
        "metallic": texture_files[3].relative_to(asset_dir).as_posix() if len(texture_files) > 3 else "",
    }
    material_blocks.append(_material_block(asset_id, "DefaultMaterial", default_maps, (0.36, 0.46, 0.38), 0.58, 0.16))
    for item in texture_variants:
        variant_id = str(item.get("variant_id", "default"))
        if variant_id == "default":
            continue
        material_name = _usd_identifier(variant_id, "Material")
        roughness = {"clean_satin": 0.5, "worn_edges": 0.68, "rough_speckled": 0.84}.get(variant_id, 0.62)
        metallic = {"clean_satin": 0.1, "worn_edges": 0.18, "rough_speckled": 0.08}.get(variant_id, 0.14)
        material_blocks.append(
            _material_block(asset_id, material_name, _texture_map_paths(item), (0.45, 0.48, 0.45), roughness, metallic)
        )
    for segment in appearance_segments:
        material_name = _usd_identifier(str(segment["segment_id"]), "SegmentMaterial")
        colour = tuple(float(value) / 255.0 for value in segment.get("preview_colour", [128, 128, 128]))
        roughness = 0.38 if segment.get("material_family") == "metal" else 0.62
        metallic = 0.86 if segment.get("material_family") == "metal" else 0.16
        material_blocks.append(_material_block(asset_id, material_name, {}, colour, roughness, metallic))
    return _write_text(
        asset_dir / "mtl.usda",
        _layer_header(default_prim=asset_id)
        + f'def Xform "{asset_id}" (\n'
        + '    prepend apiSchemas = ["MaterialBindingAPI"]\n'
        + ")\n"
        + "{\n"
        + f"    rel material:binding = </{asset_id}/Materials/DefaultMaterial>\n"
        + "    custom asset assetFactory:materialXDocument = @./materials.mtlx@\n"
        + '    custom token assetFactory:canonicalMaterialRepresentation = "UsdPreviewSurface"\n'
        + '    custom token[] assetFactory:renderContexts = ["universal"]\n'
        + '    custom string assetFactory:materialXRole = "unbound_sidecar"\n'
        + '    custom string material_status = "proposal_preview_surface_with_materialx_sidecar"\n'
        + '    custom string binding_policy = "review required before promotion"\n'
        + f"    custom int appearance_segment_count = {len(appearance_segments)}\n"
        + '    def Scope "Materials"\n'
        + "    {\n"
        + "".join(material_blocks)
        + "    }\n"
        + "}\n",
    )


def _materialx_document(
    asset_dir: Path,
    texture_files: list[Path],
    texture_variants: list[dict[str, Any]],
    appearance_segments: list[dict[str, Any]],
) -> Path:
    """Write a renderer-neutral MaterialX sidecar document."""

    default_maps = {
        "base_color": texture_files[0].relative_to(asset_dir).as_posix() if len(texture_files) > 0 else "",
        "normal": texture_files[1].relative_to(asset_dir).as_posix() if len(texture_files) > 1 else "",
        "roughness": texture_files[2].relative_to(asset_dir).as_posix() if len(texture_files) > 2 else "",
        "metallic": texture_files[3].relative_to(asset_dir).as_posix() if len(texture_files) > 3 else "",
    }
    specifications: list[dict[str, Any]] = [
        {
            "name": "DefaultMaterial",
            "maps": default_maps,
            "colour": (0.36, 0.46, 0.38),
            "roughness": 0.58,
            "metallic": 0.16,
        }
    ]
    for item in texture_variants:
        variant_id = str(item.get("variant_id", "default"))
        if variant_id == "default":
            continue
        specifications.append(
            {
                "name": _usd_identifier(variant_id, "Material"),
                "maps": _texture_map_paths(item),
                "colour": (0.45, 0.48, 0.45),
                "roughness": {"clean_satin": 0.5, "worn_edges": 0.68, "rough_speckled": 0.84}.get(variant_id, 0.62),
                "metallic": {"clean_satin": 0.1, "worn_edges": 0.18, "rough_speckled": 0.08}.get(variant_id, 0.14),
            }
        )
    for segment in appearance_segments:
        colour = tuple(float(value) / 255.0 for value in segment.get("preview_colour", [128, 128, 128]))
        specifications.append(
            {
                "name": _usd_identifier(str(segment["segment_id"]), "SegmentMaterial"),
                "maps": {},
                "colour": colour,
                "roughness": 0.38 if segment.get("material_family") == "metal" else 0.62,
                "metallic": 0.86 if segment.get("material_family") == "metal" else 0.16,
            }
        )

    document = ET.Element("materialx", {"version": "1.39"})
    for specification in specifications:
        name = str(specification["name"])
        maps = dict(specification["maps"])
        texcoord_name = f"{name}_texcoord"
        if any(maps.values()):
            texcoord = ET.SubElement(document, "texcoord", {"name": texcoord_name, "type": "vector2"})
            ET.SubElement(texcoord, "input", {"name": "index", "type": "integer", "value": "0"})
        connections: dict[str, str] = {}
        for channel, value_type, colour_space in (
            ("base_color", "color3", "srgb_texture"),
            ("roughness", "float", "raw"),
            ("metallic", "float", "raw"),
            ("normal", "vector3", "raw"),
        ):
            filename = str(maps.get(channel) or "")
            if not filename:
                continue
            image_name = f"{name}_{channel}_image"
            image = ET.SubElement(
                document,
                "image",
                {"name": image_name, "type": value_type, "colorspace": colour_space},
            )
            ET.SubElement(image, "input", {"name": "file", "type": "filename", "value": filename})
            ET.SubElement(image, "input", {"name": "texcoord", "type": "vector2", "nodename": texcoord_name})
            if channel == "normal":
                normal_name = f"{name}_normalmap"
                normal = ET.SubElement(document, "normalmap", {"name": normal_name, "type": "vector3"})
                ET.SubElement(normal, "input", {"name": "in", "type": "vector3", "nodename": image_name})
                connections[channel] = normal_name
            else:
                connections[channel] = image_name
        surface_name = f"{name}_surface"
        surface = ET.SubElement(document, "standard_surface", {"name": surface_name, "type": "surfaceshader"})
        colour = tuple(float(value) for value in specification["colour"])
        if "base_color" in connections:
            ET.SubElement(
                surface,
                "input",
                {"name": "base_color", "type": "color3", "nodename": connections["base_color"]},
            )
        else:
            ET.SubElement(
                surface,
                "input",
                {
                    "name": "base_color",
                    "type": "color3",
                    "value": ", ".join(f"{value:.6f}" for value in colour),
                },
            )
        for channel, input_name, fallback in (
            ("roughness", "specular_roughness", float(specification["roughness"])),
            ("metallic", "metalness", float(specification["metallic"])),
        ):
            attributes = {"name": input_name, "type": "float"}
            if channel in connections:
                attributes["nodename"] = connections[channel]
            else:
                attributes["value"] = f"{fallback:.6f}"
            ET.SubElement(surface, "input", attributes)
        if "normal" in connections:
            ET.SubElement(
                surface,
                "input",
                {"name": "normal", "type": "vector3", "nodename": connections["normal"]},
            )
        material = ET.SubElement(document, "surfacematerial", {"name": name, "type": "material"})
        ET.SubElement(
            material,
            "input",
            {"name": "surfaceshader", "type": "surfaceshader", "nodename": surface_name},
        )
    ET.indent(document, space="  ")
    xml = ET.tostring(document, encoding="unicode", xml_declaration=True)
    path = _write_text(asset_dir / "materials.mtlx", xml + "\n")
    ET.parse(path)
    return path


def _materialx_semantic_validation(path: Path) -> dict[str, Any]:
    try:
        import MaterialX as mx
    except Exception as exc:
        return {
            "status": "blocked_validator_unavailable",
            "validator": "MaterialX Python bindings",
            "validator_version": "unavailable",
            "message": str(exc),
        }
    try:
        document = mx.createDocument()
        library = mx.createDocument()
        search_path = mx.getDefaultDataSearchPath()
        loaded_libraries = mx.loadLibraries(mx.getDefaultDataLibraryFolders(), search_path, library)
        document.importLibrary(library)
        mx.readFromXmlFile(document, str(path), search_path)
        validation_result = document.validate()
        if isinstance(validation_result, tuple):
            valid = bool(validation_result[0])
            message = str(validation_result[1]) if len(validation_result) > 1 else ""
        else:
            valid = bool(validation_result)
            message = ""
        return {
            "status": "pass" if valid else "blocked",
            "validator": "MaterialX document validation",
            "validator_version": str(mx.getVersionString()),
            "loaded_library_count": len(loaded_libraries),
            "loaded_library_files": sorted({Path(str(item)).name for item in loaded_libraries}),
            "message": message,
        }
    except Exception as exc:
        return {
            "status": "blocked_validation_error",
            "validator": "MaterialX document validation",
            "validator_version": str(mx.getVersionString()),
            "message": str(exc),
        }


def _material_adapter_record(asset_dir: Path, materialx: Path, preview_surface: Path) -> Path:
    return _write_json(
        asset_dir / "material-adapters.json",
        {
            "format_version": "1.0",
            "canonical": {
                "representation": "MaterialX",
                "path": materialx.relative_to(asset_dir).as_posix(),
                "sha256": sha256_file(materialx),
                "role": "sidecar_source_document",
                "usd_bound": False,
                "status": "authored_unbound_sidecar",
            },
            "adapters": [
                {
                    "render_context": "universal",
                    "representation": "UsdPreviewSurface",
                    "path": preview_surface.relative_to(asset_dir).as_posix(),
                    "sha256": sha256_file(preview_surface),
                    "status": "authored",
                },
                {
                    "render_context": "mtlx",
                    "representation": "MaterialX",
                    "path": materialx.relative_to(asset_dir).as_posix(),
                    "sha256": sha256_file(materialx),
                    "status": "blocked_not_bound_to_usd_render_context",
                },
                {
                    "render_context": "mdl",
                    "representation": "MDL",
                    "path": "",
                    "sha256": "",
                    "status": "blocked_not_configured",
                },
            ],
            "renderer_validation": {
                "storm": "pending_runtime_render_evidence",
                "rtx": "pending_runtime_render_evidence",
            },
            "materialx_semantic_validation": _materialx_semantic_validation(materialx),
        },
    )


def _collision_override_tree(collision_prim_paths: list[str]) -> str:
    tree: dict[str, Any] = {}
    for raw_path in collision_prim_paths:
        node = tree
        for part in [item for item in raw_path.strip("/").split("/") if item]:
            node = node.setdefault(part, {})
        node["__collision__"] = True

    def render(node: dict[str, Any], indent: int) -> str:
        body = ""
        padding = " " * indent
        for name in sorted(key for key in node if key != "__collision__"):
            child = node[name]
            if child.get("__collision__"):
                body += (
                    f'{padding}over "{_usda_escape(name)}" (\n'
                    + f'{padding}    prepend apiSchemas = ["PhysicsCollisionAPI", "PhysicsMeshCollisionAPI"]\n'
                    + f"{padding})\n"
                    + f"{padding}{{\n"
                    + f"{padding}    bool physics:collisionEnabled = true\n"
                    + f'{padding}    token physics:approximation = "convexHull"\n'
                    + render(child, indent + 4)
                    + f"{padding}}}\n"
                )
            else:
                body += (
                    f'{padding}over "{_usda_escape(name)}"\n{padding}{{\n'
                    + render(child, indent + 4)
                    + f"{padding}}}\n"
                )
        return body

    return render(tree, 4)


def _finite_vector(value: Any, size: int, *, strictly_positive: bool = False) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        return None
    result: list[float] = []
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(float(item)):
            return None
        number = float(item)
        if strictly_positive and number <= 0.0:
            return None
        result.append(number)
    return result


def _normalise_physics_evidence(
    project_dir: Path,
    asset_id: str,
    raw_specification: Any,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Accept only materialised, reviewed SI mass-property evidence."""

    specification = raw_specification if isinstance(raw_specification, dict) else {}
    candidates = specification.get("mass_properties")
    if isinstance(candidates, list):
        matching = [
            item
            for item in candidates
            if isinstance(item, dict) and str(item.get("prim_path") or f"/{asset_id}") == f"/{asset_id}"
        ]
        specification = matching[0] if len(matching) == 1 else {}
    errors: list[str] = []
    if not specification:
        return None, ["accepted mass-property evidence was not supplied"]

    try:
        attestation_secret = physics_evidence_secret_from_environment()
    except ValueError as exc:
        errors.append(str(exc))
    else:
        errors.extend(verify_physics_evidence_attestation(specification, attestation_secret))

    if specification.get("status") != "accepted":
        errors.append("mass-property evidence status must be accepted")
    if specification.get("prim_path") != f"/{asset_id}":
        errors.append(f"mass-property evidence must target /{asset_id}")
    method = specification.get("method")
    if not isinstance(method, str) or method not in {
        "measured",
        "manufacturer_specification",
        "computed_from_measured_density",
    }:
        errors.append(
            "mass-property method must be measured, manufacturer_specification or computed_from_measured_density"
        )
    unit_policy = specification.get("unit_policy")
    valid_unit_policy = unit_policy == "si_m_kg_s" or unit_policy == {
        "mass": "kg",
        "length": "m",
        "inertia": "kg*m^2",
    }
    if not valid_unit_policy:
        errors.append("mass-property unit_policy must explicitly declare kg, m and kg*m^2")

    mass = specification.get("mass")
    if (
        not isinstance(mass, (int, float))
        or isinstance(mass, bool)
        or not math.isfinite(float(mass))
        or float(mass) <= 0.0
    ):
        errors.append("mass must be a finite positive value in kilograms")
    centre = _finite_vector(specification.get("center_of_mass"), 3)
    if centre is None:
        errors.append("center_of_mass must contain three finite metre values")
    inertia = _finite_vector(specification.get("diagonal_inertia"), 3, strictly_positive=True)
    if inertia is None:
        errors.append("diagonal_inertia must contain three finite positive kg*m^2 values")
    elif any(value > sum(inertia) - value + max(1e-12, 1e-9 * sum(inertia)) for value in inertia):
        errors.append("diagonal_inertia principal moments must satisfy the rigid-body triangle inequalities")
    axes = _finite_vector(specification.get("principal_axes"), 4)
    axes_norm = math.sqrt(sum(value * value for value in axes)) if axes is not None else 0.0
    if axes is None or not math.isclose(axes_norm, 1.0, rel_tol=1e-6, abs_tol=1e-6):
        errors.append("principal_axes must contain a finite unit quaternion")

    uncertainty = specification.get("uncertainty")
    if not isinstance(uncertainty, dict):
        errors.append("mass-property evidence requires an uncertainty object")
    else:
        mass_uncertainty = uncertainty.get("mass")
        inertia_uncertainty = _finite_vector(uncertainty.get("diagonal_inertia"), 3)
        if (
            not isinstance(mass_uncertainty, (int, float))
            or isinstance(mass_uncertainty, bool)
            or not math.isfinite(float(mass_uncertainty))
            or float(mass_uncertainty) < 0.0
        ):
            errors.append("uncertainty.mass must be a finite non-negative value in kilograms")
        if inertia_uncertainty is None or any(value < 0.0 for value in inertia_uncertainty):
            errors.append("uncertainty.diagonal_inertia must contain three finite non-negative kg*m^2 values")

    approval = specification.get("approval")
    if not isinstance(approval, dict):
        errors.append("mass-property evidence requires an approval record")
    else:
        if approval.get("status") != "accepted":
            errors.append("mass-property approval status must be accepted")
        for field in ("decision_id", "reviewer", "decided_at"):
            value = approval.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"mass-property approval requires {field}")

    raw_source_evidence_ids = specification.get("source_evidence_ids")
    if (
        not isinstance(raw_source_evidence_ids, list)
        or not raw_source_evidence_ids
        or any(not isinstance(item, str) or not item.strip() for item in raw_source_evidence_ids)
    ):
        source_evidence_ids: list[str] = []
        errors.append("mass-property evidence requires a non-empty list of source_evidence_ids")
    else:
        source_evidence_ids = list(raw_source_evidence_ids)
        if len(source_evidence_ids) != len(set(source_evidence_ids)):
            errors.append("mass-property source_evidence_ids must be unique")
    evidence = specification.get("evidence")
    evidence_by_id: dict[str, dict[str, str]] = {}
    project_root = project_dir.resolve()
    if not isinstance(evidence, list) or not evidence:
        errors.append("mass-property evidence requires materialised evidence records")
    else:
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                errors.append(f"mass-property evidence record {index} must be an object")
                continue
            supplied_evidence_id = item.get("evidence_id")
            supplied_path = item.get("path")
            supplied_sha256 = item.get("sha256")
            evidence_id = supplied_evidence_id if isinstance(supplied_evidence_id, str) else ""
            relative_path = supplied_path if isinstance(supplied_path, str) else ""
            supplied_sha256 = supplied_sha256 if isinstance(supplied_sha256, str) else ""
            expected_sha256 = supplied_sha256.lower()
            if not evidence_id.strip() or evidence_id in evidence_by_id:
                errors.append(f"mass-property evidence record {index} requires a unique evidence_id")
                continue
            if not relative_path.strip() or Path(relative_path).is_absolute():
                errors.append(f"mass-property evidence {evidence_id} path must be project-relative")
                continue
            candidate = (project_dir / relative_path).resolve()
            try:
                candidate.relative_to(project_root)
            except ValueError:
                errors.append(f"mass-property evidence {evidence_id} escapes the project workspace")
                continue
            if not candidate.is_file():
                errors.append(f"mass-property evidence {evidence_id} does not resolve to a regular file")
                continue
            actual_sha256 = sha256_file(candidate)
            if (
                supplied_sha256 != expected_sha256
                or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
                or actual_sha256 != expected_sha256
            ):
                errors.append(f"mass-property evidence {evidence_id} digest does not match the materialised file")
                continue
            evidence_by_id[evidence_id] = {
                "evidence_id": evidence_id,
                "path": candidate.relative_to(project_root).as_posix(),
                "sha256": actual_sha256,
            }
    unresolved = sorted(set(source_evidence_ids) - set(evidence_by_id))
    if unresolved:
        errors.append("mass-property source_evidence_ids do not resolve: " + ", ".join(unresolved))
    if errors:
        return None, errors

    assert isinstance(mass, (int, float))
    assert centre is not None
    assert inertia is not None
    assert axes is not None
    assert isinstance(uncertainty, dict)
    assert isinstance(approval, dict)
    accepted_record = copy.deepcopy(specification)
    return accepted_record, []


def _physics_layer_fallback(
    asset_dir: Path,
    asset_id: str,
    collision_prim_paths: list[str],
    accepted_evidence: dict[str, Any] | None,
) -> Path:
    collision_status = "authored" if collision_prim_paths else "blocked_missing_collision_geometry"
    if accepted_evidence:
        schemas = '["MaterialBindingAPI", "PhysicsRigidBodyAPI", "PhysicsMassAPI"]'
        mass_body = (
            "    bool physics:rigidBodyEnabled = true\n"
            + f"    float physics:mass = {accepted_evidence['mass']:.9g}\n"
            + "    point3f physics:centerOfMass = ("
            + ", ".join(f"{value:.9g}" for value in accepted_evidence["center_of_mass"])
            + ")\n"
            + "    float3 physics:diagonalInertia = ("
            + ", ".join(f"{value:.9g}" for value in accepted_evidence["diagonal_inertia"])
            + ")\n"
            + "    quatf physics:principalAxes = ("
            + ", ".join(f"{value:.9g}" for value in accepted_evidence["principal_axes"])
            + ")\n"
        )
        physics_status = "validated_evidence_authored"
        evidence_status = "accepted"
        evidence_root_body = f'    custom string assetFactory:physicsEvidenceFingerprint = "{accepted_evidence["evidence_fingerprint"]}"\n'
        source_ids = ", ".join(f'"{_usda_escape(item)}"' for item in accepted_evidence["source_evidence_ids"])
        evidence_body = (
            f'        custom string assetFactory:evidenceFingerprint = "{accepted_evidence["evidence_fingerprint"]}"\n'
            + f'        custom string assetFactory:approvalDecisionId = "{_usda_escape(accepted_evidence["approval"]["decision_id"])}"\n'
            + f'        custom string assetFactory:measurementMethod = "{_usda_escape(accepted_evidence["method"])}"\n'
            + f"        custom string[] assetFactory:sourceEvidenceIds = [{source_ids}]\n"
        )
    else:
        schemas = '["MaterialBindingAPI", "PhysicsRigidBodyAPI"]'
        mass_body = "    bool physics:rigidBodyEnabled = false\n"
        physics_status = "disabled_missing_validated_evidence"
        evidence_status = "missing_or_rejected"
        evidence_root_body = ""
        evidence_body = ""
    return _write_text(
        asset_dir / "phy.usda",
        _layer_header(default_prim=asset_id)
        + f'def Xform "{asset_id}" (\n'
        + f"    prepend apiSchemas = {schemas}\n"
        + ")\n"
        + "{\n"
        + f"    rel material:binding:physics = </{asset_id}/PhysicsMaterials/DefaultPhysicsMaterial>\n"
        + mass_body
        + f'    custom string assetFactory:physicsStatus = "{physics_status}"\n'
        + f'    custom string assetFactory:physicsEvidenceStatus = "{evidence_status}"\n'
        + evidence_root_body
        + f'    custom string assetFactory:collisionStatus = "{collision_status}"\n'
        + _collision_override_tree(collision_prim_paths)
        + '    def Scope "PhysicsEvidence"\n'
        + "    {\n"
        + '        custom string assetFactory:rigidBodyOpinion = "UsdPhysics.RigidBodyAPI"\n'
        + '        custom string assetFactory:colliderOpinion = "UsdPhysics.CollisionAPI convexHull"\n'
        + '        custom string assetFactory:massPropertyPolicy = "numeric values require materialised accepted evidence"\n'
        + evidence_body
        + "    }\n"
        + '    def Scope "PhysicsMaterials"\n'
        + "    {\n"
        + '        def Material "DefaultPhysicsMaterial" (\n'
        + '            prepend apiSchemas = ["PhysicsMaterialAPI"]\n'
        + "        )\n"
        + "        {\n"
        + '            custom string assetFactory:propertyStatus = "review_required_no_numeric_opinions"\n'
        + "        }\n"
        + "    }\n"
        + "}\n",
    )


def _physics_layer(
    project_dir: Path,
    asset_dir: Path,
    asset_id: str,
    collision_prim_paths: list[str],
    raw_specification: Any,
) -> tuple[Path, dict[str, Any]]:
    path = asset_dir / "phy.usda"
    path.parent.mkdir(parents=True, exist_ok=True)
    accepted_evidence, evidence_errors = _normalise_physics_evidence(project_dir, asset_id, raw_specification)
    physics_record = {
        "status": "authored_from_accepted_evidence" if accepted_evidence else "disabled_pending_accepted_evidence",
        "rigid_body_enabled": bool(accepted_evidence),
        "mass_properties_authored": bool(accepted_evidence),
        "accepted_evidence": accepted_evidence or {},
        "blocked_reasons": evidence_errors,
    }
    try:
        from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade
    except Exception:
        return _physics_layer_fallback(asset_dir, asset_id, collision_prim_paths, accepted_evidence), physics_record

    stage = Usd.Stage.CreateNew(str(path))
    if stage is None:
        return _physics_layer_fallback(asset_dir, asset_id, collision_prim_paths, accepted_evidence), physics_record
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    root = UsdGeom.Xform.Define(stage, f"/{asset_id}").GetPrim()
    stage.SetDefaultPrim(root)
    UsdPhysics.RigidBodyAPI.Apply(root).CreateRigidBodyEnabledAttr().Set(bool(accepted_evidence))
    if accepted_evidence:
        mass_api = UsdPhysics.MassAPI.Apply(root)
        mass_api.CreateMassAttr().Set(accepted_evidence["mass"])
        mass_api.CreateCenterOfMassAttr().Set(Gf.Vec3f(*accepted_evidence["center_of_mass"]))
        mass_api.CreateDiagonalInertiaAttr().Set(Gf.Vec3f(*accepted_evidence["diagonal_inertia"]))
        axes = accepted_evidence["principal_axes"]
        mass_api.CreatePrincipalAxesAttr().Set(Gf.Quatf(axes[0], Gf.Vec3f(*axes[1:])))
    root.CreateAttribute("assetFactory:physicsStatus", Sdf.ValueTypeNames.String).Set(
        "validated_evidence_authored" if accepted_evidence else "disabled_missing_validated_evidence"
    )
    root.CreateAttribute("assetFactory:physicsEvidenceStatus", Sdf.ValueTypeNames.String).Set(
        "accepted" if accepted_evidence else "missing_or_rejected"
    )
    if accepted_evidence:
        root.CreateAttribute("assetFactory:physicsEvidenceFingerprint", Sdf.ValueTypeNames.String).Set(
            accepted_evidence["evidence_fingerprint"]
        )
    root.CreateAttribute("assetFactory:collisionStatus", Sdf.ValueTypeNames.String).Set(
        "authored" if collision_prim_paths else "blocked_missing_collision_geometry"
    )
    physics_material = UsdShade.Material.Define(stage, f"/{asset_id}/PhysicsMaterials/DefaultPhysicsMaterial")
    UsdPhysics.MaterialAPI.Apply(physics_material.GetPrim())
    physics_material.GetPrim().CreateAttribute(
        "assetFactory:propertyStatus",
        Sdf.ValueTypeNames.String,
    ).Set("review_required_no_numeric_opinions")
    UsdShade.MaterialBindingAPI.Apply(root).Bind(
        physics_material,
        UsdShade.Tokens.weakerThanDescendants,
        "physics",
    )
    for relative_path in collision_prim_paths:
        collision_prim = stage.OverridePrim(f"/{asset_id}/{relative_path.strip('/')}")
        UsdPhysics.CollisionAPI.Apply(collision_prim).CreateCollisionEnabledAttr().Set(True)
        UsdPhysics.MeshCollisionAPI.Apply(collision_prim).CreateApproximationAttr().Set("convexHull")
    evidence = stage.DefinePrim(f"/{asset_id}/PhysicsEvidence", "Scope")
    evidence.CreateAttribute("assetFactory:rigidBodyOpinion", Sdf.ValueTypeNames.String).Set("UsdPhysics.RigidBodyAPI")
    evidence.CreateAttribute("assetFactory:colliderOpinion", Sdf.ValueTypeNames.String).Set(
        "UsdPhysics.CollisionAPI convexHull"
    )
    evidence.CreateAttribute("assetFactory:massPropertyPolicy", Sdf.ValueTypeNames.String).Set(
        "numeric values require materialised accepted evidence"
    )
    if accepted_evidence:
        evidence.CreateAttribute("assetFactory:sourceEvidenceIds", Sdf.ValueTypeNames.StringArray).Set(
            accepted_evidence["source_evidence_ids"]
        )
        evidence.CreateAttribute("assetFactory:approvalDecisionId", Sdf.ValueTypeNames.String).Set(
            accepted_evidence["approval"]["decision_id"]
        )
        evidence.CreateAttribute("assetFactory:measurementMethod", Sdf.ValueTypeNames.String).Set(
            accepted_evidence["method"]
        )
        evidence.CreateAttribute("assetFactory:evidenceFingerprint", Sdf.ValueTypeNames.String).Set(
            accepted_evidence["evidence_fingerprint"]
        )
    stage.GetRootLayer().Save()
    return path, physics_record


def _materialise_physics_evidence(
    project_dir: Path,
    asset_dir: Path,
    physics_record: dict[str, Any],
) -> list[Path]:
    accepted = physics_record.get("accepted_evidence")
    if not isinstance(accepted, dict) or not accepted:
        return []
    destination_dir = asset_dir / "evidence" / "physics"
    destination_dir.mkdir(parents=True, exist_ok=True)
    materialised_records: list[dict[str, str]] = []
    paths: list[Path] = []
    for index, evidence in enumerate(accepted.get("evidence", [])):
        source = project_dir / str(evidence["path"])
        suffix = source.suffix.lower() or ".bin"
        filename = f"{index:03d}_{slugify(str(evidence['evidence_id']))}_{str(evidence['sha256'])[:12]}{suffix}"
        destination = destination_dir / filename
        shutil.copy2(source, destination)
        digest = sha256_file(destination)
        if digest != evidence["sha256"]:
            raise ValueError(f"materialised physics evidence digest changed: {evidence['evidence_id']}")
        materialised_records.append(
            {
                "evidence_id": str(evidence["evidence_id"]),
                "path": destination.relative_to(asset_dir).as_posix(),
                "sha256": digest,
            }
        )
        paths.append(destination)
    binding_payload = {
        "schema_version": "1.0.0",
        "status": "accepted",
        "evidence_fingerprint": accepted["evidence_fingerprint"],
        "prim_path": accepted["prim_path"],
        "mass": accepted["mass"],
        "center_of_mass": accepted["center_of_mass"],
        "diagonal_inertia": accepted["diagonal_inertia"],
        "principal_axes": accepted["principal_axes"],
        "method": accepted["method"],
        "unit_policy": accepted["unit_policy"],
        "uncertainty": accepted["uncertainty"],
        "source_evidence_ids": accepted["source_evidence_ids"],
        "evidence": materialised_records,
        "approval": accepted["approval"],
        "attested_evidence": copy.deepcopy(accepted),
    }
    binding = _write_json(asset_dir / "evidence" / "physics-evidence-binding.json", binding_payload)
    physics_record["package_evidence_binding"] = binding.relative_to(asset_dir).as_posix()
    physics_record["materialised_evidence"] = materialised_records
    paths.append(binding)
    return paths


def _articulation_layer_fallback(asset_dir: Path, asset_id: str, blocked_reasons: list[str]) -> Path:
    status = _usda_escape("; ".join(blocked_reasons) or "not_requested_static_asset")
    return _write_text(
        asset_dir / "art.usda",
        _layer_header(default_prim=asset_id)
        + f'def Xform "{asset_id}"\n'
        + "{\n"
        + '    custom string articulation_status = "not_detected_static_asset"\n'
        + '    custom string articulation_root_opinion = "not_authored_without_joint_evidence"\n'
        + '    def Scope "Articulation"\n'
        + "    {\n"
        + f'        custom string joint_plan_status = "{status}"\n'
        + '        custom string[] joint_fields = ["joint_name", "joint_type", "axis", "lower_limit", "upper_limit", "drive"]\n'
        + '        custom string drive_policy = "no drives authored without evidence"\n'
        + '        custom string limit_policy = "no limits authored without evidence"\n'
        + "    }\n"
        + "}\n",
    )


def _articulation_layer(
    asset_dir: Path,
    asset_id: str,
    raw_specification: Any,
) -> tuple[Path, dict[str, Any]]:
    specification = raw_specification if isinstance(raw_specification, dict) else {}
    raw_joints = specification.get("joints") if isinstance(specification.get("joints"), list) else []
    if not raw_joints:
        reasons = ["joint evidence was not supplied"]
        return _articulation_layer_fallback(asset_dir, asset_id, reasons), {
            "status": "not_applicable_static_asset",
            "articulation_root_path": "",
            "body_paths": [],
            "joints": [],
            "blocked_reasons": reasons,
        }
    normalised_joints: list[dict[str, Any]] = []
    errors: list[str] = []
    names: set[str] = set()

    def body_path(value: Any, field: str, joint_name: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            errors.append(f"joint {joint_name} requires {field}")
            return ""
        path = raw if raw.startswith("/") else f"/{asset_id}/{raw.strip('/')}"
        if path != f"/{asset_id}" and not path.startswith(f"/{asset_id}/"):
            errors.append(f"joint {joint_name} {field} must be beneath /{asset_id}")
            return ""
        return path

    for index, raw_joint in enumerate(raw_joints):
        if not isinstance(raw_joint, dict):
            errors.append(f"joint {index} must be an object")
            continue
        name = _usd_identifier(str(raw_joint.get("name") or f"joint_{index}"))
        if name in names:
            errors.append(f"joint name is duplicated: {name}")
            continue
        names.add(name)
        joint_type = str(raw_joint.get("type") or "").lower()
        if joint_type not in {"fixed", "revolute", "prismatic"}:
            errors.append(f"joint {name} type must be fixed, revolute or prismatic")
        axis = str(raw_joint.get("axis") or "X").upper()
        if axis not in {"X", "Y", "Z"}:
            errors.append(f"joint {name} axis must be X, Y or Z")
        lower = raw_joint.get("lower_limit")
        upper = raw_joint.get("upper_limit")
        if joint_type != "fixed":
            if not isinstance(lower, (int, float)) or not isinstance(upper, (int, float)):
                errors.append(f"joint {name} requires numeric lower and upper limits")
            elif not math.isfinite(float(lower)) or not math.isfinite(float(upper)) or float(lower) > float(upper):
                errors.append(f"joint {name} limits must be finite and ordered")
        evidence_ids = [str(item) for item in raw_joint.get("source_evidence_ids", []) if str(item)]
        if not evidence_ids:
            errors.append(f"joint {name} requires source evidence IDs")
        frame_unit = str(raw_joint.get("frame_unit") or specification.get("frame_unit") or "")
        if frame_unit != "m":
            errors.append(f"joint {name} frame_unit must be m")
        local_pos0 = _finite_vector(raw_joint.get("local_pos0"), 3)
        local_pos1 = _finite_vector(raw_joint.get("local_pos1"), 3)
        local_rot0 = _finite_vector(raw_joint.get("local_rot0"), 4)
        local_rot1 = _finite_vector(raw_joint.get("local_rot1"), 4)
        if local_pos0 is None or local_pos1 is None:
            errors.append(f"joint {name} requires finite three-component local_pos0 and local_pos1 evidence")
        if local_rot0 is None or math.sqrt(sum(value * value for value in local_rot0)) <= 0.0:
            errors.append(f"joint {name} requires a non-zero finite local_rot0 quaternion")
        if local_rot1 is None or math.sqrt(sum(value * value for value in local_rot1)) <= 0.0:
            errors.append(f"joint {name} requires a non-zero finite local_rot1 quaternion")
        body0 = body_path(raw_joint.get("body0"), "body0", name)
        body1 = body_path(raw_joint.get("body1"), "body1", name)
        if body0 and body1 and body0 == body1:
            errors.append(f"joint {name} body0 and body1 must be distinct")
        raw_drive = raw_joint.get("drive") or {}
        if not isinstance(raw_drive, dict):
            errors.append(f"joint {name} drive must be an object")
            drive_record: dict[str, Any] = {}
        else:
            drive_record = dict(raw_drive)
        unknown_drive_fields = sorted(
            set(drive_record) - {"type", "stiffness", "damping", "max_force", "target_position", "target_velocity"}
        )
        if unknown_drive_fields:
            errors.append(f"joint {name} drive has unsupported fields: {', '.join(unknown_drive_fields)}")
        if drive_record:
            if joint_type == "fixed":
                errors.append(f"joint {name} is fixed and cannot carry a drive")
            drive_type = str(drive_record.get("type") or "force")
            if drive_type not in {"force", "acceleration"}:
                errors.append(f"joint {name} drive type must be force or acceleration")
            drive_record["type"] = drive_type
            missing_drive_fields = [
                field for field in ("stiffness", "damping", "max_force") if field not in drive_record
            ]
            if missing_drive_fields:
                errors.append(f"joint {name} drive requires bounded {', '.join(missing_drive_fields)}")
            for field in ("stiffness", "damping", "max_force"):
                value = drive_record.get(field)
                if value is None:
                    continue
                if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0.0:
                    errors.append(f"joint {name} drive {field} must be finite and non-negative")
                else:
                    drive_record[field] = float(value)
            for field in ("target_position", "target_velocity"):
                value = drive_record.get(field)
                if value is None:
                    continue
                if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    errors.append(f"joint {name} drive {field} must be finite")
                else:
                    drive_record[field] = float(value)
            target_position = drive_record.get("target_position")
            if (
                target_position is not None
                and isinstance(lower, (int, float))
                and isinstance(upper, (int, float))
                and not float(lower) <= float(target_position) <= float(upper)
            ):
                errors.append(f"joint {name} drive target_position must lie within the joint limits")
        normalised_joints.append(
            {
                "name": name,
                "type": joint_type,
                "body0": body0,
                "body1": body1,
                "axis": axis,
                "frame_unit": frame_unit,
                "local_pos0": local_pos0,
                "local_rot0": local_rot0,
                "local_pos1": local_pos1,
                "local_rot1": local_rot1,
                "lower_limit": float(lower) if isinstance(lower, (int, float)) else None,
                "upper_limit": float(upper) if isinstance(upper, (int, float)) else None,
                "drive": drive_record,
                "source_evidence_ids": evidence_ids,
            }
        )
    if errors:
        return _articulation_layer_fallback(asset_dir, asset_id, errors), {
            "status": "blocked",
            "articulation_root_path": "",
            "body_paths": sorted(
                {
                    str(joint.get(key) or "")
                    for joint in normalised_joints
                    for key in ("body0", "body1")
                    if joint.get(key)
                }
            ),
            "joints": normalised_joints,
            "blocked_reasons": errors,
        }
    try:
        from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
    except Exception as exc:
        reasons = [f"OpenUSD physics schemas are unavailable: {exc}"]
        return _articulation_layer_fallback(asset_dir, asset_id, reasons), {
            "status": "blocked",
            "articulation_root_path": "",
            "body_paths": [],
            "joints": normalised_joints,
            "blocked_reasons": reasons,
        }
    path = asset_dir / "art.usda"
    stage = Usd.Stage.CreateNew(str(path))
    if stage is None:
        reasons = ["articulation layer could not be created"]
        return _articulation_layer_fallback(asset_dir, asset_id, reasons), {
            "status": "blocked",
            "articulation_root_path": "",
            "body_paths": [],
            "joints": normalised_joints,
            "blocked_reasons": reasons,
        }
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    root = UsdGeom.Xform.Define(stage, f"/{asset_id}").GetPrim()
    stage.SetDefaultPrim(root)
    UsdPhysics.ArticulationRootAPI.Apply(root)
    joint_scope = UsdGeom.Scope.Define(stage, f"/{asset_id}/Joints")
    joint_scope.GetPrim().CreateAttribute("assetFactory:evidenceRequired", Sdf.ValueTypeNames.Bool).Set(True)
    body_paths = sorted({joint[key] for joint in normalised_joints for key in ("body0", "body1")})
    joint_scope.GetPrim().CreateAttribute("assetFactory:bodyPaths", Sdf.ValueTypeNames.StringArray).Set(body_paths)
    joint_scope.GetPrim().CreateAttribute("assetFactory:bodyAuthoringPolicy", Sdf.ValueTypeNames.String).Set(
        "relationships_only_no_synthetic_body_prims"
    )
    for joint_record in normalised_joints:
        joint_path = f"/{asset_id}/Joints/{joint_record['name']}"
        joint_type = joint_record["type"]
        if joint_type == "revolute":
            joint = UsdPhysics.RevoluteJoint.Define(stage, joint_path)
        elif joint_type == "prismatic":
            joint = UsdPhysics.PrismaticJoint.Define(stage, joint_path)
        else:
            joint = UsdPhysics.FixedJoint.Define(stage, joint_path)
        joint.CreateBody0Rel().SetTargets([Sdf.Path(joint_record["body0"])])
        joint.CreateBody1Rel().SetTargets([Sdf.Path(joint_record["body1"])])
        joint.CreateCollisionEnabledAttr().Set(False)
        if joint_type != "fixed":
            joint.CreateAxisAttr().Set(joint_record["axis"])
            joint.CreateLowerLimitAttr().Set(joint_record["lower_limit"])
            joint.CreateUpperLimitAttr().Set(joint_record["upper_limit"])
            drive_record = joint_record["drive"]
            if drive_record:
                drive_name = "angular" if joint_type == "revolute" else "linear"
                drive = UsdPhysics.DriveAPI.Apply(joint.GetPrim(), drive_name)
                drive.CreateTypeAttr().Set(str(drive_record.get("type") or "force"))
                for field, creator in (
                    ("stiffness", drive.CreateStiffnessAttr),
                    ("damping", drive.CreateDampingAttr),
                    ("max_force", drive.CreateMaxForceAttr),
                    ("target_position", drive.CreateTargetPositionAttr),
                    ("target_velocity", drive.CreateTargetVelocityAttr),
                ):
                    value = drive_record.get(field)
                    if isinstance(value, (int, float)) and math.isfinite(float(value)):
                        creator().Set(float(value))
        joint.GetPrim().CreateAttribute("assetFactory:sourceEvidenceIds", Sdf.ValueTypeNames.StringArray).Set(
            joint_record["source_evidence_ids"]
        )
        joint.GetPrim().CreateAttribute("assetFactory:localFramePolicy", Sdf.ValueTypeNames.String).Set(
            str(specification.get("local_frame_policy") or "joint frames require runtime verification")
        )
        joint.GetPrim().CreateAttribute("assetFactory:frameUnit", Sdf.ValueTypeNames.String).Set(
            joint_record["frame_unit"]
        )
        joint.CreateLocalPos0Attr().Set(Gf.Vec3f(*joint_record["local_pos0"]))
        joint.CreateLocalPos1Attr().Set(Gf.Vec3f(*joint_record["local_pos1"]))
        local_rot0 = joint_record["local_rot0"]
        local_rot1 = joint_record["local_rot1"]
        joint.CreateLocalRot0Attr().Set(Gf.Quatf(local_rot0[0], Gf.Vec3f(*local_rot0[1:])))
        joint.CreateLocalRot1Attr().Set(Gf.Quatf(local_rot1[0], Gf.Vec3f(*local_rot1[1:])))
    stage.GetRootLayer().Save()
    return path, {
        "status": "authored",
        "articulation_root_path": f"/{asset_id}",
        "body_paths": body_paths,
        "body_validation_status": "pending_composed_stage_validation",
        "joints": normalised_joints,
        "blocked_reasons": [],
    }


def _semantic_layer(
    asset_dir: Path, asset_id: str, primary_source: dict[str, Any], appearance_segments: list[dict[str, Any]]
) -> Path:
    segment_body = ""
    for segment in appearance_segments:
        segment_body += (
            f'        def Xform "{_usda_escape(str(segment["segment_id"]))}" (\n'
            + '            prepend apiSchemas = ["SemanticsLabelsAPI:class", "SemanticsLabelsAPI:label", "MaterialBindingAPI"]\n'
            + "        )\n"
            + "        {\n"
            + f'            token[] semantics:labels:class = ["{_usda_escape(str(segment["semantic_class"]))}"]\n'
            + f'            token[] semantics:labels:label = ["{_usda_escape(str(segment["semantic_label"]))}"]\n'
            + f"            rel material:binding = <{_usda_escape(str(segment['material_prim_path']))}>\n"
            + f'            custom string appearance_segment_id = "{_usda_escape(str(segment["segment_id"]))}"\n'
            + f'            custom string appearance_mask_path = "{_usda_escape(str(segment["mask_path"]))}"\n'
            + f'            custom string material_name = "{_usda_escape(str(segment["material_name"]))}"\n'
            + f"            custom double segmentation_confidence = {float(segment['confidence'])}\n"
            + "        }\n"
        )
    return _write_text(
        asset_dir / "sem.usda",
        _layer_header(default_prim=asset_id)
        + f'def Xform "{asset_id}" (\n'
        + '    prepend apiSchemas = ["SemanticsLabelsAPI:class", "SemanticsLabelsAPI:label"]\n'
        + ")\n"
        + "{\n"
        + '    token[] semantics:labels:class = ["asset"]\n'
        + f'    token[] semantics:labels:label = ["{_usda_escape(asset_id)}"]\n'
        + '    custom string semantic_label = "asset"\n'
        + '    custom string[] affordances = ["inspectable", "simready_candidate"]\n'
        + '    custom string task_metadata = "generated asset package candidate"\n'
        + f'    custom string provenance_source_sha256 = "{_usda_escape(primary_source["copy_sha256"])}"\n'
        + f'    custom string provenance_source_path = "{_usda_escape(primary_source["project_copy_path"])}"\n'
        + f"    custom int appearance_segment_count = {len(appearance_segments)}\n"
        + '    def Scope "SemanticSegments"\n'
        + "    {\n"
        + segment_body
        + "    }\n"
        + "}\n",
    )


def _variants_layer(
    asset_dir: Path,
    asset_id: str,
    texture_variants: list[dict[str, Any]],
    deformations: list[dict[str, Any]],
    appearance_segments: list[dict[str, Any]],
) -> Path:
    material_variants = texture_variants or [_default_texture_variant_record(asset_dir)]
    material_body = ""
    for item in material_variants:
        variant_id = str(item["variant_id"])
        material_name = "DefaultMaterial" if variant_id == "default" else _usd_identifier(variant_id, "Material")
        region_body = ""
        for segment in appearance_segments:
            region_body += (
                f'                def Scope "{_usda_escape(str(segment["segment_id"]))}"\n'
                + "                {\n"
                + f'                    custom string segment_id = "{_usda_escape(str(segment["segment_id"]))}"\n'
                + f'                    custom string mask_path = "{_usda_escape(str(segment["mask_path"]))}"\n'
                + f'                    custom string material_prim_path = "{_usda_escape(str(segment["material_prim_path"]))}"\n'
                + "                }\n"
            )
        material_body += (
            f'        "{_usda_escape(variant_id)}" {{\n'
            + f"            rel material:binding = </{asset_id}/Materials/{material_name}>\n"
            + '            def Scope "MaterialProfile"\n'
            + "            {\n"
            + '                custom string material_variant_status = "proposal"\n'
            + f'                custom string texture_intent = "{_usda_escape(str(item.get("texture_intent", "")))}"\n'
            + f'                custom string base_color_path = "{_usda_escape(str(item.get("base_color_path", "")))}"\n'
            + f'                custom string normal_path = "{_usda_escape(str(item.get("normal_path", "")))}"\n'
            + f'                custom string roughness_path = "{_usda_escape(str(item.get("roughness_path", "")))}"\n'
            + f'                custom string metallic_path = "{_usda_escape(str(item.get("metallic_path", "")))}"\n'
            + f"                custom int appearance_segment_count = {len(appearance_segments)}\n"
            + '                def Scope "MaterialRegions"\n'
            + "                {\n"
            + region_body
            + "                }\n"
            + "            }\n"
            + "        }\n"
        )
    deformation_body = (
        '        "none" {\n'
        + '            def Scope "GeometryDeformation"\n'
        + "            {\n"
        + '                custom string deformation_status = "off"\n'
        + "            }\n"
        + "        }\n"
    )
    for item in deformations:
        deformation_body += (
            f'        "{_usda_escape(item["variant_id"])}" {{\n'
            + '            def Scope "GeometryDeformation"\n'
            + "            {\n"
            + '                custom string deformation_status = "proposal"\n'
            + f'                custom string deformation_kind = "{_usda_escape(str(item.get("deformation_kind", "")))}"\n'
            + f"                custom double amplitude_m = {float(item.get('amplitude_m', 0.0))}\n"
            + f"                custom double radius_m = {float(item.get('radius_m', 0.0))}\n"
            + f'                custom string height_or_displacement_path = "{_usda_escape(str(item.get("height_or_displacement_path", "")))}"\n'
            + "            }\n"
            + "        }\n"
        )
    return _write_text(
        asset_dir / "variants.usda",
        _layer_header(default_prim=asset_id)
        + f'def Xform "{asset_id}" (\n'
        + "    variants = {\n"
        + '        string materialProfile = "default"\n'
        + '        string geometryDeformation = "none"\n'
        + '        string physicsProfile = "review_required"\n'
        + '        string articulationProfile = "static"\n'
        + '        string domainRandomization = "off"\n'
        + "    }\n"
        + '    prepend variantSets = ["materialProfile", "geometryDeformation", "physicsProfile", "articulationProfile", "domainRandomization"]\n'
        + ")\n"
        + "{\n"
        + '    variantSet "materialProfile" = {\n'
        + material_body
        + "    }\n"
        + '    variantSet "geometryDeformation" = {\n'
        + deformation_body
        + "    }\n"
        + '    variantSet "physicsProfile" = {\n'
        + '        "review_required" {\n'
        + '            def Scope "PhysicsProfile"\n'
        + "            {\n"
        + '                custom string physics_variant_status = "requires_validation"\n'
        + "            }\n"
        + "        }\n"
        + "    }\n"
        + '    variantSet "articulationProfile" = {\n'
        + '        "static" {\n'
        + '            def Scope "ArticulationProfile"\n'
        + "            {\n"
        + '                custom string articulation_variant_status = "static_asset"\n'
        + "            }\n"
        + "        }\n"
        + "    }\n"
        + '    variantSet "domainRandomization" = {\n'
        + '        "off" {\n'
        + '            def Scope "DomainRandomization"\n'
        + "            {\n"
        + '                custom string randomisation_status = "disabled_until_policy_selected"\n'
        + "            }\n"
        + "        }\n"
        + "    }\n"
        + "}\n",
    )


def _deformation_layer(asset_dir: Path, asset_id: str, deformations: list[dict[str, Any]]) -> Path:
    body = ""
    for item in deformations:
        body += (
            f'        def Scope "{_usda_escape(item["variant_id"])}"\n'
            + "        {\n"
            + f'            custom string deformation_kind = "{_usda_escape(str(item.get("deformation_kind", "")))}"\n'
            + f'            custom string deformation_description = "{_usda_escape(str(item.get("description", "")))}"\n'
            + f"            custom double deformation_amplitude_m = {float(item.get('amplitude_m', 0.0))}\n"
            + f"            custom double deformation_radius_m = {float(item.get('radius_m', 0.0))}\n"
            + f"            custom int deformation_count = {int(item.get('count', 0))}\n"
            + f'            custom string height_or_displacement_path = "{_usda_escape(str(item.get("height_or_displacement_path", "")))}"\n'
            + '            custom string deformation_validation_status = "review_required"\n'
            + "        }\n"
        )
    return _write_text(
        asset_dir / "deform.usda",
        _layer_header(default_prim=asset_id)
        + f'def Xform "{asset_id}"\n'
        + "{\n"
        + '    def Scope "GeometryDeformations"\n'
        + "    {\n"
        + body
        + "    }\n"
        + "}\n",
    )


def _contents_layer(asset_dir: Path, asset_id: str) -> Path:
    return _write_text(
        asset_dir / "contents.usda",
        _layer_header(default_prim=asset_id)
        + f'def Xform "{asset_id}"\n'
        + "{\n"
        + '    def Scope "Contents"\n'
        + "    {\n"
        + f"        rel sourceGeometry = </{asset_id}/Geometry>\n"
        + "        custom asset sourceGeometryLayer = @./source/normalised.usda@\n"
        + '        custom string assembly_policy = "canonical geometry is composed once through geo.usda"\n'
        + "    }\n"
        + "}\n",
    )


def _is_linklike(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _prepare_generated_package_root(project_dir: Path, package_target: Path) -> Path:
    """Replace one generated package directory without crossing the project package boundary."""

    project_root = project_dir.resolve(strict=True)
    declared_package_root = project_dir / "packaged"
    expected_package_root = project_root / "packaged"
    if _is_linklike(declared_package_root):
        raise ValueError("generated package root must not be a symbolic link or junction")
    if declared_package_root.exists() and not declared_package_root.is_dir():
        raise ValueError("generated package root must be a directory")
    if declared_package_root.resolve(strict=False) != expected_package_root:
        raise ValueError("generated package root resolves outside the project")

    try:
        relative_target = package_target.relative_to(declared_package_root)
    except ValueError as exc:
        raise ValueError("generated package target must be exactly beneath project/packaged") from exc
    if len(relative_target.parts) != 1 or relative_target.name in {"", ".", ".."}:
        raise ValueError("generated package target must be exactly one asset beneath project/packaged")
    expected_target = expected_package_root / relative_target.name
    if package_target.resolve(strict=False) != expected_target:
        raise ValueError("generated package target resolves outside project/packaged")
    if _is_linklike(package_target):
        raise ValueError("generated asset package must not be a symbolic link or junction")
    if package_target.exists():
        if not package_target.is_dir():
            raise ValueError("generated asset package must be a directory")
        if package_target.resolve(strict=True) != expected_target:
            raise ValueError("existing generated asset package resolves outside project/packaged")
        shutil.rmtree(package_target)

    declared_package_root.mkdir(parents=True, exist_ok=True)
    package_target.mkdir()
    if _is_linklike(package_target) or package_target.resolve(strict=True) != expected_target:
        raise ValueError("generated asset package could not be recreated safely")
    return package_target


def compose_project_asset(
    project_dir: Path,
    asset_id: str,
    source_ingestion: dict[str, Any],
    requested_outputs: list[str] | tuple[str, ...] = (),
    constraints: dict[str, Any] | None = None,
    live_texture_generation: bool = False,
) -> dict[str, Any]:
    copied_sources = [
        record for record in source_ingestion.get("source_assets", []) if record.get("status") == "copied"
    ]
    if not copied_sources:
        return {
            "status": "blocked",
            "blocked_reasons": ["no copied source assets available for asset composition"],
            "files": [],
        }

    constraints = constraints or {}
    safe_asset_id = slugify(asset_id)
    asset_dir = project_dir / "assets" / safe_asset_id
    asset_evidence_dir = asset_dir / "evidence"
    asset_reports_dir = asset_dir / "reports"
    packaged_dir = project_dir / "packaged"
    packaged_asset_dir = packaged_dir / safe_asset_id
    primary_source = copied_sources[0]
    source_copy = _copy_or_reference_source(primary_source, project_dir, asset_dir)
    allow_baking = bool(constraints.get("allow_project_copy_baking") or constraints.get("permit_project_copy_baking"))
    source_inspection = _inspect_usd_source(source_copy, allow_baking)
    external_reconstruction_run: dict[str, Any] = {}
    geometry_source: Path | None = source_copy if source_copy.suffix.lower() in SCAN_SUFFIXES else None
    if source_copy.suffix.lower() in IMAGE_SUFFIXES | VIDEO_SUFFIXES:
        source_inspection["reconstruction_required"] = True
        external_reconstruction_run = _recorded_external_reconstruction(project_dir, safe_asset_id)
        if external_reconstruction_run:
            source_inspection["external_reconstruction_recorded"] = True
            recorded_mesh = project_dir / str(external_reconstruction_run["mesh_path"])
            geometry_source = asset_dir / "source" / f"validated_reconstruction{recorded_mesh.suffix.lower()}"
            geometry_source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(recorded_mesh, geometry_source)
        else:
            source_inspection["blocked_reasons"].append("external reconstruction validation required before release")
    approved_geometry = _approved_canonical_geometry(project_dir)
    if approved_geometry is not None:
        geometry_source = approved_geometry
        source_inspection["mesh_verification_status"] = "approved"
        source_inspection["canonical_geometry_path"] = approved_geometry.relative_to(project_dir).as_posix()
    normalised_source, mesh_conditioning = _normalised_source_layer(
        asset_dir, source_copy, source_inspection, geometry_source
    )
    source_inspection["mesh_conditioning"] = mesh_conditioning
    if mesh_conditioning.get("status") == "blocked":
        reason = str(mesh_conditioning.get("blocked_reason") or "conditioned geometry is unavailable")
        if reason not in source_inspection["blocked_reasons"]:
            source_inspection["blocked_reasons"].append(reason)
    if mesh_conditioning.get("unit_status") == "unknown":
        reason = "mesh source units must be declared before SimReady promotion"
        if reason not in source_inspection["blocked_reasons"]:
            source_inspection["blocked_reasons"].append(reason)

    raw_profile = constraints.get("simready_profile") or constraints.get("target_simready_profile") or {}
    if isinstance(raw_profile, str):
        raw_profile = {"profile_id": raw_profile}
    profile_id = str(raw_profile.get("profile_id") or raw_profile.get("name") or "Prop-Robotics-Neutral")
    profile_version = str(
        raw_profile.get("profile_version")
        or raw_profile.get("version")
        or constraints.get("simready_profile_version")
        or ""
    )
    simready_profile = {
        "profile_id": profile_id,
        "profile_version": profile_version or "unresolved",
        "profile_version_status": "pinned" if profile_version else "unresolved",
        "target_runtime": "runtime-neutral"
        if profile_id.endswith("Neutral")
        else profile_id.rsplit("-", 1)[-1].lower(),
        "specification_uri": "https://docs.omniverse.nvidia.com/simready/latest/simready-faq.html",
    }
    if not profile_version:
        source_inspection["blocked_reasons"].append("target SimReady Profile version must be pinned")
    texture_requested = _texture_variants_requested(requested_outputs, constraints)
    deformation_requested = _mesh_deformations_requested(requested_outputs, constraints)
    texture_file_targets = _texture_paths(asset_dir) if texture_requested else []
    asset_evidence_dir.mkdir(parents=True, exist_ok=True)
    asset_reports_dir.mkdir(parents=True, exist_ok=True)

    geo = _write_text(
        asset_dir / "geo.usda",
        _layer_header(default_prim=safe_asset_id)
        + f'def Xform "{safe_asset_id}"\n'
        + "{\n"
        + '    def Xform "Geometry" (\n'
        + "        prepend references = @./source/normalised.usda@</World>\n"
        + "    )\n"
        + "    {\n"
        + '        custom string geometry_status = "normalised_project_copy_reference"\n'
        + "    }\n"
        + "}\n",
    )
    texture_files = _write_texture_set(texture_file_targets) if texture_requested else []
    texture_generation_status = "blocked" if texture_requested else "not_requested"
    texture_blocked_reasons = (
        ["texture synthesis provider did not run; local preview scaffolds are not production PBR textures"]
        if texture_requested
        else []
    )
    texture_generation_backend = "local_preview_scaffold" if texture_requested else "not_requested"
    texture_provider_trace: list[dict[str, Any]] = []
    texture_map_policy_trace: list[dict[str, Any]] = []
    texture_prompt_plan: list[dict[str, Any]] = []
    texture_variant_records, texture_variant_files = (
        _write_texture_variant_sets(
            asset_dir, constraints.get("texture_variants"), str(constraints.get("object_prompt", safe_asset_id))
        )
        if texture_requested
        else ([], [])
    )
    if texture_requested:
        texture_prompt_plan = build_live_texture_request_plan(asset_dir, texture_variant_records, constraints)[
            "texture_prompt_plan"
        ]
    if texture_requested and live_texture_generation:
        try:
            live_texture_result = generate_live_texture_sets(asset_dir, texture_variant_records, constraints)
            texture_prompt_plan = list(live_texture_result.get("texture_prompt_plan", texture_prompt_plan))
            if live_texture_result.get("status") == "generated":
                texture_variant_records = list(live_texture_result.get("texture_variants", texture_variant_records))
                texture_generation_status = "generated"
                texture_blocked_reasons = []
                texture_generation_backend = str(live_texture_result.get("backend") or "simple_image_gen")
                texture_provider_trace = list(live_texture_result.get("provider_trace", []))
                texture_map_policy_trace = list(live_texture_result.get("map_policy_trace", []))
            else:
                texture_blocked_reasons = list(live_texture_result.get("blocked_reasons", [])) or [
                    "live texture synthesis did not produce generated PBR texture maps"
                ]
                texture_generation_backend = str(live_texture_result.get("backend") or "live_texture_generation")
        except Exception as exc:
            texture_blocked_reasons = [f"live texture synthesis failed: {exc}"]
            texture_generation_backend = "live_texture_generation"
    appearance_segments, appearance_segment_files = _write_appearance_segments(asset_dir, safe_asset_id, primary_source)
    mtl = _material_layer(asset_dir, safe_asset_id, texture_files, texture_variant_records, appearance_segments)
    materialx = _materialx_document(asset_dir, texture_files, texture_variant_records, appearance_segments)
    material_adapters = _material_adapter_record(asset_dir, materialx, mtl)
    deformation_records, deformation_files = (
        _write_deformation_plan(asset_dir, primary_source["project_copy_path"]) if deformation_requested else ([], [])
    )
    raw_physics_evidence = constraints.get("physics_evidence") or constraints.get("physics")
    phy, physics = _physics_layer(
        project_dir,
        asset_dir,
        safe_asset_id,
        list(mesh_conditioning.get("collision_prim_paths", [])),
        raw_physics_evidence,
    )
    physics_evidence_files = _materialise_physics_evidence(project_dir, asset_dir, physics)
    art, articulation = _articulation_layer(asset_dir, safe_asset_id, constraints.get("articulation"))
    articulation_required = source_copy.suffix.lower() in {".urdf", ".xml"} or any(
        term in " ".join(str(item).lower() for item in requested_outputs)
        for term in ("articulation", "articulated", "robot body", "robot-body")
    )
    sem = _semantic_layer(asset_dir, safe_asset_id, primary_source, appearance_segments)
    deform = _deformation_layer(asset_dir, safe_asset_id, deformation_records) if deformation_requested else None
    variants = _variants_layer(
        asset_dir, safe_asset_id, texture_variant_records, deformation_records, appearance_segments
    )
    contents = _contents_layer(asset_dir, safe_asset_id)
    optional_references = f"        @./deform.usda@</{safe_asset_id}>,\n" if deform else ""
    root_layer = _write_text(
        asset_dir / f"{safe_asset_id}.usda",
        _layer_header(default_prim=safe_asset_id)
        + f'def Xform "{safe_asset_id}" (\n'
        + "    prepend references = [\n"
        + f"        @./geo.usda@</{safe_asset_id}>,\n"
        + f"        @./mtl.usda@</{safe_asset_id}>,\n"
        + f"        @./phy.usda@</{safe_asset_id}>,\n"
        + f"        @./art.usda@</{safe_asset_id}>,\n"
        + f"        @./sem.usda@</{safe_asset_id}>,\n"
        + optional_references
        + f"        @./variants.usda@</{safe_asset_id}>,\n"
        + f"        @./contents.usda@</{safe_asset_id}>\n"
        + "    ]\n"
        + ")\n"
        + "{\n"
        + '    custom string asset_factory_status = "generated_proposal"\n'
        + f'    custom string asset_factory_source = "{_usda_escape(primary_source["project_copy_path"])}"\n'
        + "}\n",
    )
    scene = _write_text(
        project_dir / "scene.usda",
        _layer_header(default_prim="World")
        + 'def Xform "World"\n'
        + "{\n"
        + '    def DistantLight "KeyLight"\n'
        + "    {\n"
        + "        float inputs:intensity = 400\n"
        + "    }\n"
        + f'    def Xform "{safe_asset_id}" (\n'
        + f"        prepend references = @./assets/{safe_asset_id}/{safe_asset_id}.usda@</{safe_asset_id}>\n"
        + "    )\n"
        + "    {\n"
        + "    }\n"
        + "}\n",
    )
    environment = _write_text(
        project_dir / "environment.usda",
        _layer_header(default_prim="Environment")
        + 'def Xform "Environment"\n'
        + "{\n"
        + '    def Xform "Scene" (\n'
        + "        prepend references = @./scene.usda@</World>\n"
        + "    )\n"
        + "    {\n"
        + "    }\n"
        + '    def Scope "Task"\n'
        + "    {\n"
        + f'        custom string asset_id = "{_usda_escape(safe_asset_id)}"\n'
        + '        custom string rl_contract_status = "blocked_until_asset_load_and_physics_gates_pass"\n'
        + '        custom string observation_hooks = "declare cameras and state tensors before training"\n'
        + '        custom string action_hooks = "declare robot and controller before training"\n'
        + "    }\n"
        + "}\n",
    )
    packaged_asset_dir = _prepare_generated_package_root(project_dir, packaged_asset_dir)
    stale_flat_package = packaged_dir / f"{safe_asset_id}.usda"
    if stale_flat_package.exists():
        stale_flat_package.unlink()

    packaged = packaged_asset_dir / f"{safe_asset_id}.usda"
    packaged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root_layer, packaged)
    packaged_files = [packaged]
    for path in [
        geo,
        mtl,
        materialx,
        material_adapters,
        phy,
        art,
        sem,
        *([deform] if deform else []),
        variants,
        contents,
    ]:
        packaged_layer = packaged_asset_dir / path.name
        shutil.copy2(path, packaged_layer)
        packaged_files.append(packaged_layer)
    packaged_source_dir = packaged_asset_dir / "source"
    packaged_source_dir.mkdir(parents=True, exist_ok=True)
    source_package_files = [source_copy, normalised_source]
    if geometry_source is not None and geometry_source.exists() and geometry_source.resolve() != source_copy.resolve():
        source_package_files.append(geometry_source)
    for path in source_package_files:
        packaged_source = packaged_source_dir / path.name
        shutil.copy2(path, packaged_source)
        packaged_files.append(packaged_source)
    if texture_files:
        packaged_texture_dir = packaged_asset_dir / "textures"
        packaged_texture_dir.mkdir(parents=True, exist_ok=True)
        for path in texture_files:
            packaged_texture = packaged_texture_dir / path.name
            shutil.copy2(path, packaged_texture)
            packaged_files.append(packaged_texture)
    if texture_variant_files:
        packaged_variant_dir = packaged_asset_dir / "textures" / "variants"
        packaged_variant_dir.mkdir(parents=True, exist_ok=True)
        for path in texture_variant_files:
            packaged_texture = packaged_variant_dir / path.name
            shutil.copy2(path, packaged_texture)
            packaged_files.append(packaged_texture)
    if appearance_segment_files:
        packaged_segment_dir = packaged_asset_dir / "textures" / "segments"
        packaged_segment_dir.mkdir(parents=True, exist_ok=True)
        for path in appearance_segment_files:
            packaged_segment = packaged_segment_dir / path.name
            shutil.copy2(path, packaged_segment)
            packaged_files.append(packaged_segment)
    if deformation_files:
        packaged_deformation_dir = packaged_asset_dir / "deformations"
        packaged_deformation_dir.mkdir(parents=True, exist_ok=True)
        for path in deformation_files:
            packaged_deformation = packaged_deformation_dir / path.name
            shutil.copy2(path, packaged_deformation)
            packaged_files.append(packaged_deformation)
    for path in physics_evidence_files:
        packaged_evidence = packaged_asset_dir / path.relative_to(asset_dir)
        packaged_evidence.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, packaged_evidence)
        packaged_files.append(packaged_evidence)

    layer_paths = [root_layer, geo, mtl, phy, art, sem, *([deform] if deform else []), variants, contents]
    layer_stack = [_rel(path, project_dir) for path in layer_paths]
    package_files = [
        *source_package_files,
        *texture_files,
        *texture_variant_files,
        *appearance_segment_files,
        *deformation_files,
        *physics_evidence_files,
        geo,
        mtl,
        materialx,
        material_adapters,
        phy,
        art,
        sem,
        *([deform] if deform else []),
        variants,
        contents,
        root_layer,
        scene,
        environment,
        *packaged_files,
    ]
    package_file_records = [{"path": _rel(path, project_dir), "sha256": sha256_file(path)} for path in package_files]
    blocked_reasons = list(source_inspection.get("blocked_reasons", []))
    blocked_reasons.extend(physics.get("blocked_reasons", []))
    if articulation_required and articulation.get("status") != "authored":
        blocked_reasons.extend(articulation.get("blocked_reasons", []) or ["articulation evidence is required"])
    release_blockers = ["isaac load validation has not run", *texture_blocked_reasons, *blocked_reasons]
    asset_evidence = _write_json(
        asset_evidence_dir / "asset-package-evidence.json",
        {
            "asset_id": safe_asset_id,
            "status": "generated_proposal" if not release_blockers[1:] else "blocked",
            "source_asset": primary_source["project_copy_path"],
            "source_copy": _rel(source_copy, project_dir),
            "source_inspection": source_inspection,
            "mesh_conditioning": mesh_conditioning,
            "physics": physics,
            "articulation": articulation,
            "simready_profile": simready_profile,
            "normalised_source": _rel(normalised_source, project_dir),
            "texture_requested": texture_requested,
            "texture_generation_status": texture_generation_status,
            "texture_generation_backend": texture_generation_backend,
            "texture_blocked_reasons": texture_blocked_reasons,
            "texture_provider_trace": texture_provider_trace,
            "texture_map_policy_trace": texture_map_policy_trace,
            "texture_prompt_plan": texture_prompt_plan,
            "appearance_segments": appearance_segments,
            "material_representations": {
                "canonical_usd": _rel(mtl, project_dir),
                "materialx_sidecar": _rel(materialx, project_dir),
                "adapter_record": _rel(material_adapters, project_dir),
                "preview_surface": _rel(mtl, project_dir),
                "materialx_usd_binding_status": "blocked_not_bound_to_usd_render_context",
            },
            "appearance_segment_outputs": [_rel(path, project_dir) for path in appearance_segment_files],
            "texture_variants": texture_variant_records,
            "texture_preview_outputs": [_rel(path, project_dir) for path in [*texture_files, *texture_variant_files]],
            "mesh_deformation_requested": deformation_requested,
            "mesh_deformation_requests": deformation_records,
            "generated_files": package_file_records,
            "release_blockers": release_blockers,
        },
    )
    asset_report = _write_json(
        asset_reports_dir / "asset-authoring-report.json",
        {
            "asset_id": safe_asset_id,
            "status": "generated_proposal" if not release_blockers[1:] else "blocked",
            "canonical_root": _rel(root_layer, project_dir),
            "scene_layer": _rel(scene, project_dir),
            "environment_layer": _rel(environment, project_dir),
            "package_path": _rel(packaged, project_dir),
            "usd_layer_stack": layer_stack,
            "mesh_conditioning": mesh_conditioning,
            "physics": physics,
            "articulation": articulation,
            "simready_profile": simready_profile,
            "source_copy_only": True,
            "texture_outputs": [_rel(path, project_dir) for path in texture_files],
            "texture_generation_status": texture_generation_status,
            "texture_generation_backend": texture_generation_backend,
            "texture_blocked_reasons": texture_blocked_reasons,
            "texture_provider_trace": texture_provider_trace,
            "texture_map_policy_trace": texture_map_policy_trace,
            "texture_prompt_plan": texture_prompt_plan,
            "texture_preview_outputs": [_rel(path, project_dir) for path in [*texture_files, *texture_variant_files]],
            "appearance_segments": appearance_segments,
            "material_representations": {
                "canonical_usd": _rel(mtl, project_dir),
                "materialx_sidecar": _rel(materialx, project_dir),
                "adapter_record": _rel(material_adapters, project_dir),
                "preview_surface": _rel(mtl, project_dir),
                "materialx_usd_binding_status": "blocked_not_bound_to_usd_render_context",
            },
            "texture_variant_outputs": [_rel(path, project_dir) for path in texture_variant_files],
            "appearance_segment_outputs": [_rel(path, project_dir) for path in appearance_segment_files],
            "mesh_deformation_outputs": [_rel(path, project_dir) for path in deformation_files],
            "deformation_usd_path": _rel(deform, project_dir) if deform else "",
            "blocked_reasons": [*texture_blocked_reasons, *blocked_reasons],
        },
    )
    files = package_files + [asset_evidence, asset_report]
    return {
        "status": "generated" if not blocked_reasons else "blocked",
        "asset_id": safe_asset_id,
        "asset_dir": _rel(asset_dir, project_dir),
        "asset_evidence_path": _rel(asset_evidence, project_dir),
        "asset_report_path": _rel(asset_report, project_dir),
        "normalised_source_path": _rel(normalised_source, project_dir),
        "mesh_conditioning": mesh_conditioning,
        "physics": physics,
        "articulation": articulation,
        "simready_profile": simready_profile,
        "usd_root_path": _rel(root_layer, project_dir),
        "package_path": _rel(packaged, project_dir),
        "scene_path": _rel(scene, project_dir),
        "environment_path": _rel(environment, project_dir),
        "usd_layer_stack": layer_stack,
        "texture_outputs": [_rel(path, project_dir) for path in texture_files],
        "texture_generation_status": texture_generation_status,
        "texture_generation_backend": texture_generation_backend,
        "texture_blocked_reasons": texture_blocked_reasons,
        "texture_provider_trace": texture_provider_trace,
        "texture_map_policy_trace": texture_map_policy_trace,
        "texture_prompt_plan": texture_prompt_plan,
        "texture_preview_outputs": [_rel(path, project_dir) for path in [*texture_files, *texture_variant_files]],
        "appearance_segments": appearance_segments,
        "material_representations": {
            "canonical_usd": _rel(mtl, project_dir),
            "materialx_sidecar": _rel(materialx, project_dir),
            "adapter_record": _rel(material_adapters, project_dir),
            "preview_surface": _rel(mtl, project_dir),
            "materialx_usd_binding_status": "blocked_not_bound_to_usd_render_context",
        },
        "appearance_segment_outputs": [_rel(path, project_dir) for path in appearance_segment_files],
        "texture_variants": texture_variant_records,
        "texture_variant_outputs": [_rel(path, project_dir) for path in texture_variant_files],
        "mesh_deformation_requested": deformation_requested,
        "mesh_deformation_requests": deformation_records,
        "mesh_deformation_outputs": [_rel(path, project_dir) for path in deformation_files],
        "deformation_usd_path": _rel(deform, project_dir) if deform else "",
        "files": [{"path": _rel(path, project_dir), "sha256": sha256_file(path)} for path in files],
        "source_inspection": source_inspection,
        "external_reconstruction_run": external_reconstruction_run,
        "blocked_reasons": [*texture_blocked_reasons, *blocked_reasons],
    }
