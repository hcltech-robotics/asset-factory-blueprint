from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render an Asset Factory visual-set GIF in Isaac Sim.")
    parser.add_argument("--usd", required=True, help="USD stage with mesh-backed semantic parts.")
    parser.add_argument("--texture-manifest", required=True, help="texturing-manifest.json with generated PBR maps.")
    parser.add_argument("--project-root", default="", help="Project root used to resolve manifest-relative texture paths.")
    parser.add_argument("--parts-manifest", default="", help="Optional PartCrafter parts-manifest.json.")
    parser.add_argument("--part-material-policy", default="", help="Optional JSON mapping part prims to material segments.")
    parser.add_argument("--mesh-quality-report", default="", help="Optional geometry-quality-report.json generated from the reconstructed parts.")
    parser.add_argument("--part-root", default="", help="Optional USD prim path whose children are part meshes.")
    parser.add_argument("--part-prim-path", action="append", default=[], help="Explicit part mesh prim path. Repeat for each part.")
    parser.add_argument("--frames-dir", default="", help="Output frame directory.")
    parser.add_argument("--gif", default="", help="Output animated GIF.")
    parser.add_argument("--report", required=True, help="JSON report path.")
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--settle-frames", type=int, default=90)
    parser.add_argument("--per-frame-updates", type=int, default=8)
    parser.add_argument("--frame-duration-ms", type=int, default=220)
    parser.add_argument("--turntable-axis", choices=["X", "Y", "Z"], default="Y")
    parser.add_argument("--generate-cylindrical-uvs", action="store_true")
    parser.add_argument("--frame-limit", type=int, default=0)
    parser.add_argument("--start-frame", type=int, default=0)
    parser.add_argument("--frame-index", action="append", type=int, default=[], help="Render an exact sequence frame index. Repeat for more frames.")
    parser.add_argument("--film-iso", type=float, default=250.0)
    parser.add_argument("--dome-intensity", type=float, default=180.0)
    parser.add_argument("--key-intensity", type=float, default=65000.0)
    parser.add_argument("--studio-preset", choices=["basic", "product"], default="basic")
    parser.add_argument("--camera-distance-multiplier", type=float, default=3.2)
    parser.add_argument("--camera-focal-length", type=float, default=32.0)
    parser.add_argument("--render-mode", choices=["rt2", "path_tracing"], default="rt2")
    parser.add_argument("--pathtrace-spp", type=int, default=256)
    parser.add_argument("--pathtrace-max-bounces", type=int, default=6)
    parser.add_argument("--warmup-discard-captures", type=int, default=1)
    parser.add_argument("--material-change-discard-captures", type=int, default=1)
    parser.add_argument("--discard-first-texture-frame-on-material-change", action="store_true")
    parser.add_argument("--frame-retry-count", type=int, default=2)
    parser.add_argument("--frame-retry-updates", type=int, default=50)
    parser.add_argument("--frame-retry-min-mean", type=float, default=10.0)
    parser.add_argument("--no-studio-floor", action="store_false", dest="studio_floor")
    parser.add_argument("--validate-only", action="store_true")
    parser.set_defaults(studio_floor=True)
    return parser


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_manifest_path(value: str, project_root: Path, manifest_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if project_root:
        candidate = project_root / value
        if candidate.exists():
            return candidate
    return manifest_dir / value


def real_texture_outputs(texture_manifest: dict[str, Any], project_root: Path, manifest_dir: Path) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for item in texture_manifest.get("texture_outputs", []):
        if not item.get("is_generated_texture"):
            continue
        maps: dict[str, str] = {}
        missing: list[str] = []
        for key in ("base_color_path", "normal_path", "roughness_path", "metallic_path"):
            value = str(item.get(key, ""))
            maps[key] = value
            if not value or not resolve_manifest_path(value, project_root, manifest_dir).exists():
                missing.append(key)
        if missing:
            item = {**item, "missing_maps": missing}
        segment_materials = []
        for segment in item.get("segment_materials", []):
            segment_maps: dict[str, str] = {}
            segment_missing: list[str] = []
            for key in ("base_color_path", "normal_path", "roughness_path", "metallic_path"):
                value = str(segment.get(key, ""))
                segment_maps[key] = value
                if not value or not resolve_manifest_path(value, project_root, manifest_dir).exists():
                    segment_missing.append(key)
            segment_record = {**segment, "maps": segment_maps}
            if segment_missing:
                segment_record["missing_maps"] = segment_missing
            segment_materials.append(segment_record)
        outputs.append({**item, "maps": maps, "segment_materials": segment_materials})
    return outputs


def load_part_records(parts_manifest: Path | None) -> list[dict[str, Any]]:
    if not parts_manifest:
        return []
    payload = load_json(parts_manifest)
    records = []
    for item in payload.get("parts", []):
        if item.get("kind") != "part_mesh":
            continue
        records.append(item)
    return records


def validate_inputs(args: argparse.Namespace) -> dict[str, Any]:
    usd_path = Path(args.usd).resolve()
    texture_manifest_path = Path(args.texture_manifest).resolve()
    project_root = Path(args.project_root).resolve() if args.project_root else Path()
    parts_manifest_path = Path(args.parts_manifest).resolve() if args.parts_manifest else None
    part_material_policy_path = Path(getattr(args, "part_material_policy", "")).resolve() if getattr(args, "part_material_policy", "") else None
    mesh_quality_report_path = Path(getattr(args, "mesh_quality_report", "")).resolve() if getattr(args, "mesh_quality_report", "") else None
    report: dict[str, Any] = {
        "status": "blocked",
        "usd": str(usd_path),
        "texture_manifest": str(texture_manifest_path),
        "parts_manifest": str(parts_manifest_path) if parts_manifest_path else "",
        "part_material_policy": str(part_material_policy_path) if part_material_policy_path else "",
        "mesh_quality_report": str(mesh_quality_report_path) if mesh_quality_report_path else "",
        "preconditions": [],
        "warnings": [],
        "blocked_reasons": [],
    }
    if not usd_path.exists():
        report["blocked_reasons"].append("USD stage does not exist")
    if not texture_manifest_path.exists():
        report["blocked_reasons"].append("texture manifest does not exist")
        return report
    if parts_manifest_path and not parts_manifest_path.exists():
        report["blocked_reasons"].append("parts manifest does not exist")
    if part_material_policy_path and not part_material_policy_path.exists():
        report["blocked_reasons"].append("part material policy does not exist")
    if mesh_quality_report_path and not mesh_quality_report_path.exists():
        report["blocked_reasons"].append("mesh quality report does not exist")

    texture_manifest = load_json(texture_manifest_path)
    texture_status = str(texture_manifest.get("texture_generation_status", ""))
    outputs = real_texture_outputs(texture_manifest, project_root, texture_manifest_path.parent)
    report["texture_generation_status"] = texture_status
    report["texture_output_count"] = len(outputs)
    report["texture_variants"] = [
        {
            "variant_id": item.get("variant_id", ""),
            "generation_method": item.get("generation_method", ""),
            "generated_map_kinds": item.get("generated_map_kinds", []),
            "policy_map_kinds": item.get("policy_map_kinds", []),
            "missing_maps": item.get("missing_maps", []),
            "maps": item.get("maps", {}),
            "segment_material_count": len(item.get("segment_materials", [])),
            "segment_materials": [
                {
                    "segment_id": segment.get("segment_id", ""),
                    "material_name": segment.get("material_name", ""),
                    "generation_method": segment.get("generation_method", ""),
                    "generated_map_kinds": segment.get("generated_map_kinds", []),
                    "policy_map_kinds": segment.get("policy_map_kinds", []),
                    "missing_maps": segment.get("missing_maps", []),
                    "maps": segment.get("maps", {}),
                }
                for segment in item.get("segment_materials", [])
            ],
        }
        for item in outputs
    ]
    if texture_status not in {"generated", "validated"}:
        report["blocked_reasons"].append("texture manifest does not contain generated PBR maps")
    if not outputs:
        report["blocked_reasons"].append("no generated texture outputs were found")
    for item in outputs:
        if item.get("missing_maps"):
            report["blocked_reasons"].append(f"texture output {item.get('variant_id', '')} is missing map files")
        for segment in item.get("segment_materials", []):
            if segment.get("missing_maps"):
                segment_id = segment.get("segment_id", "")
                report["blocked_reasons"].append(
                    f"texture output {item.get('variant_id', '')} segment {segment_id} is missing map files"
                )

    parts = load_part_records(parts_manifest_path) if parts_manifest_path and parts_manifest_path.exists() else []
    part_policy = load_part_material_policy(part_material_policy_path) if part_material_policy_path and part_material_policy_path.exists() else {}
    if mesh_quality_report_path and mesh_quality_report_path.exists():
        mesh_quality = load_json(mesh_quality_report_path)
        mesh_summary = mesh_quality.get("summary", {})
        quality_status = str(mesh_summary.get("status", ""))
        report["geometry_quality"] = {
            "status": quality_status,
            "total_faces": mesh_summary.get("total_faces", 0),
            "total_vertices": mesh_summary.get("total_vertices", 0),
            "quality_flags": mesh_summary.get("quality_flags", []),
            "visible_part_count": mesh_summary.get("visible_part_count", 0),
            "dropped_part_count": mesh_summary.get("dropped_part_count", 0),
        }
        report["geometry_quality_status"] = quality_status
        if quality_status == "pass":
            report["preconditions"].append("mesh quality report is passing")
        elif quality_status:
            report["warnings"].append(f"mesh quality report status is {quality_status}")
    explicit_parts = [str(item) for item in args.part_prim_path if str(item).strip()]
    report["part_asset_count"] = len(parts)
    report["part_material_policy_assignment_count"] = len(part_policy.get("assignments", []))
    report["explicit_part_prim_count"] = len(explicit_parts)
    if not explicit_parts and not args.part_root and len(parts) < 2:
        report["blocked_reasons"].append("visual sets require at least two mesh-backed semantic parts")
    if len(outputs) > 0:
        report["preconditions"].append("generated PBR texture maps are present")
    if explicit_parts or args.part_root:
        report["preconditions"].append("mesh-backed part prim selector is available for the exploded phase")
    elif len(parts) >= 2:
        report["preconditions"].append("part mesh files are available; render mode will verify stage prims")
    report["status"] = "ready" if not report["blocked_reasons"] else "blocked"
    return report


def load_part_material_policy(path: Path | None) -> dict[str, Any]:
    if not path:
        return {}
    payload = load_json(path)
    assignments = []
    by_path: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for item in payload.get("part_roles", []):
        segment_id = str(item.get("segment_id", "")).strip()
        if not segment_id:
            continue
        assignment = {**item, "segment_id": segment_id}
        assignments.append(assignment)
        prim_path = str(item.get("prim_path", "")).strip()
        part_id = str(item.get("part_id", "")).strip()
        if prim_path:
            by_path[prim_path] = assignment
        if part_id:
            by_name[part_id] = assignment
    return {
        "assignments": assignments,
        "by_path": by_path,
        "by_name": by_name,
        "semantic_colours": payload.get("semantic_colours", {}),
    }


def sequence(texture_variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    angle = 0.0

    def add(count: int, phase: str, style: str, explode_a: float, explode_b: float, variant: dict[str, Any] | None = None) -> None:
        nonlocal angle
        denom = max(1, count - 1)
        for index in range(count):
            t = index / denom
            eased = t * t * (3.0 - 2.0 * t)
            frames.append(
                {
                    "phase": phase,
                    "style": style,
                    "explosion": explode_a + (explode_b - explode_a) * eased,
                    "angle_degrees": angle,
                    "variant": variant or {},
                }
            )
            angle += 2.5

    add(20, "whole_mono_mesh", "mono", 0.0, 0.0)
    add(20, "semantic_colour", "semantic", 0.0, 0.0)
    add(28, "explode_semantic_parts", "semantic", 0.0, 1.0)
    add(28, "reanneal_semantic_parts", "semantic", 1.0, 0.0)
    for variant in texture_variants[:4]:
        add(24, f"texture_{variant.get('variant_id', 'variant')}", "texture", 0.0, 0.0, variant)
    return frames


def select_frame_sequence(
    texture_variants: list[dict[str, Any]],
    *,
    start_frame: int = 0,
    frame_limit: int = 0,
    frame_indices: list[int] | None = None,
) -> list[dict[str, Any]]:
    full_sequence = [
        {"sequence_index": index, **frame}
        for index, frame in enumerate(sequence(texture_variants))
    ]
    if frame_indices:
        selected: list[dict[str, Any]] = []
        seen: set[int] = set()
        for raw_index in frame_indices:
            index = int(raw_index)
            if index in seen or index < 0 or index >= len(full_sequence):
                continue
            selected.append(full_sequence[index])
            seen.add(index)
        return selected
    start = max(0, int(start_frame))
    selected = full_sequence[start:]
    if frame_limit:
        selected = selected[: max(1, int(frame_limit))]
    return selected


def render_mode_name(render_mode: str) -> str:
    return "PathTracing" if render_mode == "path_tracing" else "RayTracedLighting"


def configure_render_settings(settings: Any, args: argparse.Namespace) -> dict[str, Any]:
    mode = render_mode_name(str(args.render_mode))
    settings.set("/rtx/rendermode", mode)
    settings.set("/rtx/post/tonemap/op", 4)
    settings.set("/rtx/post/tonemap/filmIso", float(args.film_iso))
    settings.set("/rtx/post/tonemap/enabled", True)
    settings.set("/rtx/post/tonemap/whitepoint", 6500.0)
    settings.set("/rtx/post/aa/op", 3)
    settings.set("/persistent/app/viewport/displayOptions", 0)
    settings.set("/app/viewport/grid/enabled", False)
    authored = {
        "render_mode": mode,
        "aces_tonemap": True,
        "film_iso": float(args.film_iso),
        "anti_aliasing_op": 3,
        "frame_retry_count": max(0, int(args.frame_retry_count)),
        "frame_retry_updates": max(1, int(args.frame_retry_updates)),
        "frame_retry_min_mean": float(args.frame_retry_min_mean),
        "warmup_discard_captures": max(0, int(args.warmup_discard_captures)),
        "material_change_discard_captures": max(0, int(args.material_change_discard_captures)),
        "discard_first_texture_frame_on_material_change": bool(args.discard_first_texture_frame_on_material_change),
    }
    if mode == "PathTracing":
        spp = max(1, int(args.pathtrace_spp))
        max_bounces = max(1, int(args.pathtrace_max_bounces))
        settings.set("/rtx/pt/samplesPerPixel", spp)
        settings.set("/rtx/pt/limits/maxBounces", max_bounces)
        settings.set("/rtx/pt/denoising/enabled", True)
        authored.update(
            {
                "pathtrace_samples_per_pixel": spp,
                "pathtrace_max_bounces": max_bounces,
                "pathtrace_denoising": True,
            }
        )
    return authored


def frame_quality_summary(frame_stats: list[dict[str, Any]]) -> dict[str, Any]:
    if not frame_stats:
        return {"status": "blocked", "reasons": ["no frames captured"]}
    means = [float(item["mean_rgb"]) for item in frame_stats]
    max_values = [int(item["max_rgb"]) for item in frame_stats]
    reasons = []
    if max(max_values) == 0:
        reasons.append("no lighting reached the camera")
    if min(means) < 10.0:
        reasons.append("at least one frame is underexposed")
    if max(means) > 220.0:
        reasons.append("at least one frame is overexposed")
    return {
        "status": "pass" if not reasons else "warn",
        "reasons": reasons,
        "mean_rgb_min": min(means),
        "mean_rgb_max": max(means),
        "max_rgb": max(max_values),
    }


def needs_frame_retry(mean_rgb: float, max_rgb: int, attempt: int, max_retries: int, min_mean: float) -> bool:
    if attempt >= max(0, int(max_retries)):
        return False
    return int(max_rgb) == 0 or float(mean_rgb) < float(min_mean)


def should_discard_transient_texture_frame(
    *,
    enabled: bool,
    material_changed: bool,
    style: str,
    frame_stats: list[dict[str, Any]],
) -> bool:
    return bool(enabled) and bool(material_changed) and style == "texture" and bool(frame_stats)


def render_visual_set(args: argparse.Namespace, validation: dict[str, Any]) -> dict[str, Any]:
    from isaacsim import SimulationApp

    import numpy as np
    from PIL import Image

    app = SimulationApp({"headless": True, "width": args.width, "height": args.height, "renderer": render_mode_name(str(args.render_mode))})
    report = {**validation, "status": "blocked", "frames": [], "errors": []}
    try:
        import carb
        import omni.replicator.core as rep
        import omni.usd
        from pxr import Gf, Sdf, UsdGeom, UsdLux, UsdShade, Vt

        usd_path = Path(args.usd).resolve()
        context = omni.usd.get_context()
        context.open_stage(str(usd_path))
        for _ in range(30):
            app.update()
        stage = context.get_stage()
        if stage is None:
            report["errors"].append("stage did not open")
            return report

        settings = carb.settings.get_settings()
        render_settings = configure_render_settings(settings, args)

        default_prim = stage.GetDefaultPrim()
        if not default_prim:
            report["errors"].append("stage has no default prim")
            return report

        part_prims = collect_part_prims(stage, default_prim, args)
        if len(part_prims) < 2:
            report["errors"].append("stage does not contain enough part mesh prims")
            return report
        scene_bounds = compute_scene_bounds(part_prims, UsdGeom)
        part_policy = load_part_material_policy(Path(args.part_material_policy).resolve()) if args.part_material_policy else {}
        part_roles = collect_part_roles(part_prims, part_policy)

        if args.generate_cylindrical_uvs:
            report["uv_authoring"] = author_cylindrical_uvs(part_prims, UsdGeom, Gf, Vt)

        axis_op = add_turntable_op(UsdGeom.Xformable(default_prim), args.turntable_axis)
        translate_ops = [UsdGeom.Xformable(prim).AddTranslateOp(opSuffix="visualSetExplosion") for prim in part_prims]
        directions = explosion_directions(len(part_prims), Gf)
        grey_colour = (0.54, 0.55, 0.53) if str(args.studio_preset) == "product" else (0.68, 0.70, 0.68)
        grey = create_preview_material(stage, "/World/VisualSetMaterials/grey", grey_colour, 0.56, 0.02)
        semantic_materials = [
            create_preview_material(stage, f"/World/VisualSetMaterials/semantic_{index:02d}", colour, 0.48, 0.04)
            for index, colour in enumerate(segment_colours(len(part_prims)))
        ]
        semantic_role_materials = create_role_semantic_materials(stage, part_roles)
        texture_materials, texture_material_report = create_texture_materials(stage, validation.get("texture_variants", []), args, Sdf, UsdShade)

        dome = UsdLux.DomeLight.Define(stage, "/World/VisualSetDome")
        dome.CreateIntensityAttr(float(args.dome_intensity))
        sun = UsdLux.DistantLight.Define(stage, "/World/VisualSetSun")
        sun.CreateIntensityAttr(900.0)
        UsdGeom.Xformable(sun.GetPrim()).AddRotateXYZOp().Set(Gf.Vec3f(-45, 20, 0))
        if str(args.studio_preset) == "product":
            dome.CreateColorAttr(Gf.Vec3f(0.88, 0.90, 0.95))
        studio_lights = add_studio_lights(stage, UsdGeom, UsdLux, Gf, scene_bounds, float(args.key_intensity), str(args.studio_preset))
        floor_report: dict[str, Any] | None = None
        if args.studio_floor:
            floor_report = create_studio_floor(stage, UsdGeom, scene_bounds, str(args.studio_preset))
        backdrop_report = create_studio_backdrop(stage, UsdGeom, scene_bounds, str(args.studio_preset)) if str(args.studio_preset) == "product" else None

        camera = UsdGeom.Camera.Define(stage, "/World/VisualSetCamera")
        camera.CreateFocalLengthAttr().Set(float(args.camera_focal_length))
        camera_report = set_camera_transform(camera, Gf, UsdGeom, scene_bounds, float(args.camera_distance_multiplier))
        camera_report["focal_length"] = float(args.camera_focal_length)
        render_product = rep.create.render_product("/World/VisualSetCamera", (args.width, args.height))
        rgb_annot = rep.AnnotatorRegistry.get_annotator("rgb")
        rgb_annot.attach([render_product])

        frames_dir = Path(args.frames_dir).resolve()
        gif_path = Path(args.gif).resolve()
        frames_dir.mkdir(parents=True, exist_ok=True)
        gif_path.parent.mkdir(parents=True, exist_ok=True)
        frame_paths: list[Path] = []
        last_style = ""
        last_variant = ""
        frame_sequence = select_frame_sequence(
            validation.get("texture_variants", []),
            start_frame=int(args.start_frame),
            frame_limit=int(args.frame_limit),
            frame_indices=[int(item) for item in getattr(args, "frame_index", [])],
        )
        if not frame_sequence:
            report["errors"].append("no frames selected for rendering")
            return report
        warmup_discard_stats: list[dict[str, Any]] = []
        material_change_discard_stats: list[dict[str, Any]] = []
        warmup_count = max(0, int(args.warmup_discard_captures))
        if warmup_count:
            warmup_frame = frame_sequence[0]
            warmup_style = str(warmup_frame["style"])
            warmup_variant = str(warmup_frame.get("variant", {}).get("variant_id", ""))
            binding_count = bind_phase_materials(
                part_prims,
                part_roles,
                warmup_style,
                warmup_variant,
                grey,
                semantic_materials,
                semantic_role_materials,
                texture_materials,
                UsdShade,
            )
            axis_op.Set(math.fmod(float(warmup_frame["angle_degrees"]), 360.0))
            warmup_explosion = float(warmup_frame["explosion"])
            for op, direction in zip(translate_ops, directions):
                op.Set(direction * (0.55 * warmup_explosion))
            report.setdefault("material_binding_events", []).append(
                {
                    "style": warmup_style,
                    "variant_id": warmup_variant,
                    "binding_target_count": binding_count,
                    "discarded_warmup": True,
                }
            )
            for discard_index in range(warmup_count):
                for _ in range(max(1, int(args.frame_retry_updates))):
                    app.update()
                rep.orchestrator.step()
                data = rgb_annot.get_data()
                warmup_rgb = np.asarray(data[:, :, :3], dtype=np.uint8)
                warmup_discard_stats.append(
                    {
                        "discard_index": discard_index,
                        "sequence_frame": int(warmup_frame.get("sequence_index", 0)),
                        "style": warmup_style,
                        "variant_id": warmup_variant,
                        "mean_rgb": float(warmup_rgb.mean()),
                        "max_rgb": int(warmup_rgb.max()),
                    }
                )
        frame_stats: list[dict[str, Any]] = []
        transient_texture_frame_stats: list[dict[str, Any]] = []
        for index, frame in enumerate(frame_sequence):
            style = str(frame["style"])
            variant_id = str(frame.get("variant", {}).get("variant_id", ""))
            material_changed = False
            if style != last_style or variant_id != last_variant:
                binding_count = bind_phase_materials(
                    part_prims,
                    part_roles,
                    style,
                    variant_id,
                    grey,
                    semantic_materials,
                    semantic_role_materials,
                    texture_materials,
                    UsdShade,
                )
                report.setdefault("material_binding_events", []).append(
                    {
                        "style": style,
                        "variant_id": variant_id,
                        "binding_target_count": binding_count,
                    }
                )
                last_style = style
                last_variant = variant_id
                material_changed = True
            axis_op.Set(math.fmod(float(frame["angle_degrees"]), 360.0))
            explosion = float(frame["explosion"])
            for op, direction in zip(translate_ops, directions):
                op.Set(direction * (0.55 * explosion))
            updates = args.settle_frames if index == 0 else args.per_frame_updates
            for _ in range(updates):
                app.update()
            if material_changed:
                for discard_index in range(max(0, int(args.material_change_discard_captures))):
                    rep.orchestrator.step()
                    data = rgb_annot.get_data()
                    discard_rgb = np.asarray(data[:, :, :3], dtype=np.uint8)
                    material_change_discard_stats.append(
                        {
                            "frame": index,
                            "discard_index": discard_index,
                            "sequence_frame": int(frame.get("sequence_index", index)),
                            "style": style,
                            "variant_id": variant_id,
                            "mean_rgb": float(discard_rgb.mean()),
                            "max_rgb": int(discard_rgb.max()),
                        }
                    )
                    for _ in range(max(1, int(args.frame_retry_updates))):
                        app.update()
            retry_count = 0
            for attempt in range(max(1, int(args.frame_retry_count) + 1)):
                rep.orchestrator.step()
                data = rgb_annot.get_data()
                rgb = np.asarray(data[:, :, :3], dtype=np.uint8)
                mean_rgb = float(rgb.mean())
                max_rgb = int(rgb.max())
                if not needs_frame_retry(
                    mean_rgb,
                    max_rgb,
                    attempt,
                    int(args.frame_retry_count),
                    float(args.frame_retry_min_mean),
                ):
                    break
                retry_count += 1
                for _ in range(max(1, int(args.frame_retry_updates))):
                    app.update()
            output_frame_index = len(frame_paths)
            frame_record = {
                "frame": output_frame_index,
                "source_frame": index,
                "sequence_frame": int(frame.get("sequence_index", index)),
                "phase": frame["phase"],
                "style": style,
                "variant_id": variant_id,
                "mean_rgb": float(rgb.mean()),
                "max_rgb": int(rgb.max()),
                "min_rgb": int(rgb.min()),
                "retry_count": retry_count,
            }
            if should_discard_transient_texture_frame(
                enabled=bool(args.discard_first_texture_frame_on_material_change),
                material_changed=material_changed,
                style=style,
                frame_stats=frame_stats,
            ):
                transient_texture_frame_stats.append(
                    {
                        **frame_record,
                        "discarded_transient_texture_frame": True,
                        "reason": "first texture frame after material change was suppressed",
                    }
                )
                continue

            frame_path = frames_dir / f"frame_{output_frame_index:04d}.png"
            Image.fromarray(rgb).save(frame_path)
            frame_paths.append(frame_path)
            frame_stats.append(frame_record)

        if not frame_paths:
            report["errors"].append("all selected frames were discarded before GIF assembly")
            return report
        gif_frames = [Image.open(path).convert("P", palette=Image.Palette.ADAPTIVE) for path in frame_paths]
        gif_frames[0].save(
            gif_path,
            save_all=True,
            append_images=gif_frames[1:],
            duration=args.frame_duration_ms,
            loop=0,
            optimize=False,
        )
        for frame in gif_frames:
            frame.close()
        quality_summary = frame_quality_summary(frame_stats)
        report.update(
            {
                "status": "pass",
                "gif": str(gif_path),
                "frames_dir": str(frames_dir),
                "frame_count": len(frame_paths),
                "frames": [str(path) for path in frame_paths],
                "frame_stats": frame_stats,
                "mean_rgb_min": quality_summary["mean_rgb_min"],
                "mean_rgb_max": quality_summary["mean_rgb_max"],
                "max_rgb": quality_summary["max_rgb"],
                "frame_quality": quality_summary,
                "warmup_discard_stats": warmup_discard_stats,
                "material_change_discard_stats": material_change_discard_stats,
                "transient_texture_frame_stats": transient_texture_frame_stats,
                "part_material_assignments": part_roles,
                "texture_materials": texture_material_report,
                "texture_binding_mode": "segment_material_policy"
                if any(":" in key for key in texture_materials)
                else "whole_variant_material",
                "turntable_axis": args.turntable_axis,
                "render_settings": render_settings,
                "film_iso": float(args.film_iso),
                "dome_intensity": float(args.dome_intensity),
                "key_intensity": float(args.key_intensity),
                "studio_preset": str(args.studio_preset),
                "lighting": {
                    "dome": {
                        "path": "/World/VisualSetDome",
                        "type": "DomeLight",
                        "intensity": float(args.dome_intensity),
                        "colour": [0.88, 0.90, 0.95] if str(args.studio_preset) == "product" else [],
                    },
                    "sun": {
                        "path": "/World/VisualSetSun",
                        "type": "DistantLight",
                        "intensity": 900.0,
                        "rotate_xyz": [-45.0, 20.0, 0.0],
                    },
                    "studio_lights": studio_lights,
                },
                "camera": camera_report,
                "stage_setup": {
                    "default_prim": str(default_prim.GetPath()),
                    "part_count": len(part_prims),
                    "scene_bounds": {
                        "min": [float(value) for value in scene_bounds[0]],
                        "max": [float(value) for value in scene_bounds[1]],
                    },
                    "studio_floor": floor_report,
                    "studio_backdrop": backdrop_report,
                    "studio_preset": str(args.studio_preset),
                },
                "texture_material_contract": {
                    "expected_map_kinds": ["base_color", "normal", "roughness", "metallic"],
                    "shader_id": "UsdPreviewSurface",
                    "material_workflow": "metallic_roughness",
                    "material_count": len(texture_material_report),
                    "all_material_maps_connected": all(not item.get("missing_map_kinds") for item in texture_material_report),
                },
                "visual_set_order": [
                    "whole mono shaded mesh",
                    "semantic colour mesh",
                    "exploded semantic parts",
                    "re-annealed semantic parts",
                    "generated PBR texture variants",
                ],
            }
        )
        return report
    except Exception as exc:
        report["errors"].append(str(exc))
        return report
    finally:
        write_json(Path(args.report).resolve(), report)
        app.close()


def collect_part_prims(stage: Any, default_prim: Any, args: argparse.Namespace) -> list[Any]:
    if args.part_prim_path:
        return [stage.GetPrimAtPath(path) for path in args.part_prim_path if stage.GetPrimAtPath(path).IsValid()]
    if args.part_root:
        root = stage.GetPrimAtPath(args.part_root)
        if root.IsValid():
            return mesh_part_children(root)
    geometry = stage.GetPrimAtPath(f"{default_prim.GetPath()}/Geometry")
    if geometry.IsValid():
        parts = mesh_part_children(geometry)
        if len(parts) >= 2:
            return parts
    parts = mesh_part_children(default_prim)
    if len(parts) >= 2:
        return parts
    return recursive_mesh_prims(default_prim)


def mesh_part_children(root: Any) -> list[Any]:
    return [prim for prim in root.GetChildren() if prim.GetTypeName() == "Mesh" or has_mesh_child(prim)]


def recursive_mesh_prims(root: Any) -> list[Any]:
    found = []
    for child in root.GetChildren():
        if child.GetTypeName() == "Mesh":
            found.append(child)
            continue
        found.extend(recursive_mesh_prims(child))
    return found


def has_mesh_child(prim: Any) -> bool:
    for child in prim.GetChildren():
        if child.GetTypeName() == "Mesh" or has_mesh_child(child):
            return True
    return False


def mesh_descendants(prim: Any) -> list[Any]:
    if prim.GetTypeName() == "Mesh":
        return [prim]
    meshes = []
    for child in prim.GetChildren():
        meshes.extend(mesh_descendants(child))
    return meshes


def compute_scene_bounds(part_prims: list[Any], UsdGeom: Any) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    mins = [float("inf"), float("inf"), float("inf")]
    maxs = [float("-inf"), float("-inf"), float("-inf")]
    for part in part_prims:
        for prim in mesh_descendants(part):
            mesh = UsdGeom.Mesh(prim)
            points = mesh.GetPointsAttr().Get() or []
            if not points:
                continue
            for point in points:
                for axis in range(3):
                    value = float(point[axis])
                    mins[axis] = min(mins[axis], value)
                    maxs[axis] = max(maxs[axis], value)
    if not all(math.isfinite(value) for value in [*mins, *maxs]):
        return ((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0))
    return (tuple(mins), tuple(maxs))


def author_cylindrical_uvs(part_prims: list[Any], UsdGeom: Any, Gf: Any, Vt: Any) -> dict[str, Any]:
    from pxr import Sdf

    authored = []
    for part in part_prims:
        for prim in mesh_descendants(part):
            mesh = UsdGeom.Mesh(prim)
            points = mesh.GetPointsAttr().Get() or []
            if not points:
                continue
            ys = [float(point[1]) for point in points]
            min_y = min(ys)
            height = max(max(ys) - min_y, 1e-6)
            values = []
            point_values = []
            for point in points:
                u = (math.atan2(float(point[2]), float(point[0])) / math.tau + 0.5) % 1.0
                v = (float(point[1]) - min_y) / height
                point_values.append(Gf.Vec2f(float(u), float(v)))
            face_vertex_indices = mesh.GetFaceVertexIndicesAttr().Get() or []
            if not face_vertex_indices:
                continue
            for index in face_vertex_indices:
                values.append(point_values[int(index)])
            primvar = UsdGeom.PrimvarsAPI(prim).CreatePrimvar(
                "st",
                Sdf.ValueTypeNames.TexCoord2fArray,
                UsdGeom.Tokens.faceVarying,
            )
            primvar.Set(Vt.Vec2fArray(values))
            authored.append(
                {
                    "prim_path": str(prim.GetPath()),
                    "uv_count": len(values),
                    "uv_index_count": len(face_vertex_indices),
                    "interpolation": "faceVarying",
                    "indexed": False,
                    "projection": "cylindrical_y_up",
                }
            )
    return {"status": "authored" if authored else "blocked", "mesh_count": len(authored), "meshes": authored}


def add_turntable_op(xformable: Any, axis: str) -> Any:
    if axis == "X":
        return xformable.AddRotateXOp(opSuffix="visualSetTurntable")
    if axis == "Z":
        return xformable.AddRotateZOp(opSuffix="visualSetTurntable")
    return xformable.AddRotateYOp(opSuffix="visualSetTurntable")


def segment_colours(count: int) -> list[tuple[float, float, float]]:
    palette = [
        (0.05, 0.40, 0.80),
        (0.88, 0.32, 0.12),
        (0.08, 0.66, 0.28),
        (0.58, 0.35, 0.78),
        (0.84, 0.60, 0.12),
    ]
    return [palette[index % len(palette)] for index in range(count)]


def role_colours() -> dict[str, tuple[float, float, float]]:
    return {
        "body": (0.16, 0.60, 0.44),
        "handle": (0.04, 0.32, 0.24),
        "rims": (0.82, 0.58, 0.24),
        "lid": (0.78, 0.64, 0.40),
        "trim": (0.72, 0.70, 0.66),
        "logo": (0.95, 0.85, 0.18),
    }


def normalise_material_id(value: str) -> str:
    cleaned = []
    for char in value.lower():
        if char.isalnum():
            cleaned.append(char)
        elif char in {"-", "_"}:
            cleaned.append("_")
    return "".join(cleaned).strip("_") or "segment"


def collect_part_roles(part_prims: list[Any], part_policy: dict[str, Any]) -> list[dict[str, Any]]:
    by_path = part_policy.get("by_path", {})
    by_name = part_policy.get("by_name", {})
    roles = []
    for index, prim in enumerate(part_prims):
        prim_path = str(prim.GetPath())
        prim_name = str(prim.GetName())
        assignment = by_path.get(prim_path) or by_name.get(prim_name) or {}
        segment_id = str(assignment.get("segment_id", f"part_{index:02d}")).strip() or f"part_{index:02d}"
        roles.append(
            {
                "index": index,
                "prim_path": prim_path,
                "part_id": prim_name,
                "segment_id": segment_id,
                "semantic_label": str(assignment.get("semantic_label", segment_id)),
            }
        )
    return roles


def create_role_semantic_materials(stage: Any, part_roles: list[dict[str, Any]]) -> dict[str, Any]:
    materials: dict[str, Any] = {}
    palette = role_colours()
    fallback = segment_colours(len(part_roles))
    ordered_roles = []
    for item in part_roles:
        role = str(item.get("segment_id", ""))
        if role and role not in ordered_roles:
            ordered_roles.append(role)
    for index, role in enumerate(ordered_roles):
        colour = palette.get(role, fallback[index % len(fallback)])
        safe_role = normalise_material_id(role)
        materials[role] = create_preview_material(
            stage,
            f"/World/VisualSetMaterials/semantic_role_{safe_role}",
            colour,
            0.46,
            0.04,
        )
    return materials


def create_preview_material(stage: Any, path: str, colour: tuple[float, float, float], roughness: float, metallic: float) -> Any:
    from pxr import Sdf, UsdShade

    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, f"{path}/PreviewSurface")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(colour)
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(float(roughness))
    shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(float(metallic))
    material.CreateSurfaceOutput().ConnectToSource(shader.CreateOutput("surface", Sdf.ValueTypeNames.Token))
    return material


def create_texture_materials(stage: Any, variants: list[dict[str, Any]], args: argparse.Namespace, Sdf: Any, UsdShade: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    materials: dict[str, Any] = {}
    report: list[dict[str, Any]] = []
    texture_manifest_path = Path(args.texture_manifest).resolve()
    project_root = Path(args.project_root).resolve() if args.project_root else Path()
    for item in variants:
        variant_id = str(item.get("variant_id", "variant"))
        maps = item.get("maps", {})
        material = create_texture_material(
            stage,
            f"/World/VisualSetMaterials/texture_{normalise_material_id(variant_id)}",
            maps,
            project_root,
            texture_manifest_path.parent,
            Sdf,
            UsdShade,
        )
        materials[variant_id] = material
        report.append(
            {
                "binding_key": variant_id,
                "material_path": str(material.GetPath()),
                "segment_id": "",
                "maps": maps,
                **texture_material_contract(maps, project_root, texture_manifest_path.parent),
            }
        )
        for segment in item.get("segment_materials", []):
            segment_id = str(segment.get("segment_id", "")).strip()
            if not segment_id:
                continue
            safe_variant = normalise_material_id(variant_id)
            safe_segment = normalise_material_id(segment_id)
            binding_key = f"{variant_id}:{segment_id}"
            segment_maps = segment.get("maps", {})
            segment_material = create_texture_material(
                stage,
                f"/World/VisualSetMaterials/texture_{safe_variant}_{safe_segment}",
                segment_maps,
                project_root,
                texture_manifest_path.parent,
                Sdf,
                UsdShade,
            )
            materials[binding_key] = segment_material
            report.append(
                {
                    "binding_key": binding_key,
                    "material_path": str(segment_material.GetPath()),
                    "segment_id": segment_id,
                    "maps": segment_maps,
                    **texture_material_contract(segment_maps, project_root, texture_manifest_path.parent),
                }
            )
    return materials, report


def texture_material_contract(maps: dict[str, str], project_root: Path, manifest_dir: Path) -> dict[str, Any]:
    expected = {
        "base_color_path": {
            "map_kind": "base_color",
            "shader_input": "diffuseColor",
            "source_color_space": "sRGB",
            "output_channel": "rgb",
        },
        "normal_path": {
            "map_kind": "normal",
            "shader_input": "normal",
            "source_color_space": "raw",
            "output_channel": "rgb",
            "normal_bias_scale": True,
        },
        "roughness_path": {
            "map_kind": "roughness",
            "shader_input": "roughness",
            "source_color_space": "raw",
            "output_channel": "r",
        },
        "metallic_path": {
            "map_kind": "metallic",
            "shader_input": "metallic",
            "source_color_space": "raw",
            "output_channel": "r",
        },
    }
    map_records = []
    connected = []
    missing = []
    for path_key, contract in expected.items():
        value = str(maps.get(path_key, "") or "")
        exists = False
        resolved = ""
        if value:
            resolved_path = resolve_manifest_path(value, project_root, manifest_dir)
            resolved = resolved_path.as_posix()
            exists = resolved_path.exists()
        if value and exists:
            connected.append(contract["map_kind"])
        else:
            missing.append(contract["map_kind"])
        map_records.append(
            {
                "path_key": path_key,
                "path": value,
                "resolved_path": resolved,
                "exists": exists,
                **contract,
            }
        )
    return {
        "shader_id": "UsdPreviewSurface",
        "material_workflow": "metallic_roughness",
        "pbr_map_contract": map_records,
        "connected_map_kinds": connected,
        "missing_map_kinds": missing,
        "map_count": len(connected),
    }


def create_texture_material(
    stage: Any,
    path: str,
    maps: dict[str, str],
    project_root: Path,
    texture_manifest_dir: Path,
    Sdf: Any,
    UsdShade: Any,
) -> Any:
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, f"{path}/PreviewSurface")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("useSpecularWorkflow", Sdf.ValueTypeNames.Int).Set(0)
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.55)
    shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
    connect_texture(stage, shader, "diffuseColor", Sdf.ValueTypeNames.Color3f, maps.get("base_color_path", ""), project_root, texture_manifest_dir, Sdf, UsdShade)
    connect_texture(stage, shader, "normal", Sdf.ValueTypeNames.Normal3f, maps.get("normal_path", ""), project_root, texture_manifest_dir, Sdf, UsdShade)
    connect_texture(stage, shader, "roughness", Sdf.ValueTypeNames.Float, maps.get("roughness_path", ""), project_root, texture_manifest_dir, Sdf, UsdShade)
    connect_texture(stage, shader, "metallic", Sdf.ValueTypeNames.Float, maps.get("metallic_path", ""), project_root, texture_manifest_dir, Sdf, UsdShade)
    material.CreateSurfaceOutput().ConnectToSource(shader.CreateOutput("surface", Sdf.ValueTypeNames.Token))
    return material


