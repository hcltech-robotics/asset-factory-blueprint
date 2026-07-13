from __future__ import annotations

import json
import math
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asset_factory_blueprint.config import ROOT
from asset_factory_blueprint.execution import (
    atomic_write_json,
    execute_stage_plan,
    immutable_write_json,
    workspace_lease,
    write_run_snapshot,
)
from asset_factory_blueprint.manifests import skeleton, validate_manifest, validate_payload
from asset_factory_blueprint.orchestrator import build_run_plan, load_run_request
from asset_factory_blueprint.provenance import build_provenance, write_prov_jsonld
from asset_factory_blueprint.schemas.common import RunPlan, RunRequest, StagePlan
from asset_factory_blueprint.services.asset_authoring import compose_project_asset
from asset_factory_blueprint.services.fitness import asset_package_fingerprint, evaluate_task_fitness
from asset_factory_blueprint.services.governance import evaluate_release_policy
from asset_factory_blueprint.services.material_inference import material_propose
from asset_factory_blueprint.services.physics_articulation import physics_plan
from asset_factory_blueprint.services.simready import validate_asset_package
from asset_factory_blueprint.security import (
    confine_path,
    in_service_request,
    service_source_roots,
    service_workspace_roots,
)
from asset_factory_blueprint.state import create_project, project_paths
from asset_factory_blueprint.utils.checksums import sha256_file, sha256_text
from asset_factory_blueprint.utils.ids import content_id, slugify, stage_attempt_id
from asset_factory_blueprint.validation import (
    build_project_checksum_inventory,
    validate_pre_release_graph,
    validate_project_graph,
)


STAGE_SCHEMA = {
    "intake": "asset-programme-intake-manifest",
    "source-ingestion": "source-asset-manifest",
    "reconstruction": "reconstruction-manifest",
    "mesh-verification": "mesh-verification-record",
    "segmentation": "segmentation-manifest",
    "material-inference": "material-inference-manifest",
    "texturing": "texturing-manifest",
    "physics-articulation": "physics-articulation-manifest",
    "nonvisual-materials": "nonvisual-material-manifest",
    "simready-verification": "simready-asset-manifest",
    "rl-environment": "rl-environment-manifest",
    "evaluation": "evaluation-manifest",
    "governance": "governance-record",
}

_ACCEPTED_STATUSES = frozenset({"pass", "passed", "validated", "approved", "released"})
_SHA256_PATTERN = re.compile(r"^(?:sha256:)?[A-Fa-f0-9]{64}$")


def write_json(path: Path, payload: Any) -> Path:
    return atomic_write_json(path, payload)


def _evidence_record(path: Path, project_dir: Path, evidence_id: str, kind: str) -> dict[str, str]:
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "uri": path.relative_to(project_dir).as_posix(),
        "checksum": sha256_file(path),
    }


def _provider_trace(stage: StagePlan, plan: RunPlan) -> list[dict[str, str]]:
    traces = []
    for role in stage.provider_roles:
        assignment = plan.provider_assignments.get(role)
        if not assignment:
            continue
        traces.append(
            {
                "provider": assignment.provider,
                "model": assignment.model_env,
                "role": role,
                "prompt_checksum": "dry-run",
            }
        )
    return traces


def _provider_model_handles(plan: RunPlan) -> dict[str, dict[str, str]]:
    handles = {}
    for role, assignment in plan.provider_assignments.items():
        model_id = assignment.model_id or (os.getenv(assignment.model_env, "") if assignment.model_env else "")
        resolved = bool(model_id)
        handles[role] = {
            "provider": assignment.provider,
            "kind": assignment.kind,
            "model_env": assignment.model_env,
            "model_id": model_id,
            "model_resolution_status": assignment.model_resolution_status if resolved else "blocked_unresolved",
            "blocked_reason": assignment.blocked_reason if not resolved else "",
        }
    return handles


def _stage_status(stage: StagePlan) -> str:
    return "blocked" if stage.blocked_reasons else "proposal"


