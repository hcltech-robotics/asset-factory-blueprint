from __future__ import annotations

import hashlib
import os
import re
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageOps

from asset_factory_blueprint.config import load_json
from asset_factory_blueprint.providers import ProviderImageGeneration, generate_image
from asset_factory_blueprint.utils.checksums import sha256_file


ImageGenerator = Callable[..., ProviderImageGeneration]
PROMPT_SOURCE = "content_agents_texture_agent_style"

GENERATED_MAP_PATH_KEYS = {
    "base_color": "base_color_path",
    "normal": "normal_path",
    "roughness": "roughness_path",
}

POLICY_MAP_PATH_KEYS = {
    "metallic": "metallic_path",
}

MAP_PATH_KEYS = {**GENERATED_MAP_PATH_KEYS, **POLICY_MAP_PATH_KEYS}

BARE_METAL_TERMS = {
    "aluminium",
    "aluminum",
    "brass",
    "bronze",
    "brushed metal",
    "copper",
    "iron",
    "metal",
    "steel",
}

NON_METALLIC_SURFACE_TERMS = {
    "ceramic",
    "coated",
    "corroded",
    "dirt",
    "enamel",
    "oxidised",
    "oxidized",
    "paint",
    "painted",
    "patina",
    "plastic",
    "rubber",
    "rust",
    "rusty",
    "wood",
}


def _slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "_" for char in value).strip("_") or "texture"


def _texture_size(constraints: dict[str, Any]) -> int:
    raw = constraints.get("texture_size") or constraints.get("texture_resolution") or os.getenv("AFB_TEXTURE_SIZE", "1024")
    try:
        size = int(raw)
    except (TypeError, ValueError):
        size = 1024
    return max(256, min(2048, size))


def _image_api_size(size: int) -> str:
    return "1024x1024" if size <= 1024 else "1536x1024"


def _provider_name(constraints: dict[str, Any]) -> str:
    return str(constraints.get("texture_provider") or os.getenv("AFB_TEXTURE_PROVIDER") or "openai")


def _provider_policy_path(constraints: dict[str, Any]) -> str:
    return str(constraints.get("provider_policy_path") or os.getenv("AFB_PROVIDER_POLICY_PATH") or "configs/provider-policy.json")


def _provider_model(provider_name: str, constraints: dict[str, Any]) -> str | None:
    policy_path = _provider_policy_path(constraints)
    try:
        provider = load_json(policy_path).get("providers", {}).get(provider_name, {})
    except Exception:
        provider = {}
    provider_image_model_env = str(provider.get("image_model_env") or "")
    provider_model_env = str(provider.get("model_env") or "")
    selected = (
        str(constraints.get("texture_model") or "")
        or os.getenv("AFB_TEXTURE_MODEL", "")
        or os.getenv("AFB_IMAGE_GENERATION_MODEL", "")
        or os.getenv(provider_image_model_env, "")
        or os.getenv(provider_model_env, "")
        or str(provider.get("default_image_model_id") or "")
        or str(provider.get("default_model_id") or "")
    )
    return selected or None


