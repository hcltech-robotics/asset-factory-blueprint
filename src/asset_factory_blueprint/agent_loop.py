"""Agentic execution loop for the asset factory pipeline.

Runs the deterministic workflow to materialise the project workspace, then
walks the routed stages: deterministic gates first, then a VLM sign-off
against the stage rubric, then bounded fix-library remediation for revise
verdicts, then escalation to operator review. Every iteration refreshes the
machine-consumable progress record and the operator contact sheet.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asset_factory_blueprint.config import load_json
from asset_factory_blueprint.services.fix_library import asset_fix_apply
from asset_factory_blueprint.services.progress import write_progress_artefacts
from asset_factory_blueprint.services.vlm_review import governance_vlm_review
from asset_factory_blueprint.schemas.common import RunRequest
from asset_factory_blueprint.workflow import refresh_project_checksums, run_workflow


POLICY_PATH = "configs/vlm-review-policy.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_agent_report(project_dir: Path, report: dict[str, Any]) -> Path:
    reports_dir = project_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    target = reports_dir / "agent-run-report.json"
    target.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return target


def _blocking_defect_tags(record: dict[str, Any]) -> list[str]:
    tags = [
        item["defect_tag"]
        for item in record.get("findings", [])
        if item.get("severity") in {"blocker", "major"} and item.get("tag_in_vocabulary")
    ]
    return sorted({tag for tag in tags if tag})


def _review_stage(
    project_dir: Path,
    stage_id: str,
    asset_id: str,
    project_id: str,
    dry_run: bool,
    max_fix_attempts: int,
) -> dict[str, Any]:
    iteration: dict[str, Any] = {
        "stage_id": stage_id,
        "started_at": _now(),
        "reviews": [],
        "fixes": [],
        "final_state": "review_required",
    }
    attempt = 0
    while True:
        review = governance_vlm_review(
            {
                "project": project_dir.as_posix(),
                "stage_id": stage_id,
                "asset_id": asset_id,
                "project_id": project_id,
                "dry_run": dry_run,
                "attempt": attempt,
            }
        )
        record = review.data
        iteration["reviews"].append(
            {
                "attempt": attempt,
                "verdict": record.get("verdict", "skipped"),
                "confidence": record.get("confidence", 0.0),
                "defect_tags": _blocking_defect_tags(record),
                "verdict_reason": record.get("verdict_reason", ""),
            }
        )
        verdict = record.get("verdict", "skipped")
        if review.success and record.get("review_status") == "approved":
            iteration["final_state"] = "approved"
            break
        if verdict == "approve":
            iteration["final_state"] = "review_required"
            iteration["reviews"][-1]["note"] = "approve verdict carried blocker or major findings; the record stays review required"
            break
        if verdict == "skipped":
            iteration["final_state"] = "review_required"
            break
        if verdict == "blocked":
            iteration["final_state"] = "blocked"
            break
        if attempt >= max_fix_attempts:
            iteration["final_state"] = "fix_attempts_exhausted"
            break
        defect_tags = _blocking_defect_tags(record)
        if not defect_tags:
            iteration["final_state"] = "review_required_minor_findings"
            break
        if dry_run:
            iteration["final_state"] = "review_required"
            break
        fix = asset_fix_apply(
            {
                "project": project_dir.as_posix(),
                "stage_id": stage_id,
                "asset_id": asset_id,
                "defect_tags": defect_tags,
                "findings": record.get("findings", []),
                "dry_run": dry_run,
                "attempt": attempt,
            }
        )
        iteration["fixes"].append(
            {
                "attempt": attempt,
                "defect_tags": defect_tags,
                "resolved_fixes": fix.data.get("resolved_fixes", []),
                "escalated": fix.data.get("escalated", []),
                "artefacts_refreshed": fix.data.get("artefacts_refreshed", False),
                "success": fix.success,
            }
        )
        if not fix.success:
            iteration["final_state"] = "escalated_to_review"
            break
        if not fix.data.get("artefacts_refreshed", False):
            iteration["final_state"] = "escalated_to_review"
            iteration["fixes"][-1]["note"] = "no fix changed the workspace artefacts, so re-reviewing would judge identical evidence"
            break
        attempt += 1
    iteration["finished_at"] = _now()
    return iteration


def run_stage_review(
    project_dir: str | Path,
    stage_id: str,
    dry_run: bool = True,
    max_fix_attempts: int | None = None,
    policy_path: str = POLICY_PATH,
) -> dict[str, Any]:
    """Review one stage of an existing project workspace: the direct partial
    invocation path. Runs the stage's VLM sign-off, bounded fixes and progress
    refresh without touching the other stages' review state."""
    root = Path(project_dir)
    plan_path = root / "run-plan.json"
    if not plan_path.exists():
        return {
            "status": "blocked",
            "error": "no run-plan.json in the project; run afb workflow run or afb agent run first",
        }
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    policy = load_json(policy_path)
    reviewable = sorted(set(policy.get("stages", {})))
    routed = [stage.get("id", "") for stage in plan.get("stages", [])]
    if stage_id not in reviewable:
        return {
            "status": "blocked",
            "error": f"stage {stage_id} has no review rubric; reviewable stages: {', '.join(reviewable)}",
        }
    if stage_id not in routed:
        return {
            "status": "blocked",
            "error": f"stage {stage_id} is not routed for this project; routed stages: {', '.join(routed)}",
        }
    attempts_budget = int(max_fix_attempts if max_fix_attempts is not None else policy.get("default_max_fix_attempts", 2))
    iteration = _review_stage(root, stage_id, plan.get("asset_id", ""), plan.get("request_id", ""), dry_run, attempts_budget)
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / f"stage-run-{stage_id}.json").write_text(
        json.dumps(iteration, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    artefacts = write_progress_artefacts(root)
    refresh_project_checksums(root)
    return {
        "status": "proposal" if iteration["final_state"] == "approved" else "review_required",
        "stage_id": stage_id,
        "final_state": iteration["final_state"],
        "iteration": iteration,
        "progress": artefacts["progress_path"],
        "contact_sheet": artefacts["contact_sheet_path"],
        "stage_report": (root / "reports" / f"stage-run-{stage_id}.json").as_posix(),
    }


def run_agent_loop(
    request_path: str | Path | RunRequest,
    project_root: str | Path = "projects",
    project_name: str | None = None,
    dry_run: bool = True,
    max_fix_attempts: int | None = None,
    policy_path: str = POLICY_PATH,
) -> dict[str, Any]:
    workflow_result = run_workflow(
        request_path=request_path,
        project_root=project_root,
        project_name=project_name,
        dry_run=dry_run,
    )
    project_dir = Path(workflow_result["project_dir"])
    plan = json.loads((project_dir / "run-plan.json").read_text(encoding="utf-8"))
    policy = load_json(policy_path)
    reviewed = set(policy.get("stages", {}))
    attempts_budget = int(max_fix_attempts if max_fix_attempts is not None else policy.get("default_max_fix_attempts", 2))
    asset_id = plan.get("asset_id", "")
    project_id = workflow_result.get("project_id", "")

    report: dict[str, Any] = {
        "id": f"{plan.get('id', 'run')}_agent_loop",
        "version": "1.0",
        "run_id": plan.get("id", ""),
        "project_id": project_id,
        "dry_run": dry_run,
        "max_fix_attempts": attempts_budget,
        "started_at": _now(),
        "workflow_status": workflow_result.get("status", ""),
        "iterations": [],
        "completed": False,
    }
    _write_agent_report(project_dir, report)

    for stage in plan.get("stages", []):
        stage_id = stage.get("id", "")
        if stage_id not in reviewed:
            continue
        iteration = _review_stage(project_dir, stage_id, asset_id, project_id, dry_run, attempts_budget)
        report["iterations"].append(iteration)
        _write_agent_report(project_dir, report)
        write_progress_artefacts(project_dir)

    report["completed"] = True
    report["finished_at"] = _now()
    approved = [item["stage_id"] for item in report["iterations"] if item["final_state"] == "approved"]
    pending = [item["stage_id"] for item in report["iterations"] if item["final_state"] != "approved"]
    report["approved_stages"] = approved
    report["pending_stages"] = pending
    _write_agent_report(project_dir, report)
    artefacts = write_progress_artefacts(project_dir)
    refresh_project_checksums(project_dir)

    return {
        "project_id": project_id,
        "project_dir": project_dir.as_posix(),
        "run_id": plan.get("id", ""),
        "dry_run": dry_run,
        "workflow_status": workflow_result.get("status", ""),
        "reviewed_stages": len(report["iterations"]),
        "approved_stages": approved,
        "pending_stages": pending,
        "progress": artefacts["progress_path"],
        "contact_sheet": artefacts["contact_sheet_path"],
        "agent_report": (project_dir / "reports" / "agent-run-report.json").as_posix(),
        "status": "proposal" if not pending else "review_required",
    }
