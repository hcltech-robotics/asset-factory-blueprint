from __future__ import annotations

import json
import os
import sys
import types
from importlib import resources
from pathlib import Path
from typing import Any

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
except ModuleNotFoundError:
    np = None
    Image = ImageDraw = ImageFilter = None

from asset_factory_blueprint.skills.base import ToolResult
from asset_factory_blueprint.utils.checksums import sha256_file


SELECTOR_KEYS = ("material_name", "material_family", "segment_id", "prim_path")
DEFAULT_SEGMENT_COLOURS = {
    "body": (214, 42, 36),
    "handle": (32, 42, 52),
    "spout": (46, 118, 214),
    "trim": (178, 164, 132),
    "rims": (178, 164, 132),
    "lid": (194, 184, 158),
    "metal_lid": (194, 184, 158),
    "metal_base": (176, 176, 168),
    "knob": (222, 210, 178),
    "logo": (34, 94, 82),
    "feature": (44, 164, 120),
}


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def _normalise_mesh_records(source_meshes: Any, base_dir: Path) -> list[dict[str, str]]:
    records = source_meshes if isinstance(source_meshes, list) else [source_meshes]
    normalised: list[dict[str, str]] = []
    for index, item in enumerate(records):
        if isinstance(item, dict):
            raw_path = str(item.get("path") or item.get("source_path") or "")
            path = Path(raw_path)
            if raw_path and not path.is_absolute():
                path = base_dir / path
            segment_id = str(item.get("segment_id") or item.get("part_id") or path.stem or f"mesh_{index}")
            normalised.append(
                {
                    "path": str(path),
                    "prim_path": str(item.get("prim_path") or ""),
                    "segment_id": segment_id,
                    "material_name": str(item.get("material_name") or ""),
                    "material_family": str(item.get("material_family") or ""),
                }
            )
            continue
        path = Path(str(item))
        if not path.is_absolute():
            path = base_dir / path
        normalised.append(
            {
                "path": str(path),
                "prim_path": "",
                "segment_id": path.stem or f"mesh_{index}",
                "material_name": "",
                "material_family": "",
            }
        )
    return normalised


def _selector_matches(record: dict[str, str], selector: dict[str, Any]) -> bool:
    if selector.get("all") is True:
        return True
    present = False
    for key in SELECTOR_KEYS:
        if key not in selector:
            continue
        present = True
        allowed = {item.lower() for item in _as_list(selector.get(key))}
        actual = str(record.get(key, "")).lower()
        if actual not in allowed:
            return False
    return present


def _normalise_operations(raw_operations: Any) -> list[dict[str, Any]]:
    operations = raw_operations if isinstance(raw_operations, list) else [raw_operations] if raw_operations else []
    normalised: list[dict[str, Any]] = []
    for index, item in enumerate(operations):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("operation") or "smooth").lower()
        operation_id = str(item.get("operation_id") or item.get("id") or f"{kind}_{index + 1}")
        normalised.append(
            {
                "operation_id": operation_id,
                "kind": kind,
                "selector": item.get("selector") if isinstance(item.get("selector"), dict) else {},
                "amplitude_m": float(item.get("amplitude_m") or item.get("amplitude") or (-0.01 if kind == "dent" else 0.01)),
                "radius_m": float(item.get("radius_m") or item.get("radius") or 0.06),
                "count": int(item.get("count") or 4),
                "iterations": int(item.get("iterations") or 3),
                "seed": int(item.get("seed") or index + 1),
            }
        )
    return normalised


def _normalise_expected_segments(raw_segments: Any) -> list[dict[str, Any]]:
    if not raw_segments:
        raw_segments = ["body", "handle", "spout"]
    segments = raw_segments if isinstance(raw_segments, list) else [raw_segments]
    records: list[dict[str, Any]] = []
    for index, item in enumerate(segments):
        if isinstance(item, dict):
            segment_id = str(item.get("segment_id") or item.get("id") or item.get("label") or f"segment_{index + 1}")
            material_family = str(item.get("material_family") or item.get("material_name") or "painted_metal")
            label = str(item.get("label") or segment_id.replace("_", " "))
            prompt = str(item.get("prompt") or item.get("text_prompt") or label)
            aliases = [str(value) for value in _as_list(item.get("aliases"))]
            raw_colour = item.get("colour") or item.get("preview_colour")
            if raw_colour is not None:
                colour = tuple(int(value) for value in list(raw_colour)[:3])
            else:
                colour = DEFAULT_SEGMENT_COLOURS.get(segment_id, (70 + index * 37 % 150, 110, 190))
        else:
            segment_id = str(item)
            label = segment_id.replace("_", " ")
            material_family = "metal" if segment_id in {"spout", "trim", "rims"} else "painted_metal"
            prompt = label
            aliases = []
            colour = DEFAULT_SEGMENT_COLOURS.get(segment_id, (70 + index * 37 % 150, 110, 190))
        records.append(
            {
                "segment_id": segment_id,
                "label": label,
                "prompt": prompt,
                "aliases": aliases,
                "material_family": material_family,
                "colour": colour,
                "sort_order": index,
            }
        )
    return records