def _copy_source_file(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return {
        "source_path": source.as_posix(),
        "project_copy_path": destination.as_posix(),
        "source_sha256": sha256_file(source),
        "copy_sha256": sha256_file(destination),
        "suffix": source.suffix.lower(),
        "size_bytes": source.stat().st_size,
        "status": "copied",
    }


def _source_rights_records(request: RunRequest, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw = request.constraints.get("source_rights", []) if isinstance(request.constraints, dict) else []
    records = list(raw) if isinstance(raw, list) else []
    keyed = raw if isinstance(raw, dict) else {}
    result: list[dict[str, Any]] = []
    for index, source in enumerate(sources):
        source_id = f"source_{index}"
        candidate: dict[str, Any] = {}
        if index < len(records) and isinstance(records[index], dict):
            candidate = records[index]
        elif keyed:
            value = (
                keyed.get(source.get("source_path"))
                or keyed.get(source.get("project_copy_path"))
                or keyed.get(source_id)
            )
            if isinstance(value, dict):
                candidate = value
        result.append(
            {
                "rights_id": str(candidate.get("rights_id") or f"{request.id}_rights_{index}"),
                "source_id": str(candidate.get("source_id") or source_id),
                "rights_status": str(candidate.get("rights_status") or "unknown"),
                "licence_expression": str(candidate.get("licence_expression") or "NOASSERTION"),
                "terms_uri": candidate.get("terms_uri"),
                "creator": candidate.get("creator"),
                "revision": candidate.get("revision"),
                "attribution": candidate.get("attribution"),
                "permitted_uses": list(candidate.get("permitted_uses") or []),
                "redistribution_allowed": candidate.get("redistribution_allowed") is True,
                "derivatives_allowed": candidate.get("derivatives_allowed") is True,
                "privacy_status": str(candidate.get("privacy_status") or "unknown"),
                "consent_evidence_ids": list(candidate.get("consent_evidence_ids") or []),
                "evidence_ids": list(candidate.get("evidence_ids") or []),
                "expires_at": candidate.get("expires_at"),
                "extensions": dict(candidate.get("extensions") or {}),
            }
        )
    return result


def _localize_governance_evidence(request: RunRequest, project_dir: Path) -> tuple[list[dict[str, str]], list[str]]:
    raw = request.constraints.get("governance_evidence", []) if isinstance(request.constraints, dict) else []
    items = raw if isinstance(raw, list) else []
    evidence: list[dict[str, str]] = []
    blockers: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            blockers.append(f"governance evidence item {index} is not an object")
            continue
        evidence_id = str(item.get("evidence_id") or f"governance_evidence_{index}")
        kind = str(item.get("kind") or "governance_evidence")
        source_path = str(item.get("path") or "")
        if source_path:
            source = Path(source_path)
            if in_service_request():
                try:
                    source = confine_path(source, service_source_roots(ROOT), must_exist=True)
                except (OSError, ValueError) as exc:
                    blockers.append(f"governance evidence path is not authorised: {exc}")
                    continue
            if not source.is_file():
                blockers.append(f"governance evidence path does not exist: {source}")
                continue
            destination = (
                project_dir / "evidence" / "governance" / f"{index}_{slugify(source.stem)}{source.suffix.lower()}"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "kind": kind,
                    "uri": destination.relative_to(project_dir).as_posix(),
                    "checksum": sha256_file(destination),
                }
            )
            continue
        uri = str(item.get("uri") or "")
        checksum = str(item.get("checksum") or "")
        if not uri or not checksum:
            blockers.append(f"governance evidence {evidence_id} requires a local path or URI and checksum")
            continue
        evidence.append({"evidence_id": evidence_id, "kind": kind, "uri": uri, "checksum": checksum})
    return evidence, blockers


def localize_sources(request: RunRequest, project_dir: Path) -> dict[str, Any]:
    source_root = project_dir / "source-assets"
    records: list[dict[str, Any]] = []
    blocked: list[str] = []
    service_mode = in_service_request()
    max_files = int(os.environ.get("AFB_MAX_SOURCE_FILES", "1000" if service_mode else "100000"))
    max_bytes = int(os.environ.get("AFB_MAX_SOURCE_BYTES", str(2 * 1024**3 if service_mode else 20 * 1024**3)))
    copied_file_count = 0
    copied_bytes = 0
    for index, raw_source in enumerate(request.sources):
        source = Path(raw_source)
        if service_mode:
            try:
                source = confine_path(source, service_source_roots(ROOT), must_exist=True)
            except (OSError, ValueError) as exc:
                blocked.append(f"source path is not authorised: {exc}")
                continue
        if not source.exists():
            blocked.append(f"source path does not exist: {source}")
            records.append(
                {
                    "source_path": source.as_posix(),
                    "project_copy_path": "",
                    "source_sha256": "",
                    "copy_sha256": "",
                    "suffix": source.suffix.lower(),
                    "size_bytes": 0,
                    "status": "blocked",
                }
            )
            continue
        if source.is_dir():
            resolved_source = source.resolve(strict=True)
            candidates: list[Path] = []
            for child in sorted(item for item in source.rglob("*") if item.is_file()):
                resolved_child = child.resolve(strict=True)
                if resolved_child != resolved_source and resolved_source not in resolved_child.parents:
                    blocked.append(f"source directory contains a symlink escape: {child}")
                    continue
                candidates.append(resolved_child)
            for child in candidates:
                size = child.stat().st_size
                if copied_file_count + 1 > max_files or copied_bytes + size > max_bytes:
                    blocked.append(f"source ingestion limit exceeded: maximum {max_files} files and {max_bytes} bytes")
                    break
                relative = child.relative_to(resolved_source)
                destination = source_root / f"{index}_{slugify(source.name)}" / relative
                record = _copy_source_file(child, destination)
                record["source_path"] = child.as_posix()
                record["project_copy_path"] = destination.relative_to(project_dir).as_posix()
                records.append(record)
                copied_file_count += 1
                copied_bytes += size
            continue
        size = source.stat().st_size
        if copied_file_count + 1 > max_files or copied_bytes + size > max_bytes:
            blocked.append(f"source ingestion limit exceeded: maximum {max_files} files and {max_bytes} bytes")
            continue
        destination = source_root / f"{index}_{slugify(source.stem)}{source.suffix.lower()}"
        record = _copy_source_file(source, destination)
        record["project_copy_path"] = destination.relative_to(project_dir).as_posix()
        records.append(record)
        copied_file_count += 1
        copied_bytes += size
    governance_evidence, governance_blockers = _localize_governance_evidence(request, project_dir)
    source_rights = _source_rights_records(request, records)
    rights_statuses = {item["rights_status"] for item in source_rights}
    return {
        "source_assets": records,
        "local_copies": [record["project_copy_path"] for record in records if record["status"] == "copied"],
        "source_rights": source_rights,
        "rights_status": "cleared" if source_rights and rights_statuses == {"cleared"} else "pending",
        "governance_evidence": governance_evidence,
        "blocked_reasons": [*blocked, *governance_blockers],
    }


class _WorkflowStageRuntime:
    """Execute concrete stage operations against one project workspace."""

    def __init__(
        self,
        request: RunRequest,
        plan: RunPlan,
        project_dir: Path,
        *,
        dry_run: bool,
        source_ingestion: dict[str, Any] | None = None,
    ) -> None:
        self.request = request
        self.plan = plan
        self.project_dir = project_dir
        self.dry_run = dry_run
        self.source_ingestion = source_ingestion
        self.asset_package: dict[str, Any] | None = None
        self.asset_validation: dict[str, Any] | None = None
        self.task_fitness: dict[str, Any] | None = None

    def execute(self, stage: StagePlan) -> dict[str, Any]:
        record: dict[str, Any] = {
            "producer_id": stage.skill,
            "stage_id": stage.id,
            "execution_mode": stage.execution_mode,
            "status": "pass",
            "artefacts": [],
            "blocked_reasons": [],
        }
        if stage.id == "source-ingestion":
            if self.source_ingestion is None:
                self.source_ingestion = localize_sources(self.request, self.project_dir)
            record["source_count"] = len(self.source_ingestion.get("source_assets", []))
            record["artefacts"] = list(self.source_ingestion.get("local_copies", []))
            record["blocked_reasons"] = list(self.source_ingestion.get("blocked_reasons", []))
        elif stage.id == "reconstruction":
            if self.source_ingestion is None:
                record["blocked_reasons"] = ["source ingestion has not produced source assets"]
            else:
                self.asset_package = compose_project_asset(
                    self.project_dir,
                    self.request.id,
                    self.source_ingestion,
                    self.request.requested_outputs,
                    self.request.constraints,
                    live_texture_generation=not self.dry_run,
                )
                conditioning = self.asset_package.get("mesh_conditioning", {})
                if conditioning.get("status") == "blocked" or not self.asset_package.get("usd_root_path"):
                    record["blocked_reasons"] = [
                        str(conditioning.get("blocked_reason") or "candidate geometry was not produced")
                    ]
                record["artefacts"] = [
                    str(self.asset_package.get("normalised_source_path") or ""),
                    str(self.asset_package.get("usd_root_path") or ""),
                ]
                record["mesh_conditioning"] = conditioning
        elif stage.id == "mesh-verification":
            if self.asset_package is None:
                record["blocked_reasons"] = ["reconstruction has not produced candidate geometry"]
            else:
                from asset_factory_blueprint.services.mesh_verification import prepare_mesh_verification

                external_run = self.asset_package.get("external_reconstruction_run", {})
                candidate_path = external_run.get("mesh_path") or self.asset_package.get("normalised_source_path", "")
                verification = prepare_mesh_verification(
                    self.project_dir,
                    self.request.id,
                    "",
                    candidate_path,
                )
                record["verification"] = verification
                record["artefacts"] = [
                    verification.get("diagnostics_path", ""),
                    *[item.get("uri", "") for item in verification.get("render_bundle", {}).get("images", [])],
                ]
                record["blocked_reasons"] = list(verification.get("blocked_reasons", []))
        elif stage.id == "segmentation":
            segments = list((self.asset_package or {}).get("appearance_segments", []))
            record["segment_count"] = len(segments)
            record["artefacts"] = list((self.asset_package or {}).get("appearance_segment_outputs", []))
            if not segments:
                record["blocked_reasons"] = ["segmentation did not produce semantic appearance segments"]
        elif stage.id == "material-inference":
            segments = list((self.asset_package or {}).get("appearance_segments", []))
            declared_material = self.request.constraints.get("declared_material")
            components = [
                {
                    "prim_path": str(item.get("prim_path") or f"/{self.request.id}"),
                    "label": str(item.get("semantic_label") or item.get("segment_id") or "asset"),
                    "declared_material": declared_material,
                }
                for item in segments
            ]
            result = material_propose(
                {
                    "components": components,
                    "declared_material": declared_material,
                    "evidence_ids": list(self.request.constraints.get("material_evidence_ids") or []),
                    "material_library_id": self.request.constraints.get("material_library_id"),
                }
            )
            record["result"] = result.data
            record["warnings"] = result.warnings
            if result.validation_status == "review_required":
                record["blocked_reasons"] = list(result.warnings) or ["material evidence requires review"]
        elif stage.id == "texturing":
            status = str((self.asset_package or {}).get("texture_generation_status") or "not_requested")
            record["texture_generation_status"] = status
            record["artefacts"] = list((self.asset_package or {}).get("texture_variant_outputs", []))
            if status not in {"generated", "validated"}:
                record["blocked_reasons"] = list((self.asset_package or {}).get("texture_blocked_reasons", [])) or [
                    "requested appearance layers were not generated"
                ]
        elif stage.id == "physics-articulation":
            raw_properties = self.request.constraints.get("physical_properties", [])
            properties = list(raw_properties) if isinstance(raw_properties, list) else []
            result = physics_plan(
                {
                    "properties": properties,
                    "usd_input_path": (self.asset_package or {}).get("usd_root_path"),
                    "usd_output_path": (self.asset_package or {}).get("usd_root_path"),
                    "rigid_bodies": self.request.constraints.get("rigid_bodies", []),
                    "colliders": self.request.constraints.get("colliders", []),
                }
            )
            record["result"] = result.data
            record["warnings"] = result.warnings
            if not result.success:
                record["blocked_reasons"] = list(result.warnings) or ["numeric physics evidence is incomplete"]
        elif stage.id == "simready-verification":
            if self.asset_package is None:
                record["blocked_reasons"] = ["asset package has not been produced"]
            else:
                self.asset_validation = validate_asset_package(
                    self.project_dir,
                    self.asset_package,
                    self.request.requested_outputs,
                )
                record["status"] = self.asset_validation.get("status", "blocked")
                record["artefacts"] = [str(self.asset_validation.get("report_path") or "")]
                record["blocked_reasons"] = list(self.asset_validation.get("blocked_reasons", []))
        elif stage.id == "evaluation":
            if self.asset_validation is None or self.asset_package is None:
                record["blocked_reasons"] = ["machine-readable conformance or asset package results are unavailable"]
            else:
                record["conformance_status"] = self.asset_validation.get("simready_conformance", {}).get("status")
                self.task_fitness = evaluate_task_fitness(
                    self.project_dir,
                    self.request,
                    self.plan,
                    self.asset_package,
                    self.asset_validation,
                )
                record["task_fitness"] = self.task_fitness
                record["artefacts"] = [
                    str(self.asset_validation.get("report_path") or ""),
                    str(self.task_fitness.get("report_path") or ""),
                ]
                if self.task_fitness["status"] != "pass":
                    record["blocked_reasons"] = list(self.task_fitness.get("blocked_reasons", []))
        if record["blocked_reasons"]:
            record["status"] = "blocked"
            stage.blocked_reasons.extend(
                item for item in record["blocked_reasons"] if item not in stage.blocked_reasons
            )
        return record


def _operator_release_decision(project_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """The durable human release decision, if the operator has written one.

    The workflow regenerates every stage manifest on rebuild, so the decision
    lives in its own file at the project root and is only ever authored by a
    person through the bound governance decision command.
    """
    decision_path = project_dir / "operator-release-decision.json"
    if not decision_path.exists():
        return {}, []
    try:
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"operator decision is unreadable: {exc}"]
    if not isinstance(decision, dict):
        return {}, ["operator decision must be a JSON object"]
    issues = validate_payload("operator-release-decision", decision)
    errors = [f"operator decision schema: {issue.render()}" for issue in issues]
    if not issues:
        expected_id = content_id(
            "operator_decision",
            {key: value for key, value in decision.items() if key != "decision_id"},
            digest_length=32,
        )
        if decision.get("decision_id") != expected_id:
            errors.append("operator decision ID does not match its content")
    return decision, errors


def _upstream_stage_reports(
    project_dir: Path,
    plan: RunPlan,
    current_stage_id: str,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Load every preceding report in plan order and materialise failures as blocked reports."""

    reports: list[dict[str, Any]] = []
    blockers: list[str] = []
    required_stage_ids: list[str] = []
    for planned_stage in plan.stages:
        if planned_stage.id == current_stage_id:
            break
        stage_id = planned_stage.id
        required_stage_ids.append(stage_id)
        report_path = project_dir / "reports" / f"{stage_id}-report.json"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            message = f"required upstream stage report {stage_id!r} is missing"
            blockers.append(message)
            reports.append({"stage_id": stage_id, "status": "blocked", "blocked_reasons": [message]})
            continue
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            message = f"required upstream stage report {stage_id!r} is unreadable: {exc}"
            blockers.append(message)
            reports.append({"stage_id": stage_id, "status": "blocked", "blocked_reasons": [message]})
            continue
        if not isinstance(report, dict):
            message = f"required upstream stage report {stage_id!r} must be a JSON object"
            blockers.append(message)
            reports.append({"stage_id": stage_id, "status": "blocked", "blocked_reasons": [message]})
            continue
        if str(report.get("stage_id") or "") != stage_id:
            message = f"required upstream stage report {stage_id!r} declares a different stage ID"
            blockers.append(message)
            reports.append({"stage_id": stage_id, "status": "blocked", "blocked_reasons": [message]})
            continue
        reports.append(report)
    return reports, blockers, required_stage_ids


def _materialised_evidence_index(project_dir: Path) -> dict[str, list[dict[str, str]]]:
    index: dict[str, list[dict[str, str]]] = {}
    for manifest_path in sorted((project_dir / "manifests").glob("*.json")):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for item in payload.get("evidence", []):
            if not isinstance(item, dict) or not item.get("evidence_id"):
                continue
            evidence_id = str(item["evidence_id"])
            record = {
                "uri": str(item.get("uri") or ""),
                "checksum": str(item.get("checksum") or ""),
                "kind": str(item.get("kind") or ""),
            }
            if record not in index.setdefault(evidence_id, []):
                index[evidence_id].append(record)
    return index


def _evidence_reference_errors(
    project_dir: Path,
    evidence_index: dict[str, list[dict[str, str]]],
    evidence_ids: list[str],
    context: str,
) -> list[str]:
    errors: list[str] = []
    if not evidence_ids:
        return [f"{context} requires materialised evidence IDs"]
    project_root = project_dir.resolve(strict=True)
    for evidence_id in evidence_ids:
        records = evidence_index.get(evidence_id, [])
        if len(records) != 1:
            reason = "missing" if not records else "ambiguous"
            errors.append(f"{context} evidence {evidence_id!r} is {reason}")
            continue
        record = records[0]
        uri = record["uri"]
        checksum = record["checksum"]
        if not uri or uri.startswith(("http://", "https://", "s3://", "omniverse://", "hf://")):
            errors.append(f"{context} evidence {evidence_id!r} is not a materialised project file")
            continue
        if not _SHA256_PATTERN.fullmatch(checksum):
            errors.append(f"{context} evidence {evidence_id!r} is not content-addressed")
            continue
        raw_path = Path(uri)
        if raw_path.is_absolute():
            errors.append(f"{context} evidence {evidence_id!r} uses an absolute path")
            continue
        try:
            evidence_path = (project_dir / raw_path).resolve(strict=True)
        except OSError:
            errors.append(f"{context} evidence {evidence_id!r} does not exist")
            continue
        if project_root not in evidence_path.parents or not evidence_path.is_file():
            errors.append(f"{context} evidence {evidence_id!r} escapes the project or is not a file")
            continue
        if sha256_file(evidence_path).lower() != checksum.lower().removeprefix("sha256:"):
            errors.append(f"{context} evidence {evidence_id!r} checksum does not match")
    return errors


def _load_semantic_manifest(
    project_dir: Path,
    schema_name: str,
    asset_id: str,
) -> tuple[dict[str, Any], list[str]]:
    manifest_path = project_dir / "manifests" / f"{schema_name}.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, [f"{schema_name} is missing"]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, [f"{schema_name} is unreadable: {exc}"]
    if not isinstance(payload, dict):
        return {}, [f"{schema_name} must be a JSON object"]
    reasons = [f"{schema_name} schema: {issue.render()}" for issue in validate_payload(schema_name, payload)]
    stage_id = {
        "material-inference-manifest": "material-inference",
        "nonvisual-material-manifest": "nonvisual-materials",
    }[schema_name]
    expected_id = f"{asset_id}_{stage_id}"
    if str(payload.get("asset_id") or "") != asset_id:
        reasons.append(f"{schema_name} belongs to a different asset")
    if str(payload.get("id") or "") != expected_id:
        reasons.append(f"{schema_name} ID does not match the current asset and stage")
    if str(payload.get("status") or "").lower() not in _ACCEPTED_STATUSES:
        reasons.append(f"{schema_name} has not reached an accepted status")
    if str(payload.get("validation_status") or "").lower() not in _ACCEPTED_STATUSES:
        reasons.append(f"{schema_name} validation has not passed")
    if payload.get("blocked_reasons"):
        reasons.append(f"{schema_name} has unresolved blockers")
    return payload, reasons


def _material_evidence_gate(project_dir: Path, asset_id: str) -> dict[str, Any]:
    payload, reasons = _load_semantic_manifest(project_dir, "material-inference-manifest", asset_id)
    evidence_index = _materialised_evidence_index(project_dir)
    components = payload.get("component_materials", []) if payload else []
    if not isinstance(components, list) or not components:
        reasons.append("material inference contains no component material decisions")
    else:
        for index, component in enumerate(components):
            context = f"component_materials[{index}]"
            if not isinstance(component, dict):
                reasons.append(f"{context} must be an object")
                continue
            if not component.get("selected_material") or not component.get("pbr_binding_target"):
                reasons.append(f"{context} lacks a selected material or binding target")
            if str(component.get("selection_status") or "").lower() not in _ACCEPTED_STATUSES:
                reasons.append(f"{context} selection has not been accepted")
            if component.get("requires_human_review") is not False:
                reasons.append(f"{context} still requires human review")
            evidence_ids = [
                str(item)
                for item in [
                    *list(component.get("visual_evidence_ids") or []),
                    *list(component.get("metadata_evidence_ids") or []),
                ]
            ]
            reasons.extend(_evidence_reference_errors(project_dir, evidence_index, evidence_ids, context))
    reasons = list(dict.fromkeys(reasons))
    return {"status": "pass" if not reasons else "blocked", "blocked_reasons": reasons}


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _nonvisual_evidence_gate(project_dir: Path, asset_id: str) -> dict[str, Any]:
    payload, reasons = _load_semantic_manifest(project_dir, "nonvisual-material-manifest", asset_id)
    evidence_index = _materialised_evidence_index(project_dir)
    if payload and payload.get("numeric_values_from_visual_evidence") is not False:
        reasons.append("nonvisual material manifest does not attest that visual evidence was excluded")
    if payload and payload.get("review_status") != "approved":
        reasons.append("nonvisual material evidence requires explicit approval")
    properties = payload.get("properties", []) if payload else []
    if not isinstance(properties, list) or not properties:
        reasons.append("nonvisual material manifest contains no accepted properties")
    else:
        for index, item in enumerate(properties):
            context = f"nonvisual properties[{index}]"
            if not isinstance(item, dict):
                reasons.append(f"{context} must be an object")
                continue
            if str(item.get("validation_status") or "").lower() not in _ACCEPTED_STATUSES:
                reasons.append(f"{context} has not been validated")
            if not item.get("unit") or not item.get("method"):
                reasons.append(f"{context} lacks units or a measurement method")
            has_value = _finite_number(item.get("value"))
            has_range = (
                _finite_number(item.get("range_low"))
                and _finite_number(item.get("range_high"))
                and float(item["range_low"]) <= float(item["range_high"])
            )
            if not has_value and not has_range:
                reasons.append(f"{context} has no finite measured value or validated range")
            evidence_ids = [str(evidence_id) for evidence_id in item.get("evidence_ids", [])]
            reasons.extend(_evidence_reference_errors(project_dir, evidence_index, evidence_ids, context))
    reasons = list(dict.fromkeys(reasons))
    return {"status": "pass" if not reasons else "blocked", "blocked_reasons": reasons}


def _accepted_physical_properties(
    project_dir: Path,
    raw_properties: Any,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    evidence_index = _materialised_evidence_index(project_dir)
    accepted: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    if not isinstance(raw_properties, list):
        return {}, ["physical_properties must be an array"]
    for index, item in enumerate(raw_properties):
        context = f"physical_properties[{index}]"
        if not isinstance(item, dict):
            reasons.append(f"{context} must be an object")
            continue
        property_name = str(item.get("property_name") or "")
        if not property_name:
            reasons.append(f"{context} lacks a property name")
            continue
        if str(item.get("validation_status") or "").lower() not in _ACCEPTED_STATUSES:
            reasons.append(f"{context} has not been accepted")
            continue
        evidence_ids = [str(evidence_id) for evidence_id in item.get("evidence_ids", [])]
        evidence_errors = _evidence_reference_errors(project_dir, evidence_index, evidence_ids, context)
        if evidence_errors:
            reasons.extend(evidence_errors)
            continue
        value = item.get("value")
        if property_name in {"mass", "density"}:
            valid_value = _finite_number(value) and float(value) > 0
        elif property_name in {"center_of_mass", "diagonal_inertia", "principal_axes"}:
            valid_value = (
                isinstance(value, list)
                and len(value) in {3, 4}
                and all(_finite_number(component) for component in value)
                and (property_name != "diagonal_inertia" or all(float(component) > 0 for component in value))
            )
        else:
            continue
        if not valid_value:
            reasons.append(f"{context} has an invalid accepted value")
            continue
        if property_name in accepted:
            reasons.append(f"accepted physical property {property_name!r} is duplicated")
            continue
        accepted[property_name] = item
    return accepted, list(dict.fromkeys(reasons))


def _enrich_manifest(
    payload: dict[str, Any],
    request: RunRequest,
    plan: RunPlan,
    stage: StagePlan,
    project_id: str,
    run_plan_path: Path,
    project_dir: Path,
    source_ingestion: dict[str, Any] | None = None,
    asset_package: dict[str, Any] | None = None,
    asset_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload["id"] = f"{request.id}_{stage.id}"
    payload["version"] = "1.0"
    payload["status"] = _stage_status(stage)
    payload["asset_id"] = request.id
    payload["project_id"] = project_id
    payload["evidence"] = [_evidence_record(run_plan_path, project_dir, "run_plan", "run_plan")]
    payload["provider_trace"] = _provider_trace(stage, plan)
    payload["provenance"] = plan.provenance
    payload["review_status"] = (
        "review_required" if stage.id in {"physics-articulation", "governance"} else "not_reviewed"
    )
    payload["validation_status"] = _stage_status(stage)
    payload["validation_gates"] = [
        {"gate_id": gate, "status": "blocked" if stage.blocked_reasons else "pending"}
        for gate in stage.validation_gates
    ]
    payload["blocked_reasons"] = list(stage.blocked_reasons)
    if stage.id == "source-ingestion" and source_ingestion:
        payload["version"] = "2.0"
        payload["source_assets"] = source_ingestion["source_assets"]
        payload["local_copies"] = source_ingestion["local_copies"]
        payload["source_assets_mutated"] = False
        payload["source_rights"] = source_ingestion.get("source_rights", [])
        payload["rights_status"] = source_ingestion.get("rights_status", "pending")
        payload["unit_policy"] = "preserve_source_units_until_declared"
        payload["status"] = "blocked" if source_ingestion["blocked_reasons"] else "proposal"
        payload["validation_status"] = payload["status"]
        payload["blocked_reasons"] = source_ingestion["blocked_reasons"]
        payload["evidence"].extend(
            {
                "evidence_id": f"source_copy_{index}",
                "kind": "source_copy",
                "uri": record["project_copy_path"],
                "checksum": record["copy_sha256"],
            }
            for index, record in enumerate(source_ingestion["source_assets"])
            if record["status"] == "copied"
        )
        payload["evidence"].extend(source_ingestion.get("governance_evidence", []))
    if stage.id == "reconstruction" and asset_package:
        source_inspection = asset_package.get("source_inspection", {})
        reconstruction_required = bool(source_inspection.get("reconstruction_required"))
        payload["reconstruction_route"] = "proxy_geometry" if reconstruction_required else "not_required_for_usd_source"
        payload["usd_output_path"] = asset_package.get("normalised_source_path", "")
        payload["source_kind"] = source_inspection.get("inspection_status", "unknown")
        payload["component_taxonomy"] = [
            {
                "component_id": "root_proxy" if reconstruction_required else "source_geometry",
                "prim_path": f"/{asset_package.get('asset_id', request.id)}/Geometry",
                "status": "proposal",
            }
        ]
        external_run = asset_package.get("external_reconstruction_run", {})
        candidate_path = external_run.get("mesh_path") or asset_package.get("normalised_source_path", "")
        candidate_checksum = ""
        if candidate_path and Path(candidate_path).exists():
            candidate_checksum = sha256_file(Path(candidate_path))
        payload["candidate_geometry_path"] = candidate_path
        payload["candidate_geometry_checksum"] = candidate_checksum
        payload["review_status"] = "review_required"
        if reconstruction_required and external_run:
            payload["status"] = "review_required"
            payload["validation_status"] = "review_required"
            payload["external_run_id"] = external_run.get("run_id", "")
            payload["review_notes"] = [
                "external reconstruction run recorded; mandatory mesh verification required before canonical promotion"
            ]
            payload["evidence"].append(
                {
                    "evidence_id": "external_reconstruction_mesh",
                    "kind": "reconstructed_mesh",
                    "uri": external_run.get("mesh_path", ""),
                    "checksum": external_run.get("mesh_sha256", ""),
                }
            )
        elif reconstruction_required:
            payload["status"] = "blocked"
            payload["validation_status"] = "blocked"
            payload["blocked_reasons"] = ["external reconstruction validation required before release"]
        payload["evidence"].append(
            {
                "evidence_id": "reconstruction_output",
                "kind": "candidate_geometry",
                "uri": candidate_path,
                "checksum": candidate_checksum
                or next(
                    (
                        item["sha256"]
                        for item in asset_package.get("files", [])
                        if item["path"] == asset_package.get("normalised_source_path", "")
                    ),
                    "",
                ),
            }
        )
    if stage.id == "segmentation" and asset_package:
        segments = asset_package.get("appearance_segments", [])
        payload["source_manifest_id"] = f"{request.id}_source-ingestion"
        payload["reconstruction_manifest_id"] = f"{request.id}_reconstruction"
        payload["segmentation_kind"] = "appearance_material_regions"
        payload["segmentation_status"] = "proposal"
        payload["appearance_segments"] = segments
        payload["segment_masks"] = [
            {
                "segment_id": segment.get("segment_id", ""),
                "mask_path": asset_package.get("asset_dir", "") + "/" + segment.get("mask_path", ""),
                "checksum": next(
                    (
                        item["sha256"]
                        for item in asset_package.get("files", [])
                        if item["path"] == asset_package.get("asset_dir", "") + "/" + segment.get("mask_path", "")
                    ),
                    "",
                ),
                "status": segment.get("selection_status", "proposal"),
            }
            for segment in segments
        ]
        payload["material_regions"] = [
            {
                "segment_id": segment.get("segment_id", ""),
                "label": segment.get("label", ""),
                "prim_path": segment.get("prim_path", ""),
                "material_name": segment.get("material_name", ""),
                "material_prim_path": segment.get("material_prim_path", ""),
                "semantic_class": segment.get("semantic_class", ""),
            }
            for segment in segments
        ]
        payload["downstream_consumers"] = ["material-inference", "texturing", "simready-verification"]
        payload["source_assets_mutated"] = False
    if stage.id == "material-inference" and asset_package:
        segments = asset_package.get("appearance_segments", [])
        payload["source_manifest_id"] = f"{request.id}_source-ingestion"
        payload["segmentation_manifest_id"] = f"{request.id}_segmentation"
        payload["material_library_id"] = "asset_factory_default_materials"
        payload["component_materials"] = [
            {
                "prim_path": segment.get("prim_path", f"/{asset_package.get('asset_id', request.id)}"),
                "component_label": segment.get("label", segment.get("segment_id", "asset_root")),
                "segment_id": segment.get("segment_id", ""),
                "mask_path": segment.get("mask_path", ""),
                "candidate_materials": [segment.get("material_name", "painted_metal"), "hard_plastic"],
                "selected_material": segment.get("material_name", "painted_metal"),
                "selection_status": "proposal",
                "pbr_binding_target": segment.get(
                    "material_prim_path", asset_package.get("asset_dir", "") + "/mtl.usda"
                ),
                "visual_evidence_ids": [f"appearance_segment_{segment.get('segment_id', 'root')}"],
                "metadata_evidence_ids": segment.get("source_evidence_ids", ["source_copy_0"]),
                "confidence": segment.get("confidence", 0.35),
                "uncertainty_reason": "appearance segment proposal requires material review before promotion",
                "requires_human_review": True,
            }
            for segment in segments
        ] or [
            {
                "prim_path": f"/{asset_package.get('asset_id', request.id)}",
                "component_label": "asset_root",
                "candidate_materials": ["painted_metal", "hard_plastic"],
                "selected_material": "painted_metal",
                "selection_status": "proposal",
                "pbr_binding_target": asset_package.get("asset_dir", "") + "/mtl.usda",
                "visual_evidence_ids": ["generated_asset_2"],
                "metadata_evidence_ids": ["source_copy_0"],
                "confidence": 0.35,
                "uncertainty_reason": "default proposal from source metadata; reviewer or material evidence required before promotion",
                "requires_human_review": True,
            }
        ]
        payload["material_bindings_authored_in"] = asset_package.get("asset_dir", "") + "/mtl.usda"
        payload["physical_property_proposals"] = [
            {
                "prim_path": f"/{asset_package.get('asset_id', request.id)}",
                "property_name": "mass",
                "property_group": "rigid_body",
                "value": None,
                "unit": "kg",
                "range_low": None,
                "range_high": None,
                "distribution": "not_established",
                "method": "no accepted materialised physical evidence supplied",
                "confidence": 0.0,
                "evidence_ids": [],
                "validation_status": "review_required",
                "notes": "mass remains unset until measured or specified evidence is accepted",
            }
        ]
        payload["numeric_values_from_visual_evidence"] = False
    if stage.id == "texturing" and asset_package:
        asset_dir = asset_package.get("asset_dir", "")
        checksum_by_path = {item["path"]: item["sha256"] for item in asset_package.get("files", [])}
        segments = asset_package.get("appearance_segments", [])
        texture_variants = asset_package.get("texture_variants") or []
        if not texture_variants and asset_package.get("texture_outputs"):
            default_paths = asset_package.get("texture_outputs", [])
            texture_variants = [
                {
                    "variant_id": "default",
                    "material_name": "painted_metal",
                    "texture_intent": "neutral default PBR maps aligned with material proposal",
                    "prompt": "neutral painted metal base colour with no baked lighting",
                    "negative_prompt": "cast shadows, baked highlights, text, logos, watermarks",
                    "provider_role": "texture_generator",
                    "seed": 0,
                    "resolution": "2x2 smoke map",
                    "tileable": True,
                    "base_color_path": Path(default_paths[0]).name if len(default_paths) > 0 else "",
                    "normal_path": Path(default_paths[1]).name if len(default_paths) > 1 else "",
                    "roughness_path": Path(default_paths[2]).name if len(default_paths) > 2 else "",
                    "metallic_path": Path(default_paths[3]).name if len(default_paths) > 3 else "",
                    "height_or_displacement_path": "",
                    "status": "proposal",
                }
            ]

        def asset_rel(value: str) -> str:
            if not value:
                return ""
            if value.startswith("assets/") or value.startswith("packaged/"):
                return value
            return f"{asset_dir}/{value}"

        source_assets = (source_ingestion or {}).get("source_assets", [])
        source_image = next(
            (
                record
                for record in source_assets
                if Path(str(record.get("project_copy_path", ""))).suffix.lower()
                in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
            ),
            None,
        )
        texture_dir = asset_package.get("asset_dir", "") + "/textures"
        texture_generation_status = str(asset_package.get("texture_generation_status", "not_requested"))
        texture_blocked_reasons = list(asset_package.get("texture_blocked_reasons", []))
        real_texture_status = texture_generation_status in {"generated", "validated"}
        texture_generation_blocked = texture_generation_status == "blocked" or (
            texture_variants and not real_texture_status
        )
        if texture_generation_blocked:
            payload["status"] = "blocked"
            payload["validation_status"] = "blocked"
            payload["blocked_reasons"].extend(
                texture_blocked_reasons or ["texture synthesis did not produce generated PBR texture maps"]
            )
        payload["material_manifest_id"] = f"{request.id}_material-inference"
        payload["segmentation_manifest_id"] = f"{request.id}_segmentation"
        payload["uv_readiness_status"] = "asset_level_binding_ready"
        payload["texture_generation_status"] = texture_generation_status
        payload["texture_blocked_reasons"] = texture_blocked_reasons
        payload["texture_generation_backend"] = asset_package.get("texture_generation_backend", "")
        payload["provider_trace"] = asset_package.get("texture_provider_trace", [])
        payload["texture_map_policy_trace"] = asset_package.get("texture_map_policy_trace", [])
        payload["texture_prompt_plan"] = asset_package.get("texture_prompt_plan", [])
        payload["texture_requests"] = [
            {
                "material_name": segment.get("material_name", item.get("material_name", "painted_metal")),
                "prim_paths": [segment.get("prim_path", f"/{asset_package.get('asset_id', request.id)}")],
                "segment_id": segment.get("segment_id", ""),
                "mask_path": asset_dir + "/" + segment.get("mask_path", ""),
                "texture_intent": (
                    item.get("texture_intent", "material variant proposal") + " for " + segment.get("label", "asset")
                ).strip(),
                "prompt": item.get("prompt", ""),
                "negative_prompt": item.get("negative_prompt", ""),
                "provider_role": item.get("provider_role", "texture_generator"),
                "seed": item.get("seed", 0),
                "resolution": item.get("resolution", "2x2 smoke map"),
                "tileable": item.get("tileable", True),
                "allowed_variation_bounds": {
                    "numeric_physics_authored": False,
                    "source_silhouette_preserved": True,
                },
                "variant_id": item.get("variant_id", "default"),
                "reference_image": item.get("reference_image", ""),
                "reference_role": item.get("reference_role", ""),
            }
            for item in texture_variants
            for segment in (
                segments
                or [
                    {
                        "prim_path": f"/{asset_package.get('asset_id', request.id)}",
                        "label": "asset",
                        "segment_id": "",
                        "mask_path": "",
                        "material_name": item.get("material_name", "painted_metal"),
                    }
                ]
            )
        ] or [
            {
                "material_name": "painted_metal",
                "prim_paths": [f"/{asset_package.get('asset_id', request.id)}"],
                "texture_intent": "neutral default PBR maps aligned with material proposal",
                "prompt": "neutral painted metal base colour with no baked lighting",
                "negative_prompt": "cast shadows, baked highlights, text, logos, watermarks",
                "provider_role": "texture_generator",
                "seed": 0,
                "resolution": "2x2 smoke map",
                "tileable": True,
                "allowed_variation_bounds": {"numeric_physics_authored": False},
            }
        ]
        payload["texture_outputs"] = [
            {
                "base_color_path": asset_rel(item.get("base_color_path", "")),
                "normal_path": asset_rel(item.get("normal_path", "")),
                "roughness_path": asset_rel(item.get("roughness_path", "")),
                "metallic_path": asset_rel(item.get("metallic_path", "")),
                "height_or_displacement_path": asset_rel(item.get("height_or_displacement_path", "")),
                "usd_output_path": asset_package.get("asset_dir", "") + "/mtl.usda",
                "checksum": checksum_by_path.get(asset_rel(item.get("base_color_path", "")), ""),
                "status": item.get("status", "proposal"),
                "generation_method": item.get("generation_method", ""),
                "is_generated_texture": bool(item.get("is_generated_texture", False)),
                "generated_map_kinds": item.get("generated_map_kinds", []),
                "policy_map_kinds": item.get("policy_map_kinds", []),
                "material_prompt": item.get("material_prompt", ""),
                "variant_id": item.get("variant_id", "default"),
                "material_regions": [
                    {
                        "segment_id": segment.get("segment_id", ""),
                        "prim_path": segment.get("prim_path", ""),
                        "mask_path": asset_dir + "/" + segment.get("mask_path", ""),
                        "material_name": segment.get("material_name", ""),
                    }
                    for segment in segments
                ],
            }
            for item in texture_variants
        ]
        payload["source_image_review"] = {
            "display_surface": "operator_review",
            "source_image_path": source_image.get("project_copy_path", "") if source_image else "",
            "source_image_checksum": source_image.get("copy_sha256", "") if source_image else "",
            "status": "review_required" if source_image else "not_available",
            "usage": ["texture_reference", "deformation_reference", "review_evidence"],
            "source_assets_mutated": False,
        }
        payload["texture_variants"] = [
            {
                **item,
                "base_color_path": asset_rel(item.get("base_color_path", "")),
                "normal_path": asset_rel(item.get("normal_path", "")),
                "roughness_path": asset_rel(item.get("roughness_path", "")),
                "metallic_path": asset_rel(item.get("metallic_path", "")),
                "generation_method": item.get("generation_method", ""),
                "is_generated_texture": bool(item.get("is_generated_texture", False)),
                "material_regions": [
                    {
                        "segment_id": segment.get("segment_id", ""),
                        "label": segment.get("label", ""),
                        "prim_path": segment.get("prim_path", ""),
                        "mask_path": asset_dir + "/" + segment.get("mask_path", ""),
                        "material_name": segment.get("material_name", ""),
                    }
                    for segment in segments
                ],
            }
            for item in texture_variants
        ]
        payload["appearance_segments"] = segments
        payload["mesh_deformation_requests"] = [
            {
                **item,
                "height_or_displacement_path": asset_rel(item.get("height_or_displacement_path", "")),
            }
            for item in asset_package.get("mesh_deformation_requests", [])
        ]
        payload["decals"] = [
            {
                "decal_id": item.get("decal_id", f"decal_{index}"),
                "target": item.get("target", ""),
                "semantic_label": item.get("semantic_label", ""),
                "placement": item.get("placement", {}),
                "source": item.get("source", ""),
                "source_evidence_ids": item.get("source_evidence_ids", []),
                "status": item.get("status", "proposal"),
            }
            for index, item in enumerate(asset_package.get("decals", []))
        ]
        payload["variant_usd_path"] = asset_package.get("asset_dir", "") + "/variants.usda"
        payload["deformation_usd_path"] = asset_package.get("deformation_usd_path", "")
        payload["render_evidence"] = [
            {
                "kind": "source_image_review",
                "uri": payload["source_image_review"]["source_image_path"],
                "status": payload["source_image_review"]["status"],
            },
            {
                "kind": "texture_files",
                "uri": texture_dir,
                "status": texture_generation_status if texture_variants else "not_requested",
            },
            {
                "kind": "appearance_segment_masks",
                "uri": asset_package.get("asset_dir", "") + "/textures/segments",
                "status": "generated" if segments else "not_available",
            },
            {
                "kind": "mesh_deformation_files",
                "uri": asset_package.get("asset_dir", "") + "/deformations",
                "status": "generated" if payload["mesh_deformation_requests"] else "not_requested",
            },
        ]
        if real_texture_status:
            payload["validation_status"] = "proposal"
        payload["physical_consistency"] = {
            "numeric_physics_authored": False,
            "material_manifest_required": f"{request.id}_material-inference",
            "segmentation_manifest_required": f"{request.id}_segmentation",
            "physical_property_source_manifest_required": f"{request.id}_material-inference",
            "mesh_deformation_requires_geometry_review": bool(payload["mesh_deformation_requests"]),
        }
    if stage.id == "nonvisual-materials" and asset_package:
        payload["source_manifest_id"] = f"{request.id}_source-ingestion"
        payload["material_manifest_id"] = f"{request.id}_material-inference"
        payload["segmentation_manifest_id"] = f"{request.id}_segmentation"
        payload["uncertainty_policy"] = "visual evidence cannot promote hidden physical values"
        payload["properties"] = [
            {
                "prim_path": f"/{asset_package.get('asset_id', request.id)}",
                "property_name": "thermal_conductivity",
                "property_group": "thermal",
                "value": None,
                "unit": "W/(m K)",
                "range_low": None,
                "range_high": None,
                "distribution": "review_required_range",
                "method": "material-class default requires measurement or review",
                "confidence": 0.0,
                "evidence_ids": ["source_copy_0"],
                "validation_status": "needs_measurement",
                "notes": "nonvisual material values are proposal-only until measured, specified or reviewed",
            }
        ]
        payload["numeric_values_from_visual_evidence"] = False
    if stage.id == "physics-articulation" and asset_package:
        accepted_properties, physical_evidence_errors = _accepted_physical_properties(
            project_dir,
            request.constraints.get("physical_properties", []),
        )
        mass_evidence = accepted_properties.get("mass")
        density_evidence = accepted_properties.get("density")
        inertia_evidence = accepted_properties.get("diagonal_inertia")
        centre_evidence = accepted_properties.get("center_of_mass")
        axes_evidence = accepted_properties.get("principal_axes")
        core_physics_accepted = mass_evidence is not None or density_evidence is not None
        accepted_evidence_ids = sorted(
            {str(evidence_id) for item in accepted_properties.values() for evidence_id in item.get("evidence_ids", [])}
        )
        if not core_physics_accepted:
            physical_evidence_errors.append("accepted mass or density evidence is required")
        physical_evidence_errors = list(dict.fromkeys(physical_evidence_errors))
        payload["material_manifest_id"] = f"{request.id}_material-inference"
        if any(item.id == "nonvisual-materials" for item in plan.stages):
            payload["nonvisual_material_manifest_id"] = f"{request.id}_nonvisual-materials"
        payload["usd_input_path"] = asset_package.get("usd_root_path", "")
        payload["usd_output_path"] = asset_package.get("asset_dir", "") + "/phy.usda"
        payload["articulation_usd_path"] = asset_package.get("asset_dir", "") + "/art.usda"
        payload["physics_authoring_plan"] = [
            {
                "target_layer": payload["usd_output_path"],
                "operation": "author rigid body, collider and mass-property proposal on project copy",
                "preconditions": [
                    "units_policy_declared",
                    "scale_policy_declared",
                    "physical_properties_review_required",
                ],
            }
        ]
        payload["rigid_bodies"] = [
            {
                "prim_path": f"/{asset_package.get('asset_id', request.id)}",
                "rigid_body_enabled": True,
                "kinematic_enabled": False,
                "simulation_owner": "isaac_sim",
                "starts_asleep": False,
                "source_evidence_ids": accepted_evidence_ids,
                "authoring_status": "validated" if core_physics_accepted else "review_required",
            }
        ]
        payload["colliders"] = [
            {
                "prim_path": f"/{asset_package.get('asset_id', request.id)}",
                "collision_enabled": True,
                "approximation": "convexHull",
                "reason": "smoke authoring path for project-local static asset",
                "validation_status": "proposal",
            }
        ]
        payload["physics_materials"] = [
            {
                "prim_path": f"/{asset_package.get('asset_id', request.id)}/PhysicsMaterials/DefaultPhysicsMaterial",
                "binding_target": f"/{asset_package.get('asset_id', request.id)}",
                "binding_purpose": "physics",
                "static_friction": None,
                "dynamic_friction": None,
                "status": "review_required",
            }
        ]
        payload["mass_properties"] = [
            {
                "prim_path": f"/{asset_package.get('asset_id', request.id)}",
                "mass": mass_evidence.get("value") if mass_evidence else None,
                "density": density_evidence.get("value") if density_evidence else None,
                "center_of_mass": centre_evidence.get("value") if centre_evidence else None,
                "diagonal_inertia": inertia_evidence.get("value") if inertia_evidence else None,
                "principal_axes": axes_evidence.get("value") if axes_evidence else None,
                "method": (
                    str((mass_evidence or density_evidence or {}).get("method") or "accepted materialised evidence")
                    if core_physics_accepted
                    else "blocked_pending_accepted_materialised_evidence"
                ),
                "unit_policy": "meters_per_unit_1",
                "confidence": (mass_evidence or density_evidence or {}).get("confidence"),
                "evidence_ids": accepted_evidence_ids,
                "validation_status": "validated" if core_physics_accepted else "review_required",
                "blocked_reasons": physical_evidence_errors,
            }
        ]
        payload["validation_gates"] = [
            {
                "gate_id": "units-and-scale-known",
                "status": "pass",
                "evidence_path": asset_package.get("usd_root_path", ""),
            },
            {"gate_id": "physics-layer-authored", "status": "pass", "evidence_path": payload["usd_output_path"]},
            {
                "gate_id": "numeric-physics-review",
                "status": "pass" if core_physics_accepted else "blocked",
                "evidence_path": payload["usd_output_path"] if core_physics_accepted else "",
            },
        ]
        if physical_evidence_errors:
            payload["review_status"] = "review_required"
            payload["blocked_reasons"].extend(
                reason for reason in physical_evidence_errors if reason not in payload["blocked_reasons"]
            )
        payload["tuning_scenarios"] = [{"scenario": "isaac_drop_smoke", "status": "blocked_until_isaac_load"}]
        articulation = asset_package.get("articulation", {})
        articulation_authored = articulation.get("status") == "authored"
        payload["validation_gates"].append(
            {
                "gate_id": "articulation-schema-authored",
                "status": "pass" if articulation_authored else "skipped",
                "evidence_path": payload["articulation_usd_path"],
            }
        )
        body_paths = list(articulation.get("body_paths", []))
        payload["part_graph"] = [{"prim_path": path, "status": "rigid_body"} for path in body_paths] or [
            {"prim_path": f"/{asset_package.get('asset_id', request.id)}", "status": "static_root"}
        ]
        payload["joints"] = [
            {
                "joint_name": joint["name"],
                "joint_type": joint["type"],
                "body0": joint["body0"],
                "body1": joint["body1"],
                "axis": joint["axis"],
                "local_pos0": joint.get("local_pos0"),
                "local_rot0": joint.get("local_rot0"),
                "local_pos1": joint.get("local_pos1"),
                "local_rot1": joint.get("local_rot1"),
                "lower_limit": joint.get("lower_limit"),
                "upper_limit": joint.get("upper_limit"),
                "limit_unit": "degrees" if joint["type"] == "revolute" else "metres",
                "collision_enabled": False,
                "source_evidence_ids": joint.get("source_evidence_ids", []),
                "status": "authored" if articulation_authored else "blocked",
            }
            for joint in articulation.get("joints", [])
        ]
        payload["drives"] = [
            {"joint_name": joint["name"], **joint["drive"]}
            for joint in articulation.get("joints", [])
            if joint.get("drive")
        ]
        payload["limits"] = [
            {
                "joint_name": joint["name"],
                "lower_limit": joint.get("lower_limit"),
                "upper_limit": joint.get("upper_limit"),
                "unit": "degrees" if joint["type"] == "revolute" else "metres",
            }
            for joint in articulation.get("joints", [])
            if joint["type"] != "fixed"
        ]
        payload["articulation_roots"] = (
            [{"prim_path": articulation["articulation_root_path"], "status": "authored"}]
            if articulation_authored
            else [
                {
                    "prim_path": f"/{asset_package.get('asset_id', request.id)}",
                    "status": "not_authored_no_joint_evidence",
                }
            ]
        )
        payload["collision_filters"] = []
        payload["affordances"] = {
            "grasp_points": [],
            "affordance_labels": ["static_asset"],
            "status": "proposal_requires_affordance_evidence",
        }
        payload["validation_scenarios"] = [
            {
                "scenario": "joint_sweep_and_limit_enforcement",
                "status": "pending_runtime_validation" if articulation_authored else "not_applicable_static_asset",
            }
        ]
    if stage.id == "simready-verification" and asset_package:
        payload["package_id"] = f"{request.id}_package"
        payload["usd_root_path"] = asset_package.get("usd_root_path", "")
        payload["usd_layer_stack"] = asset_package.get("usd_layer_stack", [])
        payload["material_representations"] = asset_package.get("material_representations", {})
        payload["material_manifest_id"] = f"{request.id}_material-inference"
        payload["physics_articulation_manifest_id"] = f"{request.id}_physics-articulation"
        payload["segmentation_manifest_id"] = f"{request.id}_segmentation"
        payload["units_policy"] = "meters_per_unit_1"
        payload["axis_policy"] = "z_up_normalised_root"
        payload["simready_profile"] = (asset_validation or {}).get("simready_profile", {})
        payload["simready_conformance"] = (asset_validation or {}).get("simready_conformance", {})
        payload["package_dependency_closure"] = (asset_validation or {}).get("package_dependency_closure", {})
        conformance_status = payload["simready_conformance"].get("status", "blocked")
        payload["promotion_status"] = "review_required" if conformance_status == "pass" else "failed"
        # an applied load check must survive rebuilds; rehydrate from the report
        isaac_check: dict[str, Any] = {
            "status": "pending",
            "reason": "run isaac load check against generated USD root",
            "usd_root_path": asset_package.get("usd_root_path", ""),
        }
        runtime_validation = payload["simready_conformance"].get("runtime_validation", {})
        isaac_report_path = project_dir / "reports" / "isaac-load-check.json"
        if runtime_validation:
            isaac_check = {
                "status": str(runtime_validation.get("status") or "blocked"),
                "reason": str(runtime_validation.get("reason") or "runtime behavioural validation is incomplete"),
                "usd_root_path": asset_package.get("usd_root_path", ""),
                "report_path": str(runtime_validation.get("report_path") or "reports/isaac-load-check.json"),
                "loaded": runtime_validation.get("status") == "pass",
                "behavioural_tests": runtime_validation.get("behavioural_tests", []),
                "required_test_ids": runtime_validation.get("required_test_ids", []),
                "performance": runtime_validation.get("performance", {}),
            }
        elif isaac_report_path.exists():
            try:
                isaac_report = json.loads(isaac_report_path.read_text(encoding="utf-8"))
                isaac_check = {
                    "status": str(isaac_report.get("status") or "pending"),
                    "reason": "rehydrated from reports/isaac-load-check.json",
                    "usd_root_path": asset_package.get("usd_root_path", ""),
                    "report_path": "reports/isaac-load-check.json",
                    "loaded": bool(isaac_report.get("loaded")),
                    "prim_count": int(isaac_report.get("prim_count") or 0),
                }
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        payload["isaac_sim_load_check"] = isaac_check
        payload["performance_budget"] = {
            "status": "measured" if isaac_check.get("performance") else "pending",
            "requires_generated_asset_measurement": not bool(isaac_check.get("performance")),
            "measurements": isaac_check.get("performance", {}),
        }
        isaac_gate: dict[str, Any] = {
            "gate_id": "isaac-load",
            "gate_type": "isaac",
            "status": "blocked",
            "evidence_path": asset_package.get("usd_root_path", ""),
            "repair_action": "run Isaac Sim load validation",
            "rerun_required": True,
        }
        if isaac_check.get("status") == "pass":
            isaac_gate.update(
                {
                    "status": "pass",
                    "evidence_path": "reports/isaac-load-check.json",
                    "repair_action": "",
                    "rerun_required": False,
                }
            )
        payload["validation_results"] = [
            *(asset_validation or {}).get("validation_results", []),
            isaac_gate,
        ]
        payload["evidence"].extend(
            {
                "evidence_id": f"generated_asset_{index}",
                "kind": "generated_usd_layer",
                "uri": item["path"],
                "checksum": item["sha256"],
            }
            for index, item in enumerate(asset_package.get("files", []))
        )
        if asset_validation:
            payload["evidence"].append(
                {
                    "evidence_id": "generated_asset_validation",
                    "kind": "validation_report",
                    "uri": asset_validation.get("report_path", ""),
                    "checksum": asset_validation.get("report_sha256", ""),
                }
            )
        payload["blocked_reasons"].extend(asset_package.get("blocked_reasons", []))
        payload["blocked_reasons"].extend((asset_validation or {}).get("blocked_reasons", []))
    if stage.id == "rl-environment" and asset_package:
        payload["status"] = "blocked"
        payload["validation_status"] = "blocked"
        payload["blocked_reasons"] = [
            "Isaac Lab RL environment contract is blocked until asset load and physics gates pass",
            "isaac-load gate has not passed",
            "numeric physics review has not passed",
        ]
        payload["environment_layer"] = asset_package.get("environment_path", "")
        payload["asset_package_path"] = asset_package.get("package_path", "")
        payload["requires_gates"] = ["isaac-load", "physics-layer-authored", "numeric-physics-review"]
    if stage.id == "evaluation" and asset_validation:
        payload["generated_asset_validation"] = {
            "status": asset_validation.get("status"),
            "report_path": asset_validation.get("report_path"),
            "blocked_reasons": asset_validation.get("blocked_reasons", []),
        }
        payload["validation_status"] = asset_validation.get("status", "not_validated")
    if stage.id == "governance" and asset_validation:
        decision, decision_schema_errors = _operator_release_decision(project_dir)
        requested_outputs = {str(item).strip().lower() for item in request.requested_outputs}
        default_scope = (
            "articulated_training"
            if "rl" in requested_outputs
            else "rigid_body_manipulation"
            if "simready" in requested_outputs
            else "visualisation"
        )
        requested_scope = str(request.constraints.get("release_scope") or decision.get("scope") or default_scope)
        supported_scopes = {"visualisation", "rigid_body_manipulation", "articulated_training", "redistribution"}
        scope = requested_scope if requested_scope in supported_scopes else default_scope
        decision_path = project_dir / "operator-release-decision.json"
        if decision_path.exists():
            payload["evidence"].append(
                _evidence_record(decision_path, project_dir, "operator_release_decision", "operator_release_decision")
            )
        asset_fingerprint = asset_package_fingerprint(asset_package or {}, asset_validation)
        profile = asset_validation.get("simready_profile", {})
        decision_binding_errors: list[str] = list(decision_schema_errors)
        required_bindings = {
            "run_id": plan.run_id,
            "request_digest": plan.request_digest,
            "asset_fingerprint": asset_fingerprint,
            "profile_id": str(profile.get("profile_id") or ""),
            "profile_version": str(profile.get("profile_version") or ""),
            "scope": scope,
        }
        for key, expected in required_bindings.items():
            if str(decision.get(key) or "") != str(expected):
                decision_binding_errors.append(f"operator decision {key} does not match the current run")
        expires_at = str(decision.get("expires_at") or "")
        if not expires_at:
            decision_binding_errors.append("operator decision expiry is required")
        else:
            try:
                if datetime.fromisoformat(expires_at.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                    decision_binding_errors.append("operator decision has expired")
            except ValueError:
                decision_binding_errors.append("operator decision expiry is invalid")
        payload["version"] = "2.0"
        payload["source_rights"] = list((source_ingestion or {}).get("source_rights", []))
        payload["evidence"].extend((source_ingestion or {}).get("governance_evidence", []))
        retention_raw = request.constraints.get("retention", {}) if isinstance(request.constraints, dict) else {}
        retention = retention_raw if isinstance(retention_raw, dict) else {}
        payload["retention"] = {
            "policy": str(retention.get("policy") or "project"),
            "expires_at": retention.get("expires_at"),
            "deletion_required": retention.get("deletion_required") is True,
            "evidence_ids": list(retention.get("evidence_ids") or []),
            "extensions": dict(retention.get("extensions") or {}),
        }
        decision_approved = (
            decision.get("decision") == "approve"
            and bool(decision.get("decided_by"))
            and bool(decision.get("decided_at"))
            and not decision_binding_errors
        )
        payload["reviewer"] = {
            "reviewer_id": str(decision.get("decided_by") or ""),
            "review_status": "approved" if decision_approved else "review_required",
            "reviewed_at": decision.get("decided_at"),
            "evidence_ids": ["operator_release_decision"] if decision_path.exists() else [],
            "extensions": {},
        }
        payload["raw_secrets_recorded"] = False
        payload["rights_status"] = (source_ingestion or {}).get("rights_status", "pending")
        payload["retention_policy"] = payload["retention"]["policy"]
        payload["task_scope"] = scope
        payload["asset_fingerprint"] = asset_fingerprint
        payload["review_status"] = payload["reviewer"]["review_status"]
        payload["release_status"] = "not_evaluated"
        payload["release_allowed"] = False
        pre_release_graph = validate_pre_release_graph(project_dir)
        pre_release_graph_path = write_json(
            project_dir / "reports" / "pre-release-graph-validation.json",
            pre_release_graph,
        )
        payload["evidence"].append(
            _evidence_record(
                pre_release_graph_path,
                project_dir,
                "pre_release_graph_validation",
                "record_graph_validation",
            )
        )
        gate_results = list(asset_validation.get("validation_results", []))
        prior_reports, upstream_report_blockers, required_stage_ids = _upstream_stage_reports(
            project_dir,
            plan,
            stage.id,
        )
        schema_pass = (
            bool(required_stage_ids)
            and len(prior_reports) == len(required_stage_ids)
            and not upstream_report_blockers
            and all(not item.get("manifest_errors") for item in prior_reports)
        )
        source_pass = bool((source_ingestion or {}).get("source_assets")) and all(
            item.get("status") == "copied" and bool(item.get("copy_sha256"))
            for item in (source_ingestion or {}).get("source_assets", [])
        )
        material_evidence = _material_evidence_gate(project_dir, request.id)
        nonvisual_evidence = _nonvisual_evidence_gate(project_dir, request.id)
        evaluation_manifest_path = project_dir / "manifests" / "evaluation-manifest.json"
        evaluation_manifest = {}
        if evaluation_manifest_path.is_file():
            try:
                evaluation_manifest = json.loads(evaluation_manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                evaluation_manifest = {}
        task_fitness = evaluation_manifest.get("task_fitness", {})
        runtime_status = str(
            asset_validation.get("simready_conformance", {}).get("runtime_validation", {}).get("status") or "blocked"
        )
        gate_results.extend(
            [
                {"gate_id": "schema-valid", "status": "pass" if schema_pass else "blocked"},
                {"gate_id": "source-lineage", "status": "pass" if source_pass else "blocked"},
                {"gate_id": "material-evidence", "status": material_evidence["status"]},
                {"gate_id": "nonvisual-evidence", "status": nonvisual_evidence["status"]},
                {"gate_id": "isaac-load", "status": runtime_status},
                {"gate_id": "record-graph", "status": pre_release_graph["status"]},
                {"gate_id": "task-fitness", "status": str(task_fitness.get("status") or "blocked")},
                {"gate_id": "governance-review", "status": "pass" if decision_approved else "blocked"},
            ]
        )
        upstream_blockers = [
            *list((asset_package or {}).get("blocked_reasons", [])),
            *list(asset_validation.get("blocked_reasons", [])),
            *upstream_report_blockers,
        ]
        if scope != "redistribution":
            upstream_blockers.extend(material_evidence["blocked_reasons"])
        if scope == "articulated_training":
            upstream_blockers.extend(nonvisual_evidence["blocked_reasons"])
        if requested_scope not in supported_scopes:
            upstream_blockers.append(f"unsupported release scope: {requested_scope}")
        if pre_release_graph["status"] != "pass":
            upstream_blockers.extend(
                f"record graph {item['code']}: {item['message']}"
                for item in pre_release_graph["findings"]
                if item["severity"] == "error"
            )
        upstream_blockers.extend(str(item) for item in task_fitness.get("blocked_reasons", []))
        upstream_blockers.extend(decision_binding_errors)
        if upstream_blockers:
            payload["release_status"] = "blocked"
        release_decision = evaluate_release_policy(
            payload,
            scope,
            gate_results=gate_results,
            stage_reports=prior_reports,
            required_stage_ids=required_stage_ids,
            asset_validation_status=str(asset_validation.get("status") or "not_validated"),
        )
        for blocker in upstream_blockers:
            if blocker not in release_decision["blockers"]:
                release_decision["blockers"].append(blocker)
        if release_decision["blockers"]:
            release_decision["release_allowed"] = False
            release_decision["release_status"] = "blocked"
        release_decision["decision_id"] = content_id(
            "release",
            {
                "governance_id": payload.get("id"),
                "scope": scope,
                "policy_version": release_decision["policy_version"],
                "evaluated_at": release_decision["evaluated_at"],
                "evaluation_order": release_decision["evaluation_order"],
                "blockers": sorted(release_decision["blockers"]),
            },
            digest_length=32,
        )
        release_approved = release_decision["release_allowed"]
        payload["release_decisions"] = [release_decision]
        payload["release_status"] = release_decision["release_status"]
        payload["release_allowed"] = release_approved
        if decision:
            payload["operator_decision"] = {
                "decided_by": str(decision.get("decided_by") or ""),
                "decided_at": str(decision.get("decided_at") or ""),
                "decision": str(decision.get("decision") or ""),
                "record_path": "operator-release-decision.json",
                "scope": str(decision.get("scope") or ""),
                "run_id": str(decision.get("run_id") or ""),
                "request_digest": str(decision.get("request_digest") or ""),
                "asset_fingerprint": str(decision.get("asset_fingerprint") or ""),
                "profile_id": str(decision.get("profile_id") or ""),
                "profile_version": str(decision.get("profile_version") or ""),
                "expires_at": expires_at,
            }
        payload["promotion_record"] = {
            "proposal": True,
            "validated": asset_validation.get("status") == "validated",
            "released": release_approved,
            "scope": scope,
        }
        payload["promotion_blockers"] = release_decision["blockers"]
        payload["blocked_reasons"] = release_decision["blockers"]
        payload["validation_status"] = "validated" if release_approved else "blocked"
        payload["status"] = "released" if release_approved else "blocked"
        payload["evidence"].append(
            {
                "evidence_id": "generated_asset_validation",
                "kind": "validation_report",
                "uri": asset_validation.get("report_path", ""),
                "checksum": asset_validation.get("report_sha256", ""),
            }
        )
    if isinstance(payload.get("blocked_reasons"), list):
        payload["blocked_reasons"] = list(dict.fromkeys(payload["blocked_reasons"]))
    return payload


def _write_stage_manifest(
    request: RunRequest,
    plan: RunPlan,
    stage: StagePlan,
    project_id: str,
    manifests_dir: Path,
    reports_dir: Path,
    run_plan_path: Path,
    project_dir: Path,
    source_ingestion: dict[str, Any] | None = None,
    asset_package: dict[str, Any] | None = None,
    asset_validation: dict[str, Any] | None = None,
    dry_run: bool = True,
    producer_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema_name = STAGE_SCHEMA.get(stage.id)
    manifest_path: Path | None = None
    manifest_errors: list[str] = []
    blocked_reasons = list(stage.blocked_reasons)
    if schema_name:
        payload = _enrich_manifest(
            skeleton(schema_name),
            request,
            plan,
            stage,
            project_id,
            run_plan_path,
            project_dir,
            source_ingestion,
            asset_package,
            asset_validation,
        )
        if stage.id == "evaluation" and producer_result:
            payload["task_fitness"] = producer_result.get("task_fitness", {})
        if stage.id == "mesh-verification" and producer_result:
            verification = producer_result.get("verification", {})
            for key in (
                "candidate",
                "diagnostics",
                "render_bundle",
                "decision",
                "decision_reason",
                "review_status",
                "findings",
                "reviewer",
                "rubric_checksum",
                "provider_trace",
                "attempts",
                "actions",
                "evidence",
                "promotion",
                "raw_secrets_recorded",
            ):
                if key in verification:
                    payload[key] = verification[key]
            payload["status"] = "validated" if verification.get("gate_status") == "pass" else "blocked"
            payload["validation_status"] = payload["status"]
            payload["blocked_reasons"] = list(verification.get("blocked_reasons", []))
        if producer_result:
            payload.setdefault("extensions", {})["producer_result"] = producer_result
        manifest_path = write_json(manifests_dir / f"{schema_name}.json", payload)
        manifest_errors = validate_manifest(schema_name, manifest_path)
        blocked_reasons = list(payload.get("blocked_reasons", blocked_reasons))

    report = {
        "stage_id": stage.id,
        "skill": stage.skill,
        "status": "blocked" if blocked_reasons else _stage_status(stage),
        "dry_run": dry_run,
        "manifest_path": manifest_path.relative_to(project_dir).as_posix() if manifest_path else None,
        "manifest_errors": manifest_errors,
        "provider_roles": stage.provider_roles,
        "validation_gates": stage.validation_gates,
        "blocked_reasons": blocked_reasons,
        "outputs": stage.outputs,
        "contract": {
            "consumes": stage.consumes,
            "produces": stage.produces,
            "preconditions": stage.preconditions,
            "resources": stage.resources,
            "max_attempts": stage.max_attempts,
            "execution_mode": stage.execution_mode,
        },
        "producer_result": producer_result or {},
        "generated_asset": asset_package if stage.id == "simready-verification" else None,
    }
    report_path = write_json(reports_dir / f"{stage.id}-report.json", report)
    return {
        "stage_id": stage.id,
        "status": report["status"],
        "manifest_path": report["manifest_path"],
        "report_path": report_path.relative_to(project_dir).as_posix(),
        "manifest_valid": not manifest_errors,
        "manifest_errors": manifest_errors,
        "blocked_reasons": blocked_reasons,
    }


def _write_checksums(project_dir: Path) -> Path:
    return write_json(
        project_dir / "evidence" / "checksums.json",
        build_project_checksum_inventory(project_dir),
    )


def refresh_project_checksums(project_dir: str | Path) -> Path:
    return _write_checksums(Path(project_dir))


def run_workflow(
    request_path: str | Path | RunRequest,
    project_root: str | Path = "projects",
    project_name: str | None = None,
    dry_run: bool = True,
    run_plan_output: str | Path | None = None,
) -> dict[str, Any]:
    project_root_target = Path(project_root)
    request: RunRequest
    if isinstance(request_path, RunRequest):
        request = request_path
    else:
        request_target = Path(request_path)
        if in_service_request():
            request_target = confine_path(
                request_target,
                (*service_source_roots(ROOT), *service_workspace_roots(ROOT)),
                must_exist=True,
            )
        request = load_run_request(request_target)
    if in_service_request():
        project_root_target = confine_path(project_root_target, service_workspace_roots(ROOT))
        if run_plan_output is not None:
            run_plan_output = confine_path(run_plan_output, service_workspace_roots(ROOT))
    plan = build_run_plan(request)
    name = project_name or request.id.replace("_", " ")
    project = create_project(name, project_root_target)
    project_id = project["project_id"]
    paths = project_paths(project_root_target, project_id)
    with workspace_lease(paths.root, plan.run_id):
        request_copy = write_json(paths.root / "run-request.json", request.model_dump(mode="json"))
        run_plan_path = paths.root / "run-plan.json"
        write_json(paths.root / "provider-assignment.json", plan.model_dump(mode="json")["provider_assignments"])
        write_json(paths.root / "validation-plan.json", {"gates": plan.validation_gates})
        write_json(paths.root / "missing-evidence.json", {"items": plan.missing_evidence})
        write_json(paths.root / "wandb-run-plan.json", plan.wandb_plan)
        runtime = _WorkflowStageRuntime(request, plan, paths.root, dry_run=dry_run)
        planned_attempt_ids = [stage_attempt_id(plan.run_id, stage.id, 1, plan.request_digest) for stage in plan.stages]
        provenance = build_provenance(
            [f"{stage.id}-manifest" for stage in plan.stages],
            provider_model_ids=_provider_model_handles(plan),
            source_assets=[],
            source_assets_mutated=False,
            run_id=plan.run_id,
            attempt_ids=planned_attempt_ids,
        )
        plan.provenance = provenance
        write_json(run_plan_path, plan.model_dump(mode="json"))
        write_json(paths.root / "provenance.json", provenance)
        run_snapshot_dir = write_run_snapshot(paths.root, plan, request.model_dump(mode="json"), provenance)
        immutable_run_plan_path = run_snapshot_dir / "plan.json"

        if run_plan_output:
            write_json(Path(run_plan_output), plan.model_dump(mode="json"))

        def produce(stage: StagePlan) -> dict[str, Any]:
            producer_result = runtime.execute(stage)
            if stage.id == "source-ingestion" and runtime.source_ingestion is not None:
                plan.provenance = build_provenance(
                    [f"{item.id}-manifest" for item in plan.stages],
                    provider_model_ids=_provider_model_handles(plan),
                    source_assets=runtime.source_ingestion.get("source_assets", []),
                    source_assets_mutated=False,
                    run_id=plan.run_id,
                    attempt_ids=planned_attempt_ids,
                )
                write_json(run_plan_path, plan.model_dump(mode="json"))
                write_json(paths.root / "provenance.json", plan.provenance)
            return _write_stage_manifest(
                request,
                plan,
                stage,
                project_id,
                paths.manifests,
                paths.reports,
                immutable_run_plan_path,
                paths.root,
                runtime.source_ingestion,
                runtime.asset_package,
                runtime.asset_validation,
                dry_run,
                producer_result,
            )

        stage_results = execute_stage_plan(
            paths.root,
            plan,
            plan.request_digest,
            produce,
            dry_run=dry_run,
            provenance_id=provenance.get("provenance_id"),
        )
        attempt_ids = [str(item.get("attempt_id") or "") for item in stage_results if item.get("attempt_id")]
        provenance = build_provenance(
            [f"{stage.id}-manifest" for stage in plan.stages],
            provider_model_ids=_provider_model_handles(plan),
            source_assets=(runtime.source_ingestion or {}).get("source_assets", []),
            source_assets_mutated=False,
            run_id=plan.run_id,
            attempt_ids=attempt_ids,
        )
        plan.provenance = provenance
        write_json(run_plan_path, plan.model_dump(mode="json"))
        write_json(paths.root / "provenance.json", provenance)
        immutable_write_json(paths.root / "runs" / plan.run_id / "result-provenance.json", provenance)
        write_prov_jsonld(paths.root / "provenance.prov.jsonld", provenance)
        write_prov_jsonld(
            paths.root / "runs" / plan.run_id / "result-provenance.prov.jsonld",
            provenance,
            immutable=True,
        )
        checksums_path = _write_checksums(paths.root)

        project_manifest_path = paths.root / "project.json"
        project_manifest = json.loads(project_manifest_path.read_text(encoding="utf-8"))
        project_manifest["active_run_id"] = plan.run_id
        project_manifest["active_run_path"] = f"runs/{plan.run_id}"
        project_manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
        project_manifest["run_request"] = request_copy.relative_to(paths.root).as_posix()
        project_manifest["run_plan"] = run_plan_path.relative_to(paths.root).as_posix()
        project_manifest["checksum_manifest"] = checksums_path.relative_to(paths.root).as_posix()
        project_manifest["provenance"] = "provenance.json"
        project_manifest["provenance_jsonld"] = "provenance.prov.jsonld"
        project_manifest["git_sha"] = provenance["repository"]["git_sha"]
        project_manifest["repository_state"] = provenance["repository"]["git_state"]
        write_json(project_manifest_path, project_manifest)
        checksums_path = _write_checksums(paths.root)
        graph_validation = validate_project_graph(paths.root)
        write_json(paths.reports / "project-graph-validation.json", graph_validation)
        checksums_path = _write_checksums(paths.root)

    blocked = [stage for stage in stage_results if stage["status"] == "blocked" or not stage["manifest_valid"]]
    if graph_validation["status"] == "blocked":
        blocked.append({"stage_id": "project-graph-validation", "status": "blocked"})
    return {
        "project_id": project_id,
        "project_dir": str(paths.root),
        "run_id": plan.run_id,
        "request_digest": plan.request_digest,
        "run_snapshot": f"runs/{plan.run_id}",
        "dry_run": dry_run,
        "run_request": request_copy.relative_to(paths.root).as_posix(),
        "run_plan": run_plan_path.relative_to(paths.root).as_posix(),
        "stage_results": stage_results,
        "graph_validation": graph_validation,
        "checksums": checksums_path.relative_to(paths.root).as_posix(),
        "blocked_count": len(blocked),
        "status": "blocked" if blocked else "proposal",
    }


def rebuild_project_artefacts(project_dir: str | Path, dry_run: bool = True) -> dict[str, Any]:
    """Regenerate the composed asset, validation results and stage manifests for an existing project.

    Used by the agent loop after fix recipes run, so reviews and gates see
    refreshed artefacts instead of the pre-fix state. The persisted run request
    and run plan in the workspace drive the rebuild; sources are not re-copied.
    """
    root = Path(project_dir)
    request = RunRequest.model_validate_json((root / "run-request.json").read_text(encoding="utf-8"))
    plan = RunPlan.model_validate_json((root / "run-plan.json").read_text(encoding="utf-8"))
    if not plan.request_digest:
        plan.request_digest = "sha256:" + sha256_text(request.model_dump_json())
    if not plan.created_at:
        plan.created_at = datetime.now(timezone.utc).isoformat()
    project_id = json.loads((root / "project.json").read_text(encoding="utf-8"))["project_id"]
    run_plan_path = root / "run-plan.json"
    source_manifest_path = root / "manifests" / "source-asset-manifest.json"
    source_payload = (
        json.loads(source_manifest_path.read_text(encoding="utf-8")) if source_manifest_path.exists() else {}
    )
    source_ingestion = {
        "source_assets": source_payload.get("source_assets", []),
        "local_copies": source_payload.get("local_copies", []),
        "source_rights": source_payload.get("source_rights", []),
        "rights_status": source_payload.get("rights_status", "pending"),
        "governance_evidence": [
            item
            for item in source_payload.get("evidence", [])
            if str(item.get("kind") or "").startswith("governance")
            or item.get("evidence_id")
            in {
                evidence_id
                for rights in source_payload.get("source_rights", [])
                for evidence_id in rights.get("evidence_ids", [])
            }
        ],
        "blocked_reasons": list(source_payload.get("blocked_reasons", [])),
    }
    with workspace_lease(root, plan.run_id):
        runtime = _WorkflowStageRuntime(
            request,
            plan,
            root,
            dry_run=dry_run,
            source_ingestion=source_ingestion,
        )
        run_dir = root / "runs" / plan.run_id
        if not run_dir.exists():
            initial_attempt_ids = [
                stage_attempt_id(plan.run_id, stage.id, 1, plan.request_digest) for stage in plan.stages
            ]
            initial_provenance = build_provenance(
                [f"{stage.id}-manifest" for stage in plan.stages],
                provider_model_ids=_provider_model_handles(plan),
                source_assets=source_ingestion["source_assets"],
                source_assets_mutated=False,
                run_id=plan.run_id,
                attempt_ids=initial_attempt_ids,
            )
            plan.provenance = initial_provenance
            write_run_snapshot(root, plan, request.model_dump(mode="json"), initial_provenance)
        immutable_run_plan_path = run_dir / "plan.json"

        def produce(stage: StagePlan) -> dict[str, Any]:
            producer_result = runtime.execute(stage)
            return _write_stage_manifest(
                request,
                plan,
                stage,
                project_id,
                root / "manifests",
                root / "reports",
                immutable_run_plan_path,
                root,
                runtime.source_ingestion,
                runtime.asset_package,
                runtime.asset_validation,
                dry_run,
                producer_result,
            )

        stage_results = execute_stage_plan(
            root,
            plan,
            plan.request_digest,
            produce,
            dry_run=dry_run,
            provenance_id=plan.provenance.get("provenance_id"),
        )
        attempt_ids = sorted(
            path.parent.name
            for path in (root / "runs" / plan.run_id / "attempts").glob("*/*/*.json")
            if path.stem == path.parent.name
        )
        provenance = build_provenance(
            [f"{stage.id}-manifest" for stage in plan.stages],
            provider_model_ids=_provider_model_handles(plan),
            source_assets=(runtime.source_ingestion or {}).get("source_assets", []),
            source_assets_mutated=False,
            run_id=plan.run_id,
            attempt_ids=attempt_ids,
        )
        plan.provenance = provenance
        write_json(run_plan_path, plan.model_dump(mode="json"))
        write_json(root / "provenance.json", provenance)
        write_prov_jsonld(root / "provenance.prov.jsonld", provenance)
        result_provenance_path = root / "runs" / plan.run_id / f"result-provenance-{provenance['provenance_id']}.json"
        if not result_provenance_path.exists():
            immutable_write_json(result_provenance_path, provenance)
        result_prov_jsonld_path = (
            root / "runs" / plan.run_id / f"result-provenance-{provenance['provenance_id']}.prov.jsonld"
        )
        if not result_prov_jsonld_path.exists():
            write_prov_jsonld(result_prov_jsonld_path, provenance, immutable=True)
        _write_checksums(root)
        graph_validation = validate_project_graph(root)
        write_json(root / "reports" / "project-graph-validation.json", graph_validation)
        _write_checksums(root)
    blocked = [stage for stage in stage_results if stage["status"] == "blocked" or not stage["manifest_valid"]]
    if graph_validation["status"] == "blocked":
        blocked.append({"stage_id": "project-graph-validation", "status": "blocked"})
    return {
        "project_dir": str(root),
        "run_id": plan.run_id,
        "request_digest": plan.request_digest,
        "stage_results": stage_results,
        "asset_validation_status": (runtime.asset_validation or {}).get("status", "not_validated"),
        "graph_validation": graph_validation,
        "blocked_count": len(blocked),
        "status": "blocked" if blocked else "proposal",
    }


def load_project_stage_reports(reports_dir: str | Path) -> list[dict[str, Any]]:
    root = Path(reports_dir)
    reports = []
    for path in sorted(root.glob("*-report.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "stage_id" not in payload:
            continue
        payload["report_path"] = path.as_posix()
        reports.append(payload)
    return reports


def summarize_run(
    run_plan_path: str | Path, reports_dir: str | Path, output_path: str | Path | None = None
) -> dict[str, Any]:
    plan_path = Path(run_plan_path)
    reports_path = Path(reports_dir)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    reports = load_project_stage_reports(reports_path)
    report_by_stage = {item["stage_id"]: item for item in reports}
    stages = []
    for stage in plan.get("stages", []):
        report = report_by_stage.get(stage["id"], {})
        manifest_errors = report.get("manifest_errors", [])
        blocked_reasons = report.get("blocked_reasons", [])
        stages.append(
            {
                "stage_id": stage["id"],
                "skill": stage["skill"],
                "status": report.get("status", stage.get("status", "proposal")),
                "manifest_path": report.get("manifest_path"),
                "manifest_valid": not manifest_errors,
                "manifest_errors": manifest_errors,
                "validation_gates": report.get("validation_gates", stage.get("validation_gates", [])),
                "blocked_reasons": blocked_reasons,
            }
        )
    blocked = [stage for stage in stages if stage["blocked_reasons"] or not stage["manifest_valid"]]
    summary = {
        "run_id": plan.get("run_id") or plan.get("id"),
        "request_id": plan.get("request_id"),
        "objective": plan.get("objective"),
        "stage_count": len(stages),
        "blocked_count": len(blocked),
        "status": "blocked" if blocked else "proposal",
        "provider_assignments": plan.get("provider_assignments", {}),
        "validation_gates": plan.get("validation_gates", []),
        "stages": stages,
    }
    if output_path:
        write_json(Path(output_path), summary)
        reports_path = Path(reports_dir)
        if reports_path.name == "reports":
            refresh_project_checksums(reports_path.parent)
    return summary
