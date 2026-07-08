from __future__ import annotations

from typing import Any

from asset_factory_blueprint.skills.base import ToolResult
from asset_factory_blueprint.state import create_project


def governance_project_create(params: dict[str, Any]) -> ToolResult:
    name = str(params.get("name") or "asset factory project")
    project_root = params.get("project_root", "projects")
    manifest = create_project(name, project_root)
    project_dir = f"{project_root}/{manifest['project_id']}"
    return ToolResult(
        success=True,
        data={"project": manifest, "project_dir": project_dir},
        artefacts=[f"{project_dir}/project.json", f"{project_dir}/scene.usda"],
        validation_status="validated",
    )