def _foreground_mask(image: Image.Image, *, prefer_rembg: bool = False) -> np.ndarray:
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    if prefer_rembg:
        try:
            from rembg import remove

            rgba = remove(image.convert("RGBA"))
            alpha = np.asarray(rgba, dtype=np.uint8)[:, :, 3]
            if int(alpha.max()) > 0:
                return alpha > 24
        except Exception:
            pass
    # Conservative light-background object cut. This is only used when no mask generator is available.
    distance_from_white = np.linalg.norm(255 - rgb.astype(np.int16), axis=2)
    mask = distance_from_white > 34
    return _clean_binary_mask(mask)


def _clean_binary_mask(mask: np.ndarray) -> np.ndarray:
    try:
        import cv2

        kernel = np.ones((5, 5), np.uint8)
        clean = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, kernel)
        clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, kernel)
        return clean.astype(bool)
    except Exception:
        return mask.astype(bool)


def _component_records(mask: np.ndarray, prefix: str) -> list[dict[str, Any]]:
    try:
        import cv2

        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    except Exception:
        return []
    h, w = mask.shape
    image_area = float(h * w)
    records: list[dict[str, Any]] = []
    for component_id in range(1, count):
        x, y, bw, bh, area = stats[component_id]
        if int(area) < max(64, int(image_area * 0.001)):
            continue
        component_mask = labels == component_id
        records.append(
            {
                "candidate_id": f"{prefix}_{component_id:02d}",
                "mask": component_mask,
                "bbox": [int(x), int(y), int(bw), int(bh)],
                "area": int(area),
                "area_ratio": float(area / image_area),
                "centre": [float((x + bw / 2.0) / w), float((y + bh / 2.0) / h)],
                "aspect": float(bw / max(1, bh)),
            }
        )
    return records


def _appearance_candidates(image: Image.Image, foreground: np.ndarray, max_segments: int) -> list[dict[str, Any]]:
    import cv2

    rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    red = foreground & (((hsv[:, :, 0] <= 12) | (hsv[:, :, 0] >= 168)) & (hsv[:, :, 1] > 60))
    green = foreground & (hsv[:, :, 0] >= 32) & (hsv[:, :, 0] <= 105) & (hsv[:, :, 1] > 24) & (hsv[:, :, 2] > 32)
    dark = foreground & (hsv[:, :, 2] < 95)
    bright_metal = foreground & (hsv[:, :, 1] < 80) & (hsv[:, :, 2] >= 80)
    candidates: list[dict[str, Any]] = []
    for name, mask in (("red", red), ("green", green), ("dark", dark), ("metal", bright_metal)):
        candidates.extend(_component_records(_clean_binary_mask(mask), name))
    if not candidates:
        candidates.extend(_component_records(foreground, "foreground"))
    candidates.sort(key=lambda item: item["area"], reverse=True)
    return candidates[: max(1, max_segments * 3)]


def _normalise_candidates_with_foreground(candidates: list[dict[str, Any]], foreground: np.ndarray) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    foreground_area = max(1, int(foreground.sum()))
    for item in candidates:
        mask = item["mask"].astype(bool)
        mask_area = int(mask.sum())
        if mask_area <= 0:
            continue
        overlap_area = int((mask & foreground).sum())
        overlap = overlap_area / float(mask_area)
        recall = overlap_area / float(foreground_area)
        if overlap < 0.25:
            inverse = (~mask) & foreground
            inverse_area = int(inverse.sum())
            if inverse_area > 0 and inverse_area / float(foreground_area) > recall:
                records.extend(_component_records(inverse, f"{item['candidate_id']}_foreground_inverse"))
                continue
        clipped = mask & foreground if overlap < 0.95 and recall > 0.02 else mask
        for component in _component_records(clipped, str(item["candidate_id"])):
            component["score"] = float(item.get("score", 0.0))
            records.append(component)
    records.sort(key=lambda item: (float(item.get("score", 0.0)), item["area"]), reverse=True)
    return records


