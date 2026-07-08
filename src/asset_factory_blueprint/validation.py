from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from asset_factory_blueprint.manifests import validate_payload
from asset_factory_blueprint.schemas.common import RunPlan, RunRequest, StageAttempt
from asset_factory_blueprint.utils.checksums import sha256_file, sha256_text


VALID_LAYERS = {"asset", "scene", "environment", "material", "physics", "articulation", "governance"}
PATH_KEY_SUFFIXES = ("_path", "_file", "_uri")
TERMINAL_ATTEMPT_STATES = {"succeeded", "failed", "cancelled", "timed_out", "blocked"}
SHA256_PATTERN = re.compile(r"^[A-Fa-f0-9]{64}$")
PROJECT_CHECKSUM_EXCLUSIONS: dict[str, str] = {
    "evidence/checksums.json": "the checksum inventory cannot contain its own digest without recursion",
    ".afb-workspace.lock": "the workspace lease is ephemeral process state and is never release evidence",
}


@dataclass(frozen=True)
class GraphValidationFinding:
    severity: str
    code: str
    instance_path: str
    schema_path: str
    message: str
    remediation: str


def _finding(
    findings: list[GraphValidationFinding],
    code: str,
    instance_path: str,
    message: str,
    remediation: str,
    *,
    severity: str = "error",
    schema_path: str = "",
) -> None:
    findings.append(
        GraphValidationFinding(
            severity=severity,
            code=code,
            instance_path=instance_path,
            schema_path=schema_path,
            message=message,
            remediation=remediation,
        )
    )


def _read_json(path: Path, findings: list[GraphValidationFinding], instance_path: str) -> dict[str, Any] | None:
    if not path.is_file():
        _finding(
            findings,
            "required_record_missing",
            instance_path,
            f"required record does not exist: {path.name}",
            "rerun the workflow so the required record is written atomically",
        )
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _finding(
            findings,
            "invalid_json",
            instance_path,
            f"record is not readable JSON: {exc}",
            "replace the record with JSON produced by the owning stage",
        )
        return None
    if not isinstance(payload, dict):
        _finding(
            findings,
            "invalid_record_shape",
            instance_path,
            "record root must be a JSON object",
            "write an object matching the declared record schema",
        )
        return None
    return payload


def _confined_project_path(
    project_root: Path,
    value: str,
    findings: list[GraphValidationFinding],
    instance_path: str,
    *,
    must_exist: bool = True,
) -> Path | None:
    raw = Path(value)
    if raw.is_absolute():
        _finding(
            findings,
            "absolute_project_path",
            instance_path,
            f"project record contains an absolute path: {value}",
            "store a project-relative path so the evidence remains relocatable",
        )
    candidate = raw if raw.is_absolute() else project_root / raw
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as exc:
        _finding(
            findings,
            "referenced_file_missing",
            instance_path,
            f"referenced path cannot be resolved: {value}: {exc}",
            "restore the referenced artefact or regenerate its producer stage",
        )
        return None
    root = project_root.resolve(strict=True)
    if resolved != root and root not in resolved.parents:
        _finding(
            findings,
            "path_escape",
            instance_path,
            f"referenced path escapes the project workspace: {value}",
            "copy the artefact into the project and record a confined relative path",
        )
        return None
    if must_exist and not resolved.is_file():
        _finding(
            findings,
            "referenced_file_missing",
            instance_path,
            f"referenced file does not exist: {value}",
            "restore the referenced artefact or regenerate its producer stage",
        )
        return None
    return resolved


