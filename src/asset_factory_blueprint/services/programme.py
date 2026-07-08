from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from asset_factory_blueprint.config import ROOT, load_json
from asset_factory_blueprint.manifests import validate_payload
from asset_factory_blueprint.orchestrator import route_ids
from asset_factory_blueprint.schemas.common import RunRequest
from asset_factory_blueprint.security import confine_path, service_source_roots, service_workspace_roots
from asset_factory_blueprint.skills.base import ToolResult


_CAD_SUFFIXES = {".dwg", ".dxf", ".iges", ".igs", ".step", ".stp"}
_NON_EXACT_VALUES = {"", "default", "latest", "main", "tbd", "todo", "unknown", "unresolved"}


def _normalise_term(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.strip().lower()).strip()


def _missing(field: str, code: str, question: str, reason: str) -> dict[str, str]:
    return {"field": field, "code": code, "question": question, "reason": reason}


def _exact_value(value: Any) -> bool:
    text = str(value or "").strip()
    folded = text.casefold()
    return (
        folded not in _NON_EXACT_VALUES
        and not folded.endswith(".x")
        and not any(marker in text for marker in ("<", ">", "*"))
    )


def _normalise_draft(raw: Any) -> dict[str, Any]:
    draft = dict(raw) if isinstance(raw, dict) else {}
    draft.setdefault("version", "1.0")
    draft.setdefault("constraints", {})
    draft.setdefault("extensions", {})
    return draft


def _requested_deliverables(outputs: list[str]) -> tuple[set[str], list[str]]:
    contracts = load_json("configs/stage-contracts.json")
    aliases = {
        _normalise_term(alias): deliverable
        for deliverable, values in contracts["output_aliases"].items()
        for alias in values
    }
    selected: set[str] = set()
    unknown: list[str] = []
    for output in outputs:
        deliverable = aliases.get(_normalise_term(output))
        if deliverable is None:
            unknown.append(output)
        else:
            selected.add(deliverable)
    return selected, unknown


def _routed_stage_order(selected: set[str]) -> list[str]:
    workflow = load_json("configs/agent-workflow.json")
    return [str(stage["id"]) for stage in workflow["stages"] if stage["id"] in selected]


def _source_blockers(sources: list[str]) -> list[dict[str, str]]:
    missing_inputs: list[dict[str, str]] = []
    cad_sources: list[str] = []
    roots = service_source_roots(ROOT)
    for index, source in enumerate(sources):
        field = f"sources[{index}]"
        raw = str(source or "").strip()
        if not raw:
            missing_inputs.append(
                _missing(field, "source_path_missing", "Where is this source file or directory?", "A source path is empty.")
            )
            continue
        if "://" in raw:
            missing_inputs.append(
                _missing(
                    field,
                    "source_url_not_supported",
                    "Download the source into an authorised source root and provide its local path.",
                    "Agentic start accepts confined local source paths, not URLs.",
                )
            )
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = ROOT / candidate
        try:
            confined = confine_path(candidate, roots)
        except ValueError:
            missing_inputs.append(
                _missing(
                    field,
                    "source_outside_authorised_roots",
                    "Move this source under an authorised source root or configure AFB_SERVICE_SOURCE_ROOTS.",
                    "The tool service cannot read paths outside its authorised roots.",
                )
            )
            continue
        if not confined.exists():
            missing_inputs.append(
                _missing(field, "source_not_found", f"Provide an existing path for {raw}.", "The declared source does not exist.")
            )
            continue
        if confined.suffix.lower() in _CAD_SUFFIXES:
            cad_sources.append(raw)
    if cad_sources:
        missing_inputs.append(
            _missing(
                "sources.converted_asset",
                "native_cad_conversion_unavailable",
                "Export the CAD source to a supported USD or mesh file and use that export as the authoring source.",
                "STEP, IGES, DWG and DXF can be registered as evidence, but native CAD conversion is not implemented.",
            )
        )
    return missing_inputs


