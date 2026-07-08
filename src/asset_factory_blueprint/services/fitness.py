from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from asset_factory_blueprint.execution import atomic_write_json
from asset_factory_blueprint.manifests import validate_payload
from asset_factory_blueprint.schemas.common import RunPlan, RunRequest
from asset_factory_blueprint.utils.checksums import sha256_file, sha256_text
from asset_factory_blueprint.utils.ids import content_id


FITNESS_TESTS_BY_SCOPE = {
    "visualisation": ("visual_render_acceptance",),
    "rigid_body_manipulation": ("manipulation_contact_fidelity",),
    "articulated_training": ("joint_task_fidelity",),
    "redistribution": ("consumer_install_reproduction",),
}
TASK_FITNESS_FORMAT_VERSION = "2.0.0"
TASK_FITNESS_PROTOCOL_SCHEMA_VERSION = "1.0.0"


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON numeric constant: {value}")


def _load_json_object(path: Path, *, maximum_bytes: int = 4_194_304) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("file must be a regular non-symbolic-link file")
    if path.stat().st_size > maximum_bytes:
        raise ValueError(f"file exceeds the {maximum_bytes}-byte limit")
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_json_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason and reason not in reasons:
        reasons.append(reason)


def _resolve_materialised_file(root: Path, raw_path: Any, label: str) -> tuple[Path | None, str]:
    value = str(raw_path or "")
    if not value:
        return None, f"{label} path is missing"
    if "\\" in value or ":" in value:
        return None, f"{label} path must be a portable project-relative POSIX path"
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None, f"{label} path must remain inside the project"
    project_root = root.resolve(strict=True)
    candidate = project_root.joinpath(*relative.parts)
    current = project_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return None, f"{label} path must not traverse a symbolic link"
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(project_root)
    except (OSError, ValueError):
        return None, f"{label} file is missing or outside the project"
    if not resolved.is_file():
        return None, f"{label} path does not name a regular file"
    return resolved, ""


def _declared_sha256_matches(path: Path, declared: Any) -> bool:
    return str(declared or "") == "sha256:" + sha256_file(path)


def _evidence_record_id(record: dict[str, Any]) -> str:
    return content_id(
        "evidence",
        {
            "kind": str(record.get("kind") or ""),
            "path": str(record.get("path") or ""),
            "sha256": str(record.get("sha256") or ""),
        },
        digest_length=32,
    )


def _task_fitness_report_id(report: dict[str, Any]) -> str:
    core = {key: value for key, value in report.items() if key != "report_id"}
    return content_id("task_fitness", core, digest_length=32)


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _release_scope(request: RunRequest) -> str:
    default_scope = (
        "articulated_training"
        if any("rl" in item.lower() for item in request.requested_outputs)
        else "rigid_body_manipulation"
        if any("simready" in item.lower() for item in request.requested_outputs)
        else "visualisation"
    )
    return str(request.constraints.get("release_scope") or default_scope)


