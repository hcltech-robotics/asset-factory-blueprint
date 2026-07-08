from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from asset_factory_blueprint.skills.base import ToolResult
from asset_factory_blueprint.utils.ids import content_id
from asset_factory_blueprint.validation import validate_mutation_payload


_PASS_STATUSES = frozenset({"pass", "passed", "validated", "approved", "released"})
_SHA256_PATTERN = re.compile(r"^(?:sha256:)?[A-Fa-f0-9]{64}$")
_EVALUATION_ORDER = (
    "source_rights",
    "retention",
    "reviewer",
    "secret_attestation",
    "validation_gates",
    "stage_reports",
    "asset_validation",
    "record_status",
)


@dataclass(frozen=True)
class ReleasePolicy:
    scope: str
    required_uses: tuple[str, ...]
    required_gates: tuple[str, ...]
    require_derivatives: bool = True
    require_redistribution: bool = False


RELEASE_POLICIES: dict[str, ReleasePolicy] = {
    "visualisation": ReleasePolicy(
        scope="visualisation",
        required_uses=("visualisation",),
        required_gates=(
            "schema-valid",
            "source-lineage",
            "material-evidence",
            "record-graph",
            "task-fitness",
            "governance-review",
        ),
    ),
    "rigid_body_manipulation": ReleasePolicy(
        scope="rigid_body_manipulation",
        required_uses=("simulation",),
        required_gates=(
            "schema-valid",
            "source-lineage",
            "material-evidence",
            "isaac-load",
            "record-graph",
            "task-fitness",
            "governance-review",
        ),
    ),
    "articulated_training": ReleasePolicy(
        scope="articulated_training",
        required_uses=("simulation", "model_training"),
        required_gates=(
            "schema-valid",
            "source-lineage",
            "material-evidence",
            "nonvisual-evidence",
            "isaac-load",
            "record-graph",
            "task-fitness",
            "governance-review",
        ),
    ),
    "redistribution": ReleasePolicy(
        scope="redistribution",
        required_uses=("redistribution",),
        required_gates=("schema-valid", "source-lineage", "record-graph", "task-fitness", "governance-review"),
        require_redistribution=True,
    ),
}


def release_policy(scope: str) -> ReleasePolicy:
    try:
        return RELEASE_POLICIES[scope]
    except KeyError as exc:
        supported = ", ".join(sorted(RELEASE_POLICIES))
        raise ValueError(f"unknown release scope {scope!r}; expected one of: {supported}") from exc


def _evidence_index(record: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(item.get("evidence_id")): item
        for item in (record.get("evidence") or [])
        if isinstance(item, Mapping) and item.get("evidence_id")
    }


def _has_credible_evidence(evidence_id: str, evidence: Mapping[str, Mapping[str, Any]]) -> bool:
    item = evidence.get(evidence_id)
    if not item:
        return False
    return bool(item.get("uri")) and bool(_SHA256_PATTERN.fullmatch(str(item.get("checksum", ""))))


def _append_once(blockers: list[str], message: str) -> None:
    if message not in blockers:
        blockers.append(message)


