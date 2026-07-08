from __future__ import annotations

from typing import Any

from asset_factory_blueprint.skills.base import ToolResult


def _library_grounding(query: str) -> dict[str, Any]:
    if not query:
        return {"grounded": False, "matches": []}
    try:
        from asset_factory_blueprint.services.library import search_library

        matches = search_library(query, ["materials"], limit=3)
    except Exception:
        return {"grounded": False, "matches": []}
    return {
        "grounded": bool(matches),
        "matches": [
            {"item_id": item["item_id"], "source": item["source"], "material_class": item.get("material_class", ""), "score": item["score"]}
            for item in matches
        ],
    }


def material_propose(params: dict[str, Any]) -> ToolResult:
    components = params.get("components") or [{"prim_path": "/World/Asset", "label": "asset"}]
    library_id = str(params.get("material_library_id") or "default_material_library")
    evidence_ids = list(params.get("evidence_ids") or [])
    proposals = []
    warnings: list[str] = []
    for component in components:
        material = component.get("declared_material") or params.get("declared_material")
        selection_status = "proposal" if material and evidence_ids else "review_required"
        if not material:
            warnings.append(f"material evidence required for {component.get('prim_path', '/World/Asset')}")
        grounding = _library_grounding(str(material or component.get("label", "")))
        if material and not grounding["grounded"]:
            warnings.append(f"material {material} has no library grounding; review required before promotion")
        proposals.append(
            {
                "prim_path": component.get("prim_path", "/World/Asset"),
                "component_label": component.get("label", "asset"),
                "candidate_materials": [material] if material else [],
                "selected_material": material,
                "selection_status": selection_status,
                "material_library_id": library_id,
                "evidence_ids": evidence_ids,
                "library_grounding": grounding,
                "requires_human_review": selection_status == "review_required" or not grounding["grounded"],
            }
        )
    return ToolResult(
        success=True,
        data={"material_library_id": library_id, "component_materials": proposals},
        warnings=warnings,
        proposals=proposals,
        validation_status="review_required" if warnings else "proposal",
    )


_DICTIONARY_PROPERTY_MAP = {
    "density": ("density_kg_m3", "kg/m3"),
    "static_friction": ("static_friction_on_dry_steel", "coefficient"),
    "dynamic_friction": ("dynamic_friction_on_dry_steel", "coefficient"),
    "restitution": ("restitution", "coefficient"),
    "youngs_modulus": ("youngs_modulus_gpa", "GPa"),
    "thermal_conductivity": ("thermal_conductivity_w_mk", "W/(m K)"),
}


def _dictionary_prior(material_class: str, property_name: str) -> dict[str, Any] | None:
    mapping = _DICTIONARY_PROPERTY_MAP.get(property_name)
    if not material_class or mapping is None:
        return None
    try:
        from asset_factory_blueprint.services.library import lookup_physical_properties

        entry = lookup_physical_properties(material_class)
    except Exception:
        return None
    if not entry:
        return None
    values = entry.get("properties", {}).get(mapping[0])
    if not isinstance(values, dict):
        return None
    return {
        "range_low": values.get("low"),
        "range_high": values.get("high"),
        "unit": mapping[1],
        "dictionary_item_id": entry.get("item_id", ""),
    }


def material_physical_properties_propose(params: dict[str, Any]) -> ToolResult:
    requested = params.get("properties") or ["density", "mass", "static_friction"]
    evidence_ids = list(params.get("evidence_ids") or [])
    material_class = str(params.get("material_class") or "")
    proposals = []
    warnings: list[str] = []
    for name in requested:
        has_value = name in params.get("measured_values", {})
        prior = None if has_value else _dictionary_prior(material_class, name)
        if not evidence_ids and not has_value and not prior:
            warnings.append(f"{name} requires physical evidence before promotion")
        record = {
            "property_name": name,
            "value": params.get("measured_values", {}).get(name),
            "unit": params.get("units", {}).get(name),
            "method": "measured" if has_value else "needs_measurement",
            "confidence": 0.0 if not has_value else 1.0,
            "evidence_ids": evidence_ids,
            "validation_status": "validated" if has_value and evidence_ids else "needs_measurement",
        }
        if prior:
            record.update(
                {
                    "unit": record["unit"] or prior["unit"],
                    "range_low": prior["range_low"],
                    "range_high": prior["range_high"],
                    "method": "library_prior",
                    "confidence": 0.4,
                    "evidence_ids": evidence_ids + [f"library:{prior['dictionary_item_id']}"],
                    "validation_status": "review_required",
                    "notes": f"range from the physical property dictionary for {material_class}; review before task-critical use",
                }
            )
        proposals.append(record)
    return ToolResult(
        success=True,
        data={"properties": proposals, "numeric_values_from_visual_evidence": False, "material_class": material_class},
        warnings=warnings,
        proposals=proposals,
        validation_status="review_required" if warnings else "proposal",
    )