def asset_package_fingerprint(asset_package: dict[str, Any], asset_validation: dict[str, Any]) -> str:
    return "sha256:" + sha256_text(
        json.dumps(
            {
                "files": sorted(
                    asset_package.get("files", []),
                    key=lambda item: (str(item.get("path") or ""), str(item.get("sha256") or "")),
                ),
                "validation_report_sha256": asset_validation.get("report_sha256", ""),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _blocked(scope: str, required_test_ids: tuple[str, ...], reasons: list[str]) -> dict[str, Any]:
    return {
        "status": "blocked",
        "scope": scope,
        "required_test_ids": list(required_test_ids),
        "tests": [],
        "report_path": "reports/task-fitness-evidence.json",
        "report_sha256": "",
        "blocked_reasons": reasons,
    }


def evaluate_task_fitness(
    project_dir: str | Path,
    request: RunRequest,
    plan: RunPlan,
    asset_package: dict[str, Any],
    asset_validation: dict[str, Any],
) -> dict[str, Any]:
    root = Path(project_dir).resolve(strict=True)
    scope = _release_scope(request)
    required_test_ids = FITNESS_TESTS_BY_SCOPE.get(scope, ())
    if not required_test_ids:
        return _blocked(scope, (), [f"unsupported fitness-for-use scope: {scope}"])
    report_path = root / "reports" / "task-fitness-evidence.json"
    if not report_path.is_file():
        return _blocked(
            scope,
            required_test_ids,
            ["task-specific fitness evidence has not been applied"],
        )
    try:
        report = _load_json_object(report_path)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        return _blocked(scope, required_test_ids, [f"task-specific fitness evidence is unreadable: {exc}"])
    schema_issues = validate_payload("task-fitness-evidence", report)
    reasons = [f"fitness evidence schema: {issue.render()}" for issue in schema_issues]
    expected_report_id = _task_fitness_report_id(report)
    if report.get("report_id") != expected_report_id:
        reasons.append("fitness evidence report_id is not derived from the canonical report content")
    profile = asset_validation.get("simready_profile", {})
    expected = {
        "scope": scope,
        "run_id": plan.run_id,
        "request_digest": plan.request_digest,
        "asset_fingerprint": asset_package_fingerprint(asset_package, asset_validation),
        "profile_id": str(profile.get("profile_id") or ""),
        "profile_version": str(profile.get("profile_version") or ""),
    }
    for field, value in expected.items():
        if str(report.get(field) or "") != str(value):
            reasons.append(f"fitness evidence {field} does not match the current run")

    protocol_binding = report.get("protocol") if isinstance(report.get("protocol"), dict) else {}
    protocol_path, protocol_path_error = _resolve_materialised_file(
        root,
        protocol_binding.get("path"),
        "task fitness protocol",
    )
    protocol: dict[str, Any] = {}
    if protocol_path is None:
        _append_reason(reasons, protocol_path_error)
    elif not _declared_sha256_matches(protocol_path, protocol_binding.get("sha256")):
        reasons.append("task fitness protocol checksum does not match the materialised protocol")
    else:
        try:
            protocol = _load_json_object(protocol_path)
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            reasons.append(f"task fitness protocol is unreadable: {exc}")
    if protocol:
        protocol_issues = validate_payload("task-fitness-protocol", protocol)
        reasons.extend(f"task fitness protocol schema: {issue.render()}" for issue in protocol_issues)
        if protocol.get("schema_version") != TASK_FITNESS_PROTOCOL_SCHEMA_VERSION:
            reasons.append("task fitness protocol schema_version is unsupported")
        for field in ("protocol_id", "protocol_version"):
            if str(protocol.get(field) or "") != str(protocol_binding.get(field) or ""):
                reasons.append(f"task fitness protocol {field} does not match its report binding")
        if protocol.get("scope") != scope:
            reasons.append("task fitness protocol scope does not match the current release scope")
        if protocol.get("status") != "approved":
            reasons.append("task fitness protocol is not approved")

    evidence_records = [item for item in report.get("evidence", []) if isinstance(item, dict)]
    evidence_by_id: dict[str, dict[str, Any]] = {}
    materialised_evidence: dict[str, Path] = {}
    for evidence in evidence_records:
        evidence_id = str(evidence.get("evidence_id") or "")
        if evidence_id in evidence_by_id:
            reasons.append(f"task fitness evidence ID is duplicated: {evidence_id or '<missing>'}")
            continue
        evidence_by_id[evidence_id] = evidence
        if evidence_id != _evidence_record_id(evidence):
            reasons.append(f"task fitness evidence ID is not content-derived: {evidence_id or '<missing>'}")
        evidence_path, evidence_path_error = _resolve_materialised_file(
            root,
            evidence.get("path"),
            f"task fitness evidence {evidence_id or '<missing>'}",
        )
        if evidence_path is None:
            _append_reason(reasons, evidence_path_error)
            continue
        if evidence_path == report_path or (protocol_path is not None and evidence_path == protocol_path):
            reasons.append(f"task fitness evidence must be independent of the report and protocol: {evidence_id}")
            continue
        if not _declared_sha256_matches(evidence_path, evidence.get("sha256")):
            reasons.append(f"task fitness evidence checksum does not match its materialised file: {evidence_id}")
            continue
        materialised_evidence[evidence_id] = evidence_path

    tests = [item for item in report.get("tests", []) if isinstance(item, dict)]
    tests_by_id = {str(item.get("test_id") or ""): item for item in tests}
    if len(tests_by_id) != len(tests):
        reasons.append("task fitness test IDs must be unique")
    protocol_tests = [item for item in protocol.get("tests", []) if isinstance(item, dict)]
    protocol_tests_by_id = {str(item.get("test_id") or ""): item for item in protocol_tests}
    if protocol and len(protocol_tests_by_id) != len(protocol_tests):
        reasons.append("task fitness protocol test IDs must be unique")
    if protocol and set(tests_by_id) != set(protocol_tests_by_id):
        reasons.append("task fitness report test coverage does not exactly match the approved protocol")
    for test_id in required_test_ids:
        if protocol and test_id not in protocol_tests_by_id:
            reasons.append(f"approved task fitness protocol omits the required test: {test_id}")
        test = tests_by_id.get(test_id)
        if test is None:
            reasons.append(f"required task fitness test is missing: {test_id}")
    for test_id, test in tests_by_id.items():
        protocol_test = protocol_tests_by_id.get(test_id)
        if protocol_test is None:
            reasons.append(f"task fitness test is not declared by the approved protocol: {test_id}")
            continue
        if test.get("scenario") != protocol_test.get("scenario"):
            reasons.append(f"task fitness scenario does not match the approved protocol: {test_id}")
        evidence_ids = [str(item) for item in test.get("evidence_ids", [])]
        if len(set(evidence_ids)) != len(evidence_ids):
            reasons.append(f"task fitness evidence references must be unique in {test_id}")
        for evidence_id in evidence_ids:
            if evidence_id not in evidence_by_id:
                reasons.append(f"task fitness evidence reference is unresolved in {test_id}: {evidence_id}")
            elif evidence_id not in materialised_evidence:
                reasons.append(f"task fitness evidence reference is not checksum-valid in {test_id}: {evidence_id}")

        metrics = [item for item in test.get("metric_results", []) if isinstance(item, dict)]
        metrics_by_id = {str(item.get("metric_id") or ""): item for item in metrics}
        protocol_metrics = [item for item in protocol_test.get("metrics", []) if isinstance(item, dict)]
        protocol_metrics_by_id = {str(item.get("metric_id") or ""): item for item in protocol_metrics}
        if len(metrics_by_id) != len(metrics):
            reasons.append(f"task fitness metric IDs must be unique in {test_id}")
        if len(protocol_metrics_by_id) != len(protocol_metrics):
            reasons.append(f"task fitness protocol metric IDs must be unique in {test_id}")
        if set(metrics_by_id) != set(protocol_metrics_by_id):
            reasons.append(f"task fitness metric coverage does not match the approved protocol in {test_id}")
        metrics_pass = True
        for metric_id, metric in metrics_by_id.items():
            protocol_metric = protocol_metrics_by_id.get(metric_id)
            if protocol_metric is None:
                metrics_pass = False
                continue
            criteria_fields = ("unit", "expected_min", "expected_max", "tolerance")
            if any(metric.get(field) != protocol_metric.get(field) for field in criteria_fields):
                reasons.append(f"task fitness metric criteria differ from the approved protocol: {test_id}/{metric_id}")
                metrics_pass = False
                continue
            values = [
                metric.get("value"),
                protocol_metric.get("expected_min"),
                protocol_metric.get("expected_max"),
                protocol_metric.get("tolerance"),
            ]
            if not all(_finite_number(value) for value in values):
                reasons.append(f"task fitness metric contains a non-finite number: {test_id}/{metric_id}")
                metrics_pass = False
                continue
            value, expected_min, expected_max, tolerance = [float(item) for item in values]
            if expected_min > expected_max or tolerance < 0.0:
                reasons.append(f"task fitness protocol metric range is invalid: {test_id}/{metric_id}")
                metrics_pass = False
                continue
            derived_status = "pass" if expected_min - tolerance <= value <= expected_max + tolerance else "blocked"
            if metric.get("status") != derived_status:
                reasons.append(f"task fitness metric status is inconsistent with the approved protocol: {test_id}/{metric_id}")
            if derived_status != "pass":
                reasons.append(f"task fitness metric did not meet the approved protocol: {test_id}/{metric_id}")
                metrics_pass = False
        derived_test_status = "pass" if metrics_pass and evidence_ids and all(
            evidence_id in materialised_evidence for evidence_id in evidence_ids
        ) else "blocked"
        if test.get("status") != derived_test_status:
            reasons.append(f"task fitness test status is inconsistent with its evidence: {test_id}")
        if derived_test_status != "pass":
            reasons.append(f"task fitness test did not pass: {test_id}")
    return {
        "status": "pass" if not reasons else "blocked",
        "scope": scope,
        "required_test_ids": list(required_test_ids),
        "tests": tests,
        "report_path": report_path.relative_to(root).as_posix(),
        "report_sha256": sha256_file(report_path),
        "bindings": expected,
        "protocol": protocol_binding,
        "materialised_evidence_count": len(materialised_evidence),
        "blocked_reasons": reasons,
    }


def build_task_fitness_template(project_dir: str | Path) -> dict[str, Any]:
    """Build a blocked evidence template bound to the current package and run."""

    root = Path(project_dir).resolve(strict=True)
    request = RunRequest.model_validate_json((root / "run-request.json").read_text(encoding="utf-8"))
    plan = RunPlan.model_validate_json((root / "run-plan.json").read_text(encoding="utf-8"))
    stage_report = json.loads((root / "reports" / "simready-verification-report.json").read_text(encoding="utf-8"))
    asset_validation_path = root / "reports" / "generated-asset-validation-report.json"
    asset_validation = json.loads(asset_validation_path.read_text(encoding="utf-8"))
    asset_validation["report_sha256"] = sha256_file(asset_validation_path)
    asset_package = stage_report.get("generated_asset") or {}
    scope = _release_scope(request)
    test_ids = FITNESS_TESTS_BY_SCOPE.get(scope)
    if not test_ids:
        raise ValueError(f"unsupported fitness-for-use scope: {scope}")
    profile = asset_validation.get("simready_profile") or {}
    profile_id = str(profile.get("profile_id") or "")
    profile_version = str(profile.get("profile_version") or "")
    if not profile_id or not profile_version:
        raise ValueError("task-fitness template requires an exact Profile ID and version")
    bindings = {
        "scope": scope,
        "run_id": plan.run_id,
        "request_digest": plan.request_digest,
        "asset_fingerprint": asset_package_fingerprint(asset_package, asset_validation),
        "profile_id": profile_id,
        "profile_version": profile_version,
    }
    template_evidence = {
        "kind": "measurement_bundle",
        "path": "reports/fitness-measurements.json",
        "sha256": "sha256:" + ("0" * 64),
    }
    template_evidence_id = _evidence_record_id(template_evidence)
    tests = [
        {
            "test_id": test_id,
            "status": "not_run",
            "scenario": "replace with the controlled task scenario and acceptance protocol",
            "metric_results": [
                {
                    "metric_id": "replace_with_measured_metric",
                    "value": 0,
                    "unit": "replace_with_unit",
                    "expected_min": 0,
                    "expected_max": 0,
                    "tolerance": 0,
                    "status": "blocked",
                }
            ],
            "evidence_ids": [template_evidence_id],
        }
        for test_id in test_ids
    ]
    core = {
        "format_version": TASK_FITNESS_FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        **bindings,
        "protocol": {
            "protocol_id": "replace_with_approved_protocol",
            "protocol_version": "1.0.0",
            "path": "reports/task-fitness-protocol.json",
            "sha256": "sha256:" + ("0" * 64),
        },
        "evidence": [{"evidence_id": template_evidence_id, **template_evidence}],
        "tests": tests,
        "extensions": {
            "template": True,
            "instructions": (
                "bind an approved task-fitness protocol, replace unmeasured fields with measured evidence and recompute "
                "every path checksum, evidence ID and report ID; a not_run or blocked result cannot release"
            ),
        },
    }
    payload = {"report_id": content_id("task_fitness", core, digest_length=32), **core}
    issues = validate_payload("task-fitness-evidence", payload)
    if issues:
        raise RuntimeError("generated task-fitness template is invalid: " + "; ".join(issue.render() for issue in issues))
    return payload


def write_task_fitness_template(project_dir: str | Path, output: str | Path) -> dict[str, Any]:
    payload = build_task_fitness_template(project_dir)
    path = atomic_write_json(output, payload)
    return {"status": "blocked", "template": payload, "output": str(path)}


def apply_task_fitness_report(project_dir: str | Path, report: str | Path) -> dict[str, Any]:
    """Validate and copy an externally produced task-fitness report into a project."""

    root = Path(project_dir).resolve(strict=True)
    supplied_source = Path(report)
    if supplied_source.is_symlink():
        raise ValueError("task fitness report must not be a symbolic link")
    source = supplied_source.resolve(strict=True)
    if not source.is_file():
        raise ValueError("task fitness report must be a regular file")
    payload = _load_json_object(source)
    issues = validate_payload("task-fitness-evidence", payload)
    if issues:
        raise ValueError("task fitness report is invalid: " + "; ".join(issue.render() for issue in issues))
    if payload.get("report_id") != _task_fitness_report_id(payload):
        raise ValueError("task fitness report_id is not derived from the canonical report content")
    target = root / "reports" / "task-fitness-evidence.json"
    atomic_write_json(target, payload)
    request = RunRequest.model_validate_json((root / "run-request.json").read_text(encoding="utf-8"))
    plan = RunPlan.model_validate_json((root / "run-plan.json").read_text(encoding="utf-8"))
    stage_report_path = root / "reports" / "simready-verification-report.json"
    validation_path = root / "reports" / "generated-asset-validation-report.json"
    if not stage_report_path.is_file() or not validation_path.is_file():
        return {
            "status": "blocked",
            "report_path": target.relative_to(root).as_posix(),
            "blocked_reasons": ["current asset package or validation evidence is unavailable"],
        }
    stage_report = json.loads(stage_report_path.read_text(encoding="utf-8"))
    asset_package = stage_report.get("generated_asset") or {}
    asset_validation = json.loads(validation_path.read_text(encoding="utf-8"))
    asset_validation["report_sha256"] = sha256_file(validation_path)
    result = evaluate_task_fitness(root, request, plan, asset_package, asset_validation)
    result["report_path"] = target.relative_to(root).as_posix()
    return result