def usd_asset_path(path: Path) -> str:
    return path.resolve().as_posix()


def connect_texture(
    stage: Any,
    shader: Any,
    input_name: str,
    value_type: Any,
    value: str,
    project_root: Path,
    manifest_dir: Path,
    Sdf: Any,
    UsdShade: Any,
) -> None:
    if not value:
        return
    resolved = resolve_manifest_path(value, project_root, manifest_dir)
    texture = UsdShade.Shader.Define(stage, f"{shader.GetPath()}_{input_name}Texture")
    texture.CreateIdAttr("UsdUVTexture")
    texture.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(usd_asset_path(resolved))
    texture.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set("sRGB" if input_name == "diffuseColor" else "raw")
    texture.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("repeat")
    texture.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("repeat")
    if input_name == "normal":
        texture.CreateInput("scale", Sdf.ValueTypeNames.Float4).Set((2.0, 2.0, 2.0, 1.0))
        texture.CreateInput("bias", Sdf.ValueTypeNames.Float4).Set((-1.0, -1.0, -1.0, 0.0))
    st_reader = UsdShade.Shader.Define(stage, f"{shader.GetPath()}_{input_name}StReader")
    st_reader.CreateIdAttr("UsdPrimvarReader_float2")
    st_reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
    texture.CreateInput("st", Sdf.ValueTypeNames.TexCoord2f).ConnectToSource(
        st_reader.CreateOutput("result", Sdf.ValueTypeNames.TexCoord2f)
    )
    output_name = "rgb" if input_name in {"diffuseColor", "normal"} else "r"
    output_type = Sdf.ValueTypeNames.Float3 if input_name in {"diffuseColor", "normal"} else Sdf.ValueTypeNames.Float
    shader.CreateInput(input_name, value_type).ConnectToSource(texture.CreateOutput(output_name, output_type))


