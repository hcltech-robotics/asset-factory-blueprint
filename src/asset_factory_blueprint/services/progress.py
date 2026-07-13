from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from asset_factory_blueprint.skills.base import ToolResult


STAGE_ORDER = [
    "orchestrate",
    "intake",
    "source-ingestion",
    "reconstruction",
    "mesh-verification",
    "segmentation",
    "material-inference",
    "texturing",
    "physics-articulation",
    "nonvisual-materials",
    "simready-verification",
    "rl-environment",
    "evaluation",
    "infrastructure",
    "governance",
]

STAGE_IMAGE_GLOBS = {
    "source-ingestion": ["source-assets/**/*.png", "source-assets/**/*.jpg", "source-assets/**/*.jpeg"],
    "reconstruction": ["assets/*/renders/**/*.png", "renders/**/*.png"],
    "mesh-verification": ["reports/mesh-verification/*.png"],
    "segmentation": ["assets/*/textures/segments/*.png"],
    "texturing": ["assets/*/textures/*.png", "assets/*/textures/variants/**/*.png"],
    "simready-verification": ["assets/*/renders/**/*.png", "renders/**/*.png"],
}

STATUS_BADGES = {
    "proposal": "proposal",
    "validated": "validated",
    "review_required": "review required",
    "blocked": "blocked",
    "approved": "approved",
    "skipped": "skipped",
}


def _load_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _stage_images(project_dir: Path, stage_id: str, limit: int = 4) -> list[str]:
    images: list[str] = []
    seen: set[str] = set()
    for pattern in STAGE_IMAGE_GLOBS.get(stage_id, []):
        for path in sorted(project_dir.glob(pattern)):
            rel = path.relative_to(project_dir).as_posix()
            if path.is_file() and rel not in seen:
                seen.add(rel)
                images.append(rel)
    return images[:limit]


