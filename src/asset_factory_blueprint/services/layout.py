from __future__ import annotations

from typing import Any

from asset_factory_blueprint.skills.base import ToolResult
from asset_factory_blueprint.validation import validate_layout_payload


def scene_layout_validate(params: dict[str, Any]) -> ToolResult:
    errors = validate_layout_payload(params)
    return ToolResult(
        success=not errors,
        data={"errors": errors, "error_count": len(errors)},
        warnings=errors,
        validation_status="validated" if not errors else "blocked",
    )