def create_studio_floor(stage: Any, UsdGeom: Any, bounds: tuple[tuple[float, float, float], tuple[float, float, float]], preset: str) -> dict[str, Any]:
    min_bound, max_bound = bounds
    centre_x = (float(min_bound[0]) + float(max_bound[0])) * 0.5
    centre_z = (float(min_bound[2]) + float(max_bound[2])) * 0.5
    min_y = float(min_bound[1])
    span_x = max(float(max_bound[0]) - float(min_bound[0]), 1.0)
    span_z = max(float(max_bound[2]) - float(min_bound[2]), 1.0)
    floor_size = max(span_x, span_z) * (5.2 if preset == "product" else 3.2)
    floor = UsdGeom.Cube.Define(stage, "/World/VisualSetFloor")
    xf = UsdGeom.Xformable(floor.GetPrim())
    xf.AddTranslateOp().Set((centre_x, min_y - 0.035, centre_z))
    xf.AddScaleOp().Set((floor_size, 0.025, floor_size))
    colour = (0.52, 0.53, 0.51) if preset == "product" else (0.24, 0.25, 0.25)
    roughness = 0.34 if preset == "product" else 0.42
    material = create_preview_material(stage, "/World/VisualSetMaterials/studio_floor", colour, roughness, 0.0)
    from pxr import UsdShade

    UsdShade.MaterialBindingAPI(floor.GetPrim()).Bind(material)
    return {
        "path": "/World/VisualSetFloor",
        "material_path": str(material.GetPath()),
        "preset": preset,
        "material": {
            "diffuse_colour": [float(value) for value in colour],
            "roughness": float(roughness),
            "metallic": 0.0,
        },
        "centre": [centre_x, min_y - 0.035, centre_z],
        "scale": [floor_size, 0.025, floor_size],
    }