def build_progress(project_dir: str | Path) -> dict[str, Any]:
    root = Path(project_dir)
    plan = _load_json_if_exists(root / "run-plan.json")
    project = _load_json_if_exists(root / "project.json")
    fix_log = _load_json_if_exists(root / "reports" / "fix-attempts.json").get("attempts", [])
    agent_state = _load_json_if_exists(root / "reports" / "agent-run-report.json")

    stages: list[dict[str, Any]] = []
    planned = {stage.get("id"): stage for stage in plan.get("stages", [])}
    ordered = [stage_id for stage_id in STAGE_ORDER if stage_id in planned]
    for stage_id in ordered:
        report = _load_json_if_exists(root / "reports" / f"{stage_id}-report.json")
        review = _load_json_if_exists(root / "reports" / f"{stage_id}-vlm-review.json")
        mesh_verification = (
            _load_json_if_exists(root / "manifests" / "mesh-verification-record.json")
            if stage_id == "mesh-verification"
            else {}
        )
        if mesh_verification:
            mesh_decision = str(mesh_verification.get("decision") or "")
            review = {
                "verdict": "approve"
                if mesh_decision == "approve"
                else "blocked"
                if mesh_decision == "blocked"
                else "revise"
                if mesh_decision in {"revise_local", "regenerate"}
                else "skipped",
                "verdict_reason": mesh_verification.get("decision_reason", ""),
                "confidence": mesh_verification.get("confidence", 0.0),
                "findings": mesh_verification.get("findings", []),
                "attempt": mesh_verification.get("attempts", {}).get("review_attempt", 0),
            }
        stage_fixes = [item for item in fix_log if item.get("stage_id") == stage_id]
        entry: dict[str, Any] = {
            "stage_id": stage_id,
            "skill": planned[stage_id].get("skill", ""),
            "status": report.get("status", planned[stage_id].get("status", "proposal")),
            "manifest_path": report.get("manifest_path"),
            "manifest_valid": not report.get("manifest_errors", []),
            "validation_gates": report.get("validation_gates", []),
            "blocked_reasons": report.get("blocked_reasons", []),
            "images": _stage_images(root, stage_id),
            "fix_attempts": len(stage_fixes),
        }
        if review:
            entry["vlm_review"] = {
                "verdict": review.get("verdict", ""),
                "verdict_reason": review.get("verdict_reason", ""),
                "confidence": review.get("confidence", 0.0),
                "defect_tags": sorted({item.get("defect_tag", "") for item in review.get("findings", []) if item.get("defect_tag")}),
                "attempt": review.get("attempt", 0),
            }
        if mesh_verification:
            entry["mesh_verification"] = {
                "decision": mesh_verification.get("decision", ""),
                "candidate_checksum": mesh_verification.get("candidate", {}).get("checksum", ""),
                "review_attempts": mesh_verification.get("attempts", {}).get("review_attempt", 0),
                "mesh_rejections": mesh_verification.get("attempts", {}).get("mesh_rejection_count", 0),
                "inference_resubmissions": mesh_verification.get("attempts", {}).get(
                    "inference_resubmission_count", 0
                ),
                "promotion_approved": mesh_verification.get("promotion", {}).get("approved", False),
            }
        stages.append(entry)

    blocked = [item for item in stages if item["status"] == "blocked" or item["blocked_reasons"]]
    review_pending = [
        item["stage_id"]
        for item in stages
        if item.get("vlm_review", {}).get("verdict") in {"revise", "skipped"} or item["status"] == "review_required"
    ]
    reviewed = [item for item in stages if item.get("vlm_review")]
    all_approved = bool(reviewed) and all(item["vlm_review"].get("verdict") == "approve" for item in reviewed)
    if blocked:
        overall = "blocked"
    elif all_approved and not review_pending:
        overall = "approved"
    elif review_pending:
        overall = "review_required"
    else:
        overall = "proposal"
    next_actions: list[str] = []
    for item in stages:
        verdict = item.get("vlm_review", {}).get("verdict", "")
        if verdict == "skipped":
            next_actions.append(f"run or record an operator review for {item['stage_id']}")
        elif verdict == "revise":
            next_actions.append(f"apply fix library recipes for {item['stage_id']}: {', '.join(item['vlm_review']['defect_tags'])}")
        for reason in item["blocked_reasons"]:
            next_actions.append(f"{item['stage_id']}: {reason}")

    return {
        "id": f"{plan.get('request_id', project.get('project_id', 'project'))}_progress",
        "version": "1.0",
        "project_id": project.get("project_id", ""),
        "run_id": plan.get("id", ""),
        "objective": plan.get("objective", ""),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "stage_count": len(stages),
        "blocked_count": len(blocked),
        "review_pending": sorted(set(review_pending)),
        "status": overall,
        "agent_loop": {
            "iterations": agent_state.get("iterations", []),
            "completed": agent_state.get("completed", False),
        }
        if agent_state
        else {},
        "stages": stages,
        "next_actions": next_actions[:20],
    }


def _badge(value: str) -> str:
    return STATUS_BADGES.get(value, value or "unknown")


