from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from asset_factory_blueprint.reconstruction_backends import (  # noqa: E402
    build_backend_run_manifest,
    run_adapter_manifest,
)
from asset_factory_blueprint.mesh_topology import (  # noqa: E402
    QUALITY_INVARIANT_FIELDS,
    TOPOLOGY_INVARIANT_FIELDS,
    exact_mesh_metrics,
    mesh_quality_checks,
    resolved_quality_policy,
)
from asset_factory_blueprint.services.mesh_verification import governance_mesh_verify  # noqa: E402
from asset_factory_blueprint.state import create_project  # noqa: E402


BENCHMARK_PATH = Path(__file__).with_name("benchmark.json")
PROJECT_NAME = "benchmark 1 reproducibility"
PROJECT_SLUG = "benchmark_1_reproducibility"
_REMBG_SESSION: Any | None = None


def default_source_root() -> Path | None:
    configured = os.environ.get("AFB_REPRODUCIBILITY_ROOT", "").strip()
    return Path(configured) if configured else None


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(payload: dict[str, Any]) -> str:
    # Match the adapter's checksum serialisation exactly.  These manifests are
    # consumed by ``run_adapter_manifest``, which uses the default JSON
    # separators when it validates the signed payload.
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def load_benchmark() -> dict[str, Any]:
    return json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))


def image_raw_path(source_root: Path, source: dict[str, Any]) -> Path:
    return source_root / str(source["path"])


def usd_raw_path(source_root: Path, source: dict[str, Any]) -> Path:
    return source_root / "sources" / "usd" / "industrial" / str(source["path"])


def project_dir() -> Path:
    return ROOT / "projects" / PROJECT_SLUG


def image_lock_entries(source_root: Path, benchmark: dict[str, Any]) -> list[dict[str, Any]]:
    entries = []
    for source in benchmark["image_sources"]:
        raw_path = image_raw_path(source_root, source)
        if not raw_path.is_file():
            raise FileNotFoundError(f"missing image source: {raw_path}")
        entries.append(
            {
                "id": source["id"],
                "kind": "image",
                "path": raw_path.as_posix(),
                "sha256": sha256_file(raw_path),
                "size_bytes": raw_path.stat().st_size,
                "source_uri": source["source_uri"],
                "download_uri": source["download_uri"],
                "licence_expression": source["licence_expression"],
                "creator": source["creator"],
            }
        )
    return entries


def usd_lock_entries(source_root: Path, benchmark: dict[str, Any]) -> list[dict[str, Any]]:
    entries = []
    for source in benchmark["usd_sources"]:
        raw_path = usd_raw_path(source_root, source)
        if not raw_path.is_file():
            raise FileNotFoundError(f"missing USD source: {raw_path}")
        entries.append(
            {
                "id": source["id"],
                "kind": "usd",
                "path": raw_path.as_posix(),
                "sha256": sha256_file(raw_path),
                "size_bytes": raw_path.stat().st_size,
                "source_uri": "https://docs.omniverse.nvidia.com/usd/latest/usd_content_samples/downloadable_packs.html",
                "archive": (source_root / "sources" / "usd" / "Industrial_NVD_10012.zip").as_posix(),
                "licence_expression": "NVIDIA Omniverse asset pack terms",
            }
        )
    return entries


def source_lock(source_root: Path, benchmark: dict[str, Any]) -> dict[str, Any]:
    archive = source_root / str(benchmark["source_cache_layout"]["usd_archive"])
    if not archive.is_file():
        raise FileNotFoundError(f"missing USD archive: {archive}")
    return {
        "id": benchmark["id"] + "-sources",
        "version": "1.0",
        "created_at": now(),
        "raw_source_root": source_root.as_posix(),
        "archive": {
            "path": archive.as_posix(),
            "sha256": sha256_file(archive),
            "size_bytes": archive.stat().st_size,
        },
        "sources": image_lock_entries(source_root, benchmark) + usd_lock_entries(source_root, benchmark),
    }


def source_manifest(source_root: Path, benchmark: dict[str, Any], lock_path: Path) -> dict[str, Any]:
    project = project_dir()
    stage_dir = project / "source-assets" / "reproducibility"
    assets: list[dict[str, Any]] = []
    rights: list[dict[str, Any]] = []
    for source in benchmark["image_sources"]:
        raw_path = image_raw_path(source_root, source)
        target = stage_dir / raw_path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(raw_path, target)
        relative_target = target.relative_to(project).as_posix()
        checksum = sha256_file(raw_path)
        rights_id = f"rights-{source['id']}"
        assets.append(
            {
                "source_path": raw_path.as_posix(),
                "project_copy_path": relative_target,
                "source_sha256": checksum,
                "copy_sha256": sha256_file(target),
                "suffix": target.suffix.lower(),
                "size_bytes": target.stat().st_size,
                "status": "copied",
                "rights_id": rights_id,
                "source_uri": source["source_uri"],
            }
        )
        rights.append(
            {
                "rights_id": rights_id,
                "source_id": source["id"],
                "rights_status": "cleared",
                "licence_expression": source["licence_expression"],
                "terms_uri": source["source_uri"],
                "creator": source["creator"],
                "revision": None,
                "attribution": f"{source['creator']} ({source['source_uri']})",
                "permitted_uses": ["benchmarking", "analysis", "redistribution"],
                "redistribution_allowed": True,
                "derivatives_allowed": True,
                "privacy_status": "cleared",
                "consent_evidence_ids": [],
                "evidence_ids": [f"source-{source['id']}"],
                "expires_at": None,
                "extensions": {"download_uri": source["download_uri"]},
            }
        )
    lock_checksum = sha256_file(lock_path)
    return {
        "id": "benchmark-1-reproducibility-image-sources",
        "version": "2.0",
        "status": "not_validated",
        "asset_id": "reproducibility-image-cohort",
        "project_id": PROJECT_SLUG,
        "evidence": [
            {
                "evidence_id": "source-lock",
                "kind": "source_lock",
                "uri": lock_path.as_posix(),
                "checksum": lock_checksum,
            }
        ],
        "source_assets": assets,
        "local_copies": [asset["project_copy_path"] for asset in assets],
        "source_assets_mutated": False,
        "rights_status": "cleared",
        "unit_policy": "source image pixels are immutable evidence; reconstruction scale remains unknown",
        "source_rights": rights,
        "extensions": {"benchmark_id": benchmark["id"], "cohort": "image"},
    }