def create_studio_backdrop(stage: Any, UsdGeom: Any, bounds: tuple[tuple[float, float, float], tuple[float, float, float]], preset: str) -> dict[str, Any]:
    min_bound, max_bound = bounds
    centre_x = (float(min_bound[0]) + float(max_bound[0])) * 0.5
    centre_z = (float(min_bound[2]) + float(max_bound[2])) * 0.5
    min_y = float(min_bound[1])
    span_x = max(float(max_bound[0]) - float(min_bound[0]), 1.0)
    span_y = max(float(max_bound[1]) - float(min_bound[1]), 1.0)
    span_z = max(float(max_bound[2]) - float(min_bound[2]), 1.0)
    wall_width = max(span_x, span_z) * 5.2
    wall_height = max(span_y * 2.8, 3.0)
    wall_z = centre_z - wall_width * 0.46
    wall = UsdGeom.Cube.Define(stage, "/World/VisualSetBackdrop")
    xf = UsdGeom.Xformable(wall.GetPrim())
    xf.AddTranslateOp().Set((centre_x, min_y + wall_height * 0.5 - 0.035, wall_z))
    xf.AddScaleOp().Set((wall_width, wall_height, 0.025))
    material = create_preview_material(stage, "/World/VisualSetMaterials/studio_backdrop", (0.52, 0.53, 0.51), 0.36, 0.0)
    from pxr import UsdShade

    UsdShade.MaterialBindingAPI(wall.GetPrim()).Bind(material)
    return {
        "path": "/World/VisualSetBackdrop",
        "material_path": str(material.GetPath()),
        "preset": preset,
        "centre": [centre_x, min_y + wall_height * 0.5 - 0.035, wall_z],
        "scale": [wall_width, wall_height, 0.025],
    }


