from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from asset_factory_blueprint.execution import atomic_write_json
from asset_factory_blueprint.isaac_evidence import (
    PROTOCOL_ID,
    PROTOCOL_VERSION,
    REPORT_ID,
    REPORT_VERSION,
    attestation_secret,
    parse_runtime_report_bytes,
    producer_sha256_pin,
    verify_runtime_report_envelope,
)
from asset_factory_blueprint.manifests import validate_payload
from asset_factory_blueprint.services.simready import evaluate_runtime_validation
from asset_factory_blueprint.utils.checksums import sha256_file
from asset_factory_blueprint.utils.ids import content_id
from asset_factory_blueprint.utils.package_fingerprint import package_inventory_fingerprint
from asset_factory_blueprint.validation import build_project_checksum_inventory


_SAFE_ASSET_ID = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")


def _write_json(path: Path, payload: Any) -> None:
    atomic_write_json(path, payload)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _contains_parent_reference(path: Path) -> bool:
    return any(part == ".." for part in path.parts)


def _assert_safe_project_target(project_dir: Path, target: Path, label: str) -> None:
    project_root = project_dir.resolve(strict=True)
    resolved = target.resolve(strict=False)
    if resolved != project_root and project_root not in resolved.parents:
        raise ValueError(f"{label} escapes the project workspace")
    current = target
    while current != project_root:
        if _is_link_or_junction(current):
            raise ValueError(f"{label} must not use a symbolic link or junction: {current}")
        if current.parent == current:
            raise ValueError(f"{label} is not rooted in the project workspace")
        current = current.parent


def _project_file(project_dir: Path, value: str, label: str) -> Path:
    raw = Path(value)
    if raw.is_absolute() or _contains_parent_reference(raw):
        raise ValueError(f"{label} must be one confined project-relative path")
    target = project_dir / raw
    _assert_safe_project_target(project_dir, target, label)
    resolved = target.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{label} is not a regular file")
    return resolved


