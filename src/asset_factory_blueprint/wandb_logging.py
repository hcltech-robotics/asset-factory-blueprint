from __future__ import annotations

import json
import os
from pathlib import Path


WANDB_LOG_FIELDS = [
    "asset_id",
    "run_id",
    "manifest_id",
    "git_sha",
    "provider_assignments",
    "model_identifiers",
    "tool_versions",
    "source_checksums",
    "stage_statuses",
    "validation_gate_statuses",
    "inferred_property_distributions",
    "material_confidence_table",
    "texture_artefact_checksums",
    "physics_tuning_trials",
    "isaac_load_results",
    "rl_smoke_rollout_metrics",
    "final_promotion_status",
    "texture_default_profile_ids",
    "physical_consistency_warnings",
    "external_model_run_ids",
    "external_model_status",
    "reviewer_decision_ids",
]


def write_wandb_plan(run_plan: str | Path, output: str | Path) -> dict:
    payload = json.loads(Path(run_plan).read_text(encoding="utf-8"))
    enabled = payload.get("wandb_plan", {}).get("enabled", False) or os.environ.get("AFB_WANDB_ENABLED", "").lower() in {"1", "true", "yes"}
    provider_assignments = payload.get("provider_assignments", {})
    provenance_models = payload.get("provenance", {}).get("provider_model_ids", {})
    model_identifiers = {
        role: provenance_models.get(
            role,
            {
                "provider": item.get("provider", ""),
                "kind": item.get("kind", ""),
                "model_env": item.get("model_env", ""),
                "model_id": item.get("model_id", ""),
                "model_resolution_status": item.get("model_resolution_status", "blocked_unresolved"),
                "blocked_reason": item.get("blocked_reason", ""),
            },
        )
        for role, item in provider_assignments.items()
    }
    stage_statuses = {stage["id"]: stage.get("status", "proposal") for stage in payload.get("stages", [])}
    plan = {
        "enabled": enabled,
        "run_group": payload.get("wandb", {}).get("group", payload.get("id")),
        "project_env": payload.get("wandb", {}).get("project_env", "AFB_WANDB_PROJECT"),
        "enabled_env": payload.get("wandb", {}).get("enabled_env", "AFB_WANDB_ENABLED"),
        "tags": payload.get("wandb", {}).get("tags", []),
        "logged_fields": WANDB_LOG_FIELDS,
        "field_values": {
            "asset_id": payload.get("asset_id"),
            "run_id": payload.get("run_id", payload.get("id")),
            "provider_assignments": provider_assignments,
            "model_identifiers": model_identifiers,
            "stage_statuses": stage_statuses,
            "validation_gate_statuses": {gate: "pending" for gate in payload.get("validation_gates", [])},
            "final_promotion_status": "not_released",
            "git_sha": payload.get("provenance", {}).get("repository", {}).get("git_sha", "unavailable"),
            "tool_versions": payload.get("provenance", {}).get("tool_versions", {}),
        },
        "artefacts": ["run-plan", "stage-reports", "validation-reports"],
        "secret_policy": "environment variables only",
    }
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    if enabled:
        metadata_path = target.with_name(target.stem + "-local-metadata.json")
        metadata_path.write_text(
            json.dumps(
                {
                    "run_group": plan["run_group"],
                    "project_env": plan["project_env"],
                    "logged_fields": plan["field_values"],
                    "secret_policy": plan["secret_policy"],
                },
                indent=2,
                sort_keys=False,
            )
            + "\n",
            encoding="utf-8",
        )
        plan["local_metadata_path"] = metadata_path.as_posix()
        target.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return plan
