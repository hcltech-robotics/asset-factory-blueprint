from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


VARIANTS = [
    {
        "variant_id": "shiny_red",
        "material_name": "glossy_red_enamel_painted_metal",
        "texture_intent": "glossy red enamel over metal with subtle orange-peel surface and fine edge wear",
    },
    {
        "variant_id": "rusty_metal",
        "material_name": "oxidised_rusty_metal",
        "texture_intent": "dark oxidised steel with orange-brown rust blooms, pits and dull exposed metal",
    },
    {
        "variant_id": "weathered",
        "material_name": "weathered_painted_metal",
        "texture_intent": "aged green paint with chipped edges, grey oxidation and dull exposed steel",
    },
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create representative UV PBR maps for an Asset Factory rollout.")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--size", type=int, default=1024)
    parser.add_argument("--manifest", default="")
    parser.add_argument("--report", default="")
    parser.add_argument("--segment-materials", action="store_true")
    return parser


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def value_noise(size: int, seed: int, octaves: int = 5) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32) / float(size)
    noise = np.zeros((size, size), dtype=np.float32)
    amp = 1.0
    total = 0.0
    for octave in range(octaves):
        freq = 2 ** octave
        phase = rng.random(4, dtype=np.float32) * math.tau
        layer = (
            np.sin((xx * freq + phase[0]) * math.tau)
            + np.sin((yy * freq + phase[1]) * math.tau)
            + np.sin(((xx + yy) * freq + phase[2]) * math.tau)
            + np.sin(((xx - yy) * freq + phase[3]) * math.tau)
        )
        noise += layer * amp
        total += amp * 4.0
        amp *= 0.52
    noise = noise / max(total, 1e-6)
    noise = (noise - noise.min()) / max(float(noise.max() - noise.min()), 1e-6)
    return noise


def normal_from_height(height: np.ndarray, strength: float = 5.0) -> np.ndarray:
    gy, gx = np.gradient(height.astype(np.float32))
    nx = -gx * strength
    ny = -gy * strength
    nz = np.ones_like(height)
    length = np.sqrt(nx * nx + ny * ny + nz * nz)
    normal = np.stack([nx / length, ny / length, nz / length], axis=-1)
    return ((normal * 0.5 + 0.5) * 255.0).clip(0, 255).astype(np.uint8)


def rgb_image(array: np.ndarray) -> Image.Image:
    return Image.fromarray((array.clip(0, 1) * 255.0).astype(np.uint8), "RGB")


def grey_image(array: np.ndarray) -> Image.Image:
    data = (array.clip(0, 1) * 255.0).astype(np.uint8)
    return Image.fromarray(data, "L").convert("RGB")


def organic_noise(size: int, seed: int, grid: int = 48) -> np.ndarray:
    rng = np.random.default_rng(seed)
    small = rng.random((grid, grid), dtype=np.float32)
    image = Image.fromarray((small * 255).astype(np.uint8), "L").resize((size, size), Image.Resampling.BICUBIC)
    data = np.asarray(image, dtype=np.float32) / 255.0
    return (data - data.min()) / max(float(data.max() - data.min()), 1e-6)


