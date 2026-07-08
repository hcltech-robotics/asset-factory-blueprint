from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compose one visual-set GIF from multiple rendered pass reports.")
    parser.add_argument("--source-report", action="append", required=True, help="visual_set_report.json to include. Repeat in output order.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--gif", required=True)
    parser.add_argument("--contact-sheet", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--checksums", required=True)
    parser.add_argument("--asset-id", default="asset")
    parser.add_argument("--frame-duration-ms", type=int, default=300)
    parser.add_argument("--contact-thumb-width", type=int, default=192)
    parser.add_argument("--contact-thumb-height", type=int, default=144)
    parser.add_argument("--composition-method", default="multi_pass_pathtrace_composite")
    parser.add_argument("--reason", default="")
    return parser


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def frame_quality(frame_stats: list[dict[str, Any]]) -> dict[str, Any]:
    if not frame_stats:
        return {"status": "blocked", "reasons": ["no frames"], "mean_rgb_min": 0.0, "mean_rgb_max": 0.0, "max_rgb": 0}
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


def image_stats(path: Path) -> dict[str, float | int]:
    image = Image.open(path).convert("RGB")
    extrema = image.getextrema()
    max_rgb = max(channel[1] for channel in extrema)
    min_rgb = min(channel[0] for channel in extrema)
    total = 0
    pixel_count = image.size[0] * image.size[1] * 3
    for channel in image.split():
        total += sum(channel.histogram()[value] * value for value in range(256))
    return {"mean_rgb": float(total / max(1, pixel_count)), "max_rgb": int(max_rgb), "min_rgb": int(min_rgb)}


def normalise_source_frames(report: dict[str, Any]) -> list[dict[str, Any]]:
    frames = [Path(item) for item in report.get("frames", [])]
    stats_by_frame = {
        int(item.get("frame", index)): item
        for index, item in enumerate(report.get("frame_stats", []))
        if isinstance(item, dict)
    }
    records = []
    for index, path in enumerate(frames):
        stat = stats_by_frame.get(index, {})
        records.append(
            {
                "source_path": path,
                "source_frame": index,
                "source_sequence_frame": int(stat.get("sequence_frame", index)),
                "phase": str(stat.get("phase", "")),
                "style": str(stat.get("style", "")),
                "variant_id": str(stat.get("variant_id", "")),
            }
        )
    return records


def first_report_value(reports: list[dict[str, Any]], key: str, default: Any) -> Any:
    for report in reports:
        value = report.get(key)
        if value not in ({}, [], "", None):
            return value
    return default


def render_contact_sheet(frames: list[dict[str, Any]], output_path: Path, thumb_width: int, thumb_height: int) -> None:
    if not frames:
        return
    sheet = Image.new("RGB", (thumb_width * len(frames), thumb_height + 28), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    for index, frame in enumerate(frames):
        image = Image.open(frame["path"]).convert("RGB").resize((thumb_width, thumb_height))
        sheet.paste(image, (index * thumb_width, 0))
        label = frame.get("variant_id") or frame.get("phase") or str(frame.get("frame"))
        draw.text((index * thumb_width + 4, thumb_height + 6), str(label)[:22], fill=(240, 240, 240))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)


def compose(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir).resolve()
    frames_dir = output_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    reports = [load_json(Path(item).resolve()) for item in args.source_report]
    source_report_paths = [Path(item).resolve() for item in args.source_report]
    frame_records: list[dict[str, Any]] = []
    for pass_index, (source_report_path, report) in enumerate(zip(source_report_paths, reports)):
        for source in normalise_source_frames(report):
            source_path = source["source_path"]
            if not source_path.is_absolute():
                source_path = source_report_path.parent / source_path
            target = frames_dir / f"frame_{len(frame_records):04d}.png"
            shutil.copy2(source_path, target)
            stats = image_stats(target)
            frame_records.append(
                {
                    "frame": len(frame_records),
                    "path": target.as_posix(),
                    "source_report": source_report_path.as_posix(),
                    "source_path": source_path.as_posix(),
                    "source_pass": pass_index,
                    "source_frame": int(source["source_frame"]),
                    "source_sequence_frame": int(source["source_sequence_frame"]),
                    "phase": source["phase"],
                    "style": source["style"],
                    "variant_id": source["variant_id"],
                    **stats,
                    "sha256": sha256_file(target),
                    "size_bytes": target.stat().st_size,
                }
            )
    gif_path = Path(args.gif).resolve()
    gif_path.parent.mkdir(parents=True, exist_ok=True)
    gif_frames = [Image.open(item["path"]).convert("P", palette=Image.Palette.ADAPTIVE) for item in frame_records]
    if gif_frames:
        gif_frames[0].save(
            gif_path,
            save_all=True,
            append_images=gif_frames[1:],
            duration=int(args.frame_duration_ms),
            loop=0,
            optimize=False,
        )
    for frame in gif_frames:
        frame.close()
    contact_sheet_path = Path(args.contact_sheet).resolve()
    render_contact_sheet(
        frame_records,
        contact_sheet_path,
        int(args.contact_thumb_width),
        int(args.contact_thumb_height),
    )
    quality = frame_quality(frame_records)
    source_render_modes = [
        str(report.get("render_settings", {}).get("render_mode", ""))
        for report in reports
        if isinstance(report, dict)
    ]
    non_empty_render_modes = [mode for mode in source_render_modes if mode]
    render_mode = (
        "PathTracing"
        if non_empty_render_modes and all(mode == "PathTracing" for mode in non_empty_render_modes)
        else (non_empty_render_modes[0] if non_empty_render_modes else "")
    )
    report_path = Path(args.report).resolve()
    payload = {
        "status": "pass" if frame_records else "blocked",
        "asset_id": args.asset_id,
        "composition_method": args.composition_method,
        "reason": args.reason,
        "source_reports": [path.as_posix() for path in source_report_paths],
        "gif": gif_path.as_posix(),
        "contact_sheet": contact_sheet_path.as_posix(),
        "frame_count": len(frame_records),
        "frames_dir": frames_dir.as_posix(),
        "frames": [item["path"] for item in frame_records],
        "frame_stats": frame_records,
        "frame_quality": quality,
        "mean_rgb_min": quality["mean_rgb_min"],
        "mean_rgb_max": quality["mean_rgb_max"],
        "max_rgb": quality["max_rgb"],
        "texture_binding_mode": "segment_material_policy"
        if any(report.get("texture_binding_mode") == "segment_material_policy" for report in reports)
        else "",
        "turntable_axis": next((str(report.get("turntable_axis")) for report in reports if report.get("turntable_axis")), ""),
        "lighting": first_report_value(reports, "lighting", {}),
        "camera": first_report_value(reports, "camera", {}),
        "stage_setup": first_report_value(reports, "stage_setup", {}),
        "studio_preset": first_report_value(reports, "studio_preset", ""),
        "texture_material_contract": first_report_value(reports, "texture_material_contract", {}),
        "part_material_assignments": first_report_value(reports, "part_material_assignments", []),
        "texture_materials": first_report_value(reports, "texture_materials", []),
        "render_settings": {
            "render_mode": render_mode,
            "source_render_modes": source_render_modes,
            "pathtrace_samples_per_pixel": next(
                (
                    report.get("render_settings", {}).get("pathtrace_samples_per_pixel")
                    for report in reports
                    if report.get("render_settings", {}).get("pathtrace_samples_per_pixel")
                ),
                None,
            ),
            "pathtrace_max_bounces": next(
                (
                    report.get("render_settings", {}).get("pathtrace_max_bounces")
                    for report in reports
                    if report.get("render_settings", {}).get("pathtrace_max_bounces")
                ),
                None,
            ),
            "aces_tonemap": any(bool(report.get("render_settings", {}).get("aces_tonemap")) for report in reports),
        },
        "visual_set_order": [
            "whole mono shaded mesh",
            "semantic colour mesh",
            "exploded semantic parts",
            "re-annealed semantic parts",
            "generated PBR texture variants",
        ],
    }
    write_json(report_path, payload)
    checksum_path = Path(args.checksums).resolve()
    checksum_files = [gif_path, contact_sheet_path, report_path, *[Path(item["path"]) for item in frame_records]]
    write_json(
        checksum_path,
        {
            "files": [
                {"path": path.as_posix(), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
                for path in checksum_files
                if path.exists()
            ]
        },
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = compose(args)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "gif": payload["gif"],
                "contact_sheet": payload["contact_sheet"],
                "report": Path(args.report).resolve().as_posix(),
                "frame_count": payload["frame_count"],
            },
            indent=2,
            sort_keys=False,
        )
    )
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