def _clean_text(value: str, constraints: dict[str, Any]) -> str:
    text = value.replace("_", " ").strip()
    for blocked in (
        str(constraints.get("object_prompt") or ""),
        str(constraints.get("asset_prompt") or ""),
        str(constraints.get("asset_id") or ""),
    ):
        blocked = blocked.replace("_", " ").strip()
        if blocked:
            text = re.sub(re.escape(blocked), "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    return text.strip(" ,.")


def _material_surface_prompt(record: dict[str, Any], constraints: dict[str, Any]) -> str:
    material = _clean_text(str(record.get("material_name") or "material"), constraints)
    texture_intent = _clean_text(str(record.get("texture_intent") or ""), constraints)
    prompt = _clean_text(str(record.get("prompt") or ""), constraints)
    variant = _clean_text(str(record.get("variant_id") or ""), constraints)
    parts: list[str] = []
    if material:
        parts.append(f"{material} material surface")
    for value in (texture_intent, prompt, variant.replace("_", " ")):
        if value and value.lower() not in {item.lower() for item in parts}:
            parts.append(value)
    return ", ".join(parts) or "physically based material surface"


def _base_prompt(record: dict[str, Any], constraints: dict[str, Any]) -> str:
    return _material_surface_prompt(record, constraints)


def _map_prompt(record: dict[str, Any], constraints: dict[str, Any], map_kind: str) -> str:
    base = _base_prompt(record, constraints)
    negative = _clean_text(
        str(
            record.get("negative_prompt")
            or "text, logos, labels, watermarks, baked lighting, cast shadows, object silhouette, product shape, perspective"
        ),
        constraints,
    )
    common_rule = (
        "The image must be a flat, front-facing material texture map that tiles seamlessly. "
        "Do not draw a complete object, product layout, silhouette, perspective view, label, logo, lighting setup, baked lighting or cast shadow."
    )
    if map_kind == "base_color":
        map_rule = (
            "Generate a PBR albedo base colour texture map only. "
            "Represent only the visible material colour and surface colour variation; no shading, lighting effects or baked highlights."
        )
    elif map_kind == "normal":
        map_rule = (
            "Generate a tangent-space PBR normal map texture only. "
            "The map should be predominantly blue-purple with subtle red and green channel variation encoding fine surface bumps, scratches, corrosion, brushing or coating grain."
        )
    elif map_kind == "roughness":
        map_rule = (
            "Generate a grayscale PBR roughness map texture only. "
            "White means rough or matte, black means smooth or glossy. "
            "Scratches, corrosion, dust, worn coating and matte regions should be brighter."
        )
    else:
        map_rule = "Generate a flat PBR texture map."
    return f"{base}. {common_rule} {map_rule} Avoid: {negative}."


def _path_value(record: dict[str, Any], path_key: str) -> str:
    return str(record.get(path_key) or "")


def _prompt_plan_id(variant_id: str, segment_id: str, map_kind: str, output_path: str) -> str:
    raw = "|".join((variant_id, segment_id, map_kind, output_path))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _texture_prompt_plan_record(
    record: dict[str, Any],
    constraints: dict[str, Any],
    *,
    provider: str,
    model: str | None,
    size: int,
    api_size: str,
    quality: str,
    output_format: str,
    map_kind: str,
    path_key: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trace_context = _trace_context(record, context)
    variant_id = _slug(str(record.get("variant_id") or trace_context.get("parent_variant_id") or "default"))
    segment_id = trace_context.get("segment_id", "")
    output_path = _path_value(record, path_key)
    prompt = _map_prompt(record, constraints, map_kind)
    prompt_checksum = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    plan = {
        "id": _prompt_plan_id(variant_id, segment_id, map_kind, output_path),
        "role": "texture_generator",
        "prompt_source": PROMPT_SOURCE,
        "variant_id": variant_id,
        "segment_id": segment_id,
        "surface_scope": "segment" if segment_id else "variant",
        "material_name": _clean_text(str(record.get("material_name") or ""), constraints),
        "texture_intent": _clean_text(str(record.get("texture_intent") or ""), constraints),
        "material_prompt": _base_prompt(record, constraints),
        "map_kind": map_kind,
        "path_key": path_key,
        "output_path": output_path,
        "provider": provider,
        "model": model or "provider_default",
        "api_size": api_size,
        "texture_size": size,
        "quality": quality,
        "output_format": output_format,
        "prompt_text": prompt,
        "prompt_checksum": prompt_checksum,
        "negative_prompt": _clean_text(
            str(
                record.get("negative_prompt")
                or "text, logos, labels, watermarks, baked lighting, cast shadows, object silhouette, product shape, perspective"
            ),
            constraints,
        ),
        "pbr_contract": "flat front-facing tileable PBR material texture map; no object silhouette, labels, baked lighting or camera perspective",
    }
    plan.update({key: value for key, value in trace_context.items() if key not in plan})
    return plan


def _metallic_policy_plan_record(
    record: dict[str, Any],
    *,
    size: int,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trace_context = _trace_context(record, context)
    variant_id = _slug(str(record.get("variant_id") or trace_context.get("parent_variant_id") or "default"))
    segment_id = trace_context.get("segment_id", "")
    output_path = _path_value(record, "metallic_path")
    plan = {
        "id": _prompt_plan_id(variant_id, segment_id, "metallic", output_path),
        "role": "material_texture_policy",
        "prompt_source": "material_policy",
        "variant_id": variant_id,
        "segment_id": segment_id,
        "surface_scope": "segment" if segment_id else "variant",
        "material_name": str(record.get("material_name") or ""),
        "texture_intent": str(record.get("texture_intent") or ""),
        "map_kind": "metallic",
        "path_key": "metallic_path",
        "output_path": output_path,
        "texture_size": size,
        "policy": "constant_metallic_from_material_terms",
        "value": _metallic_policy_value(record),
        "pbr_contract": "scalar metallic PBR map from material terms; generated without an image provider",
    }
    plan.update({key: value for key, value in trace_context.items() if key not in plan})
    return plan


def build_live_texture_request_plan(
    asset_dir: Path,
    texture_variants: list[dict[str, Any]],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    provider = _provider_name(constraints)
    model = _provider_model(provider, constraints)
    size = _texture_size(constraints)
    api_size = _image_api_size(size)
    quality = str(constraints.get("texture_quality") or os.getenv("AFB_TEXTURE_QUALITY") or "medium")
    output_format = str(constraints.get("texture_output_format") or "png")
    prompt_plan: list[dict[str, Any]] = []

    def append_record_plan(record: dict[str, Any], context: dict[str, Any] | None = None) -> None:
        for map_kind, path_key in GENERATED_MAP_PATH_KEYS.items():
            if _path_value(record, path_key):
                prompt_plan.append(
                    _texture_prompt_plan_record(
                        record,
                        constraints,
                        provider=provider,
                        model=model,
                        size=size,
                        api_size=api_size,
                        quality=quality,
                        output_format=output_format,
                        map_kind=map_kind,
                        path_key=path_key,
                        context=context,
                    )
                )
        for map_kind, path_key in POLICY_MAP_PATH_KEYS.items():
            if _path_value(record, path_key):
                prompt_plan.append(_metallic_policy_plan_record(record, size=size, context=context))

    for record in texture_variants:
        append_record_plan(record)
        parent_variant_id = _slug(str(record.get("variant_id") or "default"))
        for segment_record in record.get("segment_materials", []):
            if isinstance(segment_record, dict):
                append_record_plan(segment_record, {"parent_variant_id": parent_variant_id})

    return {
        "status": "planned" if prompt_plan else "not_requested",
        "asset_dir": asset_dir.as_posix(),
        "backend": "simple_image_gen",
        "provider": provider,
        "model": model or "provider_default",
        "texture_size": size,
        "api_size": api_size,
        "quality": quality,
        "output_format": output_format,
        "texture_prompt_plan": prompt_plan,
    }


def _write_image_bytes(path: Path, image_bytes: bytes, map_kind: str, size: int) -> Path:
    with Image.open(BytesIO(image_bytes)) as image:
        output = image.convert("RGB")
    output = output.resize((size, size), Image.Resampling.LANCZOS)
    if map_kind == "roughness":
        output = ImageOps.grayscale(output).convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    output.save(path)
    return path


def _metallic_policy_value(record: dict[str, Any]) -> int:
    text = " ".join(
        str(record.get(key, ""))
        for key in ("material_name", "texture_intent", "prompt", "variant_id")
    ).replace("_", " ").lower()
    if any(term in text for term in NON_METALLIC_SURFACE_TERMS):
        return 0
    if any(term in text for term in BARE_METAL_TERMS):
        return 255
    return 0


def _write_policy_scalar(path: Path, value: int, size: int) -> Path:
    clamped = max(0, min(255, int(value)))
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (size, size), (clamped, clamped, clamped)).save(path)
    return path


def _trace_context(record: dict[str, Any], context: dict[str, Any] | None) -> dict[str, str]:
    trace: dict[str, str] = {}
    for key, value in (context or {}).items():
        if value not in (None, ""):
            trace[key] = str(value)
    segment_id = str(record.get("segment_id", "")).strip()
    if segment_id:
        trace["segment_id"] = segment_id
    return trace


def _generate_texture_record(
    asset_dir: Path,
    record: dict[str, Any],
    constraints: dict[str, Any],
    *,
    provider: str,
    model: str | None,
    size: int,
    api_size: str,
    quality: str,
    output_format: str,
    generator: ImageGenerator,
    provider_trace: list[dict[str, Any]],
    map_policy_trace: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[Path]]:
    updated = dict(record)
    trace_context = _trace_context(updated, context)
    variant_id = _slug(str(updated.get("variant_id") or trace_context.get("parent_variant_id") or "default"))
    material_prompt = _base_prompt(updated, constraints)
    generated_map_kinds: list[str] = []
    policy_map_kinds: list[str] = []
    files: list[Path] = []

    for map_kind, path_key in GENERATED_MAP_PATH_KEYS.items():
        rel_path = str(updated.get(path_key) or "")
        if not rel_path:
            continue
        prompt = _map_prompt(updated, constraints, map_kind)
        prompt_checksum = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        generated = generator(
            provider,
            prompt,
            model=model,
            size=api_size,
            output_format=output_format,
            quality=quality,
        )
        target = asset_dir / rel_path
        _write_image_bytes(target, generated.image_bytes, map_kind, size)
        files.append(target)
        trace_record = {
            "provider": generated.provider,
            "model": generated.model,
            "role": "texture_generator",
            "map_kind": map_kind,
            "variant_id": variant_id,
            "prompt_plan_id": _prompt_plan_id(variant_id, trace_context.get("segment_id", ""), map_kind, rel_path),
            "prompt_checksum": prompt_checksum,
            "request_payload_redacted": generated.request_payload_redacted,
            "response_usage": generated.response_usage,
            "base_url_host": generated.base_url_host,
            "output_path": target.relative_to(asset_dir).as_posix(),
            "output_sha256": sha256_file(target),
        }
        trace_record.update(trace_context)
        provider_trace.append(trace_record)
        generated_map_kinds.append(map_kind)

    for map_kind, path_key in POLICY_MAP_PATH_KEYS.items():
        rel_path = str(updated.get(path_key) or "")
        if not rel_path:
            continue
        target = asset_dir / rel_path
        value = _metallic_policy_value(updated)
        _write_policy_scalar(target, value, size)
        files.append(target)
        trace_record = {
            "role": "material_texture_policy",
            "map_kind": map_kind,
            "variant_id": variant_id,
            "prompt_plan_id": _prompt_plan_id(variant_id, trace_context.get("segment_id", ""), map_kind, rel_path),
            "policy": "constant_metallic_from_material_terms",
            "value": value,
            "output_path": target.relative_to(asset_dir).as_posix(),
            "output_sha256": sha256_file(target),
        }
        trace_record.update(trace_context)
        map_policy_trace.append(trace_record)
        policy_map_kinds.append(map_kind)

    updated["status"] = "generated"
    updated["generation_method"] = f"{provider}_images_api_plus_material_policy"
    updated["is_generated_texture"] = True
    updated["texture_backend"] = "simple_image_gen"
    updated["texture_provider"] = provider
    updated["texture_model"] = model or "provider_default"
    updated["resolution"] = f"{size}x{size} PBR map set"
    updated["material_prompt"] = material_prompt
    updated["generated_map_kinds"] = generated_map_kinds
    updated["policy_map_kinds"] = policy_map_kinds
    return updated, files


def generate_live_texture_sets(
    asset_dir: Path,
    texture_variants: list[dict[str, Any]],
    constraints: dict[str, Any],
    *,
    image_generator: ImageGenerator | None = None,
) -> dict[str, Any]:
    request_plan = build_live_texture_request_plan(asset_dir, texture_variants, constraints)
    if not texture_variants:
        return {
            "status": "not_requested",
            "texture_variants": [],
            "files": [],
            "provider_trace": [],
            "map_policy_trace": [],
            "texture_prompt_plan": [],
            "blocked_reasons": [],
            "backend": "not_requested",
        }

    provider = _provider_name(constraints)
    model = _provider_model(provider, constraints)
    size = _texture_size(constraints)
    api_size = _image_api_size(size)
    quality = str(constraints.get("texture_quality") or os.getenv("AFB_TEXTURE_QUALITY") or "medium")
    output_format = str(constraints.get("texture_output_format") or "png")
    generator = image_generator or generate_image
    updated_records: list[dict[str, Any]] = []
    files: list[Path] = []
    provider_trace: list[dict[str, Any]] = []
    map_policy_trace: list[dict[str, Any]] = []

    for record in texture_variants:
        updated, record_files = _generate_texture_record(
            asset_dir,
            record,
            constraints,
            provider=provider,
            model=model,
            size=size,
            api_size=api_size,
            quality=quality,
            output_format=output_format,
            generator=generator,
            provider_trace=provider_trace,
            map_policy_trace=map_policy_trace,
        )
        files.extend(record_files)
        segment_materials: list[dict[str, Any]] = []
        parent_variant_id = _slug(str(updated.get("variant_id") or "default"))
        for segment_record in record.get("segment_materials", []):
            if not isinstance(segment_record, dict):
                continue
            segment_updated, segment_files = _generate_texture_record(
                asset_dir,
                segment_record,
                constraints,
                provider=provider,
                model=model,
                size=size,
                api_size=api_size,
                quality=quality,
                output_format=output_format,
                generator=generator,
                provider_trace=provider_trace,
                map_policy_trace=map_policy_trace,
                context={"parent_variant_id": parent_variant_id},
            )
            segment_materials.append(segment_updated)
            files.extend(segment_files)
        if segment_materials:
            updated["segment_materials"] = segment_materials
        updated_records.append(updated)

    return {
        "status": "generated",
        "texture_variants": updated_records,
        "files": files,
        "provider_trace": provider_trace,
        "map_policy_trace": map_policy_trace,
        "texture_prompt_plan": request_plan["texture_prompt_plan"],
        "blocked_reasons": [],
        "backend": "simple_image_gen",
    }