def _contact_sheet_markdown(progress: dict[str, Any]) -> str:
    lines = [
        "# Contact sheet",
        "",
        f"Project `{progress.get('project_id', '')}`, run `{progress.get('run_id', '')}`.",
        f"Objective: {progress.get('objective', '')}",
        f"Updated: {progress.get('updated_at', '')}",
        "",
        f"Overall status: **{_badge(progress.get('status', ''))}** with {progress.get('blocked_count', 0)} blocked stage(s).",
        "",
    ]
    for stage in progress.get("stages", []):
        review = stage.get("vlm_review", {})
        lines.append(f"## {stage['stage_id']}")
        lines.append("")
        lines.append(f"Status: **{_badge(stage['status'])}** | manifest valid: {'yes' if stage['manifest_valid'] else 'no'} | fix attempts: {stage['fix_attempts']}")
        if review:
            tags = ", ".join(review.get("defect_tags", [])) or "none"
            lines.append(f"VLM review: **{_badge(review.get('verdict', ''))}** (confidence {review.get('confidence', 0.0)}), defects: {tags}")
            if review.get("verdict_reason"):
                lines.append(f"Reviewer note: {review['verdict_reason']}")
        mesh_verification = stage.get("mesh_verification", {})
        if mesh_verification:
            lines.append(
                "Mesh verifier: "
                f"{mesh_verification.get('review_attempts', 0)} review(s), "
                f"{mesh_verification.get('mesh_rejections', 0)} rejection(s), "
                f"{mesh_verification.get('inference_resubmissions', 0)} inference resubmission(s)"
            )
        for reason in stage.get("blocked_reasons", [])[:4]:
            lines.append(f"- blocked: {reason}")
        if stage.get("images"):
            lines.append("")
            stage_id = stage["stage_id"]
            cells = " ".join(f'<img src="../{rel}" alt="{stage_id}" width="160"/>' for rel in stage["images"])
            lines.append(cells)
        lines.append("")
    if progress.get("next_actions"):
        lines.append("## Next actions")
        lines.append("")
        for action in progress["next_actions"]:
            lines.append(f"- {action}")
        lines.append("")
    return "\n".join(lines)


def _contact_sheet_image(project_dir: Path, progress: dict[str, Any], target: Path) -> str:
    tiles: list[tuple[str, Path]] = []
    for stage in progress.get("stages", []):
        for rel in stage.get("images", [])[:2]:
            path = project_dir / rel
            if path.exists():
                tiles.append((stage["stage_id"], path))
    if not tiles:
        return ""
    tiles = tiles[:12]
    tile_size = 256
    label_height = 28
    columns = min(4, len(tiles))
    rows = (len(tiles) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * tile_size, rows * (tile_size + label_height)), (24, 24, 28))
    draw = ImageDraw.Draw(sheet)
    for index, (label, path) in enumerate(tiles):
        column = index % columns
        row = index // columns
        x = column * tile_size
        y = row * (tile_size + label_height)
        try:
            image = Image.open(path).convert("RGB")
        except Exception:
            continue
        image.thumbnail((tile_size, tile_size))
        sheet.paste(image, (x + (tile_size - image.width) // 2, y + (tile_size - image.height) // 2))
        draw.text((x + 6, y + tile_size + 6), label[:34], fill=(235, 235, 240))
    sheet.save(target)
    return target.as_posix()


def write_progress_artefacts(project_dir: str | Path) -> dict[str, Any]:
    root = Path(project_dir)
    progress = build_progress(root)
    progress_path = root / "progress.json"
    progress_path.write_text(json.dumps(progress, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    reports_dir = root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    sheet_path = reports_dir / "contact-sheet.md"
    sheet_path.write_text(_contact_sheet_markdown(progress), encoding="utf-8")
    image_path = _contact_sheet_image(root, progress, reports_dir / "contact-sheet.png")
    return {
        "progress_path": progress_path.as_posix(),
        "contact_sheet_path": sheet_path.as_posix(),
        "contact_sheet_image": image_path,
        "status": progress["status"],
        "blocked_count": progress["blocked_count"],
    }


def governance_progress_report(params: dict[str, Any]) -> ToolResult:
    project_raw = params.get("project")
    if not project_raw:
        return ToolResult(success=False, error="project is required", validation_status="blocked")
    project_dir = Path(str(project_raw))
    if not (project_dir / "run-plan.json").exists():
        return ToolResult(success=False, error=f"no run plan under {project_dir}", validation_status="blocked")
    result = write_progress_artefacts(project_dir)
    return ToolResult(
        success=True,
        data=result,
        artefacts=[result["progress_path"], result["contact_sheet_path"]] + ([result["contact_sheet_image"]] if result["contact_sheet_image"] else []),
        validation_status="proposal",
    )