def _normalise_sha256(value: Any) -> str:
    return str(value or "").lower().removeprefix("sha256:")


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def _walk_fields(value: Any, prefix: str = "$") -> list[tuple[str, str, Any]]:
    fields: list[tuple[str, str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{prefix}.{key}"
            fields.append((child_path, key, child))
            fields.extend(_walk_fields(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            fields.extend(_walk_fields(child, f"{prefix}[{index}]"))
    return fields


def build_project_checksum_inventory(project: str | Path) -> dict[str, Any]:
    """Build the exact project-file inventory governed by the graph validator."""

    project_root = Path(project)
    resolved_root = project_root.resolve(strict=True)
    records: list[dict[str, str]] = []
    for path in sorted(project_root.rglob("*")):
        if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
            raise ValueError(f"project checksum inventory rejects links and junctions: {path.relative_to(project_root)}")
        if not path.is_file():
            continue
        try:
            path.resolve(strict=True).relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(f"project checksum inventory path escapes the workspace: {path}") from exc
        relative = path.relative_to(project_root).as_posix()
        if relative in PROJECT_CHECKSUM_EXCLUSIONS:
            continue
        records.append({"path": relative, "sha256": sha256_file(path)})
    return {
        "format_version": "1.0",
        "algorithm": "sha256",
        "exclusions": [
            {"path": path, "reason": reason}
            for path, reason in PROJECT_CHECKSUM_EXCLUSIONS.items()
        ],
        "files": records,
    }


def _validate_checksum_inventory(
    project_root: Path,
    findings: list[GraphValidationFinding],
) -> tuple[int, dict[str, str]]:
    inventory_path = project_root / "evidence" / "checksums.json"
    payload = _read_json(inventory_path, findings, "$.evidence.checksums")
    if payload is None:
        return 0, {}
    if payload.get("format_version") != "1.0" or payload.get("algorithm") != "sha256":
        _finding(
            findings,
            "invalid_checksum_inventory_contract",
            "$.evidence.checksums",
            "checksum inventory must declare format_version 1.0 and algorithm sha256",
            "regenerate evidence/checksums.json with the current workflow",
        )
    exclusions = payload.get("exclusions")
    recorded_exclusions: dict[str, str] = {}
    if not isinstance(exclusions, list):
        _finding(
            findings,
            "invalid_checksum_exclusions",
            "$.evidence.checksums.exclusions",
            "checksum inventory must explicitly declare its governed exclusions",
            "regenerate the inventory with the exact supported exclusion policy",
        )
    else:
        for index, entry in enumerate(exclusions):
            location = f"$.evidence.checksums.exclusions[{index}]"
            if not isinstance(entry, dict) or not entry.get("path") or not entry.get("reason"):
                _finding(
                    findings,
                    "invalid_checksum_exclusion",
                    location,
                    "checksum exclusion requires a path and a non-empty reason",
                    "record only explicit, justified exclusions",
                )
                continue
            path = str(entry["path"])
            if path in recorded_exclusions:
                _finding(
                    findings,
                    "duplicate_checksum_exclusion",
                    f"{location}.path",
                    f"checksum exclusion is listed more than once: {path}",
                    "retain one canonical exclusion record for each path",
                )
                continue
            recorded_exclusions[path] = str(entry["reason"])
    for path, reason in PROJECT_CHECKSUM_EXCLUSIONS.items():
        if recorded_exclusions.get(path) != reason:
            _finding(
                findings,
                "checksum_exclusion_policy_mismatch",
                "$.evidence.checksums.exclusions",
                f"required checksum exclusion is absent or has a different reason: {path}",
                "use the exact exclusion policy produced by the workflow",
            )
    for path in sorted(set(recorded_exclusions) - set(PROJECT_CHECKSUM_EXCLUSIONS)):
        _finding(
            findings,
            "unsupported_checksum_exclusion",
            "$.evidence.checksums.exclusions",
            f"checksum inventory excludes an unsupported project path: {path}",
            "include the file in the inventory or add a reviewed exclusion to the validator policy",
        )
    entries = payload.get("files")
    if not isinstance(entries, list):
        _finding(
            findings,
            "invalid_checksum_inventory",
            "$.evidence.checksums.files",
            "checksum inventory files must be an array",
            "regenerate evidence/checksums.json with the workflow",
        )
        return 0, {}
    if not entries:
        _finding(
            findings,
            "empty_checksum_inventory",
            "$.evidence.checksums.files",
            "checksum inventory must contain the complete non-excluded project-file set",
            "regenerate the inventory after the project records have been written",
        )
    checksums: dict[str, str] = {}
    for index, entry in enumerate(entries):
        location = f"$.evidence.checksums.files[{index}]"
        if not isinstance(entry, dict):
            _finding(
                findings,
                "invalid_checksum_entry",
                location,
                "checksum entry must be an object",
                "regenerate the checksum inventory",
            )
            continue
        relative = str(entry.get("path") or "")
        expected = _normalise_sha256(entry.get("sha256"))
        if not relative or not SHA256_PATTERN.fullmatch(expected):
            _finding(
                findings,
                "invalid_checksum_entry",
                location,
                "checksum entry requires a canonical path and 64-character SHA-256",
                "regenerate the checksum inventory",
            )
            continue
        if relative != Path(relative).as_posix() or "\\" in relative:
            _finding(
                findings,
                "noncanonical_checksum_path",
                f"{location}.path",
                f"checksum path is not canonical project-relative POSIX form: {relative}",
                "record the path relative to the project with forward slashes",
            )
            continue
        if relative in PROJECT_CHECKSUM_EXCLUSIONS:
            _finding(
                findings,
                "excluded_checksum_path_listed",
                f"{location}.path",
                f"excluded path must not appear in the checksum file list: {relative}",
                "remove the entry and retain its explicit exclusion record",
            )
            continue
        if relative in checksums:
            _finding(
                findings,
                "duplicate_checksum_path",
                f"{location}.path",
                f"checksum path is listed more than once: {relative}",
                "retain one canonical checksum entry for each file",
            )
            continue
        checksums[relative] = expected
        target = _confined_project_path(project_root, relative, findings, f"{location}.path")
        if target is not None and sha256_file(target).lower() != expected:
            _finding(
                findings,
                "checksum_mismatch",
                f"{location}.sha256",
                f"recorded checksum does not match {relative}",
                "treat the record as changed and regenerate or re-authorise the run",
            )
    actual_paths = {
        path.relative_to(project_root).as_posix()
        for path in project_root.rglob("*")
        if path.is_file() and path.relative_to(project_root).as_posix() not in PROJECT_CHECKSUM_EXCLUSIONS
    }
    for relative in sorted(actual_paths - set(checksums)):
        _finding(
            findings,
            "checksum_path_missing",
            "$.evidence.checksums.files",
            f"project file is absent from the checksum inventory: {relative}",
            "regenerate the inventory after all project writes complete",
        )
    for relative in sorted(set(checksums) - actual_paths):
        _finding(
            findings,
            "checksum_path_unexpected",
            "$.evidence.checksums.files",
            f"checksum inventory lists a file outside the current project inventory: {relative}",
            "restore the file or regenerate the inventory from the current project tree",
        )
    return len(entries), checksums


def _validate_manifest_graph(
    project_root: Path,
    findings: list[GraphValidationFinding],
) -> tuple[int, dict[str, dict[str, Any]], dict[str, tuple[str, str]]]:
    manifests: dict[str, dict[str, Any]] = {}
    evidence_index: dict[str, tuple[str, str]] = {}
    manifest_dir = project_root / "manifests"
    if not manifest_dir.is_dir():
        _finding(
            findings,
            "manifest_directory_missing",
            "$.manifests",
            "project manifest directory is missing",
            "rerun the workflow to produce stage manifests",
        )
        return 0, manifests, evidence_index
    for path in sorted(manifest_dir.glob("*.json")):
        payload = _read_json(path, findings, f"$.manifests.{path.stem}")
        if payload is None:
            continue
        schema_name = path.stem
        try:
            schema_issues = validate_payload(schema_name, payload)
        except FileNotFoundError:
            _finding(
                findings,
                "manifest_schema_missing",
                f"$.manifests.{path.stem}",
                f"no schema is published for {path.name}",
                "publish the matching schema or move non-manifest JSON out of manifests",
            )
            continue
        for issue in schema_issues:
            _finding(
                findings,
                issue.code,
                f"$.manifests.{path.stem}{issue.path.removeprefix('$')}",
                issue.message,
                "repair the producer output and rerun the owning stage",
                schema_path=f"schemas/{schema_name}.schema.json",
            )
        manifest_id = str(payload.get("id") or "")
        if not manifest_id:
            _finding(
                findings,
                "manifest_id_missing",
                f"$.manifests.{path.stem}.id",
                "manifest ID is missing",
                "assign the immutable ID produced by the stage contract",
            )
        elif manifest_id in manifests:
            _finding(
                findings,
                "duplicate_manifest_id",
                f"$.manifests.{path.stem}.id",
                f"manifest ID is already used: {manifest_id}",
                "use one unique immutable ID per manifest",
            )
        else:
            manifests[manifest_id] = payload
        local_evidence_ids: set[str] = set()
        evidence = payload.get("evidence", [])
        if isinstance(evidence, list):
            for index, record in enumerate(evidence):
                if not isinstance(record, dict):
                    continue
                base = f"$.manifests.{path.stem}.evidence[{index}]"
                evidence_id = str(record.get("evidence_id") or "")
                uri = str(record.get("uri") or "")
                checksum = _normalise_sha256(record.get("checksum"))
                if evidence_id in local_evidence_ids:
                    _finding(
                        findings,
                        "duplicate_evidence_id",
                        f"{base}.evidence_id",
                        f"evidence ID is repeated within the manifest: {evidence_id}",
                        "assign a unique evidence ID within each record",
                    )
                local_evidence_ids.add(evidence_id)
                prior = evidence_index.get(evidence_id)
                identity = (uri, checksum)
                if evidence_id and prior is not None and prior != identity:
                    _finding(
                        findings,
                        "conflicting_evidence_identity",
                        f"{base}.evidence_id",
                        f"evidence ID resolves to different artefacts: {evidence_id}",
                        "use a globally stable evidence identity or a new evidence ID",
                    )
                elif evidence_id:
                    evidence_index[evidence_id] = identity
                if not uri or _is_external_reference(uri):
                    continue
                evidence_path = _confined_project_path(project_root, uri, findings, f"{base}.uri")
                if evidence_path is not None and checksum and sha256_file(evidence_path).lower() != checksum:
                    _finding(
                        findings,
                        "evidence_checksum_mismatch",
                        f"{base}.checksum",
                        f"evidence checksum does not match {uri}",
                        "invalidate the stale evidence and rerun the producer stage",
                    )
    manifest_ids = set(manifests)
    evidence_ids = set(evidence_index)
    for manifest_id, payload in manifests.items():
        for field_path, key, value in _walk_fields(payload, f"$.manifest_ids.{manifest_id}"):
            if key.endswith("_manifest_id") and isinstance(value, str) and value and value not in manifest_ids:
                _finding(
                    findings,
                    "unresolved_manifest_reference",
                    field_path,
                    f"referenced manifest ID does not exist: {value}",
                    "produce the referenced upstream manifest or update the typed reference",
                )
            if key in {"evidence_ids", "consent_evidence_ids"} and isinstance(value, list):
                for index, evidence_id in enumerate(value):
                    if str(evidence_id) and str(evidence_id) not in evidence_ids:
                        _finding(
                            findings,
                            "unresolved_evidence_reference",
                            f"{field_path}[{index}]",
                            f"referenced evidence ID does not exist: {evidence_id}",
                            "attach the named evidence record or remove the unresolved reference",
                        )
    return len(manifests), manifests, evidence_index


def _validate_attempts(
    project_root: Path,
    plan: RunPlan,
    findings: list[GraphValidationFinding],
    *,
    allow_missing_stage_ids: set[str] | None = None,
) -> tuple[int, set[str]]:
    allow_missing_stage_ids = allow_missing_stage_ids or set()
    run_dir = project_root / "runs" / plan.run_id
    attempt_root = run_dir / "attempts"
    attempts: dict[str, StageAttempt] = {}
    stage_numbers: dict[str, set[int]] = {}
    if not attempt_root.is_dir():
        _finding(
            findings,
            "attempt_directory_missing",
            "$.runs.attempts",
            "active run has no immutable stage-attempt directory",
            "execute the typed stage plan before publishing the run",
        )
        return 0, set()
    for path in sorted(attempt_root.glob("*/*/*.json")):
        if path.stem != path.parent.name:
            continue
        payload = _read_json(path, findings, f"$.attempts.{path.stem}")
        if payload is None:
            continue
        try:
            attempt = StageAttempt.model_validate(payload)
        except ValidationError as exc:
            for error in exc.errors(include_url=False):
                suffix = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error["loc"])
                _finding(
                    findings,
                    "invalid_stage_attempt",
                    f"$.attempts.{path.stem}{suffix}",
                    str(error["msg"]),
                    "regenerate the immutable attempt from the typed executor",
                    schema_path="schemas/stage-attempt.schema.json",
                )
            continue
        identity = attempt.identity
        expected_relative = Path("runs") / plan.run_id / "attempts" / identity.stage_id / identity.attempt_id / f"{identity.attempt_id}.json"
        if path.relative_to(project_root) != expected_relative:
            _finding(
                findings,
                "attempt_path_identity_mismatch",
                f"$.attempts.{identity.attempt_id}",
                "attempt identity does not match its immutable storage path",
                "move no records manually; regenerate the run through the executor",
            )
        if identity.run_id != plan.run_id:
            _finding(
                findings,
                "attempt_run_mismatch",
                f"$.attempts.{identity.attempt_id}.identity.run_id",
                "attempt belongs to a different run",
                "retain the attempt under its owning run only",
            )
        if identity.request_digest != plan.request_digest:
            _finding(
                findings,
                "attempt_request_mismatch",
                f"$.attempts.{identity.attempt_id}.identity.request_digest",
                "attempt request digest does not match the active plan",
                "start a new run for a changed request",
            )
        if identity.attempt_id in attempts:
            _finding(
                findings,
                "duplicate_attempt_id",
                f"$.attempts.{identity.attempt_id}",
                "attempt ID is duplicated",
                "preserve one immutable record per attempt ID",
            )
        attempts[identity.attempt_id] = attempt
        numbers = stage_numbers.setdefault(identity.stage_id, set())
        if identity.attempt_number in numbers:
            _finding(
                findings,
                "duplicate_attempt_number",
                f"$.attempts.{identity.attempt_id}.identity.attempt_number",
                f"stage {identity.stage_id} repeats attempt number {identity.attempt_number}",
                "allocate monotonically increasing attempt numbers",
            )
        numbers.add(identity.attempt_number)
        started = _parse_time(attempt.started_at)
        completed = _parse_time(attempt.completed_at)
        if started is None or completed is None or completed < started:
            _finding(
                findings,
                "invalid_attempt_timeline",
                f"$.attempts.{identity.attempt_id}",
                "attempt timestamps are invalid or out of order",
                "record timezone-aware start and completion times in execution order",
            )
        if attempt.status not in TERMINAL_ATTEMPT_STATES:
            _finding(
                findings,
                "nonterminal_attempt_record",
                f"$.attempts.{identity.attempt_id}.status",
                f"immutable attempt record has a nonterminal state: {attempt.status}",
                "write the immutable record only after reaching a terminal state",
            )
        snapshots = attempt.extensions.get("snapshots", {})
        if isinstance(snapshots, dict):
            for snapshot_name, snapshot in snapshots.items():
                if not isinstance(snapshot, dict):
                    continue
                relative = str(snapshot.get("path") or "")
                expected = _normalise_sha256(snapshot.get("sha256"))
                target = _confined_project_path(
                    project_root,
                    relative,
                    findings,
                    f"$.attempts.{identity.attempt_id}.extensions.snapshots.{snapshot_name}.path",
                )
                if target is not None and expected and sha256_file(target).lower() != expected:
                    _finding(
                        findings,
                        "attempt_snapshot_checksum_mismatch",
                        f"$.attempts.{identity.attempt_id}.extensions.snapshots.{snapshot_name}.sha256",
                        "attempt output snapshot checksum does not match",
                        "treat the immutable attempt as corrupt and start a new run",
                    )
    stage_by_id = {stage.id: stage for stage in plan.stages}
    for stage_id, stage in stage_by_id.items():
        count = len(stage_numbers.get(stage_id, set()))
        if count == 0 and stage_id not in allow_missing_stage_ids:
            _finding(
                findings,
                "stage_attempt_missing",
                f"$.run_plan.stages.{stage_id}",
                "planned stage has no terminal attempt record",
                "execute or explicitly block the stage through the typed executor",
            )
        if count > stage.max_attempts:
            _finding(
                findings,
                "stage_attempt_limit_exceeded",
                f"$.run_plan.stages.{stage_id}.max_attempts",
                f"stage has {count} attempts but permits {stage.max_attempts}",
                "start a new run after the bounded retry policy is exhausted",
            )
    unknown_stages = set(stage_numbers) - set(stage_by_id)
    for stage_id in sorted(unknown_stages):
        _finding(
            findings,
            "unplanned_stage_attempt",
            f"$.attempts.{stage_id}",
            "attempt exists for a stage absent from the run plan",
            "retain attempts only under the run plan that declared them",
        )
    event_path = run_dir / "events.jsonl"
    started_events: set[str] = set()
    completed_events: set[str] = set()
    if not event_path.is_file():
        _finding(
            findings,
            "event_journal_missing",
            "$.runs.events",
            "active run has no append-only event journal",
            "execute stages through the journalled executor",
        )
    else:
        for line_number, line in enumerate(event_path.read_text(encoding="utf-8").splitlines(), start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                _finding(
                    findings,
                    "invalid_event_record",
                    f"$.runs.events[{line_number}]",
                    f"event journal line is invalid JSON: {exc}",
                    "preserve append-only JSON Lines records without manual edits",
                )
                continue
            attempt_id = str(event.get("payload", {}).get("attempt_id") or "")
            event_type = event.get("event_type")
            if event_type == "stage_attempt_started":
                if attempt_id in started_events:
                    _finding(
                        findings,
                        "duplicate_attempt_start_event",
                        f"$.runs.events[{line_number}]",
                        f"attempt has more than one start event: {attempt_id}",
                        "append each state transition exactly once",
                    )
                started_events.add(attempt_id)
            elif event_type == "stage_attempt_completed":
                if attempt_id not in started_events:
                    _finding(
                        findings,
                        "invalid_attempt_transition",
                        f"$.runs.events[{line_number}]",
                        f"attempt completion precedes its start: {attempt_id}",
                        "record state transitions in execution order",
                    )
                completed_events.add(attempt_id)
    for attempt_id in sorted(attempts):
        if attempt_id not in started_events or attempt_id not in completed_events:
            _finding(
                findings,
                "attempt_event_incomplete",
                f"$.attempts.{attempt_id}",
                "attempt record lacks a matching start or completion event",
                "retain both transitions in the append-only event journal",
            )
    return len(attempts), set(attempts)


def validate_project_graph(
    project: str | Path,
    *,
    require_checksum_inventory: bool = True,
    require_active_run: bool = True,
    enforce_provenance_exact: bool = True,
    allow_missing_stage_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Validate the complete record graph for one generated project.

    This is deliberately distinct from release policy. It proves that record
    identities, paths, digests, attempts and transitions form a coherent graph;
    technical, runtime and governance gates remain independently fail closed.
    """

    project_root = Path(project).resolve(strict=True)
    if not project_root.is_dir():
        raise ValueError(f"project is not a directory: {project}")
    findings: list[GraphValidationFinding] = []
    project_record = _read_json(project_root / "project.json", findings, "$.project")
    request_payload = _read_json(project_root / "run-request.json", findings, "$.run_request")
    plan_payload = _read_json(project_root / "run-plan.json", findings, "$.run_plan")
    provenance = _read_json(project_root / "provenance.json", findings, "$.provenance")
    request: RunRequest | None = None
    plan: RunPlan | None = None
    if request_payload is not None:
        try:
            request = RunRequest.model_validate(request_payload)
        except ValidationError as exc:
            for error in exc.errors(include_url=False):
                suffix = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error["loc"])
                _finding(
                    findings,
                    "invalid_run_request",
                    f"$.run_request{suffix}",
                    str(error["msg"]),
                    "repair the run request before planning another run",
                    schema_path="schemas/run-request.schema.json",
                )
    if plan_payload is not None:
        try:
            plan = RunPlan.model_validate(plan_payload)
        except ValidationError as exc:
            for error in exc.errors(include_url=False):
                suffix = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error["loc"])
                _finding(
                    findings,
                    "invalid_run_plan",
                    f"$.run_plan{suffix}",
                    str(error["msg"]),
                    "regenerate the plan from a valid request and stage contract",
                )
    if request is not None and plan is not None:
        expected_digest = "sha256:" + sha256_text(request.model_dump_json())
        if plan.request_digest != expected_digest:
            _finding(
                findings,
                "request_digest_mismatch",
                "$.run_plan.request_digest",
                "run plan digest does not identify the persisted request",
                "start a new run from the changed request",
            )
        if plan.request_id != request.id or plan.asset_id != request.id:
            _finding(
                findings,
                "request_identity_mismatch",
                "$.run_plan.request_id",
                "run plan request or asset identity does not match the persisted request",
                "regenerate the plan from the persisted request",
            )
        available = {"run-request"}
        producer_by_deliverable: dict[str, str] = {}
        stage_ids: set[str] = set()
        for index, stage in enumerate(plan.stages):
            base = f"$.run_plan.stages[{index}]"
            if stage.id in stage_ids:
                _finding(
                    findings,
                    "duplicate_stage_id",
                    f"{base}.id",
                    f"stage ID is repeated: {stage.id}",
                    "declare each stage exactly once in the dependency closure",
                )
            stage_ids.add(stage.id)
            for consumed in stage.consumes:
                if consumed not in available:
                    _finding(
                        findings,
                        "unsatisfied_stage_input",
                        f"{base}.consumes",
                        f"stage consumes a deliverable not produced earlier: {consumed}",
                        "add the producer dependency or correct the typed stage contract",
                    )
            for produced in stage.produces:
                prior = producer_by_deliverable.get(produced)
                if prior is not None:
                    _finding(
                        findings,
                        "duplicate_deliverable_producer",
                        f"{base}.produces",
                        f"deliverable {produced} is also produced by {prior}",
                        "assign one authoritative producer for each typed deliverable",
                    )
                producer_by_deliverable[produced] = stage.id
                available.add(produced)
    if require_active_run and project_record is not None and plan is not None:
        active_run_id = str(project_record.get("active_run_id") or "")
        if active_run_id != plan.run_id:
            _finding(
                findings,
                "active_run_mismatch",
                "$.project.active_run_id",
                "project active run does not match run-plan.json",
                "atomically promote one run and its matching plan",
            )
    checksum_count, checksum_index = (
        _validate_checksum_inventory(project_root, findings)
        if require_checksum_inventory
        else (0, {})
    )
    manifest_count, manifests, evidence_index = _validate_manifest_graph(project_root, findings)
    attempt_count = 0
    attempt_ids: set[str] = set()
    if plan is not None:
        attempt_count, attempt_ids = _validate_attempts(
            project_root,
            plan,
            findings,
            allow_missing_stage_ids=allow_missing_stage_ids,
        )
    if provenance is not None and plan is not None:
        if str(provenance.get("run_id") or "") != plan.run_id:
            _finding(
                findings,
                "provenance_run_mismatch",
                "$.provenance.run_id",
                "provenance belongs to a different run",
                "regenerate provenance from the active immutable attempts",
            )
        recorded_attempt_ids = {str(item) for item in provenance.get("attempt_ids", [])}
        missing_attempts = attempt_ids - recorded_attempt_ids
        stale_attempts = recorded_attempt_ids - attempt_ids if enforce_provenance_exact else set()
        for attempt_id in sorted(missing_attempts):
            _finding(
                findings,
                "provenance_attempt_missing",
                "$.provenance.attempt_ids",
                f"provenance omits active attempt: {attempt_id}",
                "regenerate final provenance after stage execution",
            )
        for attempt_id in sorted(stale_attempts):
            _finding(
                findings,
                "provenance_attempt_unresolved",
                "$.provenance.attempt_ids",
                f"provenance names an attempt that is absent: {attempt_id}",
                "restore the immutable attempt or start a clean run",
            )
    governance = next(
        (payload for payload in manifests.values() if str(payload.get("id") or "").endswith("_governance")),
        None,
    )
    if governance is not None and governance.get("release_allowed") is True:
        inconsistent = (
            governance.get("status") != "released"
            or governance.get("release_status") not in {"approved", "ready_for_release"}
            or bool(governance.get("promotion_blockers"))
            or any(decision.get("release_allowed") is not True for decision in governance.get("release_decisions", []))
        )
        if inconsistent:
            _finding(
                findings,
                "inconsistent_release_state",
                "$.manifests.governance.release_allowed",
                "release is allowed while another governance state remains blocked or unresolved",
                "derive release state again from all required policy gates",
            )
    ordered = sorted(findings, key=lambda item: (item.severity != "error", item.code, item.instance_path))
    error_count = sum(item.severity == "error" for item in ordered)
    warning_count = sum(item.severity == "warning" for item in ordered)
    return {
        "format_version": "1.0",
        "project_id": str((project_record or {}).get("project_id") or ""),
        "run_id": str(plan.run_id if plan is not None else ""),
        "status": "blocked" if error_count else "pass",
        "error_count": error_count,
        "warning_count": warning_count,
        "counts": {
            "manifests": manifest_count,
            "evidence_records": len(evidence_index),
            "stage_attempts": attempt_count,
            "checksums": checksum_count,
        },
        "checksum_inventory_entries": len(checksum_index),
        "findings": [asdict(item) for item in ordered],
    }


def validate_pre_release_graph(project: str | Path, current_stage_id: str = "governance") -> dict[str, Any]:
    """Validate the release evidence graph immediately before the governance attempt.

    The current stage has not yet reached a terminal state and final checksums
    are intentionally written after it. Every preceding record, digest and
    transition is still checked.
    """

    return validate_project_graph(
        project,
        require_checksum_inventory=False,
        require_active_run=False,
        enforce_provenance_exact=False,
        allow_missing_stage_ids={current_stage_id},
    )


def _is_external_reference(value: str) -> bool:
    return value.startswith(("http://", "https://", "omniverse://", "s3://", "hf://"))


def _path_attribute_errors(payload: Any, project: str | Path | None = None, prefix: str = "$") -> list[str]:
    if project is None:
        return []
    project_dir = Path(project)
    errors = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            child_prefix = f"{prefix}.{key}"
            if isinstance(value, str) and (key.endswith(PATH_KEY_SUFFIXES) or key in {"asset", "mesh", "texture", "hdri"}):
                if value and not _is_external_reference(value) and not Path(value).is_absolute() and not (project_dir / value).exists():
                    errors.append(f"{child_prefix} release_blocker: target file does not exist: {value}")
            else:
                errors.extend(_path_attribute_errors(value, project_dir, child_prefix))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            errors.extend(_path_attribute_errors(item, project_dir, f"{prefix}[{index}]"))
    return errors


def validate_layout_payload(payload: dict[str, Any], project: str | Path | None = None) -> list[str]:
    errors = []
    if not payload.get("validate_only", False):
        errors.append("layout validation requires validate_only=true before mutation")
    for index, item in enumerate(payload.get("placements", [])):
        layer = item.get("target_layer")
        if layer not in VALID_LAYERS:
            errors.append(f"placements[{index}].target_layer blocked_needs_target: {layer}")
        if not item.get("asset_id"):
            errors.append(f"placements[{index}].asset_id is required")
        if not str(item.get("prim_path", "")).startswith("/"):
            errors.append(f"placements[{index}].prim_path must be absolute")
    errors.extend(_path_attribute_errors(payload, project))
    return errors


def validate_mutation_payload(payload: dict[str, Any], project: str | Path | None = None) -> list[str]:
    errors = []
    if not payload.get("validate_only", False):
        errors.append("mutation validation requires validate_only=true before mutation")
    if not payload.get("rollback_notes"):
        errors.append("rollback_notes is required")
    for index, item in enumerate(payload.get("operations", [])):
        layer = item.get("target_layer")
        action = str(item.get("action") or item.get("operation") or "").lower()
        if layer not in VALID_LAYERS:
            errors.append(f"operations[{index}].target_layer blocked_needs_target: {layer}")
        if not item.get("operation_id"):
            errors.append(f"operations[{index}].operation_id is required")
        if not str(item.get("prim_path", "")).startswith("/"):
            errors.append(f"operations[{index}].prim_path must be absolute")
        if action in {"delete", "remove", "rename"}:
            if not item.get("variant_scan"):
                errors.append(f"operations[{index}].variant_scan is required for {action}")
            if not item.get("inactive_variant_scan"):
                errors.append(f"operations[{index}].inactive_variant_scan is required for {action}")
            if not item.get("orphan_cleanup_plan"):
                errors.append(f"operations[{index}].orphan_cleanup_plan is required for {action}")
        if action == "rename":
            if not item.get("new_prim_path", "").startswith("/"):
                errors.append(f"operations[{index}].new_prim_path must be absolute for rename")
            if "relationship_target_updates" not in item and "unresolved_relationship_targets" not in item:
                errors.append(f"operations[{index}].relationship_target_updates or unresolved_relationship_targets is required for rename")
        if action in {"delete", "remove"}:
            if not item.get("external_references_reported", False):
                errors.append(f"operations[{index}].external_references_reported is required for {action}")
        unresolved = item.get("unresolved_relationship_targets", [])
        if unresolved:
            errors.append(f"operations[{index}].unresolved_relationship_targets block mutation: {unresolved}")
    errors.extend(_path_attribute_errors(payload, project))
    return errors


def validate_json_file(path: str | Path, mode: str, project: str | Path | None = None) -> list[str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if mode == "layout":
        return validate_layout_payload(payload, project)
    if mode == "mutation":
        return validate_mutation_payload(payload, project)
    raise ValueError(mode)
