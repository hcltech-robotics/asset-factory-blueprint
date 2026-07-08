from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

from asset_factory_blueprint.config import load_json
from asset_factory_blueprint.manifests import load_schema
from asset_factory_blueprint.skills.base import ToolResult
from asset_factory_blueprint.utils.checksums import sha256_file, sha256_text


POLICY_PATH = "configs/vlm-review-policy.json"
SEVERITIES = {"blocker", "major", "minor", "note"}
# hosted vision endpoints reject requests above five images with a bare 500
MAX_EVIDENCE_IMAGES = 5
SOURCE_IMAGE_LIMIT = 2


def _load_rubric(rubric_name: str) -> str:
    return (resources.files("asset_factory_blueprint") / "prompts" / rubric_name).read_text(encoding="utf-8")


def _is_source_glob(pattern: str) -> bool:
    return pattern.startswith("source-assets")


def _collect_evidence_images(project_dir: Path, globs: list[str], explicit: list[str] | None) -> tuple[list[Path], bool]:
    """Collect review evidence, stage outputs first, with a cap on source photos.

    Returns the image list and whether any stage-output image was found. Stage
    outputs must never be crowded out of the review by source photos.
    """
    if explicit:
        paths = [Path(item) for item in explicit if Path(item).exists()][:MAX_EVIDENCE_IMAGES]
        return paths, bool(paths)
    output_images: list[Path] = []
    source_images: list[Path] = []
    seen: set[str] = set()
    output_globs = [pattern for pattern in globs if not _is_source_glob(pattern)]
    source_globs = [pattern for pattern in globs if _is_source_glob(pattern)]
    for pattern in output_globs:
        for path in sorted(project_dir.glob(pattern)):
            key = path.as_posix()
            if path.is_file() and key not in seen:
                seen.add(key)
                output_images.append(path)
    for pattern in source_globs:
        for path in sorted(project_dir.glob(pattern)):
            key = path.as_posix()
            if path.is_file() and key not in seen:
                seen.add(key)
                source_images.append(path)
    combined = output_images[: MAX_EVIDENCE_IMAGES - min(SOURCE_IMAGE_LIMIT, len(source_images))]
    combined.extend(source_images[:SOURCE_IMAGE_LIMIT])
    has_outputs = bool(output_images) or not output_globs
    return combined[:MAX_EVIDENCE_IMAGES], has_outputs


TOTAL_PIXEL_BUDGET = 4_000_000


def _prepared_review_images(images: list[Path], project_dir: Path, stage_id: str, max_edge: int) -> list[Path]:
    """Downscale oversized evidence for transport; hosted vision endpoints reject
    requests whose images together exceed a total pixel budget with a bare 500.
    The originals stay the canonical evidence records; the derivatives only ride
    the provider call."""
    try:
        from PIL import Image
    except ImportError:
        return images
    sizes: list[tuple[int, int]] = []
    for path in images:
        try:
            with Image.open(path) as image:
                sizes.append(image.size)
        except OSError:
            sizes.append((0, 0))
    capped = [min(1.0, max_edge / float(max(size))) if max(size) else 1.0 for size in sizes]
    total = sum(int(w * f) * int(h * f) for (w, h), f in zip(sizes, capped))
    shrink = min(1.0, (TOTAL_PIXEL_BUDGET / float(total)) ** 0.5) if total else 1.0

    prepared: list[Path] = []
    derived_dir = project_dir / "reports" / "vlm-review-evidence" / stage_id
    for path, size, cap in zip(images, sizes, capped):
        factor = cap * shrink
        try:
            oversized = path.stat().st_size > 512_000
            if factor >= 1.0 and not oversized:
                prepared.append(path)
                continue
            with Image.open(path) as image:
                resized = image.convert("RGB").resize(
                    (max(256, int(size[0] * factor)), max(256, int(size[1] * factor)))
                )
                derived_dir.mkdir(parents=True, exist_ok=True)
                target = derived_dir / f"{path.stem}.jpg"
                resized.save(target, quality=85)
                prepared.append(target)
        except OSError:
            prepared.append(path)
    return prepared