def scratch_field(
    size: int,
    seed: int,
    *,
    count: int,
    strength: float = 1.0,
    vertical_bias: float = 0.65,
    blur_radius: float = 0.35,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(image)
    for _ in range(count):
        if rng.random() < vertical_bias:
            x = int(rng.integers(0, size))
            y0 = int(rng.integers(0, size))
            length = int(rng.integers(max(8, size // 18), max(12, size // 4)))
            jitter = int(rng.integers(-size // 40, size // 40 + 1))
            xy = [(x, y0), (x + jitter, min(size - 1, y0 + length))]
        else:
            y = int(rng.integers(0, size))
            x0 = int(rng.integers(0, size))
            length = int(rng.integers(max(8, size // 22), max(12, size // 5)))
            jitter = int(rng.integers(-size // 55, size // 55 + 1))
            xy = [(x0, y), (min(size - 1, x0 + length), y + jitter)]
        width = int(rng.integers(1, max(2, size // 220 + 2)))
        draw.line(xy, fill=int(rng.integers(120, 256)), width=width)
    if blur_radius > 0:
        image = image.filter(ImageFilter.GaussianBlur(blur_radius))
    return (np.asarray(image, dtype=np.float32) / 255.0 * strength).clip(0, 1)


def dot_field(size: int, seed: int, *, density: float, blur_radius: float = 0.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    dots = (rng.random((size, size), dtype=np.float32) < density).astype(np.float32)
    if blur_radius > 0:
        image = Image.fromarray((dots * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(blur_radius))
        dots = np.asarray(image, dtype=np.float32) / 255.0
    return dots.clip(0, 1)


def vertical_stains(size: int, seed: int, *, count: int, strength: float) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(image)
    for _ in range(count):
        x = int(rng.integers(0, size))
        y0 = int(rng.integers(0, max(1, size // 3)))
        length = int(rng.integers(max(12, size // 8), max(24, int(size * 0.85))))
        width = int(rng.integers(max(1, size // 180), max(2, size // 80)))
        draw.line([(x, y0), (x + int(rng.integers(-size // 60, size // 60 + 1)), min(size - 1, y0 + length))], fill=180, width=width)
    image = image.filter(ImageFilter.GaussianBlur(max(0.8, size / 260.0)))
    return (np.asarray(image, dtype=np.float32) / 255.0 * strength).clip(0, 1)


def radial_brush(size: int, seed: int, *, fine_scale: float = 38.0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = np.linspace(0.0, 1.0, size, dtype=np.float32)[None, :]
    phases = rng.random(4, dtype=np.float32) * math.tau
    brush = (
        np.sin((x * fine_scale + phases[0]) * math.tau) * 0.45
        + np.sin((x * fine_scale * 2.7 + phases[1]) * math.tau) * 0.25
        + np.sin((x * fine_scale * 6.1 + phases[2]) * math.tau) * 0.12
    )
    brush = np.repeat(brush, size, axis=0)
    brush += organic_noise(size, seed + 9, 96) * 0.18
    return (brush - brush.min()) / max(float(brush.max() - brush.min()), 1e-6)


def red_enamel(size: int, seed_offset: int = 0) -> dict[str, Image.Image]:
    fine = organic_noise(size, 11 + seed_offset, 96)
    orange_peel = organic_noise(size, 17 + seed_offset, 160)
    micro_specks = dot_field(size, 19 + seed_offset, density=0.0025, blur_radius=0.55)
    scratches = scratch_field(size, 23 + seed_offset, count=max(70, size // 7), strength=0.8, vertical_bias=0.42)
    yy = np.linspace(0.035, -0.025, size, dtype=np.float32)[:, None]
    colour = np.zeros((size, size, 3), dtype=np.float32)
    colour[..., 0] = 0.78 + fine * 0.050 + yy
    colour[..., 1] = 0.030 + orange_peel * 0.034 + micro_specks * 0.05
    colour[..., 2] = 0.018 + fine * 0.016
    worn = np.array([0.54, 0.43, 0.35], dtype=np.float32)
    scratch_mask = scratches > 0.18
    colour[scratch_mask] = colour[scratch_mask] * 0.72 + worn * 0.28
    roughness = 0.34 + orange_peel * 0.10 + scratches * 0.20 + micro_specks * 0.08
    metallic = np.full((size, size), 0.0, dtype=np.float32)
    height = orange_peel * 0.20 + micro_specks * 0.08 - scratches * 0.045
    return {
        "base_color": rgb_image(colour),
        "roughness": grey_image(roughness),
        "metallic": grey_image(metallic),
        "normal": Image.fromarray(normal_from_height(height, 3.2), "RGB"),
    }


def rusty_metal(size: int, seed_offset: int = 0) -> dict[str, Image.Image]:
    fine = organic_noise(size, 37 + seed_offset, 164)
    soot = organic_noise(size, 39 + seed_offset, 72)
    rust_bloom = dot_field(size, 41 + seed_offset, density=0.018, blur_radius=2.2)
    black_pits = dot_field(size, 43 + seed_offset, density=0.035, blur_radius=0.42)
    pinholes = dot_field(size, 45 + seed_offset, density=0.08, blur_radius=0.08)
    stains = vertical_stains(size, 47 + seed_offset, count=max(8, size // 70), strength=0.22)
    scratches = scratch_field(size, 49 + seed_offset, count=max(90, size // 8), strength=0.48, vertical_bias=0.48)
    rust_mask = np.clip(rust_bloom * 0.95 + stains * 0.30 + (fine > 0.84).astype(np.float32) * 0.10, 0.0, 0.88)
    pit_mask = np.clip(black_pits * 0.70 + pinholes * 0.36, 0.0, 0.82)
    dark = np.array([0.10, 0.085, 0.065], dtype=np.float32)
    iron = np.array([0.36, 0.33, 0.29], dtype=np.float32)
    rust = np.array([0.61, 0.22, 0.070], dtype=np.float32)
    fresh = np.array([0.58, 0.55, 0.50], dtype=np.float32)
    oxide = np.array([0.30, 0.11, 0.035], dtype=np.float32)
    colour = dark * (1.0 - fine[..., None] * 0.22) + iron * (fine[..., None] * 0.22)
    colour = colour * (1.0 - soot[..., None] * 0.18)
    colour = colour * (1.0 - rust_mask[..., None]) + rust * rust_mask[..., None]
    colour = colour * (1.0 - pit_mask[..., None] * 0.42) + oxide * (pit_mask[..., None] * 0.42)
    exposed = scratches > 0.20
    colour[exposed] = colour[exposed] * 0.66 + fresh * 0.34
    roughness = 0.60 + rust_mask * 0.30 + pit_mask * 0.12 - scratches * 0.10
    metallic = 0.36 * (1.0 - rust_mask) + 0.035 * rust_mask + scratches * 0.18
    height = fine * 0.10 + rust_mask * 0.24 - pit_mask * 0.28 - scratches * 0.055
    return {
        "base_color": rgb_image(colour),
        "roughness": grey_image(roughness),
        "metallic": grey_image(metallic),
        "normal": Image.fromarray(normal_from_height(height, 5.8), "RGB"),
    }


def weathered_paint(size: int, seed_offset: int = 0) -> dict[str, Image.Image]:
    fine = organic_noise(size, 59 + seed_offset, 164)
    grime = organic_noise(size, 55 + seed_offset, 88)
    edge_streaks = vertical_stains(size, 63 + seed_offset, count=max(6, size // 90), strength=0.20)
    scratches = scratch_field(size, 67 + seed_offset, count=max(80, size // 8), strength=0.55, vertical_bias=0.48)
    chip_blobs = dot_field(size, 61 + seed_offset, density=0.010, blur_radius=1.5)
    chips = np.clip(chip_blobs * 0.95 + (scratches > 0.42).astype(np.float32) * 0.45, 0.0, 0.88)
    wear = np.clip(fine * 0.20 + edge_streaks * 0.24 + scratches * 0.05, 0.0, 0.36)
    paint = np.array([0.34, 0.47, 0.36], dtype=np.float32)
    faded = np.array([0.52, 0.58, 0.50], dtype=np.float32)
    exposed = np.array([0.62, 0.61, 0.56], dtype=np.float32)
    oxide = np.array([0.20, 0.26, 0.22], dtype=np.float32)
    colour = paint * (1.0 - wear[..., None]) + faded * wear[..., None]
    colour = colour * (1.0 - chips[..., None] * 0.82) + exposed * (chips[..., None] * 0.82)
    oxidised = np.clip((grime - 0.74) / 0.22 + edge_streaks * 0.22, 0.0, 0.55)
    colour = colour * (1.0 - oxidised[..., None] * 0.18) + oxide * (oxidised[..., None] * 0.18)
    roughness = 0.42 + wear * 0.34 + chips * 0.16 + oxidised * 0.18
    metallic = 0.035 + chips * 0.42
    height = fine * 0.09 + wear * 0.11 - chips * 0.10 + oxidised * 0.04 - scratches * 0.045
    return {
        "base_color": rgb_image(colour),
        "roughness": grey_image(roughness),
        "metallic": grey_image(metallic),
        "normal": Image.fromarray(normal_from_height(height, 4.2), "RGB"),
    }


def brushed_steel(size: int, seed: int = 71, warm: bool = False) -> dict[str, Image.Image]:
    grain = organic_noise(size, seed, 128)
    brush = radial_brush(size, seed + 3, fine_scale=54.0)
    scratches = scratch_field(size, seed + 5, count=max(150, size // 4), strength=0.55, vertical_bias=0.18, blur_radius=0.18)
    base = np.array([0.68, 0.66, 0.61] if warm else [0.68, 0.69, 0.67], dtype=np.float32)
    colour = base[None, None, :] + (brush[..., None] - 0.5) * 0.12 + grain[..., None] * 0.035
    colour = colour * (1.0 - scratches[..., None] * 0.18) + np.array([0.88, 0.87, 0.82], dtype=np.float32) * (scratches[..., None] * 0.18)
    roughness = 0.18 + grain * 0.12 + scratches * 0.22
    metallic = np.full((size, size), 0.92, dtype=np.float32)
    height = brush * 0.05 + grain * 0.04 - scratches * 0.055
    return {
        "base_color": rgb_image(colour),
        "roughness": grey_image(roughness),
        "metallic": grey_image(metallic),
        "normal": Image.fromarray(normal_from_height(height, 2.6), "RGB"),
    }


def dark_enamel(size: int, seed: int = 83, green: bool = False) -> dict[str, Image.Image]:
    fine = organic_noise(size, seed, 128)
    scratches = scratch_field(size, seed + 4, count=max(70, size // 8), strength=0.72, vertical_bias=0.55)
    dust = dot_field(size, seed + 8, density=0.006, blur_radius=0.4)
    paint = np.array([0.03, 0.055, 0.045] if green else [0.015, 0.014, 0.014], dtype=np.float32)
    edge = np.array([0.20, 0.23, 0.20], dtype=np.float32)
    colour = paint[None, None, :] + fine[..., None] * 0.045
    colour = colour * (1.0 - scratches[..., None] * 0.36) + edge * (scratches[..., None] * 0.36)
    colour = colour + dust[..., None] * 0.05
    roughness = 0.22 + fine * 0.18 + scratches * 0.20 + dust * 0.12
    metallic = np.full((size, size), 0.03, dtype=np.float32)
    height = fine * 0.13 - scratches * 0.055 + dust * 0.04
    return {
        "base_color": rgb_image(colour),
        "roughness": grey_image(roughness),
        "metallic": grey_image(metallic),
        "normal": Image.fromarray(normal_from_height(height, 2.8), "RGB"),
    }


def segment_maps_for_variant(variant_id: str, segment_id: str, size: int) -> dict[str, Image.Image]:
    if segment_id == "body":
        return {
            "shiny_red": lambda value: red_enamel(value, 0),
            "rusty_metal": lambda value: rusty_metal(value, 0),
            "weathered": lambda value: weathered_paint(value, 0),
        }[variant_id](size)
    if segment_id == "handle":
        if variant_id == "rusty_metal":
            return rusty_metal(size, 101)
        return dark_enamel(size, green=variant_id == "weathered")
    if segment_id in {"rims", "lid", "trim"}:
        if variant_id == "rusty_metal":
            return rusty_metal(size, 203)
        return brushed_steel(size, warm=variant_id == "weathered")
    return weathered_paint(size)


def write_contact_sheet(path: Path, records: list[dict[str, Any]], project_root: Path) -> None:
    tile = 160
    sheet = Image.new("RGB", (tile * 4, tile * len(records)), (245, 245, 242))
    draw = ImageDraw.Draw(sheet)
    for row, record in enumerate(records):
        for col, key in enumerate(("base_color_path", "normal_path", "roughness_path", "metallic_path")):
            image = Image.open(project_root / record[key]).convert("RGB").resize((tile, tile))
            sheet.paste(image, (col * tile, row * tile))
            label = f"{record['variant_id']} {key.replace('_path', '')}"
            label_x = col * tile
            label_y = row * tile
            draw.rectangle((label_x, label_y, label_x + tile, label_y + 18), fill=(245, 245, 242))
            draw.text((label_x + 6, label_y + 4), label, fill=(15, 15, 15))
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)


def write_outputs(project_root: Path, asset_id: str, output_dir: Path, size: int, segment_materials: bool = False) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    makers = {
        "shiny_red": red_enamel,
        "rusty_metal": rusty_metal,
        "weathered": weathered_paint,
    }
    records = []
    files = []
    for variant in VARIANTS:
        variant_id = variant["variant_id"]
        maps = makers[variant_id](size)
        record = {
            "variant_id": variant_id,
            "material_name": variant["material_name"],
            "texture_intent": variant["texture_intent"],
            "prompt": variant["texture_intent"],
            "negative_prompt": "object silhouette, labels, baked lighting, cast shadows",
            "provider_role": "texture_generator",
            "seed": 0,
            "resolution": f"{size}x{size} PBR map set",
            "tileable": True,
            "generation_method": "local_procedural_pbr_maps",
            "is_generated_texture": True,
            "generated_map_kinds": ["base_color", "normal", "roughness", "metallic"],
            "policy_map_kinds": [],
            "status": "generated",
            "usd_output_path": f"assets/{asset_id}/mtl.usda",
            "height_or_displacement_path": "",
        }
        for map_kind, image in maps.items():
            rel = output_dir / f"{variant_id}_{map_kind}.png"
            image.save(rel)
            record[f"{map_kind}_path"] = rel.relative_to(project_root).as_posix()
            files.append(rel)
        if segment_materials:
            record["segment_materials"] = []
            for segment_id in ("body", "handle", "rims"):
                segment_record = {
                    "segment_id": segment_id,
                    "material_name": f"{variant_id}_{segment_id}",
                    "generation_method": "local_procedural_segment_pbr_maps",
                    "is_generated_texture": True,
                    "generated_map_kinds": ["base_color", "normal", "roughness", "metallic"],
                    "policy_map_kinds": [],
                    "status": "generated",
                }
                for map_kind, image in segment_maps_for_variant(variant_id, segment_id, size).items():
                    rel = output_dir / f"{variant_id}_{segment_id}_{map_kind}.png"
                    image.save(rel)
                    segment_record[f"{map_kind}_path"] = rel.relative_to(project_root).as_posix()
                    files.append(rel)
                segment_record["checksum"] = sha256_file(project_root / segment_record["base_color_path"])
                record["segment_materials"].append(segment_record)
        record["checksum"] = sha256_file(project_root / record["base_color_path"])
        records.append(record)
    return {"records": records, "files": files}


def main() -> int:
    args = build_parser().parse_args()
    project_root = Path(args.project_root).resolve()
    asset_id = args.asset_id
    output_dir = Path(args.output_dir).resolve() if args.output_dir else project_root / "assets" / asset_id / "textures" / "rollout_pbr_v1"
    manifest_path = Path(args.manifest).resolve() if args.manifest else output_dir / "texturing-manifest.json"
    report_path = Path(args.report).resolve() if args.report else output_dir / "pbr-texture-rollout-report.json"
    size = max(256, min(2048, int(args.size)))
    result = write_outputs(project_root, asset_id, output_dir, size, segment_materials=bool(args.segment_materials))
    contact_sheet = output_dir / "pbr-texture-contact-sheet.png"
    sheet_records = result["records"]
    if args.segment_materials:
        sheet_records = [
            {**segment, "variant_id": f"{record['variant_id']}:{segment['segment_id']}"}
            for record in result["records"]
            for segment in record.get("segment_materials", [])
        ]
    write_contact_sheet(contact_sheet, sheet_records, project_root)
    files = [*result["files"], contact_sheet]
    manifest = {
        "id": f"{asset_id}_representative_texture_rollout",
        "version": "1.0",
        "asset_id": asset_id,
        "texture_generation_status": "generated",
        "texture_generation_backend": "local_procedural_segment_pbr_maps" if args.segment_materials else "local_procedural_pbr_maps",
        "texture_blocked_reasons": [],
        "provider_trace": [],
        "texture_map_policy_trace": [],
        "texture_outputs": result["records"],
        "texture_variants": result["records"],
        "render_evidence": [{"kind": "texture_contact_sheet", "uri": contact_sheet.relative_to(project_root).as_posix(), "status": "generated"}],
        "validation_status": "proposal",
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    files.append(manifest_path)
    report = {
        "status": "pass",
        "project_root": project_root.as_posix(),
        "asset_id": asset_id,
        "texture_manifest": manifest_path.relative_to(project_root).as_posix(),
        "contact_sheet": contact_sheet.relative_to(project_root).as_posix(),
        "texture_outputs": [
            {
                "variant_id": item["variant_id"],
                "base_color_path": item["base_color_path"],
                "normal_path": item["normal_path"],
                "roughness_path": item["roughness_path"],
                "metallic_path": item["metallic_path"],
            }
            for item in result["records"]
        ],
        "files": [{"path": path.relative_to(project_root).as_posix(), "sha256": sha256_file(path)} for path in files],
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
