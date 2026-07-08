from __future__ import annotations

from typing import Any

from asset_factory_blueprint.external_models import run_manifest
from asset_factory_blueprint.skills.base import ToolResult


def governance_external_model_run(params: dict[str, Any]) -> ToolResult:
    manifest = params.get("manifest")
    dry_run = bool(params.get("dry_run", True))
    if not manifest:
        return ToolResult(
            success=False,
            error="external model run requires a manifest path",
            validation_status="blocked",
        )
    payload = run_manifest(manifest, dry_run=dry_run)
    return ToolResult(
        success=True,
        data=payload,
        artefacts=[str(manifest), payload.get("logs_path", "")],
        validation_status=payload["status"],
    )
