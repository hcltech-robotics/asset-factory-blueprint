from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from asset_factory_blueprint.skills.base import ToolResult
from asset_factory_blueprint.texture_defaults import build_prompt, explain
from asset_factory_blueprint.utils.checksums import sha256_file


DEFAULT_TEXTURE_VARIANTS = [
    {
        "variant_id": "clean_satin",
        "material_name": "painted_metal",
        "texture_intent": "clean satin finish from the visible source image",
        "prompt": "clean satin painted metal, even base colour, physically plausible roughness, no baked lighting",
        "negative_prompt": "cast shadows, text, logos, watermarks, implausible wear",
        "seed": 11,
    },
    {
        "variant_id": "worn_edges",
        "material_name": "painted_metal",
        "texture_intent": "subtle worn-edge finish while preserving the source silhouette",
        "prompt": "painted metal with subtle worn edges, small scuffs, consistent UV scale, no baked lighting",
        "negative_prompt": "large damage, text, logos, watermarks, changed shape",
        "seed": 23,
    },
    {
        "variant_id": "rough_speckled",
        "material_name": "painted_metal",
        "texture_intent": "rough speckled finish for domain variety",
        "prompt": "rough speckled painted surface, fine material noise, tileable PBR maps, no baked lighting",
        "negative_prompt": "deep geometry edits, text, logos, watermarks, cast shadows",
        "seed": 37,
    },
]

