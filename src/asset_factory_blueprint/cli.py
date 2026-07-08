from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from asset_factory_blueprint import __version__
from asset_factory_blueprint.agent_loop import run_agent_loop
from asset_factory_blueprint.capsule import (
    CapsuleCreationError,
    create_reference_capsule,
    validate_reference_capsule,
)
from asset_factory_blueprint.config import ROOT, source_appliance_status
from asset_factory_blueprint.dispatcher import list_tools
from asset_factory_blueprint.external_models import list_models, run_manifest, validate_config
from asset_factory_blueprint.governance_decisions import (
    build_operator_release_decision,
    write_operator_release_decision,
)
from asset_factory_blueprint.isaac_load import apply_isaac_load_report
from asset_factory_blueprint.manifests import list_schemas, skeleton, validate_manifest
from asset_factory_blueprint.orchestrator import write_run_plan
from asset_factory_blueprint.physics_evidence import seal_physics_evidence_file
from asset_factory_blueprint.providers import check_policy, complete_chat, completion_as_dict, statuses_as_dict
from asset_factory_blueprint.readiness import write_readiness
from asset_factory_blueprint.release_evidence import write_release_evidence
from asset_factory_blueprint.reconstruction_backends import build_backend_run_manifest, list_backend_specs, provision_backend
from asset_factory_blueprint.reconstruction_installers import (
    check_backend_install,
    default_install_root,
    install_backend,
    write_backend_install_report,
)
from asset_factory_blueprint.semantic_migration import migrate_legacy_semantics
from asset_factory_blueprint.service_approval import issue_approval_token, params_digest
from asset_factory_blueprint.skill_audit import audit, write_audit
from asset_factory_blueprint.state import create_project, list_projects, open_project, snapshot_project
from asset_factory_blueprint.library_tui import run_shop
from asset_factory_blueprint.services.capability import install_capability, probe_capabilities
from asset_factory_blueprint.services.fitness import apply_task_fitness_report, write_task_fitness_template
from asset_factory_blueprint.services.library import build_local_index, fetch_from_source, list_backings, search_library, search_remote_sources, usd_search_query
from asset_factory_blueprint.services.official_validator import (
    OmniAssetValidatorConfig,
    run_official_profile_validation,
    write_official_profile_report,
)
from asset_factory_blueprint.services.progress import write_progress_artefacts
from asset_factory_blueprint.services.texturing import material_texture_variation_workflow
from asset_factory_blueprint.texture_defaults import build_prompt, explain, list_profiles
from asset_factory_blueprint.tool_server import (
    ToolServerConfig,
    serve_http,
    serve_stdio,
    token_from_environment,
    tool_requires_approval,
)
from asset_factory_blueprint.validation import validate_json_file, validate_project_graph
from asset_factory_blueprint.wandb_logging import write_wandb_plan
from asset_factory_blueprint.workflow import run_workflow, summarize_run


def emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=False))