def _sam_candidates(image: Image.Image, max_segments: int, cache_dir: Path | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    status: dict[str, Any] = {"backend": "transformers_sam", "status": "not_run"}
    try:
        from transformers import pipeline
    except Exception as exc:
        status.update({"status": "unavailable", "reason": str(exc)})
        return [], status
    try:
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        generator = pipeline(
            "mask-generation",
            model="facebook/sam-vit-base",
            device=-1,
            model_kwargs={"cache_dir": str(cache_dir)} if cache_dir else None,
        )
        raw = generator(image.convert("RGB"), points_per_batch=32)
        masks = raw.get("masks", []) if isinstance(raw, dict) else []
        scores = raw.get("scores", []) if isinstance(raw, dict) else []
        records: list[dict[str, Any]] = []
        for index, raw_mask in enumerate(masks):
            mask_array = np.asarray(raw_mask, dtype=bool)
            if mask_array.ndim > 2:
                mask_array = mask_array[:, :, 0]
            components = _component_records(mask_array, f"sam_{index:02d}")
            for item in components:
                item["score"] = float(scores[index]) if index < len(scores) else 0.0
            records.extend(components)
        records.sort(key=lambda item: (float(item.get("score", 0.0)), item["area"]), reverse=True)
        status.update({"status": "ready", "candidate_count": len(records)})
        return records[: max(1, max_segments * 4)], status
    except Exception as exc:
        status.update({"status": "blocked", "reason": str(exc)})
        return [], status


def _mask_records_for_segment(
    mask_array: np.ndarray,
    *,
    prefix: str,
    segment_id: str,
    label: str,
    score: float = 0.0,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in _component_records(mask_array.astype(bool), prefix):
        item["score"] = float(score)
        item["segment_id"] = segment_id
        item["label"] = label
        records.append(item)
    return records


def _install_pkg_resources_shim() -> None:
    try:
        import pkg_resources  # noqa: F401

        return
    except Exception:
        pass

    module = types.ModuleType("pkg_resources")

    def resource_filename(package_name: str, resource_name: str) -> str:
        return str(resources.files(package_name).joinpath(resource_name))

    module.resource_filename = resource_filename  # type: ignore[attr-defined]
    sys.modules["pkg_resources"] = module


def _official_sam31_candidates(
    image_path: Path,
    image: Image.Image,
    expected_segments: list[dict[str, Any]],
    max_segments: int,
    cache_dir: Path | None,
    checkpoint_path: Path | None,
    source_path: Path | None,
    work_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    status: dict[str, Any] = {
        "backend": "official_sam3.1",
        "status": "not_run",
        "model_ref": "facebook/sam3.1",
        "checkpoint": checkpoint_path.as_posix() if checkpoint_path else "facebook/sam3.1/sam3.1_multiplex.pt",
    }
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = str(cache_dir.parent)
        os.environ["HF_HUB_CACHE"] = str(cache_dir)
        os.environ["HUGGINGFACE_HUB_CACHE"] = str(cache_dir)
    if source_path and source_path.exists():
        source_path_string = str(source_path)
        if source_path_string not in sys.path:
            sys.path.insert(0, source_path_string)
    _install_pkg_resources_shim()

    try:
        from sam3.model_builder import build_sam3_predictor
    except Exception as exc:
        status.update({"status": "unavailable", "reason": f"sam3 package unavailable: {exc}"})
        return [], status

    try:
        frame_dir = work_dir / "sam31-single-frame"
        frame_dir.mkdir(parents=True, exist_ok=True)
        frame_path = frame_dir / "00000.jpg"
        image.save(frame_path)
        predictor = build_sam3_predictor(
            checkpoint_path=str(checkpoint_path) if checkpoint_path and checkpoint_path.exists() else None,
            version="sam3.1",
            compile=False,
            warm_up=False,
            use_fa3=False,
            use_rope_real=False,
            async_loading_frames=False,
            max_num_objects=16,
            multiplex_count=16,
        )
        session = predictor.handle_request({"type": "start_session", "resource_path": str(frame_dir)})
        session_id = session["session_id"]
        records: list[dict[str, Any]] = []
        for expected in expected_segments[:max_segments]:
            prompt = str(expected.get("prompt") or expected["label"])
            response = predictor.handle_request(
                {
                    "type": "add_prompt",
                    "session_id": session_id,
                    "frame_index": 0,
                    "text": prompt,
                    "output_prob_thresh": 0.24,
                }
            )
            output = response.get("outputs", {})
            masks = output.get("out_binary_masks", output.get("masks", []))
            scores = output.get("scores", [])
            if hasattr(masks, "detach"):
                masks = masks.detach().cpu().numpy()
            if hasattr(scores, "detach"):
                scores = scores.detach().cpu().numpy()
            for index, raw_mask in enumerate(masks):
                mask = np.asarray(raw_mask, dtype=bool)
                if mask.ndim > 2:
                    mask = mask.squeeze()
                if mask.shape != image.size[::-1]:
                    mask_image = Image.fromarray(mask.astype(np.uint8) * 255, mode="L")
                    mask = np.asarray(mask_image.resize(image.size, Image.Resampling.NEAREST), dtype=np.uint8) > 0
                score = float(scores[index]) if index < len(scores) else 0.0
                records.extend(
                    _mask_records_for_segment(
                        mask,
                        prefix=f"sam31_{expected['segment_id']}_{index:02d}",
                        segment_id=str(expected["segment_id"]),
                        label=str(expected["label"]),
                        score=score,
                    )
                )
        predictor.handle_request({"type": "close_session", "session_id": session_id, "run_gc_collect": False})
        records.sort(key=lambda item: (str(item.get("segment_id", "")), float(item.get("score", 0.0)), item["area"]), reverse=True)
        status.update({"status": "ready", "candidate_count": len(records)})
        return records[: max(1, max_segments * 6)], status
    except Exception as exc:
        status.update({"status": "blocked", "reason": str(exc)})
        return [], status


def _ultralytics_sam3_candidates(
    image_path: Path,
    image: Image.Image,
    expected_segments: list[dict[str, Any]],
    max_segments: int,
    cache_dir: Path | None,
    model_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    status: dict[str, Any] = {
        "backend": "ultralytics_sam3_semantic",
        "status": "not_run",
        "model_path": model_path.as_posix(),
    }
    try:
        from ultralytics.models.sam.predict import SAM3SemanticPredictor
        from ultralytics.utils import SETTINGS
    except Exception as exc:
        status.update({"status": "unavailable", "reason": str(exc)})
        return [], status

    try:
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
            SETTINGS.update({"weights_dir": cache_dir})
        prompts = [str(item.get("prompt") or item["label"]) for item in expected_segments[:max_segments]]
        predictor = SAM3SemanticPredictor(
            overrides={
                "model": str(model_path),
                "conf": 0.12,
                "iou": 0.5,
                "imgsz": int(max(image.size)),
                "task": "segment",
                "mode": "predict",
                "save": False,
                "verbose": False,
            }
        )
        predictor.set_prompts({"text": prompts})
        results = predictor(source=str(image_path))
        records: list[dict[str, Any]] = []
        for result in results:
            if result.masks is None or result.boxes is None:
                continue
            masks = result.masks.data.detach().cpu().numpy()
            boxes = result.boxes.data.detach().cpu().numpy()
            for index, raw_mask in enumerate(masks):
                class_index = int(boxes[index, 5]) if index < len(boxes) and boxes.shape[1] >= 6 else index
                if class_index < 0 or class_index >= len(expected_segments):
                    continue
                expected = expected_segments[class_index]
                score = float(boxes[index, 4]) if index < len(boxes) and boxes.shape[1] >= 5 else 0.0
                mask = np.asarray(raw_mask, dtype=bool)
                if mask.shape != image.size[::-1]:
                    mask_image = Image.fromarray(mask.astype(np.uint8) * 255, mode="L")
                    mask = np.asarray(mask_image.resize(image.size, Image.Resampling.NEAREST), dtype=np.uint8) > 0
                records.extend(
                    _mask_records_for_segment(
                        mask,
                        prefix=f"sam3_{expected['segment_id']}_{index:02d}",
                        segment_id=str(expected["segment_id"]),
                        label=str(expected["label"]),
                        score=score,
                    )
                )
        records.sort(key=lambda item: (float(item.get("score", 0.0)), item["area"]), reverse=True)
        status.update({"status": "ready", "candidate_count": len(records)})
        return records[: max(1, max_segments * 6)], status
    except Exception as exc:
        status.update({"status": "blocked", "reason": str(exc)})
        return [], status


def _select_semantic_masks(
    image: Image.Image,
    expected_segments: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    foreground: np.ndarray,
) -> list[dict[str, Any]]:
    h, w = foreground.shape
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    candidates = _normalise_candidates_with_foreground(candidates, foreground)
    if not candidates:
        candidates = _component_records(foreground, "foreground")

    def _iou(mask_a: np.ndarray, mask_b: np.ndarray) -> float:
        union = float(np.logical_or(mask_a, mask_b).sum())
        if not union:
            return 0.0
        return float(np.logical_and(mask_a, mask_b).sum()) / union

    def choose(segment_id: str) -> dict[str, Any] | None:
        # near-duplicate masks under fresh candidate ids must not satisfy a
        # second label; every selected segment has to be a distinct region
        available = [
            item
            for item in candidates
            if item["candidate_id"] not in used
            and all(_iou(item["mask"], chosen["mask"]) < 0.6 for chosen in selected)
        ]
        if not available:
            return None
        prompted = [item for item in available if str(item.get("segment_id", "")) == segment_id]
        if prompted:
            return max(prompted, key=lambda item: (float(item.get("score", 0.0)), item["area"]))
        if segment_id == "body":
            red = [item for item in available if str(item["candidate_id"]).startswith(("red", "green"))]
            central = [
                item
                for item in red or available
                if 0.08 < item["centre"][0] < 0.62 and item["centre"][1] > 0.28 and item["aspect"] > 0.25
            ]
            return max(central or red or available, key=lambda item: item["area"])
        if segment_id == "spout":
            upper = [
                item
                for item in available
                if item["centre"][1] < 0.55 and "foreground_inverse" not in str(item["candidate_id"])
            ]
            dark_upper = [
                item
                for item in upper
                if str(item["candidate_id"]).startswith(("dark", "metal", "sam")) and item["aspect"] > 1.15
            ]
            pool = dark_upper or upper or available
            return max(pool, key=lambda item: item["aspect"] * max(item["area_ratio"], 0.001))
        if segment_id == "handle":
            side = [
                item
                for item in available
                if (item["centre"][0] < 0.22 or item["centre"][0] > 0.42)
                and "foreground_inverse" not in str(item["candidate_id"])
            ]
            dark_side = [item for item in side if str(item["candidate_id"]).startswith(("dark", "green", "sam"))]
            pool = dark_side or side or available
            return max(pool, key=lambda item: (1.0 / max(item["aspect"], 0.05)) * max(item["area_ratio"], 0.001))
        if segment_id in {"trim", "rims", "lid", "metal_lid", "metal_base"}:
            flat = [item for item in available if item["aspect"] > 1.4]
            if segment_id == "metal_base":
                lower = [item for item in flat or available if item["centre"][1] > 0.72]
                return max(lower or flat or available, key=lambda item: item["area"])
            upper = [item for item in flat or available if item["centre"][1] < 0.45]
            return max(upper or flat or available, key=lambda item: item["area"])
        if segment_id in {"cap", "closure"}:

            def bbox_fill(item: dict[str, Any]) -> float:
                _, _, width, height = item["bbox"]
                return float(item["area"]) / float(max(width * height, 1))

            upper_small = [
                item
                for item in available
                if item["centre"][1] < 0.35 and 0.002 < item["area_ratio"] < 0.08
            ]
            # a closure is a compact region, not a specular sliver
            compact = [item for item in upper_small if bbox_fill(item) > 0.35 and 0.4 < item["aspect"] < 2.5]
            return max(
                compact or upper_small or available,
                key=lambda item: float(item.get("score", 0.0)) + item["area_ratio"],
            )
        if segment_id == "knob":
            upper_central = [
                item
                for item in available
                if 0.25 < item["centre"][0] < 0.62 and item["centre"][1] < 0.28 and item["area_ratio"] < 0.06
            ]
            return max(upper_central or available, key=lambda item: float(item.get("score", 0.0)) + item["area_ratio"])
        if segment_id == "logo":
            front = [
                item
                for item in available
                if 0.12 < item["centre"][0] < 0.55 and 0.32 < item["centre"][1] < 0.68 and item["area_ratio"] < 0.08
            ]
            dark_front = [
                item
                for item in front
                if str(item["candidate_id"]).startswith(("dark", "sam")) and 0.001 < item["area_ratio"] < 0.04
            ]
            return max(dark_front or front or available, key=lambda item: float(item.get("score", 0.0)) + item["area_ratio"])
        return max(available, key=lambda item: item["area"])

    for expected in expected_segments:
        segment_id = str(expected["segment_id"])
        chosen = choose(segment_id)
        if chosen is None:
            continue
        used.add(str(chosen["candidate_id"]))
        selected.append(
            {
                "segment_id": segment_id,
                "label": expected["label"],
                "material_family": expected["material_family"],
                "candidate_id": chosen["candidate_id"],
                "mask": chosen["mask"],
                "bbox": chosen["bbox"],
                "area_ratio": chosen["area_ratio"],
                "confidence": min(0.92, max(0.35, 0.48 + chosen["area_ratio"] * 2.4)),
                "preview_colour": list(expected["colour"]),
            }
        )
    if not selected:
        selected.append(
            {
                "segment_id": "object",
                "label": "object",
                "material_family": "unknown",
                "candidate_id": "foreground",
                "mask": foreground,
                "bbox": [0, 0, int(w), int(h)],
                "area_ratio": float(foreground.mean()),
                "confidence": 0.35,
                "preview_colour": [120, 130, 140],
            }
        )
    return selected


def _write_segmentation_images(
    image: Image.Image,
    selected: list[dict[str, Any]],
    output_dir: Path,
) -> tuple[list[dict[str, Any]], list[Path]]:
    source = image.convert("RGB")
    overlay = source.copy()
    colour_sheet = Image.new("RGB", source.size, (245, 245, 245))
    condition = source.convert("RGBA")
    overlay_draw = ImageDraw.Draw(overlay, "RGBA")
    files: list[Path] = []
    records: list[dict[str, Any]] = []
    mask_dir = output_dir / "segments"
    mask_dir.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(selected):
        segment_id = str(item["segment_id"])
        colour = tuple(int(value) for value in item["preview_colour"])
        mask = item["mask"].astype(np.uint8) * 255
        mask_image = Image.fromarray(mask, mode="L")
        mask_path = mask_dir / f"{segment_id}_mask.png"
        mask_image.save(mask_path)
        files.append(mask_path)
        colour_layer = Image.new("RGB", source.size, colour)
        colour_sheet.paste(colour_layer, mask=mask_image)
        condition_layer = Image.new("RGBA", source.size, (*colour, 142))
        condition = Image.composite(Image.alpha_composite(condition, condition_layer), condition, mask_image)
        contour = mask_image.filter(ImageFilter.FIND_EDGES)
        overlay_draw.bitmap((0, 0), contour, fill=(*colour, 210))
        records.append(
            {
                "segment_id": segment_id,
                "label": item["label"],
                "material_family": item["material_family"],
                "mask_path": mask_path.as_posix(),
                "bbox": item["bbox"],
                "area_ratio": item["area_ratio"],
                "confidence": item["confidence"],
                "candidate_id": item["candidate_id"],
                "preview_colour": list(colour),
                "sort_order": index,
            }
        )
    overlay_path = output_dir / "segmentation-overlay.png"
    colour_path = output_dir / "semantic-colour-mask.png"
    condition_path = output_dir / "partcrafter-conditioning-image.png"
    condition_draw = ImageDraw.Draw(condition, "RGBA")
    for item in selected:
        colour = tuple(int(value) for value in item["preview_colour"])
        mask = item["mask"].astype(np.uint8) * 255
        contour = Image.fromarray(mask, mode="L").filter(ImageFilter.FIND_EDGES)
        condition_draw.bitmap((0, 0), contour, fill=(*colour, 245))
    overlay.save(overlay_path)
    colour_sheet.save(colour_path)
    condition.convert("RGB").save(condition_path)
    files.extend([overlay_path, colour_path, condition_path])
    return records, files


def asset_image_segmentation_prior(params: dict[str, Any]) -> ToolResult:
    if np is None or Image is None:
        return ToolResult(
            success=False,
            error="numpy and pillow are required for image segmentation priors; install them into the runtime environment",
            validation_status="blocked",
        )
    image_path = Path(str(params.get("image_path") or params.get("image") or ""))
    if not image_path.exists():
        return ToolResult(success=False, error="image_path is required and must exist", validation_status="blocked")
    output_dir = Path(str(params.get("output_dir") or image_path.parent / "segmentation_prior"))
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    asset_id = str(params.get("asset_id") or image_path.stem)
    expected_segments = _normalise_expected_segments(params.get("expected_segments"))
    max_segments = int(params.get("max_segments") or len(expected_segments) or 3)
    method = str(params.get("method") or "auto").lower()
    cache_dir_raw = params.get("cache_dir") or os.environ.get("AFB_HF_CACHE") or os.environ.get("HF_HOME")
    cache_dir = Path(str(cache_dir_raw)) if cache_dir_raw else Path(".cache/afb/hf")
    image = Image.open(image_path).convert("RGB")
    foreground = _foreground_mask(image, prefer_rembg=bool(params.get("use_rembg")))
    sam_status: dict[str, Any] = {"backend": "transformers_sam", "status": "not_requested"}
    candidates: list[dict[str, Any]] = []
    backend = "appearance_cv"
    appearance = _appearance_candidates(image, foreground, max_segments=max_segments)
    if method in {"sam3.1", "sam31", "official_sam3.1", "official_sam31"}:
        checkpoint_raw = params.get("sam3_checkpoint_path") or params.get("checkpoint_path")
        checkpoint_path = Path(str(checkpoint_raw)) if checkpoint_raw else None
        source_raw = params.get("sam3_source_path") or os.environ.get("SAM3_SOURCE_PATH")
        source_path = Path(str(source_raw)) if source_raw else None
        candidates, sam_status = _official_sam31_candidates(
            image_path,
            image,
            expected_segments,
            max_segments=max_segments,
            cache_dir=cache_dir,
            checkpoint_path=checkpoint_path,
            source_path=source_path,
            work_dir=output_dir,
        )
        if candidates:
            backend = "official_sam3.1_prompted"
    elif method in {"sam3", "ultralytics_sam3", "prompted_sam3"}:
        model_raw = params.get("sam3_model_path") or params.get("model_path") or "sam3.pt"
        model_path = Path(str(model_raw))
        candidates, sam_status = _ultralytics_sam3_candidates(
            image_path,
            image,
            expected_segments,
            max_segments=max_segments,
            cache_dir=cache_dir,
            model_path=model_path,
        )
        if candidates:
            backend = "ultralytics_sam3_prompted_plus_appearance_cv"
    elif method in {"auto", "sam", "transformers_sam"}:
        candidates, sam_status = _sam_candidates(image, max_segments=max_segments, cache_dir=cache_dir)
        if candidates:
            backend = "transformers_sam_plus_appearance_cv"
    fallback_methods = {
        "auto",
        "appearance",
        "appearance_cv",
        "sam",
        "transformers_sam",
        "sam3",
        "ultralytics_sam3",
        "prompted_sam3",
        "sam3.1",
        "sam31",
        "official_sam3.1",
        "official_sam31",
    }
    if not candidates and method in fallback_methods and bool(params.get("allow_fallback", True)):
        candidates = appearance
        backend = "appearance_cv"
    elif candidates:
        candidates = candidates + appearance
    selected = _select_semantic_masks(image, expected_segments, candidates, foreground)
    segment_records, files = _write_segmentation_images(image, selected, output_dir)
    conditioning_image = output_dir / "partcrafter-conditioning-image.png"
    manifest = {
        "id": f"{asset_id}_segmentation_prior",
        "asset_id": asset_id,
        "status": "proposal",
        "source_image": image_path.as_posix(),
        "segmentation_backend": backend,
        "sam_status": sam_status,
        "expected_segments": [
            {key: value for key, value in item.items() if key != "colour"} for item in expected_segments
        ],
        "segments": segment_records,
        "partcrafter_bias": {
            "mode": "conditioning_image_plus_part_count",
            "image_path": conditioning_image.as_posix(),
            "num_parts": len(segment_records),
            "reason": "PartCrafter does not expose a native mask input; this uses a painted semantic conditioning image and explicit part count.",
        },
    }
    report = {
        **manifest,
        "candidate_count": len(candidates),
        "foreground_coverage": float(foreground.mean()),
        "artifacts": [path.as_posix() for path in files],
    }
    manifest_path = output_dir / "segmentation-prior-manifest.json"
    report_path = output_dir / "segmentation-prior-report.json"
    checksums_path = output_dir / "segmentation-prior-checksums.json"
    _write_json(manifest_path, manifest)
    _write_json(report_path, report)
    checksum_files = files + [manifest_path, report_path]
    _write_json(
        checksums_path,
        {
            "files": [
                {"path": path.as_posix(), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
                for path in checksum_files
            ]
        },
    )
    artefacts = [path.as_posix() for path in checksum_files + [checksums_path]]
    warnings = []
    if not backend.startswith(("transformers_sam", "ultralytics_sam3", "official_sam3.1")):
        warnings.append("prompted SAM backend did not produce masks; used appearance_cv segmentation prior")
    return ToolResult(
        success=True,
        data=report,
        warnings=warnings,
        artefacts=artefacts,
        proposals=[manifest],
        validation_status="proposal",
    )


def _mesh_stats(mesh: Any) -> dict[str, Any]:
    bounds = mesh.bounds.tolist() if getattr(mesh, "bounds", None) is not None else []
    try:
        euler_number = int(mesh.euler_number)
    except Exception:
        euler_number = None
    return {
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "is_watertight": bool(mesh.is_watertight),
        "is_winding_consistent": bool(mesh.is_winding_consistent),
        "euler_number": euler_number,
        "bounds": bounds,
        "surface_area": float(mesh.area),
    }


def _load_as_mesh(path: Path, trimesh: Any) -> Any:
    loaded = trimesh.load(path, force="scene")
    if isinstance(loaded, trimesh.Scene):
        geometries = [geometry for geometry in loaded.geometry.values() if len(geometry.vertices) and len(geometry.faces)]
        if not geometries:
            raise ValueError(f"no mesh geometry found in {path}")
        return trimesh.util.concatenate(tuple(geometries))
    return loaded


def _heal_mesh(mesh: Any, trimesh: Any) -> dict[str, Any]:
    actions: list[str] = []
    mesh.remove_unreferenced_vertices()
    actions.append("remove_unreferenced_vertices")
    if hasattr(mesh, "merge_vertices"):
        mesh.merge_vertices()
        actions.append("merge_vertices")
    if hasattr(mesh, "nondegenerate_faces"):
        mesh.update_faces(mesh.nondegenerate_faces())
        actions.append("remove_degenerate_faces")
    elif hasattr(mesh, "remove_degenerate_faces"):
        mesh.remove_degenerate_faces()
        actions.append("remove_degenerate_faces")
    if hasattr(mesh, "unique_faces"):
        mesh.update_faces(mesh.unique_faces())
        actions.append("remove_duplicate_faces")
    trimesh.repair.fix_normals(mesh, multibody=True)
    actions.append("fix_normals")
    filled = bool(trimesh.repair.fill_holes(mesh))
    if filled:
        actions.append("fill_holes")
    mesh.remove_unreferenced_vertices()
    return {"actions": actions, "filled_holes": filled}


def _apply_smooth(mesh: Any, operation: dict[str, Any], trimesh: Any) -> dict[str, Any]:
    before = mesh.vertices.copy()
    iterations = max(1, int(operation["iterations"]))
    trimesh.smoothing.filter_laplacian(mesh, lamb=0.25, iterations=iterations, volume_constraint=False)
    displacement = np.linalg.norm(mesh.vertices - before, axis=1)
    return {
        "iterations": iterations,
        "max_displacement_m": float(displacement.max(initial=0.0)),
        "mean_displacement_m": float(displacement.mean() if len(displacement) else 0.0),
    }


def _apply_radial_displacement(mesh: Any, operation: dict[str, Any], kind: str) -> dict[str, Any]:
    vertices = mesh.vertices.copy()
    if not len(vertices):
        return {"count": 0, "max_displacement_m": 0.0}
    radius = max(float(operation["radius_m"]), 1e-6)
    count = min(max(1, int(operation["count"])), len(vertices))
    amplitude = abs(float(operation["amplitude_m"]))
    if kind == "dent":
        amplitude *= -1.0
    rng = np.random.default_rng(int(operation["seed"]))
    centre_indices = rng.choice(len(vertices), size=count, replace=False)
    normals = mesh.vertex_normals
    total_weight = np.zeros(len(vertices), dtype=np.float64)
    for centre_index in centre_indices:
        centre = vertices[int(centre_index)]
        distance = np.linalg.norm(vertices - centre, axis=1)
        total_weight += np.exp(-((distance / radius) ** 2) * 2.0)
    total_weight = np.clip(total_weight, 0.0, 1.0)
    displacement = normals * (amplitude * total_weight[:, None])
    mesh.vertices = vertices + displacement
    applied = np.linalg.norm(displacement, axis=1)
    return {
        "count": int(count),
        "radius_m": radius,
        "amplitude_m": amplitude,
        "max_displacement_m": float(applied.max(initial=0.0)),
        "mean_displacement_m": float(applied.mean() if len(applied) else 0.0),
    }


def _apply_operation(mesh: Any, operation: dict[str, Any], trimesh: Any) -> dict[str, Any]:
    kind = str(operation["kind"])
    if kind == "smooth":
        return _apply_smooth(mesh, operation, trimesh)
    if kind in {"dent", "bump"}:
        return _apply_radial_displacement(mesh, operation, kind)
    return {"skipped": True, "reason": f"unsupported operation kind: {kind}"}


def asset_mesh_condition(params: dict[str, Any]) -> ToolResult:
    try:
        import trimesh
    except Exception as exc:
        return ToolResult(
            success=False,
            error=f"trimesh is required for asset_mesh_condition: {exc}",
            validation_status="blocked",
        )

    base_dir = Path(str(params.get("base_dir") or ".")).resolve()
    output_dir_raw = params.get("output_dir")
    if not output_dir_raw:
        return ToolResult(success=False, error="output_dir is required", validation_status="blocked")
    output_dir = Path(str(output_dir_raw))
    if not output_dir.is_absolute():
        output_dir = base_dir / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    source_meshes = _normalise_mesh_records(params.get("source_meshes") or params.get("meshes") or [], base_dir)
    if not source_meshes:
        return ToolResult(success=False, error="source_meshes is required", validation_status="blocked")
    operations = _normalise_operations(params.get("operations") or params.get("mesh_operations"))

    warnings: list[str] = []
    mesh_results: list[dict[str, Any]] = []
    operation_results: list[dict[str, Any]] = []
    output_files: list[Path] = []
    for record in source_meshes:
        source_path = Path(record["path"])
        if not source_path.exists():
            warnings.append(f"missing mesh: {source_path}")
            continue
        mesh = _load_as_mesh(source_path, trimesh)
        before = _mesh_stats(mesh)
        healing = _heal_mesh(mesh, trimesh)
        applied_operations: list[str] = []
        for operation in operations:
            selector = operation.get("selector", {})
            if not selector:
                operation_results.append(
                    {
                        "operation_id": operation["operation_id"],
                        "mesh": str(source_path),
                        "status": "skipped",
                        "reason": "operation selector is required for material-aware mesh edits",
                    }
                )
                continue
            if not _selector_matches(record, selector):
                continue
            result = _apply_operation(mesh, operation, trimesh)
            applied_operations.append(str(operation["operation_id"]))
            operation_results.append(
                {
                    "operation_id": operation["operation_id"],
                    "mesh": str(source_path),
                    "segment_id": record["segment_id"],
                    "material_name": record["material_name"],
                    "material_family": record["material_family"],
                    "status": "applied" if not result.get("skipped") else "skipped",
                    "result": result,
                }
            )
        mesh.remove_unreferenced_vertices()
        after = _mesh_stats(mesh)
        output_mesh = output_dir / f"{source_path.stem}_conditioned.glb"
        mesh.export(output_mesh)
        output_files.append(output_mesh)
        mesh_results.append(
            {
                **record,
                "source_path": str(source_path),
                "output_path": str(output_mesh),
                "healing": healing,
                "operations_applied": applied_operations,
                "before": before,
                "after": after,
            }
        )

    applied_ids = {item["operation_id"] for item in operation_results if item.get("status") == "applied"}
    for operation in operations:
        if operation["operation_id"] not in applied_ids:
            warnings.append(f"operation did not match a mesh selector: {operation['operation_id']}")

    asset_id = str(params.get("asset_id") or "asset")
    report = {
        "id": f"{asset_id}_mesh_condition_report",
        "asset_id": asset_id,
        "status": "pass" if output_files else "blocked",
        "policy": "healing applies to all listed source meshes; shape operations require explicit material or segment selectors",
        "source_mesh_count": len(source_meshes),
        "output_mesh_count": len(output_files),
        "operations": operations,
        "mesh_results": mesh_results,
        "operation_results": operation_results,
        "warnings": warnings,
    }
    manifest = {
        "id": f"{asset_id}_mesh_condition_manifest",
        "asset_id": asset_id,
        "status": "proposal" if output_files else "blocked",
        "mesh_healing": "deterministic trimesh repair pass",
        "material_aware_operations": operations,
        "outputs": [str(path) for path in output_files],
    }
    checksum_records = [
        {"path": str(path), "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
        for path in output_files
    ]
    report_path = Path(str(params.get("report_path") or output_dir / "mesh-condition-report.json"))
    manifest_path = Path(str(params.get("manifest_path") or output_dir / "mesh-condition-manifest.json"))
    checksums_path = Path(str(params.get("checksums_path") or output_dir / "mesh-condition-checksums.json"))
    if not report_path.is_absolute():
        report_path = base_dir / report_path
    if not manifest_path.is_absolute():
        manifest_path = base_dir / manifest_path
    if not checksums_path.is_absolute():
        checksums_path = base_dir / checksums_path
    _write_json(report_path, report)
    _write_json(manifest_path, manifest)
    _write_json(checksums_path, {"files": checksum_records})
    artefacts = [str(path) for path in output_files] + [str(report_path), str(manifest_path), str(checksums_path)]
    return ToolResult(
        success=bool(output_files),
        data=report,
        warnings=warnings,
        artefacts=artefacts,
        proposals=[manifest],
        validation_status="proposal" if output_files else "blocked",
    )
