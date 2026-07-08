from __future__ import annotations

from pathlib import Path
from typing import Any

from asset_factory_blueprint.skills.base import ToolResult


def asset_stage_run(params: dict[str, Any]) -> ToolResult:
    """Run one pipeline stage against a project workspace: direct partial invocation.

    Refreshes the workspace artefacts so the stage judges current inputs, runs
    the stage's VLM sign-off with bounded fixes and updates progress records.
    Bootstraps the workspace from a run request when the project does not exist yet.
    """
    from asset_factory_blueprint.agent_loop import run_stage_review
    from asset_factory_blueprint.workflow import rebuild_project_artefacts, run_workflow

    stage_id = str(params.get("stage_id") or "")
    if not stage_id:
        return ToolResult(success=False, error="stage_id is required", validation_status="blocked")
    dry_run = bool(params.get("dry_run", True))
    project_raw = str(params.get("project") or "")
    request_raw = str(params.get("request") or "")
    project_dir = Path(project_raw) if project_raw else None

    if (project_dir is None or not project_dir.exists()) and request_raw:
        workflow = run_workflow(
            request_path=request_raw,
            project_root=str(params.get("project_root") or "projects"),
            project_name=params.get("project_name"),
            dry_run=dry_run,
        )
        project_dir = Path(workflow["project_dir"])
    if project_dir is None or not project_dir.exists():
        return ToolResult(
            success=False,
            error="project is required and must exist, or pass a run request to bootstrap the workspace",
            validation_status="blocked",
        )

    if bool(params.get("refresh_artefacts", True)) and (project_dir / "run-request.json").exists():
        rebuild = rebuild_project_artefacts(project_dir, dry_run=dry_run)
        rebuild_status = rebuild.get("status", "")
    else:
        rebuild_status = "skipped"

    result = run_stage_review(
        project_dir,
        stage_id,
        dry_run=dry_run,
        max_fix_attempts=params.get("max_fix_attempts"),
    )
    if result.get("status") == "blocked":
        return ToolResult(success=False, error=result.get("error", "stage run blocked"), validation_status="blocked")

    data = {
        "stage_id": stage_id,
        "project_dir": project_dir.as_posix(),
        "dry_run": dry_run,
        "workspace_rebuild": rebuild_status,
        "final_state": result["final_state"],
        "iteration": result["iteration"],
        "progress": result["progress"],
        "contact_sheet": result["contact_sheet"],
        "stage_report": result["stage_report"],
    }
    approved = result["final_state"] == "approved"
    return ToolResult(
        success=True,
        data=data,
        validation_status="proposal" if approved else "review_required",
        artefacts=[result["stage_report"], result["progress"]],
    )