def _normalise_gate_results(gate_results: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    return {
        str(item.get("gate_id") or item.get("id") or ""): str(item.get("status") or "").lower()
        for item in gate_results
        if isinstance(item, Mapping) and (item.get("gate_id") or item.get("id"))
    }


def _evaluation_datetime(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("governance evaluation time must include a timezone")
    return parsed.astimezone(timezone.utc)


def _expiry_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def evaluate_release_policy(
    record: Mapping[str, Any],
    scope: str,
    *,
    gate_results: Iterable[Mapping[str, Any]] = (),
    stage_reports: Iterable[Mapping[str, Any]] = (),
    required_stage_ids: Iterable[str] = (),
    asset_validation_status: str | None = None,
    evaluated_at: str | datetime | None = None,
) -> dict[str, Any]:
    """Derive a fail-closed release decision from rights, review and validation evidence."""

    policy = release_policy(scope)
    evaluation_time = _evaluation_datetime(evaluated_at)
    evaluation_time_text = evaluation_time.isoformat()
    blockers: list[str] = []
    evidence = _evidence_index(record)
    source_rights = [item for item in (record.get("source_rights") or []) if isinstance(item, Mapping)]
    if not source_rights:
        blockers.append("structured source rights evidence is required")
    for index, rights in enumerate(source_rights):
        source_id = str(rights.get("source_id") or f"source_{index}")
        if rights.get("rights_status") != "cleared":
            _append_once(blockers, f"{source_id}: rights are not cleared")
        if str(rights.get("licence_expression") or "NOASSERTION").upper() == "NOASSERTION" and not rights.get(
            "terms_uri"
        ):
            _append_once(blockers, f"{source_id}: licence expression or terms URI is required")
        permitted_uses = {str(item) for item in rights.get("permitted_uses", [])}
        for required_use in policy.required_uses:
            if "*" not in permitted_uses and required_use not in permitted_uses and scope not in permitted_uses:
                _append_once(blockers, f"{source_id}: {required_use} use is not permitted")
        if policy.require_derivatives and rights.get("derivatives_allowed") is not True:
            _append_once(blockers, f"{source_id}: derivative works are not permitted")
        if policy.require_redistribution and rights.get("redistribution_allowed") is not True:
            _append_once(blockers, f"{source_id}: redistribution is not permitted")
        if rights.get("privacy_status") not in {"cleared", "not_applicable"}:
            _append_once(blockers, f"{source_id}: privacy status is not cleared")
        if rights.get("privacy_status") == "cleared" and not rights.get("consent_evidence_ids"):
            _append_once(blockers, f"{source_id}: consent evidence is required for privacy clearance")
        rights_evidence = [str(item) for item in rights.get("evidence_ids", [])]
        if not rights_evidence:
            _append_once(blockers, f"{source_id}: rights evidence is required")
        for evidence_id in rights_evidence:
            if not _has_credible_evidence(evidence_id, evidence):
                _append_once(blockers, f"{source_id}: rights evidence {evidence_id!r} is missing or not content-addressed")
        rights_expiry_raw = rights.get("expires_at")
        if rights_expiry_raw:
            rights_expiry = _expiry_datetime(rights_expiry_raw)
            if rights_expiry is None:
                _append_once(blockers, f"{source_id}: rights expiry is invalid or lacks a timezone")
            elif rights_expiry <= evaluation_time:
                _append_once(blockers, f"{source_id}: rights expired at {rights_expiry.isoformat()}")

    retention = record.get("retention")
    if not isinstance(retention, Mapping):
        blockers.append("structured retention policy is required")
    else:
        retention_expiry_raw = retention.get("expires_at")
        if retention.get("policy") == "fixed_period" and not retention_expiry_raw:
            blockers.append("fixed-period retention requires an expiry time")
        if retention_expiry_raw:
            retention_expiry = _expiry_datetime(retention_expiry_raw)
            if retention_expiry is None:
                blockers.append("retention expiry is invalid or lacks a timezone")
            elif retention_expiry <= evaluation_time:
                blockers.append(f"retention policy expired at {retention_expiry.isoformat()}")
        retention_evidence = [str(item) for item in retention.get("evidence_ids", [])]
        if not retention_evidence:
            blockers.append("retention evidence is required")
        for evidence_id in retention_evidence:
            if not _has_credible_evidence(evidence_id, evidence):
                _append_once(blockers, f"retention evidence {evidence_id!r} is missing or not content-addressed")

    reviewer = record.get("reviewer")
    if not isinstance(reviewer, Mapping) or reviewer.get("review_status") != "approved":
        blockers.append("review approval is required")
    else:
        if not reviewer.get("reviewer_id") or not reviewer.get("reviewed_at"):
            blockers.append("reviewer identity and review time are required")
        reviewer_evidence = [str(item) for item in reviewer.get("evidence_ids", [])]
        if not reviewer_evidence:
            blockers.append("review evidence is required")
        for evidence_id in reviewer_evidence:
            if not _has_credible_evidence(evidence_id, evidence):
                _append_once(blockers, f"review evidence {evidence_id!r} is missing or not content-addressed")

    if record.get("raw_secrets_recorded") is not False:
        blockers.append("raw secret handling has not been attested")

    gates = _normalise_gate_results(gate_results)
    for gate_id in policy.required_gates:
        if gates.get(gate_id) not in _PASS_STATUSES:
            blockers.append(f"required gate {gate_id!r} has not passed")

    reports = list(stage_reports)
    required_stages = [str(stage_id) for stage_id in required_stage_ids]
    report_ids: list[str] = []
    if not reports:
        blockers.append("upstream stage reports are required for release evaluation")
    for report in reports:
        if not isinstance(report, Mapping):
            _append_once(blockers, "stage report is malformed")
            continue
        stage_id = str(report.get("stage_id") or "unknown")
        if stage_id in report_ids:
            _append_once(blockers, f"stage report {stage_id!r} is duplicated")
        report_ids.append(stage_id)
        status = str(report.get("status") or "not_validated").lower()
        if status not in _PASS_STATUSES:
            _append_once(blockers, f"stage {stage_id!r} is {status}")
        if report.get("blocked_reasons") or report.get("manifest_errors"):
            _append_once(blockers, f"stage {stage_id!r} has unresolved blockers")
    for stage_id in required_stages:
        if stage_id not in report_ids:
            _append_once(blockers, f"required upstream stage report {stage_id!r} is missing")

    if asset_validation_status is not None and asset_validation_status.lower() not in _PASS_STATUSES:
        blockers.append("generated asset validation has not passed")
    if record.get("release_status") == "blocked":
        blockers.append("governance record is blocked")

    decision_basis = {
        "governance_id": record.get("id"),
        "scope": scope,
        "policy_version": "1.1",
        "evaluated_at": evaluation_time_text,
        "evaluation_order": list(_EVALUATION_ORDER),
        "blockers": sorted(blockers),
    }
    release_allowed = not blockers
    return {
        "decision_id": content_id("release", decision_basis, digest_length=32),
        "scope": scope,
        "policy_version": "1.1",
        "evaluated_at": evaluation_time_text,
        "evaluation_order": list(_EVALUATION_ORDER),
        "release_allowed": release_allowed,
        "release_status": "approved" if release_allowed else "blocked",
        "required_uses": list(policy.required_uses),
        "required_gates": list(policy.required_gates),
        "blockers": blockers,
    }


def evaluate_release_scopes(
    record: Mapping[str, Any],
    scopes: Iterable[str],
    **context: Any,
) -> list[dict[str, Any]]:
    """Evaluate several named scopes without mutating the governance record."""

    return [evaluate_release_policy(record, scope, **context) for scope in scopes]


def _legacy_rights_record(params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "rights_id": str(params.get("rights_id") or "rights_source_0"),
        "source_id": str(params.get("source_id") or "source_0"),
        "rights_status": str(params.get("rights_status") or "unknown"),
        "licence_expression": str(
            params.get("licence_expression") or params.get("license_expression") or "NOASSERTION"
        ),
        "terms_uri": params.get("terms_uri"),
        "creator": params.get("creator"),
        "revision": params.get("source_revision"),
        "attribution": params.get("attribution"),
        "permitted_uses": list(params.get("permitted_uses") or []),
        "redistribution_allowed": params.get("redistribution_allowed") is True,
        "derivatives_allowed": params.get("derivatives_allowed") is True,
        "privacy_status": str(params.get("privacy_status") or "unknown"),
        "consent_evidence_ids": list(params.get("consent_evidence_ids") or []),
        "evidence_ids": list(params.get("rights_evidence_ids") or []),
        "expires_at": params.get("rights_expires_at"),
        "extensions": {},
    }


def governance_record(params: dict[str, Any]) -> ToolResult:
    source_rights = params.get("source_rights")
    if not isinstance(source_rights, list):
        source_rights = [_legacy_rights_record(params)]
    retention = params.get("retention")
    if not isinstance(retention, Mapping):
        retention = {
            "policy": str(params.get("retention_policy") or "project"),
            "expires_at": params.get("retention_expires_at"),
            "deletion_required": params.get("deletion_required") is True,
            "evidence_ids": list(params.get("retention_evidence_ids") or []),
            "extensions": {},
        }
    reviewer = params.get("reviewer")
    if not isinstance(reviewer, Mapping):
        reviewer = {
            "reviewer_id": str(params.get("reviewer_id") or ""),
            "review_status": str(params.get("review_status") or "not_reviewed"),
            "reviewed_at": params.get("reviewed_at"),
            "evidence_ids": list(params.get("review_evidence_ids") or []),
            "extensions": {},
        }
    record = {
        "id": str(params.get("id") or "governance_record"),
        "version": str(params.get("version") or "2.0"),
        "status": "review_required",
        "evidence": list(params.get("evidence") or []),
        "source_rights": source_rights,
        "retention": dict(retention),
        "reviewer": dict(reviewer),
        "raw_secrets_recorded": False,
        "extensions": dict(params.get("extensions") or {}),
        # Compatibility views retained for v1 tool consumers.
        "rights_status": str(params.get("rights_status") or "unknown"),
        "retention_policy": str(retention.get("policy") or "project"),
        "review_status": str(reviewer.get("review_status") or "not_reviewed"),
    }
    scope = str(params.get("task_scope") or "visualisation")
    decision = evaluate_release_policy(
        record,
        scope,
        gate_results=params.get("gate_results") or [],
        stage_reports=params.get("stage_reports") or [],
        required_stage_ids=params.get("required_stage_ids") or [],
        asset_validation_status=params.get("asset_validation_status"),
        evaluated_at=params.get("evaluated_at"),
    )
    record.update(
        {
            "status": "validated" if decision["release_allowed"] else "review_required",
            "release_status": decision["release_status"],
            "release_allowed": decision["release_allowed"],
            "release_decisions": [decision],
            "promotion_blockers": decision["blockers"],
        }
    )
    return ToolResult(
        success=True,
        data=record,
        warnings=decision["blockers"],
        validation_status="validated" if decision["release_allowed"] else "review_required",
    )


def governance_mutation_validate(params: dict[str, Any]) -> ToolResult:
    errors = validate_mutation_payload(params)
    return ToolResult(
        success=not errors,
        data={"errors": errors, "error_count": len(errors)},
        warnings=errors,
        validation_status="validated" if not errors else "blocked",
    )