def add_studio_lights(
    stage: Any,
    UsdGeom: Any,
    UsdLux: Any,
    Gf: Any,
    bounds: tuple[tuple[float, float, float], tuple[float, float, float]],
    key_intensity: float,
    preset: str,
) -> list[dict[str, Any]]:
    if preset == "product":
        return add_product_studio_lights(stage, UsdGeom, UsdLux, Gf, bounds, key_intensity)
    lights = [
        ("/World/VisualSetKeyLight", (2.2, 2.6, 2.6), key_intensity, 0.35),
        ("/World/VisualSetFillLight", (-2.6, 1.4, 1.4), key_intensity * 0.18, 0.65),
        ("/World/VisualSetRimLight", (-1.6, 2.1, -2.2), key_intensity * 0.45, 0.28),
    ]
    records = []
    for path, position, intensity, radius in lights:
        light = UsdLux.SphereLight.Define(stage, path)
        light.CreateIntensityAttr(float(intensity))
        light.CreateRadiusAttr(float(radius))
        UsdGeom.Xformable(light.GetPrim()).AddTranslateOp().Set(Gf.Vec3d(*position))
        records.append(
            {
                "path": path,
                "type": "SphereLight",
                "position": [float(value) for value in position],
                "intensity": float(intensity),
                "radius": float(radius),
            }
        )
    return records


