from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from asset_factory_blueprint.execution import atomic_write_json
from asset_factory_blueprint.services.governance import evaluate_release_policy
from asset_factory_blueprint.validation import build_project_checksum_inventory, validate_project_graph


def _status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def _project_payload(project: str | Path) -> dict[str, Any]:
    project_dir = Path(project)
    project_manifest = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    run_plan_path = project_dir / project_manifest.get("run_plan", "run-plan.json")
    run_plan = json.loads(run_plan_path.read_text(encoding="utf-8"))
    reports = []
    for path in sorted((project_dir / "reports").glob("*-report.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        if "stage_id" not in report:
            continue
        report["report_path"] = path.relative_to(project_dir).as_posix()
        reports.append(report)
    manifests = {}
    for path in sorted((project_dir / "manifests").glob("*.json")):
        manifests[path.name] = json.loads(path.read_text(encoding="utf-8"))
    asset_validation_path = project_dir / "reports" / "generated-asset-validation-report.json"
    asset_validation = json.loads(asset_validation_path.read_text(encoding="utf-8")) if asset_validation_path.exists() else None
    checksums_path = project_dir / project_manifest.get("checksum_manifest", "evidence/checksums.json")
    checksums = json.loads(checksums_path.read_text(encoding="utf-8")) if checksums_path.exists() else {"files": []}
    graph_validation = validate_project_graph(project_dir)
    return {
        "project": project_manifest,
        "run_plan": run_plan,
        "reports": reports,
        "manifests": manifests,
        "asset_validation": asset_validation,
        "checksums": checksums,
        "graph_validation": graph_validation,
    }


def _asset_payload(asset_manifest: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(asset_manifest).read_text(encoding="utf-8"))
    return {"asset_manifest": payload}


def _refresh_project_checksums(project_dir: Path) -> None:
    checksums_path = project_dir / "evidence" / "checksums.json"
    atomic_write_json(checksums_path, build_project_checksum_inventory(project_dir))


def _project_status(
    blocked_reports: list[dict[str, Any]],
    gate_counts: dict[str, int],
    asset_validation: dict[str, Any],
    governance: dict[str, Any],
    simready: dict[str, Any],
    reports: list[dict[str, Any]],
    release_decision: dict[str, Any],
) -> str:
    if blocked_reports or gate_counts.get("blocked") or governance.get("release_status") == "blocked":
        return "blocked"
    if governance.get("release_allowed") is True and not release_decision["release_allowed"]:
        return "blocked"
    if release_decision["release_allowed"]:
        return "released"
    if asset_validation.get("status") == "validated" or simready.get("status") == "validated":
        return "validated"
    return "proposal" if reports else "not_validated"


def _project_release_decision(
    governance: dict[str, Any],
    gate_results: list[dict[str, Any]],
    reports: list[dict[str, Any]],
    asset_validation: dict[str, Any],
    run_plan: dict[str, Any],
) -> dict[str, Any]:
    recorded_decisions = governance.get("release_decisions") or []
    first_decision = recorded_decisions[0] if recorded_decisions and isinstance(recorded_decisions[0], dict) else {}
    recorded_scope = first_decision.get("scope")
    scope = str(governance.get("task_scope") or recorded_scope or "visualisation")
    required_stage_ids: list[str] = []
    for stage in run_plan.get("stages", []):
        stage_id = str(stage.get("id") or "")
        if stage_id == "governance":
            break
        if stage_id:
            required_stage_ids.append(stage_id)
    report_by_stage = {str(report.get("stage_id") or ""): report for report in reports}
    return evaluate_release_policy(
        governance,
        scope,
        gate_results=gate_results,
        stage_reports=[report_by_stage[stage_id] for stage_id in required_stage_ids if stage_id in report_by_stage],
        required_stage_ids=required_stage_ids,
        asset_validation_status=asset_validation.get("status") or "not_validated",
    )


def render_readiness(payload: dict[str, Any]) -> str:
    if "project" in payload:
        project = payload["project"]
        run_plan = payload["run_plan"]
        reports = payload["reports"]
        blocked_reports = [
            report for report in reports if report.get("blocked_reasons") or report.get("manifest_errors") or report.get("status") == "blocked"
        ]
        stage_counts = _status_counts(reports)
        manifests = payload.get("manifests", {})
        simready = manifests.get("simready-asset-manifest.json", {})
        governance = manifests.get("governance-record.json", {})
        asset_validation = payload.get("asset_validation") or {}
        graph_validation = payload.get("graph_validation") or {"status": "blocked", "findings": []}
        gate_results = [
            *simready.get("validation_results", []),
            {
                "gate_id": "record-graph",
                "status": graph_validation.get("status", "blocked"),
                "evidence_path": "reports/project-graph-validation.json",
            },
        ]
        gate_counts = _status_counts(gate_results)
        release_decision = _project_release_decision(governance, gate_results, reports, asset_validation, run_plan)
        project_status = _project_status(
            blocked_reports,
            gate_counts,
            asset_validation,
            governance,
            simready,
            reports,
            release_decision,
        )
        lines = [
            "# Readiness report",
            "",
            f"Project: {project.get('project_id')}",
            f"Run: {run_plan.get('run_id') or run_plan.get('id')}",
            f"Status: {project_status}",
            f"Stages: {len(reports)}",
            f"Stage status counts: {json.dumps(stage_counts, sort_keys=True)}",
            f"Gate status counts: {json.dumps(gate_counts, sort_keys=True)}",
            f"Generated artefact validation: {asset_validation.get('status', 'not_validated')}",
            f"Governance release status: {governance.get('release_status', 'not_recorded')}",
            f"Effective release scope: {release_decision['scope']}",
            f"Effective release allowed: {release_decision['release_allowed']}",
            f"Checksum records: {len(payload['checksums'].get('files', []))}",
            f"Record graph validation: {graph_validation.get('status', 'blocked')}",
            "",
            "## Gates",
        ]
        if gate_results:
            for item in gate_results:
                lines.append(f"- {item.get('gate_id', 'gate')}: {item.get('status', 'unknown')} ({item.get('evidence_path', '')})")
        else:
            for gate in run_plan.get("validation_gates", []):
                lines.append(f"- {gate}: pending")
        lines.extend(["", "## Promotion states"])
        lines.append(f"- proposal: {bool(reports)}")
        lines.append(f"- generated artefacts validated: {asset_validation.get('status') == 'validated'}")
        lines.append(f"- blocked gates: {gate_counts.get('blocked', 0)}")
        lines.append(f"- release claimed by record: {governance.get('release_allowed') is True}")
        lines.append(f"- released: {release_decision['release_allowed']}")
        if release_decision["blockers"]:
            lines.extend(["", "## Release policy blockers"])
            for blocker in release_decision["blockers"]:
                lines.append(f"- {blocker}")
        graph_findings = [item for item in graph_validation.get("findings", []) if item.get("severity") == "error"]
        if graph_findings:
            lines.extend(["", "## Record graph findings"])
            for item in graph_findings:
                lines.append(f"- {item.get('code')}: {item.get('message')}")
        lines.extend(["", "## Stage reports"])
        for report in reports:
            manifest_state = "valid" if not report.get("manifest_errors") else "invalid"
            lines.append(f"- {report.get('stage_id')}: {report.get('status')} manifest {manifest_state}")
        if blocked_reports:
            lines.extend(["", "## Blocked items"])
            for report in blocked_reports:
                reasons = report.get("blocked_reasons") or report.get("manifest_errors") or ["blocked"]
                lines.append(f"- {report.get('stage_id')}: {'; '.join(str(item) for item in reasons)}")
        return "\n".join(lines) + "\n"

    asset = payload["asset_manifest"]
    validation_results = asset.get("validation_results", [])
    lines = [
        "# Readiness report",
        "",
        f"Asset: {asset.get('asset_id', asset.get('id', 'unknown'))}",
        f"Status: {asset.get('status', asset.get('promotion_status', 'proposal'))}",
        f"Validation results: {len(validation_results)}",
        "",
        "## Gates",
    ]
    for item in validation_results:
        lines.append(f"- {item.get('gate_id', 'gate')}: {item.get('status', 'unknown')}")
    if not validation_results:
        lines.append("- validation evidence required before promotion")
    lines.append("")
    lines.append("Promotion requires schema, source, material, physics, Isaac and governance gates.")
    return "\n".join(lines) + "\n"


def write_readiness(asset_manifest: str | Path | None, output: str | Path, project: str | Path | None = None) -> str:
    if project:
        payload = _project_payload(project)
    elif asset_manifest:
        payload = _asset_payload(asset_manifest)
    else:
        raise RuntimeError("readiness requires --asset-manifest or --project")
    text = render_readiness(payload)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    if project:
        _refresh_project_checksums(Path(project))
    return text
