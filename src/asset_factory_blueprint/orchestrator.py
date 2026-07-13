from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asset_factory_blueprint.config import load_json
from asset_factory_blueprint.provenance import build_provenance
from asset_factory_blueprint.schemas.common import RunPlan, RunRequest, StagePlan
from asset_factory_blueprint.utils.checksums import sha256_text
from asset_factory_blueprint.utils.ids import new_id


STAGE_CONTRACTS_PATH = "configs/stage-contracts.json"


ROLE_BY_STAGE = {
    "orchestrate": ["planner"],
    "intake": ["planner"],
    "source-ingestion": ["planner"],
    "reconstruction": ["external_model_runner", "vision_reasoner"],
    "mesh-verification": ["vision_reasoner", "vlm_reviewer", "validator_judge"],
    "segmentation": ["vision_reasoner", "material_reasoner"],
    "material-inference": ["material_reasoner"],
    "texturing": ["texture_prompt_writer", "texture_generator"],
    "physics-articulation": ["physics_reasoner"],
    "nonvisual-materials": ["nonvisual_material_reasoner"],
    "simready-verification": ["validator_judge"],
    "rl-environment": ["planner"],
    "evaluation": ["validator_judge"],
    "infrastructure": ["planner"],
    "governance": ["planner"],
}


VLM_REVIEWED_STAGES = {
    "segmentation",
    "material-inference",
    "texturing",
    "physics-articulation",
    "nonvisual-materials",
    "simready-verification",
}


ROLE_MODEL_ENV = {
    "planner": "AFB_PLANNER_MODEL",
    "vision_reasoner": "AFB_VISION_MODEL",
    "vlm_reviewer": "AFB_VISION_MODEL",
    "material_reasoner": "AFB_NVIDIA_MODEL",
    "texture_prompt_writer": "AFB_TEXTURE_MODEL",
    "nonvisual_material_reasoner": "AFB_NONVISUAL_MODEL",
    "physics_reasoner": "AFB_PHYSICS_MODEL",
    "validator_judge": "AFB_VALIDATOR_MODEL",
    "embeddings": "AFB_OPENAI_MODEL",
    "image_generation": "AFB_TEXTURE_MODEL",
    "texture_generator": "AFB_TEXTURE_MODEL",
    "external_model_runner": "AFB_LOCAL_MODEL",
}


def _model_record(provider_name: str, provider: dict[str, str], model_env: str) -> dict[str, str]:
    model_id = os.getenv(model_env, "") if model_env else ""
    if not model_id:
        model_id = str(provider.get("default_model_id", ""))
    resolved = bool(model_id)
    return {
        "provider": provider_name,
        "kind": str(provider.get("kind", "")),
        "model_env": model_env,
        "model_id": model_id,
        "model_resolution_status": "resolved" if resolved else "blocked_unresolved",
        "blocked_reason": "" if resolved else f"set {model_env} or provider default_model_id",
    }


def _provider_model_handles(assignments: dict[str, dict[str, str | None]]) -> dict[str, dict[str, str]]:
    handles = {}
    for role, item in assignments.items():
        handles[role] = {
            "provider": item.get("provider") or "",
            "kind": item.get("kind") or "",
            "model_env": item.get("model_env") or "",
            "model_id": item.get("model_id") or "",
            "model_resolution_status": item.get("model_resolution_status") or "blocked_unresolved",
            "blocked_reason": item.get("blocked_reason") or "",
        }
    return handles


def load_run_request(path: str | Path) -> RunRequest:
    return RunRequest.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _normalise_term(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.strip().lower()).strip()


def _source_kind(source: str, contracts: dict[str, Any], declared_kind: str | None = None) -> str:
    if declared_kind:
        candidate = _normalise_term(declared_kind).replace(" ", "_")
        if candidate in contracts["source_kinds"]:
            return candidate
    suffix = Path(source).suffix.lower()
    for kind, suffixes in contracts["source_kinds"].items():
        if suffix in suffixes:
            return kind
    return "unknown"


def _requested_deliverables(request: RunRequest, contracts: dict[str, Any]) -> tuple[set[str], list[str]]:
    aliases = {
        _normalise_term(alias): deliverable
        for deliverable, values in contracts["output_aliases"].items()
        for alias in values
    }
    selected: set[str] = set()
    unknown: list[str] = []
    for raw_output in request.requested_outputs:
        normalised = _normalise_term(raw_output)
        deliverable = aliases.get(normalised)
        if deliverable:
            selected.add(deliverable)
        else:
            unknown.append(raw_output)
    return selected, unknown


def _dependency_closure(selected: set[str], contracts: dict[str, Any]) -> set[str]:
    stages = contracts["stages"]
    pending = list(selected)
    while pending:
        stage_id = pending.pop()
        for dependency in stages.get(stage_id, {}).get("depends_on", []):
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    return selected


