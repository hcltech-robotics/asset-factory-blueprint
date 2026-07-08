from __future__ import annotations

from typing import Any

from asset_factory_blueprint.skills.base import ToolResult


def physics_plan(params: dict[str, Any]) -> ToolResult:
    properties = params.get("properties") or []
    validated = [item for item in properties if item.get("validation_status") == "validated"]
    blocked = [item.get("property_name", "property") for item in properties if item.get("validation_status") != "validated"]
    if not properties:
        blocked.append("physical property proposals")
    plan = {
        "usd_input_path": params.get("usd_input_path"),
        "usd_output_path": params.get("usd_output_path"),
        "rigid_bodies": params.get("rigid_bodies", []),
        "colliders": params.get("colliders", []),
        "validated_property_count": len(validated),
        "blocked_properties": blocked,
        "numeric_physics_authored_without_evidence": False,
    }
    return ToolResult(
        success=not blocked,
        data=plan,
        warnings=[f"validated evidence required for {item}" for item in blocked],
        proposals=[plan],
        validation_status="proposal" if not blocked else "review_required",
    )


def _normalise_grasp_points(raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    grasp_points: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, item in enumerate(raw or []):
        if not isinstance(item, dict):
            warnings.append(f"grasp_points[{index}] must be an object")
            continue
        record = {
            "grasp_id": str(item.get("grasp_id") or f"grasp_{index}"),
            "frame": item.get("frame"),
            "approach_vector": item.get("approach_vector"),
            "gripper_width": item.get("gripper_width"),
            "confidence": item.get("confidence", 0.0),
            "evidence_ids": list(item.get("evidence_ids") or []),
            "validation_status": "proposal" if item.get("evidence_ids") else "review_required",
        }
        if not record["frame"]:
            warnings.append(f"grasp_points[{index}] requires a frame")
        if not record["approach_vector"]:
            warnings.append(f"grasp_points[{index}] requires an approach vector")
        grasp_points.append(record)
    return grasp_points, warnings


def articulation_plan(params: dict[str, Any]) -> ToolResult:
    joints = params.get("joints") or []
    warnings = []
    for index, joint in enumerate(joints):
        if not joint.get("axis") and joint.get("joint_type") not in {"fixed", None}:
            warnings.append(f"joints[{index}] axis is required for non-fixed joints")
        if "lower_limit" not in joint or "upper_limit" not in joint:
            warnings.append(f"joints[{index}] limit policy is required")
    if not joints:
        warnings.append("joint evidence required before articulation promotion")
    grasp_points, grasp_warnings = _normalise_grasp_points(params.get("grasp_points"))
    warnings.extend(grasp_warnings)
    data = {
        "joints": joints,
        "part_graph": params.get("part_graph", []),
        "affordances": {
            "grasp_points": grasp_points,
            "affordance_labels": list(params.get("affordance_labels") or []),
        },
        "review_required": bool(warnings),
    }
    return ToolResult(
        success=not warnings,
        data=data,
        warnings=warnings,
        proposals=[data],
        validation_status="proposal" if not warnings else "review_required",
    )