def _read_config(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _host_from_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    return parsed.netloc or parsed.path.split("/", 1)[0]


def _module_present(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _path_status(raw: str | None) -> str:
    if not raw:
        return "not_configured"
    return "ready" if Path(raw).exists() else "missing"


def cmd_format(args: argparse.Namespace) -> int:
    missing = [path for path in args.paths if not Path(path).exists()]
    if missing:
        emit({"ok": False, "missing": missing})
        return 1
    bad = []
    for raw in args.paths:
        path = Path(raw)
        files = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()]
        for item in files:
            if any(part in {".git", ".pytest_cache", "__pycache__", ".venv", ".cache"} for part in item.parts):
                continue
            if item.suffix in {".md", ".py", ".json", ".toml", ".yaml", ".yml"}:
                text = item.read_text(encoding="utf-8")
                if text and not text.endswith("\n"):
                    bad.append(str(item))
    emit({"ok": not bad, "files_missing_final_newline": bad})
    return 1 if args.check and bad else 0


def cmd_info(_: argparse.Namespace) -> int:
    runtime = _read_config("configs/runtime-config.example.json")
    provider_policy = _read_config(runtime.get("provider_policy", "configs/provider-policy.json"))
    skill_registry = _read_config(runtime.get("skill_registry", "configs/skill-registry.json"))
    runtime_paths = runtime.get("runtime", {})
    isaac_sim_root = os.environ.get("AFB_ISAAC_SIM_ROOT") or runtime_paths.get("isaac_sim_root", "")
    isaac_lab_root = os.environ.get("AFB_ISAAC_LAB_ROOT") or runtime_paths.get("isaac_lab_root", "")
    providers = []
    for name, provider in provider_policy["providers"].items():
        base_url = os.environ.get(provider.get("base_url_env", ""), provider.get("default_base_url", ""))
        key_env = provider.get("api_key_env", "")
        providers.append(
            {
                "name": name,
                "kind": provider.get("kind"),
                "api_key_env": key_env,
                "api_key_present": bool(key_env and os.environ.get(key_env)),
                "base_url_host": _host_from_url(base_url),
                "model_env": provider.get("model_env", ""),
                "model_present": bool(os.environ.get(provider.get("model_env", ""))),
            }
        )
    emit(
        {
            "name": "asset-factory-blueprint",
            "version": __version__,
            "cli": "afb",
            "mode": os.environ.get("AFB_ENV", "local"),
            "source_appliance": source_appliance_status(),
            "project_roots": {
                "project_root": runtime.get("project_root", "projects"),
                "artifact_root": runtime.get("artifact_root", "artifacts"),
                "cache_root": runtime.get("cache_root", ".cache/afb"),
            },
            "provider_lanes": providers,
            "skill_registry": {
                "path": runtime.get("skill_registry", "configs/skill-registry.json"),
                "skill_count": len(skill_registry.get("skills", [])),
                "status": "ready",
            },
            "runtime_dependencies": {
                "python": sys.version.split()[0],
                "jsonschema": "ready" if _module_present("jsonschema") else "missing",
                "pydantic": "ready" if _module_present("pydantic") else "missing",
                "pxr": "ready" if _module_present("pxr") else "runtime_missing",
                "isaac_sim_root": _path_status(isaac_sim_root),
                "isaac_lab_root": _path_status(isaac_lab_root),
            },
        }
    )
    return 0


def cmd_project(args: argparse.Namespace) -> int:
    if args.project_command == "new":
        emit(create_project(args.name, args.project_root))
    elif args.project_command == "open":
        emit(open_project(args.slug, args.project_root))
    elif args.project_command == "list":
        emit({"projects": list_projects(args.project_root)})
    elif args.project_command == "snapshot":
        emit(snapshot_project(args.slug, args.name, args.project_root))
    elif args.project_command == "validate":
        result = validate_project_graph(args.project)
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        emit(result)
        return 0 if result["status"] == "pass" else 1
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    if args.schema_command == "list":
        emit({"schemas": list_schemas()})
        return 0
    payload = skeleton(args.schema_name)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    emit(payload)
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    errors = validate_manifest(args.schema_name, args.manifest)
    emit({"ok": not errors, "errors": errors})
    return 0 if not errors else 1


def cmd_validate_file(args: argparse.Namespace) -> int:
    errors = validate_json_file(args.path, args.mode, args.project)
    emit({"ok": not errors, "errors": errors})
    return 0 if not errors else 1


def cmd_run_plan(args: argparse.Namespace) -> int:
    plan = write_run_plan(args.request, args.output)
    emit(plan.model_dump())
    return 0


def cmd_workflow(args: argparse.Namespace) -> int:
    if args.workflow_command == "run":
        try:
            result = run_workflow(
                request_path=args.request,
                project_root=args.project_root,
                project_name=args.project_name,
                dry_run=not args.live,
                run_plan_output=args.output,
            )
        except Exception as exc:
            emit({"ok": False, "status": "blocked", "error": str(exc)})
            return 1
        emit(result)
    else:
        reports = Path(args.reports)
        reports.mkdir(parents=True, exist_ok=True)
        summary = summarize_run(args.run_plan, args.reports, reports / "workflow-summary.json")
        emit(summary)
    return 0


def cmd_texture(args: argparse.Namespace) -> int:
    if args.texture_command == "defaults":
        if args.defaults_command == "list":
            emit({"profiles": list_profiles()})
        elif args.defaults_command == "explain":
            emit(explain(args.material, args.profile))
        else:
            errors = validate_manifest("texturing-manifest", args.texture_manifest)
            property_errors = validate_manifest("material-inference-manifest", args.property_manifest)
            emit({"ok": not errors and not property_errors, "errors": errors + property_errors})
            return 0 if not errors and not property_errors else 1
    elif args.texture_command == "prompt":
        emit(build_prompt(args.material_manifest, args.property_manifest, args.output))
    elif args.texture_command == "variation-workflow":
        result = material_texture_variation_workflow(
            {
                "image_path": args.image,
                "asset_id": args.asset_id,
                "texture_variants": args.texture_variant,
                "mesh_deformations": args.mesh_deformation,
                "appearance_segments": args.appearance_segment,
                "output": args.output,
            }
        )
        emit(result.data)
        return 0 if result.success else 1
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    from asset_factory_blueprint.services.programme import asset_factory_start, asset_programme_intake

    if args.agent_command == "intake":
        try:
            draft = json.loads(Path(args.draft).read_text(encoding="utf-8"))
            if not isinstance(draft, dict):
                raise ValueError("run-request draft must be a JSON object")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            emit({"ok": False, "status": "blocked", "error": str(exc)})
            return 1
        result = asset_programme_intake({"draft": draft})
        emit(asdict(result))
        return 0 if result.data.get("ready") else 2

    if args.agent_command == "start":
        try:
            request = json.loads(Path(args.request).read_text(encoding="utf-8"))
            if not isinstance(request, dict):
                raise ValueError("run request must be a JSON object")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            emit({"ok": False, "status": "blocked", "error": str(exc)})
            return 1
        result = asset_factory_start(
            {
                "run_request": request,
                "project_root": args.project_root,
                "project_name": args.project_name,
                "dry_run": not args.live,
                "max_fix_attempts": args.max_fix_attempts,
            }
        )
        emit(asdict(result))
        return 0 if result.success else 1

    try:
        result = run_agent_loop(
            request_path=args.request,
            project_root=args.project_root,
            project_name=args.project_name,
            dry_run=not args.live,
            max_fix_attempts=args.max_fix_attempts,
        )
    except Exception as exc:
        emit({"ok": False, "status": "blocked", "error": str(exc)})
        return 1
    emit(result)
    return 0


def cmd_stage(args: argparse.Namespace) -> int:
    from asset_factory_blueprint.config import load_json as _load_json
    from asset_factory_blueprint.services.stage_runner import asset_stage_run

    if args.stage_command == "list":
        policy = _load_json("configs/vlm-review-policy.json")
        workflow = _load_json("configs/agent-workflow.json")
        reviewable = set(policy.get("stages", {}))
        stages = [
            {
                "stage_id": stage["id"],
                "name": stage.get("name", ""),
                "skill": stage.get("skill", ""),
                "directly_invocable": stage["id"] in reviewable,
            }
            for stage in workflow.get("stages", [])
        ]
        emit({"stages": stages, "usage": "afb stage run <stage_id> --project projects/<slug> [--live]"})
        return 0
    result = asset_stage_run(
        {
            "stage_id": args.stage_id,
            "project": args.project,
            "request": args.request,
            "project_root": args.project_root,
            "project_name": args.project_name,
            "dry_run": not args.live,
            "max_fix_attempts": args.max_fix_attempts,
            "refresh_artefacts": not args.no_refresh,
        }
    )
    emit({"ok": result.success, "validation_status": result.validation_status, **(result.data or {}), **({"error": result.error} if result.error else {})})
    return 0 if result.success else 1


def cmd_progress(args: argparse.Namespace) -> int:
    result = write_progress_artefacts(args.project)
    emit(result)
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    if args.install:
        plan = install_capability(args.install, args.option or None, dry_run=not args.live)
        emit(plan)
        return 0 if plan.get("status") in {"planned", "installed", "manual"} else 1
    report = probe_capabilities(args.registry)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    emit(report)
    return 0 if report["blocked_count"] == 0 else 1


def cmd_library(args: argparse.Namespace) -> int:
    if args.library_command == "search":
        payload: dict = {
            "query": args.query,
            "hits": search_library(args.query, args.domain or None, args.limit),
        }
        if args.remote:
            if not args.domain or "usd_assets" in args.domain:
                payload["usd_search"] = usd_search_query(args.query, args.limit)
            payload["remote_sources"] = search_remote_sources(args.query, None, args.limit)
        emit(payload)
        return 0
    if args.library_command == "index":
        if args.backing:
            emit(build_local_index(args.backing))
            return 0
        results = [
            build_local_index(backing["backing_id"])
            for backing in list_backings()
            if backing.get("kind") in {"local_folder", "manual_pack"} and backing.get("resolved_path")
        ]
        emit({"results": results})
        return 0
    if args.library_command == "backings":
        emit({"backings": list_backings()})
        return 0
    if args.library_command == "packs":
        emit(_read_config("library/asset-packs.json"))
        return 0
    if args.library_command == "fetch":
        result = fetch_from_source(
            args.source,
            query=args.query or "",
            item_ids=args.item or None,
            limit=args.limit,
            dry_run=not args.live,
        )
        emit(result)
        return 0 if result["status"] in {"planned", "completed"} else 1
    return run_shop(
        query=args.query or "",
        sources=args.source_filter or None,
        select=args.select or "",
        live=args.live,
        limit=args.limit,
        assume_yes=args.yes,
    )


def cmd_external_models(args: argparse.Namespace) -> int:
    if args.external_command == "list":
        emit({"models": list_models(args.config)})
        return 0
    if args.external_command == "validate":
        errors = validate_config(args.config)
        emit({"ok": not errors, "errors": errors})
        return 0 if not errors else 1
    emit(run_manifest(args.manifest, args.dry_run))
    return 0


def cmd_reconstruction(args: argparse.Namespace) -> int:
    if args.reconstruction_command == "backends":
        emit({"backends": list_backend_specs(args.registry)})
        return 0
    if args.reconstruction_command == "create-backend":
        payload = build_backend_run_manifest(
            args.backend,
            output_path=args.output,
            input_manifest=args.input_manifest,
            output_manifest=args.output_manifest,
            registry_path=args.registry,
            asset_id=args.asset_id,
            project_id=args.project_id,
        )
        emit(payload)
        return 0
    if args.reconstruction_command == "install-check":
        install_root = Path(args.install_root) if args.install_root else default_install_root(args.backend)
        report = check_backend_install(args.backend, install_root)
        report = write_backend_install_report(Path(args.output), report)
        emit(report)
        if args.require_ready and report["status"] != "ready":
            return 1
        return 0
    if args.reconstruction_command == "install":
        install_root = Path(args.install_root) if args.install_root else default_install_root(args.backend)
        report = install_backend(args.backend, install_root, force=args.force)
        report = write_backend_install_report(Path(args.output), report)
        emit(report)
        return 0 if report["status"] == "ready" else 1
    report = provision_backend(args.backend, registry_path=args.registry, output_path=args.output)
    emit(report)
    if args.require_ready and report["status"] != "ready":
        return 1
    return 0


def cmd_tool_server(args: argparse.Namespace) -> int:
    if any(
        value < 1
        for value in (
            args.max_request_bytes,
            args.max_result_bytes,
            args.max_http_threads,
            args.max_workers,
            args.max_retained_jobs,
        )
    ):
        emit({"ok": False, "error": "tool-server limits must be positive integers"})
        return 1
    if args.max_retries < 0:
        emit({"ok": False, "error": "tool-server max retries must be zero or greater"})
        return 1
    configured_tools = args.allowed_tools or os.environ.get(args.allowed_tools_env, "")
    allowed_tools = frozenset(item.strip() for item in configured_tools.split(",") if item.strip()) or None
    approval_secret = token_from_environment(args.approval_secret_env)
    available_tools = {tool.name for tool in list_tools()}
    unknown_tools = sorted((allowed_tools or frozenset()) - available_tools)
    if unknown_tools:
        emit({"ok": False, "error": f"allowed tool set contains unknown tools: {', '.join(unknown_tools)}"})
        return 1
    if approval_secret is not None and len(approval_secret.encode("utf-8")) < 32:
        emit({"ok": False, "error": f"approval secret in {args.approval_secret_env} must contain at least 32 bytes"})
        return 1
    configured_job_store = args.job_store or os.environ.get("AFB_TOOL_SERVER_JOB_STORE")
    bearer_token = token_from_environment(args.token_env)
    if bearer_token is not None and approval_secret is not None and bearer_token == approval_secret:
        emit({"ok": False, "error": "bearer token and approval secret must be independent values"})
        return 1
    config = ToolServerConfig(
        host=args.host,
        port=args.port,
        catalogue_path=args.path,
        max_request_bytes=args.max_request_bytes,
        max_result_bytes=args.max_result_bytes,
        max_http_threads=args.max_http_threads,
        max_workers=args.max_workers,
        max_retained_jobs=args.max_retained_jobs,
        bearer_token=bearer_token,
        audit_log=Path(args.audit_log) if args.audit_log else None,
        job_store=Path(configured_job_store) if configured_job_store else None,
        approval_secret=approval_secret,
        allowed_tools=allowed_tools,
        max_retries=args.max_retries,
    )
    if args.transport == "stdio":
        serve_stdio(config)
        return 0

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        if os.environ.get("AFB_TRUSTED_TOOL_SERVER_NETWORK", "").lower() not in {"1", "true", "yes"}:
            emit({"ok": False, "error": "http tool-server binds to loopback unless AFB_TRUSTED_TOOL_SERVER_NETWORK is enabled"})
            return 1
        if config.bearer_token is None:
            emit({"ok": False, "error": f"non-loopback tool-server requires a bearer token in {args.token_env}"})
            return 1
        if config.approval_secret is None:
            emit(
                {
                    "ok": False,
                    "error": f"non-loopback tool-server requires an approval secret in {args.approval_secret_env}",
                }
            )
            return 1
        if len(config.bearer_token.encode("utf-8")) < 32:
            emit({"ok": False, "error": f"bearer token in {args.token_env} must contain at least 32 bytes"})
            return 1
    serve_http(config)
    return 0


def cmd_tool_approval(args: argparse.Namespace) -> int:
    secret = token_from_environment(args.secret_env)
    if secret is None:
        emit({"ok": False, "error": f"approval secret is not set in {args.secret_env}"})
        return 1
    try:
        if args.params_file:
            params = json.loads(Path(args.params_file).read_text(encoding="utf-8"))
        else:
            params = json.loads(args.params)
        if not isinstance(params, dict):
            raise ValueError("approval parameters must be a JSON object")
        tools = {tool.name: tool for tool in list_tools()}
        tool = tools.get(args.tool)
        if tool is None:
            raise ValueError(f"unknown tool: {args.tool}")
        if not tool_requires_approval(tool, params):
            raise ValueError("this invocation does not require a reviewed-mutation approval")
        token = issue_approval_token(
            secret,
            tool=args.tool,
            params=params,
            expires_at=args.expires_at,
            approved_by=args.approved_by,
            reason=args.reason,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        emit({"ok": False, "error": str(exc)})
        return 1
    emit(
        {
            "approval_token": token,
            "tool": args.tool,
            "params_digest": params_digest(params),
            "expires_at": args.expires_at,
            "approved_by": args.approved_by,
            "reason": args.reason,
            "single_use": True,
        }
    )
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    tools = [tool.__dict__ for tool in list_tools()]
    emit({"tools": tools} if args.format == "json" else [tool["name"] for tool in tools])
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    if args.skills_command == "list":
        result = audit(".")
        emit({"skills": result["skills"]})
        return 0
    result = audit(".")
    emit(result)
    return 0 if result["ok"] else 1


def cmd_readiness(args: argparse.Namespace) -> int:
    text = write_readiness(args.asset_manifest, args.output, args.project)
    print(text, end="")
    return 0


def cmd_isaac_load(args: argparse.Namespace) -> int:
    emit(apply_isaac_load_report(args.project, args.report))
    return 0


def cmd_simready(args: argparse.Namespace) -> int:
    output = Path(args.output)
    raw_output = Path(args.raw_output) if args.raw_output else output.with_name(f"{output.stem}.raw.json")
    usd_path = Path(args.usd).resolve()
    if output.resolve() in {usd_path, raw_output.resolve()}:
        emit({"status": "blocked", "error": "normalised, vendor and USD paths must be distinct"})
        return 1
    package_root = usd_path.parent
    if any(path.resolve() == package_root or package_root in path.resolve().parents for path in (output, raw_output)):
        emit({"status": "blocked", "error": "validator reports must be written outside the immutable package directory"})
        return 1
    try:
        environment = OmniAssetValidatorConfig.from_environment()
    except ValueError as exc:
        emit({"status": "blocked", "error": f"invalid validator environment configuration: {exc}"})
        return 1
    config = OmniAssetValidatorConfig(
        executable=environment.executable,
        executable_sha256=environment.executable_sha256,
        attestation_secret=environment.attestation_secret,
        timeout_seconds=args.timeout_seconds if args.timeout_seconds is not None else environment.timeout_seconds,
        max_process_output_bytes=(
            args.max_process_output_bytes
            if args.max_process_output_bytes is not None
            else environment.max_process_output_bytes
        ),
        max_report_bytes=args.max_report_bytes if args.max_report_bytes is not None else environment.max_report_bytes,
    )
    report = run_official_profile_validation(
        args.usd,
        profile_id=args.profile_id,
        profile_version=args.profile_version,
        raw_report_path=raw_output,
        config=config,
    )
    write_official_profile_report(output, report)
    emit(report)
    return 0 if report["status"] == "pass" else 1


def cmd_semantics(args: argparse.Namespace) -> int:
    report = migrate_legacy_semantics(args.source, args.output, report_path=args.report)
    emit(report)
    return 0 if report["status"] == "pass" else 1


def cmd_fitness(args: argparse.Namespace) -> int:
    if args.fitness_command == "template":
        result = write_task_fitness_template(args.project, args.output)
        emit(result)
        return 0
    result = apply_task_fitness_report(args.project, args.report)
    emit(result)
    return 0 if result["status"] == "pass" else 1


def cmd_physics_evidence(args: argparse.Namespace) -> int:
    try:
        result = seal_physics_evidence_file(args.input, args.output)
    except (OSError, TypeError, ValueError) as exc:
        emit({"status": "blocked", "error": str(exc)})
        return 1
    emit(result)
    return 0


def cmd_governance(args: argparse.Namespace) -> int:
    decision = build_operator_release_decision(
        args.project,
        reviewer=args.reviewer,
        decision=args.decision,
        expires_at=args.expires_at,
        scope=args.scope,
        notes=args.note,
        decided_at=args.decided_at,
    )
    emit(write_operator_release_decision(args.project, decision) if args.write else {"decision": decision})
    return 0


def cmd_provider(args: argparse.Namespace) -> int:
    if args.provider_command == "check":
        statuses = check_policy(args.policy, live=not args.no_live)
        payload = statuses_as_dict(statuses)
        emit({"providers": payload})
        if args.require_live and any(not item.live for item in statuses):
            return 1
        return 0
    try:
        completion = completion_as_dict(
            complete_chat(
                provider_name=args.provider,
                prompt=args.prompt,
                policy_path=args.policy,
                model=args.model,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
            )
        )
    except Exception as exc:
        emit({"ok": False, "provider": args.provider, "status": "blocked", "error": str(exc)})
        return 1
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(completion, indent=2) + "\n", encoding="utf-8")
    emit(completion)
    return 0


def cmd_skill_audit(args: argparse.Namespace) -> int:
    result = write_audit(args.root, args.output)
    emit(result)
    return 0 if result["ok"] else 1


def cmd_wandb_plan(args: argparse.Namespace) -> int:
    emit(write_wandb_plan(args.run_plan, args.output))
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    emit(write_release_evidence(args.output_dir))
    return 0


def cmd_capsule(args: argparse.Namespace) -> int:
    if args.capsule_command == "validate":
        report = validate_reference_capsule(args.capsule)
        emit(report)
        return 0 if report["valid"] else 1

    include_outputs = True if args.include_outputs else False if args.no_outputs else None
    try:
        result = create_reference_capsule(
            args.project,
            args.output,
            outcome=args.outcome,
            run_id=args.run_id,
            release_scope=args.scope,
            include_source_media=args.include_source_media,
            include_outputs=include_outputs,
        )
    except Exception as exc:
        blockers = list(exc.blockers) if isinstance(exc, CapsuleCreationError) else [str(exc)]
        emit(
            {
                "status": "blocked",
                "valid": False,
                "error": str(exc),
                "blockers": blockers,
            }
        )
        return 1
    emit(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="afb",
        description="Operate governed OpenUSD asset workflows and their evidence records.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("format", help="Format or check repository text files")
    p.add_argument("--check", action="store_true")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_format)

    p = sub.add_parser("info", help="Report the runtime and source-appliance configuration")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("project", help="Create, inspect and validate project workspaces")
    ps = p.add_subparsers(dest="project_command", required=True)
    pn = ps.add_parser("new", help="Create a project workspace")
    pn.add_argument("name")
    pn.add_argument("--project-root", default="projects")
    po = ps.add_parser("open", help="Open a project record")
    po.add_argument("slug")
    po.add_argument("--project-root", default="projects")
    pl = ps.add_parser("list", help="List project workspaces")
    pl.add_argument("--project-root", default="projects")
    snap = ps.add_parser("snapshot", help="Create a named project snapshot")
    snap.add_argument("slug")
    snap.add_argument("--name", required=True)
    snap.add_argument("--project-root", default="projects")
    pv = ps.add_parser("validate", help="Validate the complete project record graph")
    pv.add_argument("--project", required=True)
    pv.add_argument("--output")
    p.set_defaults(func=cmd_project)

    p = sub.add_parser("schema", help="Inspect and instantiate JSON Schema contracts")
    ss = p.add_subparsers(dest="schema_command", required=True)
    ss.add_parser("list", help="List public schema contracts")
    sk = ss.add_parser("skeleton", help="Write a schema-valid skeleton record")
    sk.add_argument("schema_name")
    sk.add_argument("--output")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("manifest", help="Validate durable manifest records")
    ms = p.add_subparsers(dest="manifest_command", required=True)
    mv = ms.add_parser("validate", help="Validate a manifest against a named schema")
    mv.add_argument("schema_name")
    mv.add_argument("manifest")
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("layout", help="Validate governed asset layout plans")
    ls = p.add_subparsers(dest="layout_command", required=True)
    lv = ls.add_parser("validate", help="Validate a layout manifest")
    lv.add_argument("path")
    lv.add_argument("--project")
    lv.add_argument("--validate-only", action="store_true")
    lv.set_defaults(mode="layout")
    p.set_defaults(func=cmd_validate_file)

    p = sub.add_parser("mutation", help="Validate governed mutation plans")
    mus = p.add_subparsers(dest="mutation_command", required=True)
    muv = mus.add_parser("validate", help="Validate a mutation manifest")
    muv.add_argument("path")
    muv.add_argument("--project")
    muv.add_argument("--validate-only", action="store_true")
    muv.set_defaults(mode="mutation")
    p.set_defaults(func=cmd_validate_file)

    p = sub.add_parser("run-plan", help="Build a dependency-closed run plan")
    p.add_argument("--request", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_run_plan)

    p = sub.add_parser("workflow", help="Run or summarise a project workflow")
    ws = p.add_subparsers(dest="workflow_command", required=True)
    wr = ws.add_parser("run", help="Execute the typed workflow")
    wr.add_argument("--request", required=True)
    wr.add_argument("--live", action="store_true")
    wr.add_argument("--dry-run", action="store_true", help="accepted for compatibility; dry run is the default")
    wr.add_argument("--output")
    wr.add_argument("--project-root", default="projects")
    wr.add_argument("--project-name")
    wz = ws.add_parser("summarize", help="Summarise a run plan and its reports")
    wz.add_argument("--run-plan", required=True)
    wz.add_argument("--reports", required=True)
    p.set_defaults(func=cmd_workflow)

    p = sub.add_parser("texture", help="Create and validate texture records")
    ts = p.add_subparsers(dest="texture_command", required=True)
    td = ts.add_parser("defaults", help="Inspect and validate texture defaults")
    tds = td.add_subparsers(dest="defaults_command", required=True)
    tds.add_parser("list", help="List texture-default profiles")
    tde = tds.add_parser("explain", help="Explain defaults for a material")
    tde.add_argument("--material", required=True)
    tde.add_argument("--profile")
    tdv = tds.add_parser("validate", help="Validate texture and property records")
    tdv.add_argument("--texture-manifest", required=True)
    tdv.add_argument("--property-manifest", required=True)
    tp = ts.add_parser("prompt", help="Build a grounded texture prompt")
    tp.add_argument("--material-manifest", required=True)
    tp.add_argument("--property-manifest", required=True)
    tp.add_argument("--output", required=True)
    tv = ts.add_parser("variation-workflow", help="Write a texture-variation workflow record")
    tv.add_argument("--image", required=True)
    tv.add_argument("--asset-id", required=True)
    tv.add_argument("--texture-variant", action="append", default=[])
    tv.add_argument("--mesh-deformation", action="append", default=[])
    tv.add_argument("--appearance-segment", action="append", default=[])
    tv.add_argument("--output")
    p.set_defaults(func=cmd_texture)

    p = sub.add_parser("library", help="Search, index and fetch grounding libraries")
    ls_lib = p.add_subparsers(dest="library_command", required=True)
    lsr = ls_lib.add_parser("search", help="Search local and optional remote library records")
    lsr.add_argument("--query", required=True)
    lsr.add_argument("--domain", action="append", default=[])
    lsr.add_argument("--limit", type=int, default=12)
    lsr.add_argument("--remote", action="store_true")
    lix = ls_lib.add_parser("index", help="Build a local backing index")
    lix.add_argument("--backing")
    ls_lib.add_parser("backings", help="List configured library backings")
    ls_lib.add_parser("packs", help="List registered downloadable packs")
    lft = ls_lib.add_parser("fetch", help="Fetch selected public library items")
    lft.add_argument("--source", default="ambientcg")
    lft.add_argument("--query")
    lft.add_argument("--item", action="append", default=[])
    lft.add_argument("--limit", type=int, default=5)
    lft.add_argument("--live", action="store_true")
    lsh = ls_lib.add_parser("shop", help="Open the terminal library selector")
    lsh.add_argument("--query")
    lsh.add_argument("--source-filter", action="append", default=[])
    lsh.add_argument("--select")
    lsh.add_argument("--limit", type=int, default=12)
    lsh.add_argument("--live", action="store_true")
    lsh.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_library)

    p = sub.add_parser("agent", help="Run the governed agent loop")
    ags = p.add_subparsers(dest="agent_command", required=True)
    agi = ags.add_parser("intake", help="Validate a run-request draft and return start-blocking questions")
    agi.add_argument("--draft", required=True)
    ags_ = ags.add_parser("start", help="Validate a complete request and start the whole agent loop")
    ags_.add_argument("--request", required=True)
    ags_.add_argument("--project-root", default="projects")
    ags_.add_argument("--project-name")
    ags_.add_argument("--live", action="store_true")
    ags_.add_argument("--max-fix-attempts", type=int, default=None)
    agr = ags.add_parser("run", help="Review and advance routed stages")
    agr.add_argument("--request", required=True)
    agr.add_argument("--project-root", default="projects")
    agr.add_argument("--project-name")
    agr.add_argument("--live", action="store_true")
    agr.add_argument("--max-fix-attempts", type=int, default=None)
    p.set_defaults(func=cmd_agent)

    p = sub.add_parser("stage", help="Inspect or run one routed stage")
    sts = p.add_subparsers(dest="stage_command", required=True)
    sts.add_parser("list", help="List directly invocable stages")
    str_ = sts.add_parser("run", help="Run one stage with dependency checks")
    str_.add_argument("stage_id")
    str_.add_argument("--project")
    str_.add_argument("--request")
    str_.add_argument("--project-root", default="projects")
    str_.add_argument("--project-name")
    str_.add_argument("--live", action="store_true")
    str_.add_argument("--max-fix-attempts", type=int, default=None)
    str_.add_argument("--no-refresh", action="store_true", help="review existing artefacts without rebuilding the workspace")
    p.set_defaults(func=cmd_stage)

    p = sub.add_parser("progress", help="Rebuild project progress artefacts")
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_progress)

    p = sub.add_parser("capabilities", help="Probe capabilities and plan installations")
    p.add_argument("--registry", default="configs/capability-registry.json")
    p.add_argument("--output")
    p.add_argument("--install")
    p.add_argument("--option")
    p.add_argument("--live", action="store_true")
    p.set_defaults(func=cmd_capabilities)

    p = sub.add_parser("external-models", help="Validate and run governed external models")
    es = p.add_subparsers(dest="external_command", required=True)
    el = es.add_parser("list", help="List configured external models")
    el.add_argument("--config", default="configs/external-models.json")
    ev = es.add_parser("validate", help="Validate the external-model configuration")
    ev.add_argument("--config", default="configs/external-models.json")
    er = es.add_parser("run", help="Run an external-model manifest")
    er.add_argument("--manifest", required=True)
    er.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_external_models)

    p = sub.add_parser("reconstruction", help="Inspect, provision and install reconstruction backends")
    rs = p.add_subparsers(dest="reconstruction_command", required=True)
    rb = rs.add_parser("backends", help="List governed reconstruction backends")
    rb.add_argument("--registry", default="configs/reconstruction-backends.json")
    rc = rs.add_parser("create-backend", help="Create an external reconstruction run manifest")
    rc.add_argument("--backend", required=True)
    rc.add_argument("--registry", default="configs/reconstruction-backends.json")
    rc.add_argument("--output")
    rc.add_argument("--input-manifest", default="projects/<project>/manifests/source-asset-manifest.json")
    rc.add_argument("--output-manifest")
    rc.add_argument("--asset-id", default="")
    rc.add_argument("--project-id", default="")
    rp = rs.add_parser("provision", help="Write a backend provisioning report")
    rp.add_argument("--backend", required=True)
    rp.add_argument("--registry", default="configs/reconstruction-backends.json")
    rp.add_argument("--output", required=True)
    rp.add_argument("--require-ready", action="store_true")
    ric = rs.add_parser("install-check", help="Check an existing backend installation")
    ric.add_argument("--backend", required=True)
    ric.add_argument("--install-root")
    ric.add_argument("--output", required=True)
    ric.add_argument("--require-ready", action="store_true")
    ri = rs.add_parser("install", help="Install a pinned reconstruction backend")
    ri.add_argument("--backend", required=True)
    ri.add_argument("--install-root")
    ri.add_argument("--output", required=True)
    ri.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_reconstruction)

    p = sub.add_parser("tool-server", help="Serve the bounded public tool catalogue")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8181)
    p.add_argument("--path", default="/tools")
    p.add_argument("--max-request-bytes", type=int, default=1_048_576)
    p.add_argument("--max-result-bytes", type=int, default=4_194_304)
    p.add_argument("--max-http-threads", type=int, default=32)
    p.add_argument("--max-workers", type=int, default=4)
    p.add_argument("--max-retained-jobs", type=int, default=256)
    p.add_argument("--max-retries", type=int, default=1)
    p.add_argument("--token-env", default="AFB_TOOL_SERVER_TOKEN")
    p.add_argument("--approval-secret-env", default="AFB_TOOL_SERVER_APPROVAL_SECRET")
    p.add_argument("--allowed-tools-env", default="AFB_TOOL_SERVER_ALLOWED_TOOLS")
    p.add_argument("--allowed-tools", help="comma-separated tool allowlist")
    p.add_argument("--job-store")
    p.add_argument("--audit-log")
    p.set_defaults(func=cmd_tool_server)

    p = sub.add_parser("tool-approval", help="Issue reviewed-mutation capability tokens")
    tas = p.add_subparsers(dest="tool_approval_command", required=True)
    tai = tas.add_parser("issue", help="Issue a short-lived parameter-bound approval")
    tai.add_argument("--tool", required=True)
    params_source = tai.add_mutually_exclusive_group(required=True)
    params_source.add_argument("--params")
    params_source.add_argument("--params-file")
    tai.add_argument("--expires-at", required=True)
    tai.add_argument("--approved-by", required=True)
    tai.add_argument("--reason", required=True)
    tai.add_argument("--secret-env", default="AFB_TOOL_SERVER_APPROVAL_SECRET")
    p.set_defaults(func=cmd_tool_approval)

    p = sub.add_parser("tools", help="Inspect the public tool catalogue")
    ts = p.add_subparsers(dest="tools_command", required=True)
    tl = ts.add_parser("list", help="List public tools")
    tl.add_argument("--format", choices=["json", "text"], default="text")
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("skills", help="Inspect skill packages and runtime configuration")
    ks = p.add_subparsers(dest="skills_command", required=True)
    ks.add_parser("list", help="List skill packages")
    kv = ks.add_parser("validate-config", help="Validate a runtime skill configuration")
    kv.add_argument("--config", default="configs/runtime-config.example.json")
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("readiness", help="Write a release-readiness report")
    p.add_argument("--asset-manifest")
    p.add_argument("--project")
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_readiness)

    p = sub.add_parser("isaac-load", help="Import trusted Isaac runtime evidence")
    il = p.add_subparsers(dest="isaac_load_command", required=True)
    ila = il.add_parser("apply", help="Verify and apply an Isaac runtime report")
    ila.add_argument("--project", required=True)
    ila.add_argument("--report", required=True)
    p.set_defaults(func=cmd_isaac_load)

    p = sub.add_parser("simready", help="Run exact SimReady Profile validation")
    srs = p.add_subparsers(dest="simready_command", required=True)
    srv = srs.add_parser("validate-profile", help="Run the pinned NVIDIA validator bridge")
    srv.add_argument("--usd", required=True)
    srv.add_argument("--profile-id", required=True)
    srv.add_argument("--profile-version", required=True)
    srv.add_argument("--output", required=True)
    srv.add_argument("--raw-output")
    srv.add_argument("--timeout-seconds", type=float)
    srv.add_argument("--max-process-output-bytes", type=int)
    srv.add_argument("--max-report-bytes", type=int)
    p.set_defaults(func=cmd_simready)

    p = sub.add_parser("semantics", help="Migrate legacy semantic labels")
    sems = p.add_subparsers(dest="semantics_command", required=True)
    semm = sems.add_parser("migrate", help="Migrate a USD layer to the current semantics API")
    semm.add_argument("--source", required=True)
    semm.add_argument("--output", required=True)
    semm.add_argument("--report")
    p.set_defaults(func=cmd_semantics)

    p = sub.add_parser("fitness", help="Create and apply task-fitness evidence")
    fts = p.add_subparsers(dest="fitness_command", required=True)
    ftt = fts.add_parser("template", help="Write a blocked task-fitness template")
    ftt.add_argument("--project", required=True)
    ftt.add_argument("--output", required=True)
    fta = fts.add_parser("apply", help="Verify and apply completed task-fitness evidence")
    fta.add_argument("--project", required=True)
    fta.add_argument("--report", required=True)
    p.set_defaults(func=cmd_fitness)

    p = sub.add_parser("physics-evidence", help="Seal accepted physical-property evidence")
    pes = p.add_subparsers(dest="physics_evidence_command", required=True)
    peseal = pes.add_parser("seal", help="HMAC-seal a physical-evidence record")
    peseal.add_argument("--input", required=True)
    peseal.add_argument("--output", required=True)
    p.set_defaults(func=cmd_physics_evidence)

    p = sub.add_parser("governance", help="Build content-bound operator decisions")
    gs = p.add_subparsers(dest="governance_command", required=True)
    gd = gs.add_parser("decide", help="Preview or write an operator release decision")
    gd.add_argument("--project", required=True)
    gd.add_argument("--reviewer", required=True)
    gd.add_argument("--decision", choices=["approve", "reject"], required=True)
    gd.add_argument("--scope", choices=["visualisation", "rigid_body_manipulation", "articulated_training", "redistribution"])
    gd.add_argument("--expires-at", required=True)
    gd.add_argument("--decided-at")
    gd.add_argument("--note", action="append", default=[])
    gd.add_argument("--write", action="store_true", help="write the current decision and immutable history record")
    p.set_defaults(func=cmd_governance)

    p = sub.add_parser("provider", help="Check providers and record proposal responses")
    pr = p.add_subparsers(dest="provider_command", required=True)
    pc = pr.add_parser("check", help="Check configured provider lanes")
    pc.add_argument("--policy", default="configs/provider-policy.json")
    pc.add_argument("--no-live", action="store_true")
    pc.add_argument("--require-live", action="store_true")
    pp = pr.add_parser("prompt", help="Send and record a provider prompt")
    pp.add_argument("--policy", default="configs/provider-policy.json")
    pp.add_argument("--provider", default="nvidia_nim")
    pp.add_argument("--model")
    pp.add_argument("--prompt", required=True)
    pp.add_argument("--output")
    pp.add_argument("--max-tokens", type=int, default=96)
    pp.add_argument("--temperature", type=float, default=0.0)
    p.set_defaults(func=cmd_provider)

    p = sub.add_parser("skill-audit", help="Audit skill package completeness")
    p.add_argument("--root", default=".")
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_skill_audit)

    p = sub.add_parser("wandb-plan", help="Write a Weights and Biases telemetry plan")
    p.add_argument("--run-plan", required=True)
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_wandb_plan)

    p = sub.add_parser("release", help="Generate publication evidence")
    rs_release = p.add_subparsers(dest="release_command", required=True)
    re = rs_release.add_parser("evidence", help="Write the SBOM and release metadata bundle")
    re.add_argument("--output-dir", required=True)
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("capsule", help="Create and validate reference-run capsules")
    cs = p.add_subparsers(dest="capsule_command", required=True)
    cc = cs.add_parser("create", help="Create a positive or negative reference capsule")
    cc.add_argument("--project", required=True)
    cc.add_argument("--output", required=True)
    cc.add_argument("--outcome", choices=["positive", "negative"], required=True)
    cc.add_argument("--run-id")
    cc.add_argument(
        "--scope",
        choices=["visualisation", "rigid_body_manipulation", "articulated_training", "redistribution"],
    )
    cc.add_argument("--include-source-media", action="store_true")
    output_selection = cc.add_mutually_exclusive_group()
    output_selection.add_argument("--include-outputs", action="store_true")
    output_selection.add_argument("--no-outputs", action="store_true")
    cv = cs.add_parser("validate", help="Independently validate a reference capsule")
    cv.add_argument("--capsule", required=True)
    p.set_defaults(func=cmd_capsule)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