DEFAULT_DEFORMATION_VARIANTS = [
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


def material_texture_prompt(params: dict[str, Any]) -> ToolResult:
    material_manifest = params.get("material_manifest")
    property_manifest = params.get("property_manifest")
    output = params.get("output")
    if material_manifest and property_manifest and output:
        prompt = build_prompt(material_manifest, property_manifest, output)
        return ToolResult(success=True, data=prompt, artefacts=[str(Path(output))], validation_status="proposal")
    material = str(params.get("material_class") or "rubber")
    profile = explain(material, params.get("profile"))
    data = {
        "material_class": material,
        "profile_id": profile["profile_id"],
        "prompt": profile["pbr_defaults"]["base_color_prompt"],
        "negative_prompt": ", ".join(profile["forbidden_cues"]),
        "maps": profile["map_policy"]["maps"],
        "numeric_physics_authored": False,
    }
    return ToolResult(success=True, data=data, proposals=[data], validation_status="proposal")


def material_texture_defaults_validate(params: dict[str, Any]) -> ToolResult:
    material = str(params.get("material_class") or "rubber")
    visible_cues = {str(item).lower() for item in params.get("visible_cues", [])}
    profile = explain(material, params.get("profile"))
    forbidden = {str(item).lower() for item in profile["forbidden_cues"]}
    contradictions = sorted(visible_cues & forbidden)
    warnings = [f"visible cue conflicts with texture policy: {item}" for item in contradictions]
    return ToolResult(
        success=not contradictions,
        data={"material_class": material, "profile_id": profile["profile_id"], "contradictions": contradictions},
        warnings=warnings,
        validation_status="validated" if not contradictions else "review_required",
    )


def _normalise_texture_variants(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return [dict(item) for item in DEFAULT_TEXTURE_VARIANTS]
    variants = raw if isinstance(raw, list) else [raw]
    records: list[dict[str, Any]] = []
    for index, item in enumerate(variants):
        if isinstance(item, dict):
            variant_id = str(item.get("variant_id") or item.get("id") or f"variant_{index + 1}")
            records.append(
                {
                    "variant_id": variant_id,
                    "material_name": str(item.get("material_name") or "painted_metal"),
                    "texture_intent": str(item.get("texture_intent") or item.get("intent") or variant_id.replace("_", " ")),
                    "prompt": str(item.get("prompt") or f"{variant_id.replace('_', ' ')} PBR material from the source image"),
                    "negative_prompt": str(item.get("negative_prompt") or "text, logos, watermarks, baked lighting"),
                    "seed": int(item.get("seed") or index + 1),
                }
            )
            continue
        variant_id = str(item)
        records.append(
            {
                "variant_id": variant_id,
                "material_name": "painted_metal",
                "texture_intent": variant_id.replace("_", " "),
                "prompt": f"{variant_id.replace('_', ' ')} PBR material from the source image",
                "negative_prompt": "text, logos, watermarks, baked lighting",
                "seed": index + 1,
            }
        )
    return records


def _normalise_deformations(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return [dict(item) for item in DEFAULT_DEFORMATION_VARIANTS]
    variants = raw if isinstance(raw, list) else [raw]
    records: list[dict[str, Any]] = []
    for index, item in enumerate(variants):
        if isinstance(item, dict):
            kind = str(item.get("deformation_kind") or item.get("kind") or "dent")
            variant_id = str(item.get("variant_id") or item.get("id") or f"{kind}_{index + 1}")
            records.append(
                {
                    "variant_id": variant_id,
                    "deformation_kind": kind,
                    "description": str(item.get("description") or f"{kind} mesh deformation"),
                    "amplitude_m": float(item.get("amplitude_m") or item.get("amplitude") or (-0.01 if kind == "dent" else 0.01)),
                    "radius_m": float(item.get("radius_m") or item.get("radius") or 0.06),
                    "count": int(item.get("count") or 4),
                }
            )
            continue
        kind = str(item)
        records.append(
            {
                "variant_id": kind,
                "deformation_kind": kind,
                "description": f"{kind} mesh deformation",
                "amplitude_m": -0.01 if kind == "dent" else 0.01,
                "radius_m": 0.06,
                "count": index + 3,
            }
        )
    return records


def _normalise_appearance_segments(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    segments = raw if isinstance(raw, list) else [raw]
    records: list[dict[str, Any]] = []
    for index, item in enumerate(segments):
        if isinstance(item, dict):
            segment_id = str(item.get("segment_id") or item.get("id") or f"segment_{index + 1}")
            records.append(
                {
                    "segment_id": segment_id,
                    "label": str(item.get("label") or segment_id.replace("_", " ")),
                    "semantic_class": str(item.get("semantic_class") or "asset_part"),
                    "prim_path": str(item.get("prim_path") or f"/asset/SemanticSegments/{segment_id}"),
                    "mask_path": str(item.get("mask_path") or f"textures/segments/{segment_id}_mask.png"),
                    "material_name": str(item.get("material_name") or "painted_metal"),
                    "status": str(item.get("status") or "proposal"),
                }
            )
            continue
        segment_id = str(item)
        records.append(
            {
                "segment_id": segment_id,
                "label": segment_id.replace("_", " "),
                "semantic_class": "asset_part",
                "prim_path": f"/asset/SemanticSegments/{segment_id}",
                "mask_path": f"textures/segments/{segment_id}_mask.png",
                "material_name": "painted_metal",
                "status": "proposal",
            }
        )
    return records


def material_texture_variation_workflow(params: dict[str, Any]) -> ToolResult:
    image_path = str(params.get("image_path") or params.get("source_image") or "")
    output = params.get("output")
    texture_variants = _normalise_texture_variants(params.get("texture_variants"))
    deformations = _normalise_deformations(params.get("mesh_deformations") or params.get("deformations"))
    appearance_segments = _normalise_appearance_segments(params.get("appearance_segments"))
    image = Path(image_path) if image_path else None
    image_exists = bool(image and image.exists())
    image_record = {
        "kind": "source_image_display",
        "image_path": image_path,
        "display_surface": "operator_review",
        "status": "ready" if image_exists else "blocked",
        "checksum": sha256_file(image) if image_exists and image else "",
        "usage": ["texture_reference", "deformation_reference", "review_evidence"],
        "source_assets_mutated": False,
    }
    data = {
        "workflow_id": str(params.get("workflow_id") or "image_texture_deformation"),
        "asset_id": str(params.get("asset_id") or "asset"),
        "source_image_review": image_record,
        "stage_order": [
            "display source image",
            "segment material and appearance regions",
            "generate texture variants",
            "generate mesh deformation variants",
            "author USD variant selectors",
            "record evidence and checksums",
        ],
        "appearance_segments": appearance_segments,
        "texture_variants": texture_variants,
        "mesh_deformation_requests": [
            {
                **item,
                "height_or_displacement_path": f"deformations/{item['variant_id']}_height.png",
                "provider_role": "texture_generator",
                "status": "proposal",
            }
            for item in deformations
        ],
        "output_layers": {
            "material": "mtl.usda",
            "variants": "variants.usda",
            "deformation": "deform.usda",
        },
    }
    artefacts: list[str] = []
    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        artefacts.append(str(target))
    return ToolResult(
        success=image_exists,
        data=data,
        artefacts=artefacts,
        proposals=[data],
        warnings=[] if image_exists else ["source image does not exist"],
        validation_status="proposal" if image_exists else "blocked",
    )