def add_product_studio_lights(stage: Any, UsdGeom: Any, UsdLux: Any, Gf: Any, bounds: tuple[tuple[float, float, float], tuple[float, float, float]], key_intensity: float) -> list[dict[str, Any]]:
    min_bound, max_bound = bounds
    centre = Gf.Vec3d(
        (float(min_bound[0]) + float(max_bound[0])) * 0.5,
        (float(min_bound[1]) + float(max_bound[1])) * 0.5,
        (float(min_bound[2]) + float(max_bound[2])) * 0.5,
    )
    span = max(float(max_bound[axis]) - float(min_bound[axis]) for axis in range(3))
    span = max(span, 1.0)
    lights = [
        {
            "path": "/World/VisualSetKeySoftbox",
            "position": centre + Gf.Vec3d(span * 1.45, span * 1.95, span * 1.35),
            "intensity": key_intensity,
            "width": span * 1.15,
            "height": span * 0.85,
            "colour_temperature": 5200.0,
        },
        {
            "path": "/World/VisualSetFillSoftbox",
            "position": centre + Gf.Vec3d(-span * 1.9, span * 0.95, span * 1.1),
            "intensity": key_intensity * 0.28,
            "width": span * 1.5,
            "height": span * 1.2,
            "colour_temperature": 6100.0,
        },
        {
            "path": "/World/VisualSetRimSoftbox",
            "position": centre + Gf.Vec3d(-span * 1.45, span * 1.65, -span * 1.8),
            "intensity": key_intensity * 0.55,
            "width": span * 0.75,
            "height": span * 1.3,
            "colour_temperature": 4700.0,
        },
    ]
    records = []
    for item in lights:
        light = UsdLux.RectLight.Define(stage, item["path"])
        light.CreateIntensityAttr(float(item["intensity"]))
        light.CreateWidthAttr(float(item["width"]))
        light.CreateHeightAttr(float(item["height"]))
        light.CreateEnableColorTemperatureAttr(True)
        light.CreateColorTemperatureAttr(float(item["colour_temperature"]))
        matrix = look_at_matrix(item["position"], centre, Gf.Vec3d(0.0, 1.0, 0.0), Gf)
        UsdGeom.Xformable(light.GetPrim()).AddTransformOp().Set(matrix)
        records.append(
            {
                "path": item["path"],
                "type": "RectLight",
                "position": [float(item["position"][axis]) for axis in range(3)],
                "target": [float(centre[axis]) for axis in range(3)],
                "intensity": float(item["intensity"]),
                "width": float(item["width"]),
                "height": float(item["height"]),
                "colour_temperature": float(item["colour_temperature"]),
            }
        )
    return records