def prepare(args: argparse.Namespace) -> int:
    source_root = Path(args.source_root).resolve()
    benchmark = load_benchmark()
    lock_path = source_root / "sources.lock.json"
    lock = source_lock(source_root, benchmark)
    write_json(lock_path, lock)
    create_project(PROJECT_NAME)
    manifest_path = project_dir() / "manifests" / "source-asset-manifest.json"
    write_json(manifest_path, source_manifest(source_root, benchmark, lock_path))
    print(json.dumps({"source_lock": lock_path.as_posix(), "source_manifest": manifest_path.as_posix()}, indent=2))
    return 0


def write_manifest_checksum(path: Path, payload: dict[str, Any]) -> None:
    payload["manifest_checksum"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "manifest_checksum"}
    )
    write_json(path, payload)
    write_json(
        path.with_suffix(".sha256.json"),
        {"algorithm": "sha256", "path": path.as_posix(), "sha256": sha256_file(path)},
    )


def no_agent_review() -> dict[str, Any]:
    """Describe a legacy run that predates mandatory mesh verification."""
    return {
        "configured": False,
        "reviewer_id": None,
        "review_attempts": 0,
        "mesh_rejections": 0,
        "inference_resubmissions": 0,
        "final_decision": "not_run",
    }


def configured_agent_review(benchmark: dict[str, Any]) -> dict[str, Any]:
    policy = benchmark["mesh_verification"]
    if policy.get("mandatory") is not True:
        raise ValueError("mesh verification must be mandatory for benchmark 1")
    return {
        "configured": True,
        "reviewer_id": str(policy["agent_id"]),
        "provider": str(policy["provider"]),
        "model": str(policy["model"]),
        "review_attempts": 0,
        "mesh_rejections": 0,
        "inference_resubmissions": 0,
        "final_decision": "pending",
    }


def execution_trace(manifest_path: Path, benchmark: dict[str, Any]) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "id": f"{manifest_path.parent.name}-execution-trace",
        "root_run_id": str(manifest.get("run_id") or manifest.get("id") or manifest_path.parent.name),
        "created_at": now(),
        "manifest": manifest_path.as_posix(),
        "inference_attempts": 1,
        "agent_review": configured_agent_review(benchmark),
        "events": [
            {
                "sequence": 1,
                "timestamp": now(),
                "kind": "inference_submitted",
                "inference_attempt": 1,
                "trigger": "initial_submission",
            },
            {
                "sequence": 2,
                "timestamp": now(),
                "kind": "mesh_verifier_configured",
                "reviewer_id": benchmark["mesh_verification"]["agent_id"],
                "review_attempts": 0,
                "mesh_rejections": 0,
                "inference_resubmissions": 0,
            },
        ],
    }


def finalise_execution_trace(trace: dict[str, Any], result: dict[str, Any]) -> None:
    status = str(result.get("status", "blocked"))
    execution_status = str(result.get("execution_status", "not_started"))
    trace["completed_at"] = now()
    trace["final_status"] = status
    trace["execution_status"] = execution_status
    trace["events"].append(
        {
            "sequence": len(trace["events"]) + 1,
            "timestamp": now(),
            "kind": "inference_completed" if execution_status == "completed" else "inference_ended",
            "inference_attempt": trace["inference_attempts"],
            "status": status,
            "execution_status": execution_status,
            "result_manifest": result.get("result_manifest", ""),
        }
    )


def fail_execution_trace(trace: dict[str, Any], error: Exception) -> None:
    trace["completed_at"] = now()
    trace["final_status"] = "error"
    trace["execution_status"] = "failed"
    trace["events"].append(
        {
            "sequence": len(trace["events"]) + 1,
            "timestamp": now(),
            "kind": "inference_error",
            "inference_attempt": trace["inference_attempts"],
            "error_type": type(error).__name__,
            "message": str(error),
        }
    )


def backfilled_execution_trace(manifest_path: Path, result_path: Path, result: dict[str, Any]) -> dict[str, Any]:
    observed_at = str(result.get("updated_at") or now())
    review = no_agent_review()
    return {
        "id": f"{manifest_path.parent.name}-execution-trace",
        "created_at": now(),
        "trace_mode": "backfilled_from_result_manifest",
        "manifest": manifest_path.as_posix(),
        "result_manifest": result_path.as_posix(),
        "inference_attempts": 1,
        "agent_review": review,
        "final_status": str(result.get("status", "blocked")),
        "execution_status": str(result.get("execution_status", "not_started")),
        "events": [
            {
                "sequence": 1,
                "timestamp": observed_at,
                "kind": "inference_attempt_observed",
                "inference_attempt": 1,
                "evidence": result_path.as_posix(),
            },
            {
                "sequence": 2,
                "timestamp": observed_at,
                "kind": "agent_review_not_configured",
                "review_attempts": 0,
                "mesh_rejections": 0,
                "inference_resubmissions": 0,
            },
            {
                "sequence": 3,
                "timestamp": observed_at,
                "kind": "inference_completed_observed",
                "inference_attempt": 1,
                "status": str(result.get("status", "blocked")),
                "execution_status": str(result.get("execution_status", "not_started")),
            },
        ],
    }


def review_summary(execution: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "review_attempts": sum(entry["agent_review"]["review_attempts"] for entry in execution),
        "mesh_rejections": sum(entry["agent_review"]["mesh_rejections"] for entry in execution),
        "inference_resubmissions": sum(entry["agent_review"]["inference_resubmissions"] for entry in execution),
    }


def execution_entry(
    manifest_path: Path,
    result: dict[str, Any],
    trace_path: Path,
    trace: dict[str, Any],
    duration_seconds: float | None,
) -> dict[str, Any]:
    return {
        "manifest": manifest_path.as_posix(),
        "run_id": trace.get("root_run_id") or result.get("run_id", ""),
        "status": result.get("status", "blocked"),
        "execution_status": result.get("execution_status", "not_started"),
        "duration_seconds": duration_seconds,
        "result_manifest": result.get("result_manifest", ""),
        "execution_trace": trace_path.as_posix(),
        "inference_attempts": trace["inference_attempts"],
        "agent_review": trace["agent_review"],
    }


def _asset_crop(source: Path, target: Path, crop_box: list[int]) -> None:
    if len(crop_box) != 4:
        raise ValueError("asset crop box must contain left, top, right and bottom")
    with Image.open(source) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
        left, top, right, bottom = (int(value) for value in crop_box)
        if left < 0 or top < 0 or right > image.width or bottom > image.height or left >= right or top >= bottom:
            raise ValueError(f"asset crop box is outside source bounds: {crop_box}")
        target.parent.mkdir(parents=True, exist_ok=True)
        image.crop((left, top, right, bottom)).save(target)


