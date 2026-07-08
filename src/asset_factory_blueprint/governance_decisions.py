from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from asset_factory_blueprint.execution import atomic_write_json, immutable_write_json, workspace_lease
from asset_factory_blueprint.manifests import validate_payload
from asset_factory_blueprint.schemas.common import RunPlan
from asset_factory_blueprint.security import ensure_path_component
from asset_factory_blueprint.utils.ids import content_id


SUPPORTED_RELEASE_SCOPES = {
    "visualisation",
    "rigid_body_manipulation",
    "articulated_training",
    "redistribution",
}


def _load_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _normalise_timestamp(value: str, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_operator_release_decision(
    project: str | Path,
    *,
    reviewer: str,
    decision: str,
    expires_at: str,
    scope: str | None = None,
    notes: Iterable[str] = (),
    decided_at: str | None = None,
) -> dict[str, Any]:
    """Bind an operator decision to the exact current run and asset evidence."""

    root = Path(project).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"project is not a directory: {project}")
    reviewer = reviewer.strip()
    if not reviewer:
        raise ValueError("reviewer identity is required")
    if decision not in {"approve", "reject"}:
        raise ValueError("decision must be approve or reject")
    plan = RunPlan.model_validate(_load_object(root / "run-plan.json", "run plan"))
    governance = _load_object(root / "manifests" / "governance-record.json", "governance record")
    simready = _load_object(root / "manifests" / "simready-asset-manifest.json", "SimReady candidate manifest")
    selected_scope = str(scope or governance.get("task_scope") or "visualisation")
    if selected_scope not in SUPPORTED_RELEASE_SCOPES:
        supported = ", ".join(sorted(SUPPORTED_RELEASE_SCOPES))
        raise ValueError(f"unsupported release scope {selected_scope!r}; expected one of: {supported}")
    expiry = _normalise_timestamp(expires_at, "expires_at")
    if datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
        raise ValueError("expires_at must be in the future")
    decision_time = _normalise_timestamp(
        decided_at or datetime.now(timezone.utc).isoformat(),
        "decided_at",
    )
    profile = simready.get("simready_profile") or {}
    if not isinstance(profile, dict):
        raise ValueError("SimReady candidate manifest does not contain a structured Profile")
    if decision == "approve" and (
        not profile.get("profile_id")
        or not profile.get("profile_version")
        or profile.get("profile_version_status") != "pinned"
    ):
        raise ValueError("approval requires an exact pinned Profile ID and version")
    asset_fingerprint = str(governance.get("asset_fingerprint") or "")
    if not asset_fingerprint.startswith("sha256:") or len(asset_fingerprint) != 71:
        raise ValueError("governance record does not contain the current asset fingerprint")
    core = {
        "decided_by": reviewer,
        "decided_at": decision_time,
        "decision": decision,
        "scope": selected_scope,
        "run_id": plan.run_id,
        "request_digest": plan.request_digest,
        "asset_fingerprint": asset_fingerprint,
        "profile_id": str(profile.get("profile_id") or ""),
        "profile_version": str(profile.get("profile_version") or ""),
        "expires_at": expiry,
        "notes": [str(item) for item in notes if str(item).strip()],
    }
    core["decision_id"] = content_id("operator_decision", core, digest_length=32)
    issues = validate_payload("operator-release-decision", core)
    if issues:
        raise RuntimeError("generated operator decision is invalid: " + "; ".join(issue.render() for issue in issues))
    return core


def write_operator_release_decision(project: str | Path, decision: dict[str, Any]) -> dict[str, Any]:
    """Persist the current decision and an immutable content-addressed history record."""

    root = Path(project).resolve(strict=True)
    plan = RunPlan.model_validate(_load_object(root / "run-plan.json", "run plan"))
    issues = validate_payload("operator-release-decision", decision)
    if issues:
        raise ValueError("operator decision is invalid: " + "; ".join(issue.render() for issue in issues))
    ensure_path_component(plan.run_id, "run ID")
    decision_id = str(decision.get("decision_id") or "")
    expected_id = content_id(
        "operator_decision",
        {key: value for key, value in decision.items() if key != "decision_id"},
        digest_length=32,
    )
    if decision_id != expected_id:
        raise ValueError("operator decision ID does not match its content")
    ensure_path_component(decision_id, "decision ID")
    history_path = root / "governance-decisions" / f"{decision_id}.json"
    current_path = root / "operator-release-decision.json"
    with workspace_lease(root, plan.run_id):
        if history_path.exists():
            existing = _load_object(history_path, "operator decision history record")
            if existing != decision:
                raise ValueError(f"decision ID collision at {history_path}")
        else:
            immutable_write_json(history_path, decision)
        atomic_write_json(current_path, decision)
    return {
        "decision": decision,
        "current_path": current_path.relative_to(root).as_posix(),
        "history_path": history_path.relative_to(root).as_posix(),
    }