def bind_phase_materials(
    part_prims: list[Any],
    part_roles: list[dict[str, Any]],
    style: str,
    variant_id: str,
    grey: Any,
    semantic_materials: list[Any],
    semantic_role_materials: dict[str, Any],
    texture_materials: dict[str, Any],
    UsdShade: Any,
) -> int:
    binding_count = 0
    for index, prim in enumerate(part_prims):
        segment_id = str(part_roles[index].get("segment_id", "")) if index < len(part_roles) else ""
        if style == "mono":
            material = grey
        elif style == "semantic":
            material = semantic_role_materials.get(segment_id) or semantic_materials[index % len(semantic_materials)]
        else:
            material = texture_materials.get(f"{variant_id}:{segment_id}") or texture_materials.get(variant_id) or grey
        binding_count += bind_material_to_part(prim, material, UsdShade)
    return binding_count


def bind_material_to_part(prim: Any, material: Any, UsdShade: Any) -> int:
    targets = {str(prim.GetPath()): prim}
    for mesh in mesh_descendants(prim):
        targets[str(mesh.GetPath())] = mesh
    for target in targets.values():
        binding_api = UsdShade.MaterialBindingAPI(target)
        if hasattr(binding_api, "UnbindAllBindings"):
            binding_api.UnbindAllBindings()
        try:
            binding_api.Bind(material, UsdShade.Tokens.strongerThanDescendants)
        except TypeError:
            binding_api.Bind(material)
    return len(targets)