def _review_workspace_source(run_dir: Path, input_asset: Path, crop_box: list[int] | None = None) -> Path:
    source_dir = run_dir / "source-assets" / "benchmark"
    source_dir.mkdir(parents=True, exist_ok=True)
    target = source_dir / input_asset.name
    if crop_box:
        target = target.with_name(f"{target.stem}-asset-crop.jpg")
        _asset_crop(input_asset, target, crop_box)
        return target
    if not target.exists() or sha256_file(target) != sha256_file(input_asset):
        shutil.copy2(input_asset, target)
    return target


def _download_file(uri: str, target: Path) -> None:
    if target.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(uri, headers={"User-Agent": "asset-factory-blueprint-benchmark/1.0"})
    try:
        response = urllib.request.urlopen(request, timeout=180)
    except urllib.error.HTTPError as error:
        if error.code != 429 or "upload.wikimedia.org" not in uri:
            raise
        parsed = urllib.parse.urlsplit(uri)
        proxied = "https://images.weserv.nl/?" + urllib.parse.urlencode(
            {"url": parsed.netloc + parsed.path, "w": 1600, "output": "jpg"}
        )
        response = urllib.request.urlopen(
            urllib.request.Request(proxied, headers={"User-Agent": "asset-factory-blueprint-benchmark/1.0"}),
            timeout=180,
        )
    with response, temporary.open("wb") as stream:
        shutil.copyfileobj(response, stream)
    temporary.replace(target)


def _alternate_image_path(source_root: Path, source: dict[str, Any]) -> Path:
    return source_root / "sources" / "alternate-images" / f"{source['id']}.jpg"


def prepare_adaptive_sources(args: argparse.Namespace) -> int:
    source_root = Path(args.source_root).resolve()
    benchmark = load_benchmark()
    records: list[dict[str, Any]] = []
    for source in benchmark["image_sources"]:
        alternate = dict(source["alternate_source"])
        target = _alternate_image_path(source_root, source)
        _download_file(str(alternate["download_uri"]), target)
        records.append(
            {
                "source_id": source["id"],
                "path": target.as_posix(),
                "sha256": sha256_file(target),
                "size_bytes": target.stat().st_size,
                **alternate,
            }
        )
    lock_path = source_root / "alternate-sources.lock.json"
    write_json(
        lock_path,
        {
            "id": "benchmark-1-adaptive-alternate-sources",
            "created_at": now(),
            "sources": records,
        },
    )
    print(json.dumps({"alternate_source_lock": lock_path.as_posix(), "source_count": len(records)}, indent=2))
    return 0


