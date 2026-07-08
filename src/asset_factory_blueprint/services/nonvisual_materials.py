from __future__ import annotations

from typing import Any

from asset_factory_blueprint.skills.base import ToolResult


def physics_nonvisual_materials_propose(params: dict[str, Any]) -> ToolResult:
    requested = params.get("properties") or ["thermal_conductivity", "acoustic_absorption", "electrical_conductivity"]
    evidence_ids = list(params.get("evidence_ids") or [])
    proposals = []
    warnings: list[str] = []
    for name in requested:
        has_value = name in params.get("measured_values", {})
        if not evidence_ids and not has_value:
            warnings.append(f"{name} requires nonvisual evidence before promotion")
        proposals.append(
            {
                "property_name": name,
                "value": params.get("measured_values", {}).get(name),
                "unit": params.get("units", {}).get(name),
                "method": "measured" if has_value else "needs_measurement",
                "confidence": 0.0 if not has_value else 1.0,
                "evidence_ids": evidence_ids,
                "validation_status": "validated" if has_value and evidence_ids else "needs_measurement",
            }
        )
    return ToolResult(
        success=True,
        data={"properties": proposals, "numeric_values_from_visual_evidence": False},
        warnings=warnings,
        proposals=proposals,
        validation_status="review_required" if warnings else "proposal",
    )