def route_ids(request: RunRequest) -> set[str]:
    contracts = load_json(STAGE_CONTRACTS_PATH)
    selected = {"orchestrate", "intake", "governance", "evaluation", "infrastructure"}
    declared_kinds = request.constraints.get("source_kinds", {}) if isinstance(request.constraints, dict) else {}
    for index, source in enumerate(request.sources):
        declared_kind = None
        if isinstance(declared_kinds, dict):
            declared_kind = declared_kinds.get(source) or declared_kinds.get(str(index))
        kind = _source_kind(source, contracts, str(declared_kind) if declared_kind else None)
        selected.update(contracts["source_targets"].get(kind, ["source-ingestion"]))
    deliverables, unknown_outputs = _requested_deliverables(request, contracts)
    if unknown_outputs:
        supported = sorted(contracts["output_aliases"])
        raise ValueError(
            "unknown requested output values: "
            + ", ".join(repr(item) for item in unknown_outputs)
            + "; use a declared deliverable alias for "
            + ", ".join(supported)
        )
    for deliverable in deliverables:
        selected.update(contracts["output_targets"].get(deliverable, []))
    return _dependency_closure(selected, contracts)


def build_run_plan(request: RunRequest) -> RunPlan:
    workflow = load_json("configs/agent-workflow.json")
    contracts = load_json(STAGE_CONTRACTS_PATH)
    providers = load_json("configs/provider-policy.json")
    role_defaults = providers["role_defaults"]
    selected = route_ids(request)
    stages = []
    missing_evidence = []
    for item in workflow["stages"]:
        if item["id"] not in selected:
            continue
        contract = contracts["stages"].get(item["id"], {})
        roles = ROLE_BY_STAGE.get(item["id"], ["planner"])
        gates = ["schema-valid"]
        if item["id"] in {
            "source-ingestion",
            "mesh-verification",
            "segmentation",
            "material-inference",
            "nonvisual-materials",
        }:
            gates.append("source-lineage")
        if item["id"] == "segmentation":
            gates.append("segmentation-segments")
        if item["id"] == "mesh-verification":
            gates.append("mesh-verification")
        elif item["id"] in VLM_REVIEWED_STAGES:
            gates.append("vlm-signoff")
        if item["id"] == "simready-verification":
            gates.append("isaac-load")
        if item["id"] == "governance":
            gates.append("governance-review")
        blocked = []
        if item["id"] == "rl-environment" and "simready-verification" not in selected:
            blocked.append("validated asset package required before RL environment generation")
        if item["id"] == "source-ingestion" and not request.sources:
            missing_evidence.append("source asset path")
        consumes = list(contract.get("consumes", ["run-request"]))
        produces = list(contract.get("produces", [f"{item['id']}-manifest"]))
        if item["id"] == "simready-verification" and "texturing" in selected:
            consumes.append("appearance-layers")
        if item["id"] == "evaluation" and "simready-verification" in selected:
            consumes.extend(["simready-conformance", "runtime-evidence"])
        stages.append(
            StagePlan(
                id=item["id"],
                name=item["name"],
                skill=item["skill"],
                provider_roles=roles,
                required_inputs=list(dict.fromkeys(consumes)),
                outputs=list(dict.fromkeys(produces)),
                consumes=list(dict.fromkeys(consumes)),
                produces=list(dict.fromkeys(produces)),
                preconditions=list(contract.get("preconditions", [])),
                resources=dict(contract.get("resources", {})),
                max_attempts=int(contract.get("max_attempts", 1)),
                execution_mode=str(contract.get("execution_mode", "local")),
                validation_gates=gates,
                blocked_reasons=blocked,
            )
        )
    assignments = {}
    for stage in stages:
        for role in stage.provider_roles:
            if role not in role_defaults:
                continue
            provider_name = role_defaults[role]
            provider = providers["providers"][provider_name]
            model_env = ROLE_MODEL_ENV.get(role, provider.get("model_env", ""))
            model_record = _model_record(provider_name, provider, model_env)
            assignments[role] = {
                "provider": provider_name,
                "kind": provider["kind"],
                "model_env": model_env,
                "model_id": model_record["model_id"],
                "model_resolution_status": model_record["model_resolution_status"],
                "blocked_reason": model_record["blocked_reason"],
                "base_url_env": provider.get("base_url_env"),
            }
    request_digest = "sha256:" + sha256_text(request.model_dump_json())
    plan_id = new_id("run")
    created_at = datetime.now(timezone.utc).isoformat()
    wandb = {
        "enabled_env": "AFB_WANDB_ENABLED",
        "project_env": "AFB_WANDB_PROJECT",
        "group": f"asset-factory/{request.id}",
        "tags": ["asset-factory", "simready", "isaac", "manifest-driven"],
    }
    return RunPlan(
        id=plan_id,
        run_id=plan_id,
        request_digest=request_digest,
        created_at=created_at,
        stage_contract_version=str(contracts["version"]),
        asset_id=request.id,
        request_id=request.id,
        objective=request.objective,
        requested_outputs=request.requested_outputs,
        stages=stages,
        provider_assignments=assignments,
        missing_evidence=sorted(set(missing_evidence)),
        validation_gates=sorted({gate for stage in stages for gate in stage.validation_gates}),
        wandb_plan={
            "enabled": os.environ.get("AFB_WANDB_ENABLED", "").lower() in {"1", "true", "yes"},
            "run_group": plan_id,
            "log_artifacts": True,
            "group": wandb["group"],
        },
        wandb=wandb,
        provenance=build_provenance(
            [f"{stage.id}-manifest" for stage in stages],
            provider_model_ids=_provider_model_handles(assignments),
            run_id=plan_id,
        ),
    )


def write_run_plan(request_path: str | Path, output_path: str | Path) -> RunPlan:
    request = load_run_request(request_path)
    plan = build_run_plan(request)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(plan.model_dump(), indent=2) + "\n", encoding="utf-8")
    return plan