def explosion_directions(count: int, Gf: Any) -> list[Any]:
    directions = []
    for index in range(count):
        angle = (math.tau * index) / max(1, count)
        directions.append(Gf.Vec3f(math.cos(angle), 0.0, math.sin(angle)))
    return directions


def look_at_matrix(eye: Any, target: Any, up: Any, Gf: Any) -> Any:
    forward = (target - eye).GetNormalized()
    if abs(float(forward * up)) > 0.99:
        up = Gf.Vec3d(0.0, 0.0, 1.0)
    right = (forward ^ up).GetNormalized()
    camera_up = (right ^ forward).GetNormalized()
    matrix = Gf.Matrix4d()
    matrix[0] = [right[0], right[1], right[2], 0.0]
    matrix[1] = [camera_up[0], camera_up[1], camera_up[2], 0.0]
    matrix[2] = [-forward[0], -forward[1], -forward[2], 0.0]
    matrix[3] = [eye[0], eye[1], eye[2], 1.0]
    return matrix


def set_camera_transform(camera: Any, Gf: Any, UsdGeom: Any, bounds: tuple[tuple[float, float, float], tuple[float, float, float]], distance_multiplier: float) -> dict[str, Any]:
    min_bound, max_bound = bounds
    centre = [
        (float(min_bound[axis]) + float(max_bound[axis])) * 0.5
        for axis in range(3)
    ]
    span = max(float(max_bound[axis]) - float(min_bound[axis]) for axis in range(3))
    distance = max(span * max(float(distance_multiplier), 0.5), 3.2)
    direction = Gf.Vec3d(0.62, 0.36, 0.70).GetNormalized()
    target = Gf.Vec3d(centre[0], centre[1], centre[2])
    eye = target + direction * distance
    up = Gf.Vec3d(0.0, 1.0, 0.0)
    matrix = look_at_matrix(eye, target, up, Gf)
    UsdGeom.Xformable(camera.GetPrim()).AddTransformOp().Set(matrix)
    return {
        "path": str(camera.GetPrim().GetPath()),
        "eye": [float(eye[0]), float(eye[1]), float(eye[2])],
        "target": [float(target[0]), float(target[1]), float(target[2])],
        "up": [float(up[0]), float(up[1]), float(up[2])],
        "distance": float(distance),
        "projection": "perspective_look_at",
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report_path = Path(args.report).resolve()
    validation = validate_inputs(args)
    if args.validate_only:
        write_json(report_path, validation)
        return 0 if validation["status"] == "ready" else 1
    if validation["status"] != "ready":
        write_json(report_path, validation)
        return 1
    if not args.frames_dir or not args.gif:
        validation["status"] = "blocked"
        validation["blocked_reasons"].append("frames-dir and gif are required for render mode")
        write_json(report_path, validation)
        return 1
    result = render_visual_set(args, validation)
    write_json(report_path, result)
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
