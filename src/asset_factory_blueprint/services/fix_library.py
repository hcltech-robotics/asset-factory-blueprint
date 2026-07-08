from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asset_factory_blueprint.config import load_json
from asset_factory_blueprint.skills.base import ToolResult


LIBRARY_PATH = "configs/fix-library.json"
MESH_SUFFIXES = {".glb", ".obj", ".ply", ".stl"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def load_fix_library(library_path: str = LIBRARY_PATH) -> dict[str, Any]:
    return load_json(library_path)


def resolve_fixes(stage_id: str, defect_tags: list[str], library_path: str = LIBRARY_PATH) -> list[dict[str, Any]]:
    library = load_fix_library(library_path)
    wanted = set(defect_tags)
    matches: list[dict[str, Any]] = []
    for fix in library.get("fixes", []):
        if fix.get("stage") != stage_id:
            continue
        covered = wanted.intersection(fix.get("defect_tags", []))
        if covered:
            entry = dict(fix)
            entry["matched_defect_tags"] = sorted(covered)
            matches.append(entry)
    return matches


def _project_context(project_dir: Path) -> dict[str, Any]:
    """Resolve the workspace facts fix recipes need: asset id, meshes, source image, manifests."""
    context: dict[str, Any] = {"asset_id": "", "source_image": "", "meshes": [], "asset_dir": ""}
    request_path = project_dir / "run-request.json"
    if request_path.exists():
        try:
            context["asset_id"] = json.loads(request_path.read_text(encoding="utf-8")).get("id", "")
        except json.JSONDecodeError:
            pass
    asset_dir = project_dir / "assets" / context["asset_id"] if context["asset_id"] else None
    if asset_dir and asset_dir.exists():
        context["asset_dir"] = asset_dir.as_posix()
    source_root = project_dir / "source-assets"
    if source_root.exists():
        for path in sorted(source_root.rglob("*")):
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                context["source_image"] = path.as_posix()
                break
    for pattern_root in (project_dir / "assets", project_dir / "artifacts"):
        if pattern_root.exists():
            for path in sorted(pattern_root.rglob("*")):
                if path.is_file() and path.suffix.lower() in MESH_SUFFIXES:
                    context["meshes"].append(path.as_posix())
    manifests = project_dir / "manifests"
    for name in ("material-inference-manifest", "physics-articulation-manifest", "segmentation-manifest"):
        path = manifests / f"{name}.json"
        if path.exists():
            try:
                context[name.replace("-", "_")] = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
    return context


def _selector_from_findings(findings: list[dict[str, Any]]) -> dict[str, str] | None:
    for finding in findings:
        region = str(finding.get("region") or "").strip()
        if region:
            if region.startswith("/"):
                return {"prim_path": region}
            return {"segment_id": re.sub(r"[^a-z0-9_]+", "_", region.lower()).strip("_")}
    return None


def _resolve_tool_params(
    fix: dict[str, Any],
    project_dir: Path,
    context: dict[str, Any],
    findings: list[dict[str, Any]],
    dry_run: bool,
) -> tuple[dict[str, Any] | None, str]:
    """Build a real parameter set for the recipe's tool, or explain why it cannot run."""
    tool = fix.get("action", {}).get("tool", "")
    template = dict(fix.get("action", {}).get("params_template", {}))
    fix_dir = project_dir / "reports" / "fixes" / fix["fix_id"]
    asset_id = context["asset_id"] or "asset"

    if tool == "asset_mesh_condition":
        if not context["meshes"]:
            return None, "no mesh artefacts exist in the workspace to condition"
        operations = [dict(item) for item in template.get("operations", [])]
        selector = _selector_from_findings(findings)
        for operation in operations:
            if operation.get("operation") in {"smooth", "dent", "bump"}:
                if selector is None:
                    return None, "shape operations need a segment or prim selector from the finding region"
                operation.setdefault("selector", selector)
        return {
            "asset_id": asset_id,
            "base_dir": project_dir.as_posix(),
            "source_meshes": context["meshes"][:8],
            "operations": operations,
            "output_dir": (fix_dir / "meshes").as_posix(),
            "report_path": (fix_dir / "mesh-condition-report.json").as_posix(),
            "manifest_path": (fix_dir / "mesh-condition-manifest.json").as_posix(),
            "checksums_path": (fix_dir / "mesh-condition-checksums.json").as_posix(),
        }, ""

    if tool == "asset_image_segmentation_prior":
        if not context["source_image"]:
            return None, "no source image exists in the workspace to segment"
        expected = [
            {"label": str(finding.get("region") or finding.get("defect_tag") or "region")}
            for finding in findings
            if finding.get("region")
        ]
        params: dict[str, Any] = {
            "asset_id": asset_id,
            "image_path": context["source_image"],
            "output_dir": (fix_dir / "segments").as_posix(),
            "method": str(template.get("method") or "auto"),
        }
        if expected:
            params["expected_segments"] = expected
            params["max_segments"] = len(expected)
        return params, ""

    if tool == "material_texture_variation_workflow":
        if not context["source_image"]:
            return None, "no source image exists to drive texture regeneration"
        negative_cues = sorted({finding.get("defect_tag", "") for finding in findings if finding.get("defect_tag")})
        return {
            "image_path": context["source_image"],
            "asset_id": asset_id,
            "texture_variants": template.get("texture_variants", []),
            "appearance_segments": [],
            "reinforced_negative_cues": negative_cues,
            "output": (fix_dir / "texture-workflow.json").as_posix(),
        }, ""

    if tool == "material_texture_prompt":
        material_manifest = project_dir / "manifests" / "material-inference-manifest.json"
        if not material_manifest.exists():
            return None, "no material manifest exists to rebuild texture prompts from"
        return {
            "material_manifest": material_manifest.as_posix(),
            "property_manifest": material_manifest.as_posix(),
            "output": (fix_dir / "texture-prompt.json").as_posix(),
        }, ""

    if tool == "material_propose":
        components = [
            {"prim_path": str(finding.get("region") or f"/{asset_id}"), "label": finding.get("defect_tag", "component")}
            for finding in findings
        ] or [{"prim_path": f"/{asset_id}", "label": "asset_root"}]
        return {
            "components": components,
            "evidence_ids": [f"fix_finding_{index}" for index in range(len(findings))],
        }, ""

    if tool == "articulation_plan":
        manifest = context.get("physics_articulation_manifest", {})
        return {
            "joints": manifest.get("joints", []),
            "part_graph": manifest.get("part_graph", []),
            "grasp_points": manifest.get("affordances", {}).get("grasp_points", []),
            "affordance_labels": manifest.get("affordances", {}).get("affordance_labels", []),
        }, ""

    if tool == "physics_nonvisual_materials_propose":
        material_manifest = context.get("material_inference_manifest", {})
        components = material_manifest.get("component_materials", [])
        material_class = components[0].get("selected_material", "") if components else ""
        return {"material_class": material_class, "evidence_ids": ["fix_review"]}, ""

    return None, f"no parameter resolver exists for tool {tool}"


def _run_reverify(fix: dict[str, Any], project_dir: Path) -> list[dict[str, str]]:
    """Execute the recipe's re-verification list against the refreshed workspace."""
    from asset_factory_blueprint.manifests import validate_manifest
    from asset_factory_blueprint.workflow import STAGE_SCHEMA

    outcomes: list[dict[str, str]] = []
    stage_id = fix.get("stage", "")
    schema_name = STAGE_SCHEMA.get(stage_id)
    for gate in fix.get("reverify", []):
        if gate == "schema-valid" and schema_name:
            manifest_path = project_dir / "manifests" / f"{schema_name}.json"
            if manifest_path.exists():
                errors = validate_manifest(schema_name, manifest_path)
                outcomes.append({"gate": gate, "status": "pass" if not errors else "blocked"})
            else:
                outcomes.append({"gate": gate, "status": "blocked"})
        elif gate == "vlm-signoff":
            outcomes.append({"gate": gate, "status": "rechecked_by_next_review"})
        else:
            report_path = project_dir / "reports" / f"{stage_id}-report.json"
            status = "pending"
            if report_path.exists():
                try:
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    status = "pass" if not report.get("blocked_reasons") else "blocked"
                except json.JSONDecodeError:
                    status = "unreadable"
            outcomes.append({"gate": gate, "status": status})
    return outcomes


def _capability_fallback_plan(fix: dict[str, Any], dry_run: bool) -> dict[str, Any]:
    from asset_factory_blueprint.services.capability import probe_capabilities

    capability_id = fix.get("action", {}).get("capability_id", "")
    report = probe_capabilities()
    capability = next((item for item in report["capabilities"] if item["capability_id"] == capability_id), None)
    if capability is None:
        return {"status": "blocked", "error": f"unknown capability: {capability_id}"}
    ready = [item["option_id"] for item in capability["options"] if item["status"] == "ready"]
    if not ready:
        return {"status": "blocked", "error": f"no ready option for {capability_id}", "options": capability["options"]}
    chosen = ready[0]
    plan: dict[str, Any] = {"status": "planned" if dry_run else "requested", "capability_id": capability_id, "fallback_option": chosen}
    if not dry_run:
        try:
            from asset_factory_blueprint.reconstruction_backends import build_backend_run_manifest

            manifest = build_backend_run_manifest(chosen)
            plan["run_manifest"] = manifest.get("manifest_path", "")
            plan["note"] = "backend run manifest created; execute it with afb external-models run"
        except Exception as exc:
            plan["status"] = "blocked"
            plan["error"] = str(exc)
    return plan


def _append_attempt_log(project_dir: Path, record: dict[str, Any]) -> Path:
    reports_dir = project_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    target = reports_dir / "fix-attempts.json"
    attempts: list[dict[str, Any]] = []
    if target.exists():
        try:
            attempts = json.loads(target.read_text(encoding="utf-8")).get("attempts", [])
        except json.JSONDecodeError:
            attempts = []
    attempts.append(record)
    target.write_text(json.dumps({"attempts": attempts}, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return target


def asset_fix_apply(params: dict[str, Any]) -> ToolResult:
    stage_id = str(params.get("stage_id") or "")
    defect_tags = [str(item) for item in params.get("defect_tags") or []]
    findings = [item for item in params.get("findings") or [] if isinstance(item, dict)]
    library_path = str(params.get("library_path") or LIBRARY_PATH)
    dry_run = bool(params.get("dry_run", True))
    attempt = int(params.get("attempt") or 0)
    if not stage_id or not defect_tags:
        return ToolResult(success=False, error="stage_id and defect_tags are required", validation_status="blocked")

    fixes = resolve_fixes(stage_id, defect_tags, library_path)
    if not fixes:
        return ToolResult(
            success=False,
            data={"stage_id": stage_id, "defect_tags": defect_tags, "resolved_fixes": []},
            error="no fix recipe covers these defect tags; escalation to operator review is required",
            validation_status="review_required",
        )

    project_raw = params.get("project")
    project_dir = Path(str(project_raw)) if project_raw else None
    context = _project_context(project_dir) if project_dir and project_dir.exists() else {"asset_id": "", "source_image": "", "meshes": [], "asset_dir": ""}

    applied: list[dict[str, Any]] = []
    escalations: list[str] = []
    warnings: list[str] = []
    artefacts_changed = False

    for fix in fixes:
        action = fix.get("action", {})
        kind = action.get("kind", "escalate")
        record: dict[str, Any] = {
            "fix_id": fix["fix_id"],
            "stage_id": stage_id,
            "matched_defect_tags": fix["matched_defect_tags"],
            "action_kind": kind,
            "attempt": attempt,
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "dry_run": dry_run,
            "status": "planned",
            "reverify": fix.get("reverify", []),
            "escalation": fix.get("escalation", "review_required"),
        }
        max_attempts = int(fix.get("max_attempts", 0))
        if kind == "escalate" or max_attempts == 0:
            record["status"] = "escalated"
            record["note"] = action.get("note", "")
            escalations.append(fix["fix_id"])
        elif attempt >= max_attempts:
            record["status"] = "attempts_exhausted"
            escalations.append(fix["fix_id"])
        elif kind == "tool":
            if project_dir is None or not project_dir.exists():
                record["status"] = "blocked"
                record["error"] = "tool fixes need a project workspace"
                escalations.append(fix["fix_id"])
            else:
                resolved, reason = _resolve_tool_params(fix, project_dir, context, findings, dry_run)
                if resolved is None:
                    record["status"] = "not_applicable"
                    record["error"] = reason
                    warnings.append(f"{fix['fix_id']}: {reason}")
                    escalations.append(fix["fix_id"])
                elif dry_run:
                    record["status"] = "planned_dry_run"
                    record["tool"] = action.get("tool", "")
                    record["params"] = resolved
                else:
                    from asset_factory_blueprint.tool_router import route_tool

                    result = route_tool(action.get("tool", ""), resolved)
                    record["tool"] = action.get("tool", "")
                    record["params"] = resolved
                    record["tool_validation_status"] = result.validation_status
                    if result.success or result.validation_status in {"proposal", "review_required"}:
                        record["status"] = "applied"
                        artefacts_changed = True
                    else:
                        record["status"] = "attempt_failed"
                    if result.error:
                        record["error"] = result.error
                        warnings.append(f"{fix['fix_id']}: {result.error}")
        elif kind == "capability_fallback":
            plan = _capability_fallback_plan(fix, dry_run)
            record.update(plan)
            if plan["status"] == "blocked":
                escalations.append(fix["fix_id"])
                warnings.append(f"{fix['fix_id']}: {plan.get('error', 'capability fallback blocked')}")
        elif kind == "repackage":
            if project_dir is None or not project_dir.exists():
                record["status"] = "blocked"
                record["error"] = "repackage needs a project workspace"
                escalations.append(fix["fix_id"])
            elif dry_run:
                record["status"] = "planned_dry_run"
            else:
                from asset_factory_blueprint.workflow import rebuild_project_artefacts

                rebuild = rebuild_project_artefacts(project_dir, dry_run=False)
                record["status"] = "applied"
                record["rebuild_status"] = rebuild["status"]
                artefacts_changed = True
        applied.append(record)

    if artefacts_changed and project_dir is not None:
        from asset_factory_blueprint.workflow import rebuild_project_artefacts

        rebuild = rebuild_project_artefacts(project_dir, dry_run=dry_run)
        for record in applied:
            if record["status"] == "applied":
                record["workspace_refreshed"] = True
                record["reverify_outcomes"] = _run_reverify(
                    next(fix for fix in fixes if fix["fix_id"] == record["fix_id"]), project_dir
                )
        warnings.extend([] if rebuild["status"] != "blocked" else ["workspace rebuild reports blocked stages; see stage reports"])

    if project_dir is not None and project_dir.exists():
        for record in applied:
            _append_attempt_log(project_dir, record)

    any_applied = any(item["status"] in {"applied", "requested", "planned_dry_run", "planned"} for item in applied)
    data = {
        "stage_id": stage_id,
        "defect_tags": defect_tags,
        "resolved_fixes": [item["fix_id"] for item in fixes],
        "attempts": applied,
        "escalated": escalations,
        "artefacts_refreshed": artefacts_changed,
    }
    return ToolResult(
        success=any_applied and not escalations,
        data=data,
        warnings=warnings,
        proposals=applied,
        validation_status="proposal" if any_applied and not escalations else "review_required",
    )