def _parse_verdict_payload(content: str) -> dict[str, Any] | None:
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _normalise_findings(raw: Any, allowed_tags: list[str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for index, item in enumerate(raw or []):
        if not isinstance(item, dict):
            continue
        tag = str(item.get("defect_tag") or "unclassified")
        severity = str(item.get("severity") or "major")
        findings.append(
            {
                "finding_id": f"finding_{index}",
                "defect_tag": tag,
                "severity": severity if severity in SEVERITIES else "major",
                "description": str(item.get("description") or ""),
                "region": str(item.get("region") or ""),
                "suggested_fix_id": str(item.get("suggested_fix_id") or ""),
                "tag_in_vocabulary": tag in allowed_tags,
            }
        )
    return findings


def _record_skeleton(stage_id: str, asset_id: str, project_id: str, attempt: int) -> dict[str, Any]:
    return {
        "id": f"{asset_id or stage_id}_{stage_id}_vlm_review",
        "version": "1.0",
        "status": "review_required",
        "asset_id": asset_id,
        "project_id": project_id,
        "stage_id": stage_id,
        "verdict": "skipped",
        "verdict_reason": "",
        "confidence": 0.0,
        "findings": [],
        "evidence": [],
        "reviewer": {"provider": "", "model": "", "role": "vlm_reviewer"},
        "rubric_checksum": "",
        "rubric_path": "",
        "attempt": attempt,
        "provider_trace": [],
        "review_status": "review_required",
        "raw_secrets_recorded": False,
    }


def _validate_record(record: dict[str, Any]) -> list[str]:
    schema = load_schema("vlm-review-record")
    validator = jsonschema.Draft202012Validator(schema)
    return [error.message for error in validator.iter_errors(record)]


def _write_record(project_dir: Path, stage_id: str, record: dict[str, Any]) -> Path:
    reports_dir = project_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    target = reports_dir / f"{stage_id}-vlm-review.json"
    target.write_text(json.dumps(record, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    history = reports_dir / "vlm-review-history.jsonl"
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=False) + "\n")
    return target


def governance_vlm_review(params: dict[str, Any]) -> ToolResult:
    stage_id = str(params.get("stage_id") or "")
    policy = load_json(str(params.get("policy_path") or POLICY_PATH))
    stage_policy = policy.get("stages", {}).get(stage_id)
    if not stage_id or not stage_policy:
        return ToolResult(
            success=False,
            error=f"no VLM review policy for stage: {stage_id or 'missing stage_id'}",
            validation_status="blocked",
        )

    attempt = int(params.get("attempt") or 0)
    asset_id = str(params.get("asset_id") or "")
    project_id = str(params.get("project_id") or "")
    project_raw = params.get("project")
    project_dir = Path(str(project_raw)) if project_raw else None
    record = _record_skeleton(stage_id, asset_id, project_id, attempt)

    rubric_name = stage_policy["rubric"]
    rubric = _load_rubric(rubric_name)
    record["rubric_path"] = rubric_name
    record["rubric_checksum"] = sha256_text(rubric)

    explicit_images = [str(item) for item in params.get("image_paths") or []]
    images: list[Path] = []
    has_stage_outputs = False
    if project_dir and project_dir.exists():
        images, has_stage_outputs = _collect_evidence_images(project_dir, stage_policy.get("evidence_globs", []), explicit_images or None)
    elif explicit_images:
        images = [Path(item) for item in explicit_images if Path(item).exists()][:MAX_EVIDENCE_IMAGES]
        has_stage_outputs = bool(images)
    record["evidence"] = [
        {
            "evidence_id": f"review_image_{index}",
            "kind": "review_image",
            "uri": path.as_posix(),
            "checksum": sha256_file(path),
        }
        for index, path in enumerate(images)
    ]

    dry_run = bool(params.get("dry_run", True))
    skip_reason = ""
    if dry_run:
        skip_reason = "dry run requested; VLM review deferred to a live run or operator review"
    elif not images:
        skip_reason = "no review evidence images were found for this stage"
    elif not has_stage_outputs:
        skip_reason = "no stage-output images exist yet; reviewing source photos alone would judge nothing this stage produced"

    if not skip_reason:
        import os

        from asset_factory_blueprint.providers import complete_vision

        provider_policy = load_json(str(params.get("provider_policy") or "configs/provider-policy.json"))
        provider_name = str(params.get("provider") or provider_policy["role_defaults"].get(policy.get("provider_role", "vlm_reviewer"), "nvidia_nim"))
        model = (
            params.get("model")
            or os.environ.get("AFB_VISION_MODEL")
            or policy.get("default_models", {}).get(provider_name)
        )
        context = str(params.get("stage_context") or "")
        prompt = rubric
        if context:
            prompt = rubric + "\n\n## Stage context\n\n" + context
        max_edge = int(params.get("max_image_edge") or policy.get("max_image_edge") or 1024)
        send_images = _prepared_review_images(images, project_dir, stage_id, max_edge)
        completion = None
        last_error: Exception | None = None
        # hosted endpoints reject over-budget image sets with a bare server
        # error; shed images from the end (source photos ride last) and retry
        while send_images:
            try:
                completion = complete_vision(
                    provider_name,
                    prompt,
                    [path.as_posix() for path in send_images],
                    policy_path=str(params.get("provider_policy") or "configs/provider-policy.json"),
                    model=model,
                    max_tokens=int(params.get("max_tokens") or 1024),
                )
                break
            except Exception as exc:
                last_error = exc
                if "500" not in str(exc) and "413" not in str(exc):
                    break
                send_images = send_images[:-1]
        if completion is None:
            skip_reason = f"vision provider call failed: {last_error}"
        else:
            record["reviewer"] = {"provider": completion.provider, "model": completion.model, "role": "vlm_reviewer"}
            record["provider_trace"] = [
                {
                    "provider": completion.provider,
                    "model": completion.model,
                    "role": "vlm_reviewer",
                    "prompt_checksum": record["rubric_checksum"],
                }
            ]
            payload = _parse_verdict_payload(completion.content)
            if payload is None:
                skip_reason = "reviewer response was not a valid JSON verdict"
            else:
                verdict = str(payload.get("verdict") or "revise")
                record["verdict"] = verdict if verdict in {"approve", "revise", "blocked"} else "revise"
                try:
                    record["confidence"] = max(0.0, min(1.0, float(payload.get("confidence") or 0.0)))
                except (TypeError, ValueError):
                    record["confidence"] = 0.0
                record["findings"] = _normalise_findings(payload.get("findings"), stage_policy.get("defect_tags", []))

    if skip_reason:
        record["verdict"] = "skipped"
        record["verdict_reason"] = skip_reason

    # only findings whose tag comes from the stage's controlled vocabulary can
    # gate; a malformed tag like "none" on an otherwise clean review must not
    approved = record["verdict"] == "approve" and not any(
        item["severity"] in {"blocker", "major"} and item.get("tag_in_vocabulary")
        for item in record["findings"]
    )
    record["status"] = "validated" if approved else ("blocked" if record["verdict"] == "blocked" else "review_required")
    record["review_status"] = "approved" if approved else "review_required"

    errors = _validate_record(record)
    artefacts: list[str] = []
    if project_dir and project_dir.exists():
        artefacts.append(_write_record(project_dir, stage_id, record).as_posix())

    warnings = [f"{item['defect_tag']}: {item['description']}" for item in record["findings"]]
    if record["verdict"] == "skipped":
        warnings.append(record["verdict_reason"])
    return ToolResult(
        success=approved,
        data=record,
        error="; ".join(errors) if errors else None,
        warnings=warnings,
        artefacts=artefacts,
        proposals=[record],
        validation_status=record["status"] if not errors else "blocked",
    )