def asset_programme_intake(params: dict[str, Any]) -> ToolResult:
    """Validate a partial agent-authored run request and return precise questions."""

    draft = _normalise_draft(params.get("draft"))
    missing_inputs: list[dict[str, str]] = []
    pending_evidence: list[dict[str, str]] = []

    if not str(draft.get("id") or "").strip():
        missing_inputs.append(
            _missing("id", "asset_id_missing", "What stable ID should identify this asset?", "The run request needs an ID.")
        )
    if not str(draft.get("objective") or "").strip():
        missing_inputs.append(
            _missing(
                "objective",
                "objective_missing",
                "What should the factory produce and what will the asset be used for?",
                "Routing and acceptance are measured against an explicit objective.",
            )
        )

    raw_sources = draft.get("sources")
    sources = [str(item) for item in raw_sources] if isinstance(raw_sources, list) else []
    if not sources:
        missing_inputs.append(
            _missing(
                "sources",
                "sources_missing",
                "Which local photo, mesh, USD, robot description, point cloud or CAD-derived export should be used?",
                "At least one source is required to start a project.",
            )
        )
    else:
        missing_inputs.extend(_source_blockers(sources))

    raw_outputs = draft.get("requested_outputs")
    outputs = [str(item) for item in raw_outputs] if isinstance(raw_outputs, list) else []
    deliverables: set[str] = set()
    if not outputs:
        missing_inputs.append(
            _missing(
                "requested_outputs",
                "requested_outputs_missing",
                "What do you need: textures, physics, a SimReady package, nonvisual materials, an RL environment or an evaluation?",
                "At least one supported deliverable is required.",
            )
        )
    else:
        deliverables, unknown_outputs = _requested_deliverables(outputs)
        if unknown_outputs:
            supported = sorted(load_json("configs/stage-contracts.json")["output_aliases"])
            missing_inputs.append(
                _missing(
                    "requested_outputs",
                    "requested_outputs_unknown",
                    "Choose a supported deliverable alias for " + ", ".join(supported) + ".",
                    "Unknown requested output values: " + ", ".join(repr(item) for item in unknown_outputs),
                )
            )

    constraints = draft.get("constraints")
    if not isinstance(constraints, dict):
        missing_inputs.append(
            _missing(
                "constraints",
                "constraints_invalid",
                "Provide constraints as a JSON object.",
                "The run-request contract requires structured constraints.",
            )
        )
        constraints = {}

    if deliverables & {"rl_environment", "simready_package"}:
        profile = constraints.get("simready_profile")
        profile = profile if isinstance(profile, dict) else {}
        if not _exact_value(profile.get("profile_id")):
            missing_inputs.append(
                _missing(
                    "constraints.simready_profile.profile_id",
                    "simready_profile_id_missing",
                    "Which exact SimReady Profile ID applies to this release target?",
                    "The agent must not infer a release Profile.",
                )
            )
        if not _exact_value(profile.get("profile_version")):
            missing_inputs.append(
                _missing(
                    "constraints.simready_profile.profile_version",
                    "simready_profile_version_missing",
                    "Which exact pinned SimReady Profile version applies?",
                    "Values such as latest, main and unresolved are not release identities.",
                )
            )

    if "source_rights" not in constraints:
        pending_evidence.append(
            _missing(
                "constraints.source_rights",
                "source_rights_pending",
                "What rights, licence, permitted uses and redistribution terms apply to each source?",
                "The proposal may start, but release remains blocked until source rights are recorded.",
            )
        )
    if "physics_asset" in deliverables and "physics_evidence" not in constraints:
        pending_evidence.append(
            _missing(
                "constraints.physics_evidence",
                "physics_evidence_pending",
                "Do you have measured, manufacturer-specified or measured-density mass-property evidence?",
                "Physics may begin as a proposal, but accepted mass and inertia cannot be authored without sealed evidence.",
            )
        )

    run_request: RunRequest | None = None
    routed_stages: list[str] = []
    if not missing_inputs:
        schema_issues = validate_payload("run-request", draft)
        if schema_issues:
            missing_inputs.extend(
                _missing(issue.path, issue.code, f"Correct {issue.path}: {issue.message}", issue.message)
                for issue in schema_issues
            )
        else:
            try:
                run_request = RunRequest.model_validate(draft)
                routed_stages = _routed_stage_order(route_ids(run_request))
            except (ValidationError, ValueError) as exc:
                missing_inputs.append(
                    _missing(
                        "run_request",
                        "run_request_invalid",
                        "Correct the run request before starting the factory.",
                        str(exc),
                    )
                )

    ready = not missing_inputs and run_request is not None
    data: dict[str, Any] = {
        "ready": ready,
        "missing_inputs": missing_inputs,
        "pending_evidence": pending_evidence,
        "questions": [item["question"] for item in missing_inputs],
    }
    proposals: list[dict[str, Any]] = []
    if ready and run_request is not None:
        canonical = run_request.model_dump(mode="json")
        data.update({"run_request": canonical, "routed_stages": routed_stages})
        proposals.append({"kind": "run_request", "payload": canonical})
    else:
        data["draft"] = draft
    return ToolResult(
        success=True,
        data=data,
        warnings=[item["reason"] for item in pending_evidence],
        proposals=proposals,
        validation_status="validated" if ready else "blocked",
    )


def asset_factory_start(params: dict[str, Any]) -> ToolResult:
    """Start the full agent loop from a complete, intake-validated run request."""

    intake = asset_programme_intake({"draft": params.get("run_request")})
    if not intake.data.get("ready"):
        return ToolResult(
            success=False,
            data=intake.data,
            error="programme intake is blocked; answer the named questions before starting the factory",
            warnings=intake.warnings,
            validation_status="blocked",
        )

    project_root = Path(str(params.get("project_root") or "projects"))
    if not project_root.is_absolute():
        project_root = ROOT / project_root
    try:
        project_root = confine_path(project_root, service_workspace_roots(ROOT))
    except ValueError as exc:
        return ToolResult(success=False, error=str(exc), validation_status="blocked")

    request = RunRequest.model_validate(intake.data["run_request"])
    from asset_factory_blueprint.agent_loop import run_agent_loop

    try:
        result = run_agent_loop(
            request,
            project_root=project_root,
            project_name=str(params.get("project_name") or "") or None,
            dry_run=bool(params.get("dry_run", True)),
            max_fix_attempts=params.get("max_fix_attempts"),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return ToolResult(
            success=False,
            data={"run_request": intake.data["run_request"]},
            error=f"agentic start failed: {exc}",
            warnings=intake.warnings,
            validation_status="blocked",
        )

    project_dir = Path(result["project_dir"])
    artefacts = [
        (project_dir / "run-request.json").as_posix(),
        str(result.get("agent_report") or ""),
        str(result.get("progress") or ""),
        str(result.get("contact_sheet") or ""),
    ]
    return ToolResult(
        success=True,
        data={"run_request": intake.data["run_request"], "pending_evidence": intake.data["pending_evidence"], **result},
        warnings=intake.warnings,
        artefacts=[item for item in artefacts if item],
        validation_status=str(result.get("status") or "review_required"),
    )