def _remove_background(source: Path, target: Path) -> None:
    global _REMBG_SESSION

    from PIL import Image
    from rembg import new_session, remove

    if _REMBG_SESSION is None:
        _REMBG_SESSION = new_session("u2net")
    foreground = remove(Image.open(source).convert("RGBA"), session=_REMBG_SESSION)
    alpha = foreground.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        raise ValueError(f"foreground removal produced an empty image for {source}")
    cropped = foreground.crop(bbox)
    side = max(cropped.size)
    margin = max(16, int(side * 0.12))
    canvas_side = side + margin * 2
    canvas = Image.new("RGBA", (canvas_side, canvas_side), (0, 0, 0, 0))
    canvas.paste(cropped, ((canvas_side - cropped.width) // 2, (canvas_side - cropped.height) // 2), cropped)
    canvas.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target)


def _conditioning_image(
    run_dir: Path,
    strategy: str,
    original: Path,
    alternate: Path,
    attempt: int,
    original_crop_box: list[int] | None = None,
) -> tuple[Path, dict[str, Any]]:
    source = alternate if "alternate" in strategy else original
    suffix = ".png" if strategy.startswith("rmbg_") else source.suffix.lower()
    target = run_dir / "conditioning" / f"attempt-{attempt:02d}-{strategy}{suffix}"
    if strategy.startswith("rmbg_"):
        transform_source = source
        transforms: list[str] = []
        if strategy == "rmbg_original" and original_crop_box:
            transform_source = run_dir / "conditioning" / f"attempt-{attempt:02d}-asset-crop.png"
            _asset_crop(source, transform_source, original_crop_box)
            transforms.append("asset_crop")
        _remove_background(transform_source, target)
        transforms.extend(["foreground_removal", "alpha_crop", "square_padding", "resize_max_1600"])
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        transforms = ["alternate_source_selection"] if strategy == "alternate_photo" else ["none"]
    record = {
        "attempt": attempt,
        "strategy": strategy,
        "source_path": source.as_posix(),
        "source_sha256": sha256_file(source),
        "conditioning_path": target.as_posix(),
        "conditioning_sha256": sha256_file(target),
        "transforms": transforms,
    }
    write_json(target.with_suffix(target.suffix + ".json"), record)
    return target, record


def _next_remediation(findings: list[dict[str, Any]], used: set[str]) -> str | None:
    tags = {str(item.get("defect_tag") or "") for item in findings}
    if "source_mismatch" in tags:
        ordered = ["rmbg_original", "alternate_photo", "rmbg_alternate"]
    elif tags.intersection({"missing_parts", "wrong_proportions", "wrong_scale"}):
        ordered = ["alternate_photo", "rmbg_alternate", "rmbg_original"]
    elif tags.intersection({"extra_geometry", "fragmented_parts"}):
        ordered = ["rmbg_original", "alternate_photo", "rmbg_alternate"]
    else:
        ordered = ["rmbg_original", "alternate_photo", "rmbg_alternate"]
    return next((strategy for strategy in ordered if strategy not in used), None)


def _review_unavailable(record: dict[str, Any]) -> bool:
    reason = str(record.get("decision_reason") or "")
    return reason.startswith("vision provider call failed:") or reason == "reviewer response was not a valid JSON verdict"


def _generated_asset(result: dict[str, Any]) -> Path:
    output_manifest = Path(str(result.get("output_manifest") or ""))
    if not output_manifest.is_file():
        raise FileNotFoundError("reconstruction result has no output manifest")
    reconstruction = json.loads(output_manifest.read_text(encoding="utf-8"))
    generated = Path(str(reconstruction.get("generated_asset") or ""))
    if not generated.is_file():
        raise FileNotFoundError("reconstruction output manifest has no generated mesh")
    return generated


def _append_trace_event(trace: dict[str, Any], kind: str, **payload: Any) -> None:
    trace["events"].append(
        {
            "sequence": len(trace["events"]) + 1,
            "timestamp": now(),
            "kind": kind,
            **payload,
        }
    )


def _preserve_mesh_review_evidence(run_dir: Path, review_index: int) -> Path:
    source = run_dir / "reports" / "mesh-verification"
    target = run_dir / "reports" / f"mesh-verification-attempt-{review_index:02d}"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    for relative in (
        Path("reports/mesh-verification-vlm-review.json"),
        Path("reports/mesh-verification-blind-identity.json"),
        Path("manifests/mesh-verification-record.json"),
    ):
        evidence = run_dir / relative
        if evidence.is_file():
            shutil.copy2(evidence, target / evidence.name)
    return target


def _resubmission_manifest(
    base_manifest_path: Path,
    benchmark: dict[str, Any],
    resubmission: int,
    input_asset: Path,
    remediation: dict[str, Any],
) -> Path:
    original = json.loads(base_manifest_path.read_text(encoding="utf-8"))
    retry_dir = base_manifest_path.parent / f"resubmission-{resubmission:02d}"
    retry_path = retry_dir / "external-model-run-manifest.json"
    payload = build_backend_run_manifest(
        str(benchmark["image_reconstruction"]["backend_id"]),
        output_path=retry_path,
        input_manifest=str(original["input_manifest"]),
        output_manifest=(retry_dir / "reconstruction-manifest.json").as_posix(),
        asset_id=str(original.get("asset_id") or ""),
        project_id=str(original.get("project_id") or ""),
    )
    reproducibility = dict(benchmark["image_reconstruction"]["reproducibility"])
    reproducibility["seed"] = int(reproducibility["seed"]) + (
        resubmission * int(benchmark["mesh_verification"]["retry_seed_stride"])
    )
    payload.update(
        {
            "id": f"{original['id']}-resubmission-{resubmission:02d}",
            "run_id": f"{original['run_id']}-resubmission-{resubmission:02d}",
            "input_asset": input_asset.as_posix(),
            "reproducibility": reproducibility,
            "benchmark": {
                **dict(original.get("benchmark", {})),
                "root_run_id": str(original["run_id"]),
                "inference_resubmission": resubmission,
                "conditioning_remediation": remediation,
            },
        }
    )
    payload["allowed_paths"] = list(original["allowed_paths"])
    payload["runtime_env"]["AFB_RECONSTRUCTION_INPUT_ASSET"] = input_asset.as_posix()
    write_manifest_checksum(retry_path, payload)
    return retry_path


def run_mandatory_mesh_verification(
    base_manifest_path: Path,
    initial_result: dict[str, Any],
    benchmark: dict[str, Any],
    trace: dict[str, Any],
    source_root: Path,
) -> dict[str, Any]:
    policy = benchmark["mesh_verification"]
    original = json.loads(base_manifest_path.read_text(encoding="utf-8"))
    run_dir = base_manifest_path.parent
    original_input = Path(str(original["input_asset"]))
    source_id = str(original.get("benchmark", {}).get("source_id") or original_input.stem)
    source_config = next(source for source in benchmark["image_sources"] if source["id"] == source_id)
    original_crop_box = source_config.get("asset_crop_box")
    alternate_input = _alternate_image_path(source_root, source_config)
    _download_file(str(source_config["alternate_source"]["download_uri"]), alternate_input)
    current_conditioning, current_remediation = _conditioning_image(
        run_dir,
        "original",
        original_input,
        alternate_input,
        1,
        original_crop_box,
    )
    current_review_source = _review_workspace_source(run_dir, current_conditioning, original_crop_box)
    used_strategies = {"original"}
    result = initial_result
    final_record: dict[str, Any] = {}
    maximum_attempts = min(
        int(policy["max_review_attempts"]),
        int(policy.get("max_total_inference_attempts", policy["max_inference_resubmissions"] + 1)),
        int(policy["max_inference_resubmissions"]) + 1,
    )
    review_index = 0

    def submit_next_inference(strategy: str, trigger: str, review_decision: str) -> dict[str, Any]:
        nonlocal current_conditioning, current_remediation, current_review_source
        resubmission = trace["agent_review"]["inference_resubmissions"] + 1
        next_attempt = trace["inference_attempts"] + 1
        used_strategies.add(strategy)
        current_conditioning, current_remediation = _conditioning_image(
            run_dir,
            strategy,
            original_input,
            alternate_input,
            next_attempt,
            original_crop_box,
        )
        current_review_source = _review_workspace_source(run_dir, current_conditioning)
        retry_path = _resubmission_manifest(
            base_manifest_path,
            benchmark,
            resubmission,
            current_conditioning,
            current_remediation,
        )
        trace["agent_review"]["inference_resubmissions"] = resubmission
        trace["inference_attempts"] = next_attempt
        _append_trace_event(
            trace,
            "inference_resubmitted",
            inference_attempt=next_attempt,
            trigger=trigger,
            review_decision=review_decision,
            manifest=retry_path.as_posix(),
            conditioning=current_remediation,
        )
        try:
            next_result = run_adapter_manifest(retry_path, dry_run=False)
        except Exception as error:
            next_result = {
                "status": "error",
                "execution_status": "failed",
                "error": str(error),
                "result_manifest": "",
            }
        _append_trace_event(
            trace,
            "inference_completed",
            inference_attempt=next_attempt,
            status=next_result.get("status", "blocked"),
            execution_status=next_result.get("execution_status", "not_started"),
            result_manifest=next_result.get("result_manifest", ""),
            error=next_result.get("error", ""),
        )
        return next_result

    while trace["inference_attempts"] <= maximum_attempts:
        try:
            candidate = _generated_asset(result)
        except FileNotFoundError as error:
            _append_trace_event(
                trace,
                "inference_output_rejected",
                inference_attempt=trace["inference_attempts"],
                reason=str(error),
            )
            if trace["inference_attempts"] >= maximum_attempts:
                trace["agent_review"]["final_decision"] = "inference_attempts_exhausted"
                break
            strategy = _next_remediation(final_record.get("findings", []), used_strategies)
            if strategy is None:
                trace["agent_review"]["final_decision"] = "remediations_exhausted"
                break
            result = submit_next_inference(strategy, "inference_failure", "inference_failed")
            continue

        review_index += 1
        if review_index > int(policy["max_review_attempts"]):
            trace["agent_review"]["final_decision"] = "review_attempts_exhausted"
            break
        review_seed = int(policy["seed"]) + (review_index - 1) * int(policy["retry_seed_stride"])
        review = governance_mesh_verify(
            {
                "project": run_dir.as_posix(),
                "asset_id": str(original.get("asset_id") or ""),
                "project_id": str(original.get("project_id") or ""),
                "candidate_path": candidate.as_posix(),
                "provider": str(policy["provider"]),
                "model": str(policy["model"]),
                "temperature": float(policy["temperature"]),
                "seed": review_seed,
                "quality_policy": dict(policy.get("quality_policy", {})),
                "full_surface_backend_root": os.environ.get("TRELLIS2_ROOT", ""),
                "source_image_paths": [current_review_source.as_posix()],
                "asset_intent": source_id.replace("_", " "),
                "asset_aliases": list(source_config.get("verification_aliases", [])),
                "dry_run": False,
                "attempt": review_index - 1,
            }
        )
        final_record = review.data
        review_evidence = _preserve_mesh_review_evidence(run_dir, review_index)
        trace["agent_review"]["review_attempts"] += 1
        decision = str(final_record.get("decision") or "blocked")
        review_unavailable = _review_unavailable(final_record)
        _append_trace_event(
            trace,
            "mesh_review_completed",
            review_attempt=review_index,
            inference_attempt=trace["inference_attempts"],
            candidate_checksum=final_record.get("candidate", {}).get("checksum", ""),
            decision=decision,
            seed=review_seed,
            conditioning=current_remediation,
            renderer=final_record.get("render_bundle", {}).get("renderer", ""),
            evidence_directory=review_evidence.as_posix(),
            defect_tags=sorted(
                {
                    str(item.get("defect_tag") or "")
                    for item in final_record.get("findings", [])
                    if item.get("defect_tag")
                }
            ),
        )
        if review_unavailable:
            trace["agent_review"]["final_decision"] = "review_unavailable"
            _append_trace_event(
                trace,
                "mesh_review_unavailable",
                review_attempt=review_index,
                inference_attempt=trace["inference_attempts"],
                reason=final_record.get("decision_reason", "vision provider call failed"),
            )
            break
        if review.success and decision == "approve":
            trace["agent_review"]["final_decision"] = "approve"
            break
        trace["agent_review"]["mesh_rejections"] += 1
        _append_trace_event(
            trace,
            "mesh_rejected",
            review_attempt=review_index,
            inference_attempt=trace["inference_attempts"],
            decision=decision,
        )
        if decision not in {"revise_local", "regenerate"}:
            trace["agent_review"]["final_decision"] = "blocked"
            break
        if trace["inference_attempts"] >= maximum_attempts:
            trace["agent_review"]["final_decision"] = "resubmissions_exhausted"
            break
        strategy = _next_remediation(final_record.get("findings", []), used_strategies)
        if strategy is None:
            trace["agent_review"]["final_decision"] = "remediations_exhausted"
            break
        result = submit_next_inference(strategy, "mesh_rejection", decision)
    result["mesh_verification"] = final_record
    result["status"] = "validated" if trace["agent_review"]["final_decision"] == "approve" else "blocked"
    trace["final_status"] = result["status"]
    return result


def plan_image_runs(args: argparse.Namespace) -> int:
    source_root = Path(args.source_root).resolve()
    benchmark = load_benchmark()
    manifest_path = project_dir() / "manifests" / "source-asset-manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("run prepare before planning image runs")
    project = json.loads((project_dir() / "project.json").read_text(encoding="utf-8"))
    settings = benchmark["image_reconstruction"]["reproducibility"]
    planned: list[str] = []
    for source in benchmark["image_sources"]:
        staged = project_dir() / "source-assets" / "reproducibility" / Path(source["path"]).name
        for repeat in range(1, int(benchmark["repeat_count"]) + 1):
            run_dir = source_root / "runs" / "images" / source["id"] / f"repeat-{repeat:02d}"
            run_path = run_dir / "external-model-run-manifest.json"
            payload = build_backend_run_manifest(
                "trellisv2",
                output_path=run_path,
                input_manifest=manifest_path.as_posix(),
                output_manifest=(run_dir / "reconstruction-manifest.json").as_posix(),
                asset_id=f"{source['id']}-repeat-{repeat:02d}",
                project_id=str(project["project_id"]),
            )
            payload.update(
                {
                    "id": f"benchmark-1-{source['id']}-repeat-{repeat:02d}",
                    "run_id": f"benchmark-1-{source['id']}-repeat-{repeat:02d}",
                    "input_asset": staged.as_posix(),
                    "reproducibility": settings,
                    "benchmark": {
                        "id": benchmark["id"],
                        "cohort": "image",
                        "source_id": source["id"],
                        "repeat": repeat,
                    },
                }
            )
            payload["allowed_paths"] = ["projects", "artifacts", ".cache/afb", source_root.as_posix()]
            payload["runtime_env"]["AFB_RECONSTRUCTION_INPUT_ASSET"] = staged.as_posix()
            write_manifest_checksum(run_path, payload)
            planned.append(run_path.as_posix())
    plan_path = source_root / "runs" / "images" / "plan.json"
    write_json(plan_path, {"id": benchmark["id"], "created_at": now(), "manifests": planned})
    print(json.dumps({"plan": plan_path.as_posix(), "run_count": len(planned)}, indent=2))
    return 0


def run_image_runs(args: argparse.Namespace) -> int:
    source_root = Path(args.source_root).resolve()
    benchmark = load_benchmark()
    configured_agent_review(benchmark)
    plan_path = source_root / "runs" / "images" / "plan.json"
    if not plan_path.is_file():
        raise FileNotFoundError("run plan-images before running image reconstruction")
    execution: list[dict[str, Any]] = []
    for manifest_ref in json.loads(plan_path.read_text(encoding="utf-8"))["manifests"]:
        manifest_path = Path(manifest_ref)
        trace_path = manifest_path.with_name("execution-trace.json")
        trace = execution_trace(manifest_path, benchmark)
        write_json(trace_path, trace)
        started = time.monotonic()
        try:
            result = run_adapter_manifest(manifest_path, dry_run=False)
            finalise_execution_trace(trace, result)
            result = run_mandatory_mesh_verification(manifest_path, result, benchmark, trace, source_root)
        except Exception as error:
            fail_execution_trace(trace, error)
            write_json(trace_path, trace)
            raise
        trace["completed_at"] = now()
        write_json(trace_path, trace)
        execution.append(
            execution_entry(
                manifest_path,
                result,
                trace_path,
                trace,
                round(time.monotonic() - started, 3),
            )
        )
    summary_path = source_root / "runs" / "images" / "execution-summary.json"
    write_json(
        summary_path,
        {
            "id": "benchmark-1-image-execution",
            "created_at": now(),
            "runs": execution,
            "agent_review_summary": review_summary(execution),
        },
    )
    print(json.dumps({"execution_summary": summary_path.as_posix(), "run_count": len(execution)}, indent=2))
    return 0


def _adaptive_baseline(
    source_root: Path,
    benchmark: dict[str, Any],
    source: dict[str, Any],
    run_dir: Path,
) -> tuple[Path, dict[str, Any], Path]:
    original_manifest_path = (
        source_root / "runs" / "images" / str(source["id"]) / "repeat-01" / "external-model-run-manifest.json"
    )
    original_result_path = original_manifest_path.with_name("external-model-run-manifest.result.json")
    if not original_result_path.is_file():
        raise FileNotFoundError(f"missing baseline reconstruction result: {original_result_path}")
    original_result = json.loads(original_result_path.read_text(encoding="utf-8"))
    baseline_mesh = _generated_asset(original_result)
    candidate = run_dir / "outputs" / "asset.glb"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(baseline_mesh, candidate)
    reconstruction_path = run_dir / "reconstruction-manifest.json"
    write_json(
        reconstruction_path,
        {
            "id": f"adaptive-{source['id']}-baseline",
            "status": "proposal",
            "generated_asset": candidate.as_posix(),
            "baseline_source": baseline_mesh.as_posix(),
            "baseline_checksum": sha256_file(baseline_mesh),
        },
    )
    project = json.loads((project_dir() / "project.json").read_text(encoding="utf-8"))
    input_asset = project_dir() / "source-assets" / "reproducibility" / Path(source["path"]).name
    manifest_path = run_dir / "external-model-run-manifest.json"
    payload = build_backend_run_manifest(
        str(benchmark["image_reconstruction"]["backend_id"]),
        output_path=manifest_path,
        input_manifest=(project_dir() / "manifests" / "source-asset-manifest.json").as_posix(),
        output_manifest=reconstruction_path.as_posix(),
        asset_id=f"{source['id']}-adaptive",
        project_id=str(project["project_id"]),
    )
    payload.update(
        {
            "id": f"benchmark-1-{source['id']}-adaptive",
            "run_id": f"benchmark-1-{source['id']}-adaptive",
            "input_asset": input_asset.as_posix(),
            "reproducibility": dict(benchmark["image_reconstruction"]["reproducibility"]),
            "benchmark": {
                "id": benchmark["id"],
                "cohort": "adaptive-image-rerun",
                "source_id": source["id"],
                "baseline_repeat": 1,
            },
        }
    )
    payload["allowed_paths"] = ["projects", "artifacts", ".cache/afb", source_root.as_posix()]
    payload["runtime_env"]["AFB_RECONSTRUCTION_INPUT_ASSET"] = input_asset.as_posix()
    write_manifest_checksum(manifest_path, payload)
    result = {
        "run_id": payload["run_id"],
        "status": "proposal",
        "execution_status": "completed",
        "output_manifest": reconstruction_path.as_posix(),
        "result_manifest": (run_dir / "external-model-run-manifest.result.json").as_posix(),
        "baseline_reused": True,
    }
    write_json(Path(result["result_manifest"]), result)
    return manifest_path, result, input_asset


def _adaptive_rerun_gallery(adaptive_root: Path, summary: list[dict[str, Any]]) -> Path:
    panel_width = 390
    panel_height = 198
    label_width = 180
    row_height = 270
    maximum_attempts = max((int(item.get("inference_attempts") or 1) for item in summary), default=1)
    canvas = Image.new(
        "RGB",
        (label_width + panel_width * maximum_attempts + 20, 70 + row_height * len(summary)),
        "white",
    )
    draw = ImageDraw.Draw(canvas)
    draw.text((20, 18), "Benchmark 1 adaptive mesh verification reruns", fill="black")
    draw.text((20, 40), "Each panel compares the conditioning image with the full-surface candidate mesh", fill="black")

    for row_index, item in enumerate(summary):
        source_id = str(item["source_id"])
        y = 70 + row_index * row_height
        draw.text((20, y + 12), source_id.replace("_", " "), fill="black")
        draw.text(
            (20, y + 34),
            f"{item['agent_review']['mesh_rejections']} rejected, {item['inference_attempts']} attempt(s)",
            fill="black",
        )
        trace_path = Path(str(item["execution_trace"]))
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        reviews = [event for event in trace["events"] if event.get("kind") == "mesh_review_completed"]
        for review in reviews:
            review_attempt = int(review["review_attempt"])
            inference_attempt = int(review["inference_attempt"])
            evidence = Path(str(review["evidence_directory"])) / "source-candidate-comparison.png"
            if not evidence.is_file():
                continue
            x = label_width + (inference_attempt - 1) * panel_width
            with Image.open(evidence) as raw:
                image = ImageOps.exif_transpose(raw).convert("RGB")
                image.thumbnail((panel_width - 10, panel_height), Image.Resampling.LANCZOS)
                canvas.paste(image, (x + (panel_width - image.width) // 2, y + 56))
            strategy = str(review.get("conditioning", {}).get("strategy") or "unknown")
            draw.text(
                (x + 8, y + 16),
                f"Inference {inference_attempt}, review {review_attempt}: {strategy}",
                fill="black",
            )
            draw.text((x + 8, y + 35), f"Decision: {review.get('decision', 'unknown')}", fill="black")

    target = adaptive_root / "adaptive-rerun-gallery.png"
    canvas.save(target, format="PNG", optimize=True)
    return target


def _adaptive_attempt_comparison(adaptive_root: Path, summary: list[dict[str, Any]]) -> Path:
    metric_names = (
        "component_count",
        "euler_characteristic",
        "genus_total",
        "genus_defined",
        "watertight",
        "winding_consistent",
        "boundary_edge_count",
        "boundary_loop_count",
        "non_manifold_edge_count",
        "orientation_conflict_edge_count",
        "degenerate_face_count",
        "duplicate_face_count",
        "interior_face_count",
    )
    assets: list[dict[str, Any]] = []
    for item in summary:
        trace = json.loads(Path(str(item["execution_trace"])).read_text(encoding="utf-8"))
        attempts: list[dict[str, Any]] = []
        for event in trace["events"]:
            if event.get("kind") != "mesh_review_completed":
                continue
            evidence_dir = Path(str(event["evidence_directory"]))
            diagnostics = json.loads((evidence_dir / "diagnostics.json").read_text(encoding="utf-8"))
            metrics = diagnostics.get("metrics", {})
            attempts.append(
                {
                    "inference_attempt": event["inference_attempt"],
                    "review_attempt": event["review_attempt"],
                    "conditioning_strategy": event.get("conditioning", {}).get("strategy", ""),
                    "candidate_checksum": event.get("candidate_checksum", ""),
                    "decision": event.get("decision", ""),
                    "metrics": {name: metrics.get(name) for name in metric_names},
                    "quality_failures": diagnostics.get("quality_failures", []),
                    "evidence_directory": evidence_dir.as_posix(),
                }
            )
        assets.append(
            {
                "source_id": item["source_id"],
                "status": item["status"],
                "inference_attempts": item["inference_attempts"],
                "mesh_rejections": item["agent_review"]["mesh_rejections"],
                "final_decision": item["agent_review"]["final_decision"],
                "reviewed_attempts": attempts,
            }
        )
    target = adaptive_root / "adaptive-comparison.json"
    write_json(
        target,
        {
            "id": "benchmark-1-adaptive-attempt-comparison",
            "created_at": now(),
            "comparison_basis": "exact topology and integrity metrics for every reviewed candidate",
            "assets": assets,
        },
    )
    return target


def run_adaptive_image_reruns(args: argparse.Namespace) -> int:
    source_root = Path(args.source_root).resolve()
    benchmark = load_benchmark()
    configured_agent_review(benchmark)
    prepare_adaptive_sources(args)
    adaptive_root = source_root / "runs" / "adaptive"
    requested_source = str(getattr(args, "source_id", "") or "")
    existing_by_source: dict[str, dict[str, Any]] = {}
    existing_summary = adaptive_root / "adaptive-summary.json"
    if requested_source and existing_summary.is_file():
        existing_payload = json.loads(existing_summary.read_text(encoding="utf-8"))
        existing_by_source = {str(item["source_id"]): item for item in existing_payload.get("runs", [])}
    summary: list[dict[str, Any]] = []
    for source in benchmark["image_sources"]:
        if requested_source and source["id"] != requested_source:
            continue
        run_dir = adaptive_root / str(source["id"])
        if run_dir.exists():
            archive = adaptive_root / "archive" / f"{source['id']}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(run_dir, archive)
        manifest_path, initial_result, _ = _adaptive_baseline(source_root, benchmark, source, run_dir)
        trace_path = run_dir / "execution-trace.json"
        trace = execution_trace(manifest_path, benchmark)
        trace["events"][0] = {
            "sequence": 1,
            "timestamp": now(),
            "kind": "baseline_candidate_reused",
            "inference_attempt": 1,
            "source_run": (
                source_root / "runs" / "images" / str(source["id"]) / "repeat-01"
            ).as_posix(),
        }
        write_json(trace_path, trace)
        started = time.monotonic()
        try:
            finalise_execution_trace(trace, initial_result)
            result = run_mandatory_mesh_verification(
                manifest_path,
                initial_result,
                benchmark,
                trace,
                source_root,
            )
        except Exception as error:
            fail_execution_trace(trace, error)
            result = {"status": "error", "execution_status": "failed", "error": str(error)}
        trace["completed_at"] = now()
        write_json(trace_path, trace)
        run_summary = {
            "source_id": source["id"],
            "status": result.get("status", "error"),
            "duration_seconds": round(time.monotonic() - started, 3),
            "inference_attempts": trace["inference_attempts"],
            "agent_review": trace["agent_review"],
            "execution_trace": trace_path.as_posix(),
            "final_mesh_verification": result.get("mesh_verification", {}),
            "error": result.get("error", ""),
        }
        summary.append(run_summary)
        existing_by_source[str(source["id"])] = run_summary
        ordered_summary = [
            existing_by_source[str(item["id"])]
            for item in benchmark["image_sources"]
            if str(item["id"]) in existing_by_source
        ]
        write_json(
            adaptive_root / "adaptive-summary.json",
            {
                "id": "benchmark-1-adaptive-image-rerun",
                "created_at": now(),
                "max_total_inference_attempts": benchmark["mesh_verification"]["max_total_inference_attempts"],
                "runs": ordered_summary,
            },
        )
    final_summary = [
        existing_by_source[str(item["id"])]
        for item in benchmark["image_sources"]
        if str(item["id"]) in existing_by_source
    ]
    gallery_path = _adaptive_rerun_gallery(adaptive_root, final_summary)
    comparison_path = _adaptive_attempt_comparison(adaptive_root, final_summary)
    summary_path = adaptive_root / "adaptive-summary.json"
    print(
        json.dumps(
            {
                "adaptive_summary": summary_path.as_posix(),
                "adaptive_gallery": gallery_path.as_posix(),
                "adaptive_comparison": comparison_path.as_posix(),
                "run_count": len(summary),
            },
            indent=2,
        )
    )
    return 0 if all(item["status"] != "error" for item in summary) else 1


def backfill_execution_traces(args: argparse.Namespace) -> int:
    source_root = Path(args.source_root).resolve()
    plan_path = source_root / "runs" / "images" / "plan.json"
    if not plan_path.is_file():
        raise FileNotFoundError("run plan-images before backfilling execution traces")
    execution: list[dict[str, Any]] = []
    for manifest_ref in json.loads(plan_path.read_text(encoding="utf-8"))["manifests"]:
        manifest_path = Path(manifest_ref)
        result_path = manifest_path.with_name("external-model-run-manifest.result.json")
        if not result_path.is_file():
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        trace_path = manifest_path.with_name("execution-trace.json")
        trace = backfilled_execution_trace(manifest_path, result_path, result)
        write_json(trace_path, trace)
        execution.append(execution_entry(manifest_path, result, trace_path, trace, None))
    summary_path = source_root / "runs" / "images" / "execution-summary.json"
    write_json(
        summary_path,
        {
            "id": "benchmark-1-image-execution",
            "created_at": now(),
            "trace_mode": "backfilled_from_result_manifests",
            "runs": execution,
            "agent_review_summary": review_summary(execution),
        },
    )
    print(json.dumps({"execution_summary": summary_path.as_posix(), "run_count": len(execution)}, indent=2))
    return 0


def mesh_statistics(path: Path, quality_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    import trimesh

    loaded = trimesh.load(path, force="scene")
    meshes = list(loaded.geometry.values()) if isinstance(loaded, trimesh.Scene) else [loaded]
    valid = [mesh for mesh in meshes if len(mesh.vertices) and len(mesh.faces)]
    mesh = valid[0] if len(valid) == 1 else trimesh.util.concatenate(tuple(valid))
    exact = exact_mesh_metrics(mesh)
    checks = mesh_quality_checks(exact, resolved_quality_policy(quality_policy))
    return {
        "sha256": sha256_file(path),
        "vertices": exact["vertex_count"],
        "faces": exact["face_count"],
        "components": exact["component_count"],
        "quality_checks": checks,
        "quality_gate_pass": all(item["status"] != "fail" for item in checks),
        **exact,
        "bounds": [[round(float(value), 7) for value in row] for row in mesh.bounds.tolist()],
        "extents": [round(float(value), 7) for value in mesh.extents.tolist()],
    }


def compare_invariants(
    meshes: list[dict[str, Any]],
    fields: tuple[str, ...],
    expected_count: int,
) -> dict[str, Any]:
    comparison: dict[str, Any] = {}
    for field in fields:
        values = [mesh.get(field) for mesh in meshes]
        keyed = {json.dumps(value, sort_keys=True, separators=(",", ":")): value for value in values}
        comparable = len(meshes) == expected_count and all(value is not None for value in values)
        comparison[field] = {
            "values": [keyed[key] for key in sorted(keyed)],
            "comparable": comparable,
            "matches": len(keyed) == 1 if comparable else None,
        }
    return comparison


def analyse(args: argparse.Namespace) -> int:
    source_root = Path(args.source_root).resolve()
    benchmark = load_benchmark()
    quality_policy = dict(benchmark["mesh_verification"].get("quality_policy", {}))
    execution_path = source_root / "runs" / "images" / "execution-summary.json"
    if not execution_path.is_file():
        raise FileNotFoundError("run run-images before analysing results")
    execution_payload = json.loads(execution_path.read_text(encoding="utf-8"))
    invalid = [
        entry["run_id"]
        for entry in execution_payload["runs"]
        if entry.get("agent_review", {}).get("configured") is not True
    ]
    if invalid:
        raise RuntimeError(
            "comparison refused because mandatory mesh verification is absent from: " + ", ".join(invalid)
        )
    runs: list[dict[str, Any]] = []
    for entry in execution_payload["runs"]:
        result_path = Path(entry["result_manifest"])
        if not result_path.is_file():
            runs.append({**entry, "mesh": None})
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        reconstruction_path = Path(result["output_manifest"])
        reconstruction = json.loads(reconstruction_path.read_text(encoding="utf-8"))
        generated = Path(reconstruction.get("generated_asset", ""))
        runs.append(
            {
                **entry,
                "mesh": mesh_statistics(generated, quality_policy) if generated.is_file() else None,
            }
        )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in runs:
        grouped[entry["run_id"].rsplit("-repeat-", 1)[0]].append(entry)
    comparison = {}
    expected_count = int(benchmark["repeat_count"])
    for source_id, entries in sorted(grouped.items()):
        meshes = [entry["mesh"] for entry in entries if entry["mesh"]]
        hashes = Counter(mesh["sha256"] for mesh in meshes)
        topology_invariants = compare_invariants(meshes, TOPOLOGY_INVARIANT_FIELDS, expected_count)
        quality_invariants = compare_invariants(meshes, QUALITY_INVARIANT_FIELDS, expected_count)
        topology_reproducible = bool(topology_invariants) and all(
            item["comparable"] and item["matches"] for item in topology_invariants.values()
        )
        failed_quality_checks = Counter(
            check["id"]
            for mesh in meshes
            for check in mesh["quality_checks"]
            if check["status"] == "fail"
        )
        comparison[source_id] = {
            "completed_runs": len(meshes),
            "unique_glb_hashes": len(hashes),
            "all_glb_hashes_identical": len(meshes) == expected_count and len(hashes) == 1,
            "byte_identity_is_informational": True,
            "topology_reproducible": topology_reproducible,
            "topology_invariants": topology_invariants,
            "quality_invariants": quality_invariants,
            "current_quality_policy": {
                "evaluated_runs": len(meshes),
                "passing_runs": sum(1 for mesh in meshes if mesh["quality_gate_pass"]),
                "rejecting_runs": sum(1 for mesh in meshes if not mesh["quality_gate_pass"]),
                "failed_check_counts": dict(sorted(failed_quality_checks.items())),
            },
            "vertices": sorted({mesh["vertices"] for mesh in meshes}),
            "faces": sorted({mesh["faces"] for mesh in meshes}),
            "components": sorted({mesh["components"] for mesh in meshes}),
            "watertight_values": sorted({mesh["watertight"] for mesh in meshes}),
            "duration_seconds": [
                entry["duration_seconds"] for entry in entries if entry["duration_seconds"] is not None
            ],
            "agent_review": {
                "review_attempts": sum(entry.get("agent_review", {}).get("review_attempts", 0) for entry in entries),
                "mesh_rejections": sum(entry.get("agent_review", {}).get("mesh_rejections", 0) for entry in entries),
                "inference_resubmissions": sum(
                    entry.get("agent_review", {}).get("inference_resubmissions", 0) for entry in entries
                ),
            },
        }
    report_path = source_root / "runs" / "images" / "comparison.json"
    write_json(
        report_path,
        {
            "id": "benchmark-1-image-comparison",
            "created_at": now(),
            "quality_policy": resolved_quality_policy(quality_policy),
            "runs": runs,
            "comparison": comparison,
        },
    )
    print(json.dumps({"comparison": report_path.as_posix(), "asset_count": len(comparison)}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run benchmark 1 reproducibility artefact preparation and analysis.")
    default_root = default_source_root()
    parser.add_argument(
        "--source-root",
        default=default_root.as_posix() if default_root else None,
        required=default_root is None,
        help="runtime benchmark workspace; defaults to AFB_REPRODUCIBILITY_ROOT",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in [
        "prepare",
        "prepare-adaptive-sources",
        "plan-images",
        "run-images",
        "run-adaptive",
        "backfill-traces",
        "analyse",
    ]:
        command_parser = subparsers.add_parser(command)
        if command == "run-adaptive":
            command_parser.add_argument("--source-id", choices=[
                "coffee_mug",
                "bentwood_chair",
                "hand_powered_drill",
                "wooden_table_lamp",
                "fire_extinguisher",
            ])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "prepare": prepare,
        "prepare-adaptive-sources": prepare_adaptive_sources,
        "plan-images": plan_image_runs,
        "run-images": run_image_runs,
        "run-adaptive": run_adaptive_image_reruns,
        "backfill-traces": backfill_execution_traces,
        "analyse": analyse,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