def _atomic_write_bytes(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _read_supplied_report(report: str | Path) -> tuple[Path, bytes, dict[str, Any]]:
    lexical = Path(report)
    if _contains_parent_reference(lexical):
        raise ValueError("runtime report path must not contain parent traversal")
    if _is_link_or_junction(lexical):
        raise ValueError("runtime report must not be a symbolic link or junction")
    supplied = lexical.resolve(strict=True)
    if not supplied.is_file():
        raise ValueError("runtime report must be a regular file")
    raw = supplied.read_bytes()
    return supplied, raw, parse_runtime_report_bytes(raw)


def _validate_report_binding(
    project_dir: Path,
    simready: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    issues = validate_payload("isaac-runtime-evidence", payload)
    if issues:
        rendered = "; ".join(issue.render() for issue in issues)
        raise ValueError(f"runtime report schema validation failed: {rendered}")
    identity = payload.get("report_identity") or {}
    protocol = payload.get("protocol_identity") or {}
    if identity != {"id": REPORT_ID, "version": REPORT_VERSION}:
        raise ValueError("runtime report identity does not match the supported producer contract")
    if protocol != {"id": PROTOCOL_ID, "version": PROTOCOL_VERSION}:
        raise ValueError("runtime report protocol identity does not match the supported protocol")
    secret = attestation_secret()
    producer_pin = producer_sha256_pin()
    envelope_errors = verify_runtime_report_envelope(payload, secret, producer_pin)
    if envelope_errors:
        raise ValueError("; ".join(envelope_errors))
    execution_identity = payload.get("execution_identity") or {}
    try:
        started_at = datetime.fromisoformat(str(execution_identity.get("started_at") or "").replace("Z", "+00:00"))
        completed_at = datetime.fromisoformat(
            str(execution_identity.get("completed_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("runtime report execution timestamps are invalid") from exc
    if (
        started_at.tzinfo is None
        or started_at.utcoffset() is None
        or completed_at.tzinfo is None
        or completed_at.utcoffset() is None
        or completed_at < started_at
    ):
        raise ValueError("runtime report execution timestamps are unordered or lack a timezone")
    parameters = payload.get("validation_parameters") or {}
    for field in ("width", "height", "min_seconds", "physics_dt"):
        if payload.get(field) != parameters.get(field):
            raise ValueError(f"runtime report {field} conflicts with validation_parameters")

    profile = simready.get("simready_profile") or simready.get("simready_conformance", {}).get("profile") or {}
    if not profile.get("profile_id") or not profile.get("profile_version"):
        raise ValueError("SimReady manifest does not declare an exact Profile ID and version")
    if payload.get("profile_id") != profile["profile_id"] or payload.get("profile_version") != profile["profile_version"]:
        raise ValueError("runtime report Profile ID or version does not match the SimReady manifest")

    expected_usd_value = str(simready.get("package_path") or simready.get("usd_root_path") or "")
    if not expected_usd_value:
        raise ValueError("SimReady manifest does not identify a runtime USD file")
    expected_usd = _project_file(project_dir, expected_usd_value, "runtime USD path")
    expected_relative = expected_usd.relative_to(project_dir).as_posix()
    if str(payload.get("usd_path") or "") != expected_relative:
        raise ValueError("runtime report USD path does not match the portable project-relative target")
    expected_label = "project:///" + quote(expected_relative, safe="/-._~")
    if str(payload.get("usd_label") or "") != expected_label:
        raise ValueError("runtime report portable USD label does not match the target")
    if str(payload.get("usd_sha256") or "").lower() != sha256_file(expected_usd).lower():
        raise ValueError("runtime report USD checksum does not match the target")

    package_binding = package_inventory_fingerprint(expected_usd.parent)
    if package_binding["status"] != "pass":
        raise ValueError("runtime USD package inventory cannot be verified")
    if payload.get("package_dependency_fingerprint") != package_binding["fingerprint"]:
        raise ValueError("runtime report package fingerprint does not match the materialised package")
    if payload.get("package_inventory") != package_binding["files"]:
        raise ValueError("runtime report package inventory does not match the materialised package")
    if payload.get("status") == "pass":
        runtime_identity = payload.get("runtime_identity") or {}
        availability = payload.get("runtime_availability") or {}
        if payload.get("loaded") is not True or availability.get("isaac_sim") is not True:
            raise ValueError("passing runtime report does not attest a loaded Isaac Sim runtime")
        if not runtime_identity.get("version"):
            raise ValueError("passing runtime report does not identify the Isaac Sim version")
        if payload.get("errors"):
            raise ValueError("passing runtime report contains runtime errors")
    return expected_usd, profile


def _refresh_project_checksums(project_dir: Path) -> Path:
    checksums_path = project_dir / "evidence" / "checksums.json"
    _write_json(checksums_path, build_project_checksum_inventory(project_dir))
    return checksums_path


def apply_isaac_load_report(project: str | Path, report: str | Path) -> dict[str, Any]:
    project_dir = Path(project).resolve(strict=True)
    if not project_dir.is_dir():
        raise ValueError("project must be a directory")
    _, report_bytes, report_payload = _read_supplied_report(report)
    canonical_report_path = project_dir / "reports" / "isaac-load-check.json"
    _assert_safe_project_target(project_dir, canonical_report_path, "canonical runtime report target")
    simready_path = project_dir / "manifests" / "simready-asset-manifest.json"
    _assert_safe_project_target(project_dir, simready_path, "SimReady manifest target")
    if not simready_path.is_file():
        raise FileNotFoundError(f"missing SimReady manifest: {simready_path}")
    simready = json.loads(simready_path.read_text(encoding="utf-8"))
    if not isinstance(simready, dict):
        raise ValueError("SimReady manifest must be a JSON object")
    asset_id = str(simready.get("asset_id") or "")
    if not _SAFE_ASSET_ID.fullmatch(asset_id):
        raise ValueError("SimReady asset_id must be one lowercase safe slug")
    asset_evidence_path = project_dir / "assets" / asset_id / "evidence" / "asset-package-evidence.json"
    _assert_safe_project_target(project_dir, asset_evidence_path, "asset evidence target")
    asset_evidence: dict[str, Any] | None = None
    if asset_evidence_path.exists():
        asset_evidence = json.loads(asset_evidence_path.read_text(encoding="utf-8"))
        if not isinstance(asset_evidence, dict):
            raise ValueError("asset package evidence must be a JSON object")
    governance_path = project_dir / "manifests" / "governance-record.json"
    _assert_safe_project_target(project_dir, governance_path, "governance manifest target")
    governance: dict[str, Any] | None = None
    if governance_path.exists():
        governance = json.loads(governance_path.read_text(encoding="utf-8"))
        if not isinstance(governance, dict):
            raise ValueError("governance manifest must be a JSON object")
    checksums_target = project_dir / "evidence" / "checksums.json"
    _assert_safe_project_target(project_dir, checksums_target, "project checksum target")
    _, profile = _validate_report_binding(project_dir, simready, report_payload)

    report_path = _atomic_write_bytes(canonical_report_path, report_bytes)
    report_rel = _relative(report_path, project_dir)
    runtime_validation = evaluate_runtime_validation(
        project_dir,
        usd_root_path=str(simready.get("usd_root_path") or ""),
        package_path=str(simready.get("package_path") or ""),
        report_path=report_rel,
        profile=profile,
    )
    passed = runtime_validation["status"] == "pass"
    gate = {
        "gate_id": "isaac-load",
        "gate_type": "isaac",
        "status": "pass" if passed else "blocked",
        "evidence_path": report_rel,
        "repair_action": "" if passed else "rerun Isaac Sim load validation",
        "rerun_required": not passed,
    }
    validation_results = [
        item
        for item in simready.get("validation_results", [])
        if item.get("gate_id") not in {"isaac-load", "simready-runtime-behaviour"}
    ]
    validation_results.append(gate)
    validation_results.append(
        {
            "gate_id": "simready-runtime-behaviour",
            "gate_type": "runtime",
            "status": runtime_validation["status"],
            "evidence_path": report_rel,
            "repair_action": "" if passed else runtime_validation.get("reason", "rerun behavioural validation"),
            "rerun_required": not passed,
        }
    )
    simready["validation_results"] = validation_results
    conformance = simready.get("simready_conformance", {})
    conformance["runtime_validation"] = runtime_validation
    profile_pinned = profile.get("profile_version_status") == "pinned"
    requirements_pass = bool(conformance.get("requirements")) and all(
        item.get("status") == "pass" for item in conformance.get("requirements", [])
    )
    features_pass = bool(conformance.get("features")) and all(
        item.get("status") == "pass" for item in conformance.get("features", [])
    )
    official_pass = conformance.get("official_validator", {}).get("status") == "pass"
    conformance_passed = profile_pinned and requirements_pass and features_pass and official_pass and passed
    conformance["status"] = "pass" if conformance_passed else "blocked"
    simready["simready_conformance"] = conformance
    all_gates_pass = all(item.get("status") == "pass" for item in validation_results)
    simready["status"] = "validated" if conformance_passed and all_gates_pass else "blocked"
    simready["validation_status"] = simready["status"]
    simready["validation_gates"] = [
        {"gate_id": "schema-valid", "status": "pass"},
        {"gate_id": "isaac-load", "status": "pass" if passed else "blocked"},
    ]
    simready["isaac_sim_load_check"] = {
        "status": "pass" if passed else "blocked",
        "report_path": report_rel,
        "default_prim": report_payload.get("default_prim", ""),
        "prim_count": report_payload.get("prim_count", 0),
        "elapsed_seconds": report_payload.get("elapsed_seconds", 0.0),
        "errors": report_payload.get("errors", []),
        "profile_id": report_payload.get("profile_id", ""),
        "profile_version": report_payload.get("profile_version", ""),
        "required_test_ids": runtime_validation.get("required_test_ids", []),
        "behavioural_tests": runtime_validation.get("behavioural_tests", []),
        "reason": runtime_validation.get("reason", ""),
    }
    simready["performance_budget"] = {
        "status": "measured" if passed else "blocked",
        "measurements": report_payload.get("performance", {}),
    }
    simready["promotion_status"] = "review_required" if conformance_passed else "failed"
    evidence = [item for item in simready.get("evidence", []) if item.get("evidence_id") != "isaac_load_check"]
    evidence.append(
        {
            "evidence_id": "isaac_load_check",
            "kind": "isaac_report",
            "uri": report_rel,
            "checksum": sha256_file(report_path),
        }
    )
    simready["evidence"] = evidence
    _write_json(simready_path, simready)

    if asset_evidence is not None:
        blockers = list(asset_evidence.get("release_blockers", []))
        if passed:
            blockers = [item for item in blockers if "isaac load validation has not run" not in str(item).lower()]
            asset_evidence["isaac_load_check"] = {
                "status": "pass",
                "report_path": report_rel,
                "default_prim": report_payload.get("default_prim", ""),
                "behavioural_tests": runtime_validation.get("behavioural_tests", []),
            }
        asset_evidence["release_blockers"] = blockers
        _write_json(asset_evidence_path, asset_evidence)

    if governance is not None:
        blockers = list(governance.get("promotion_blockers", []))
        invalidation = "governance policy must be re-evaluated after runtime evidence changes"
        if invalidation not in blockers:
            blockers.append(invalidation)
        governance["promotion_blockers"] = blockers
        governance["blocked_reasons"] = blockers
        governance["release_status"] = "blocked"
        governance["release_allowed"] = False
        governance["validation_status"] = "blocked"
        governance["status"] = "blocked"
        previous_decisions = list(governance.get("release_decisions", []))
        scope = str(governance.get("task_scope") or "visualisation")
        evaluated_at = datetime.now(timezone.utc).isoformat()
        evaluation_order = ["runtime_evidence_import", "release_invalidation"]
        invalidation_decision = {
            "decision_id": content_id(
                "release",
                {
                    "governance_id": governance.get("id"),
                    "scope": scope,
                    "policy_version": "1.1",
                    "evaluated_at": evaluated_at,
                    "evaluation_order": evaluation_order,
                    "blockers": [invalidation],
                    "runtime_report_sha256": sha256_file(report_path),
                },
                digest_length=32,
            ),
            "scope": scope,
            "policy_version": "1.1",
            "evaluated_at": evaluated_at,
            "evaluation_order": evaluation_order,
            "release_allowed": False,
            "release_status": "blocked",
            "required_uses": [],
            "required_gates": ["isaac-load", "governance-review"],
            "blockers": [invalidation],
        }
        governance["superseded_decision_ids"] = [
            str(item.get("decision_id")) for item in previous_decisions if item.get("decision_id")
        ]
        governance["release_decisions"] = [*previous_decisions, invalidation_decision]
        evidence = [item for item in governance.get("evidence", []) if item.get("evidence_id") != "isaac_load_check"]
        evidence.append(
            {
                "evidence_id": "isaac_load_check",
                "kind": "isaac_report",
                "uri": report_rel,
                "checksum": sha256_file(report_path),
            }
        )
        governance["evidence"] = evidence
        _write_json(governance_path, governance)

    checksums_path = _refresh_project_checksums(project_dir)
    return {
        "status": "pass" if passed else "blocked",
        "simready_status": simready["status"],
        "conformance_status": conformance["status"],
        "project": project_dir.as_posix(),
        "simready_manifest": simready_path.relative_to(project_dir).as_posix(),
        "report": report_rel,
        "checksums": checksums_path.relative_to(project_dir).as_posix(),
    }
