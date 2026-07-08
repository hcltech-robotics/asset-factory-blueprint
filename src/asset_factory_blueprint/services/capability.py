from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asset_factory_blueprint.config import load_json
from asset_factory_blueprint.skills.base import ToolResult


REGISTRY_PATH = "configs/capability-registry.json"


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _gpu_summary() -> dict[str, Any]:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return {"available": False, "detail": "nvidia-smi not found"}
    try:
        result = subprocess.run(
            [smi, "--query-gpu=name,memory.total", "--format=csv,noheader"],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "detail": str(exc)}
    if result.returncode != 0:
        return {"available": False, "detail": result.stderr.strip()[:200]}
    gpus = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return {"available": bool(gpus), "gpus": gpus}


def _env_status(option: dict[str, Any]) -> tuple[bool, list[str]]:
    missing: list[str] = []
    for name in option.get("env_all", []):
        if not os.environ.get(name):
            missing.append(name)
    any_names = option.get("env_any", [])
    if any_names and not any(os.environ.get(name) for name in any_names):
        missing.append("one of: " + ", ".join(any_names))
    return not missing, missing


def _probe_option(option: dict[str, Any]) -> dict[str, Any]:
    kind = option.get("kind", "manual")
    status = "ready"
    reasons: list[str] = []

    modules = option.get("python_modules", [])
    missing_modules = [name for name in modules if not _module_present(name)]
    if missing_modules:
        status = "missing"
        reasons.append("missing python modules: " + ", ".join(missing_modules))

    env_ok, missing_env = _env_status(option)
    if not env_ok:
        status = "missing" if status == "ready" else status
        reasons.append("missing environment: " + ", ".join(missing_env))

    if kind == "reconstruction_backend":
        from asset_factory_blueprint.reconstruction_backends import inspect_backend

        try:
            backend_status = inspect_backend(option["backend_id"])
        except ValueError as exc:
            status = "missing"
            reasons.append(str(exc))
        else:
            if backend_status["status"] != "ready":
                status = "missing"
                reasons.extend(backend_status.get("blocked_reasons", []))
    elif kind == "provider":
        provider = option.get("provider", "")
        policy = load_json("configs/provider-policy.json")
        if provider not in policy.get("providers", {}):
            status = "missing"
            reasons.append(f"provider lane {provider} is not declared in the provider policy")

    if status != "ready" and option.get("gated"):
        status = "gated_blocked"

    return {
        "option_id": option["option_id"],
        "role": option.get("role", "fallback"),
        "kind": kind,
        "status": status,
        "gated": bool(option.get("gated", False)),
        "gate": option.get("gate", ""),
        "gate_note": option.get("gate_note", ""),
        "note": option.get("note", ""),
        "reasons": reasons,
    }


def probe_capabilities(registry_path: str = REGISTRY_PATH) -> dict[str, Any]:
    registry = load_json(registry_path)
    capabilities: list[dict[str, Any]] = []
    for capability in registry.get("capabilities", []):
        options = [_probe_option(option) for option in capability.get("options", [])]
        ordered = sorted(options, key=lambda item: (item["role"] != "primary",))
        active = next((item for item in ordered if item["status"] == "ready"), None)
        entry = {
            "capability_id": capability["capability_id"],
            "description": capability.get("description", ""),
            "status": "ready" if active else "blocked",
            "active_option": active["option_id"] if active else "",
            "active_is_primary": bool(active and active["role"] == "primary"),
            "options": options,
        }
        if not active:
            gated = [item for item in options if item["gated"]]
            entry["blocked_reasons"] = [reason for item in options for reason in item["reasons"]]
            if gated:
                entry["gate_notes"] = [f"{item['option_id']}: {item['gate_note']}" for item in gated if item["gate_note"]]
        capabilities.append(entry)
    blocked = [item for item in capabilities if item["status"] != "ready"]
    degraded = [item["capability_id"] for item in capabilities if item["status"] == "ready" and not item["active_is_primary"]]
    return {
        "id": "capability_report",
        "version": "1.0",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "gpu": _gpu_summary(),
        "capability_count": len(capabilities),
        "blocked_count": len(blocked),
        "running_on_fallback": degraded,
        "capabilities": capabilities,
    }


def install_capability(capability_id: str, option_id: str | None = None, registry_path: str = REGISTRY_PATH, dry_run: bool = True) -> dict[str, Any]:
    registry = load_json(registry_path)
    capability = next((item for item in registry.get("capabilities", []) if item["capability_id"] == capability_id), None)
    if capability is None:
        return {"status": "blocked", "error": f"unknown capability: {capability_id}"}
    options = capability.get("options", [])
    if option_id:
        option = next((item for item in options if item["option_id"] == option_id), None)
        if option is None:
            known = ", ".join(item["option_id"] for item in options)
            return {"status": "blocked", "error": f"unknown option {option_id} for {capability_id}; known options: {known}"}
    else:
        option = options[0] if options else None
    if option is None:
        return {"status": "blocked", "error": f"capability {capability_id} declares no options"}

    kind = option.get("kind", "manual")
    if option.get("gated"):
        probe = _probe_option(option)
        if probe["status"] == "ready":
            return {
                "capability_id": capability_id,
                "option_id": option["option_id"],
                "kind": kind,
                "dry_run": dry_run,
                "status": "already_ready",
            }
    plan: dict[str, Any] = {
        "capability_id": capability_id,
        "option_id": option["option_id"],
        "kind": kind,
        "dry_run": dry_run,
        "gated": bool(option.get("gated", False)),
        "gate_note": option.get("gate_note", ""),
    }
    if option.get("gated"):
        plan["status"] = "gated_blocked"
        plan["action"] = "resolve the licence or access gate, then rerun the install"
        return plan
    if kind == "pip":
        packages = option.get("pip_packages", [])
        plan["command"] = [sys.executable, "-m", "pip", "install", *packages]
        if dry_run:
            plan["status"] = "planned"
            return plan
        result = subprocess.run(plan["command"], text=True, capture_output=True, check=False)
        plan["status"] = "installed" if result.returncode == 0 else "blocked"
        plan["detail"] = (result.stdout + result.stderr)[-1500:]
        return plan
    if kind == "reconstruction_backend":
        from asset_factory_blueprint.reconstruction_installers import default_install_root, install_backend

        backend_id = option["backend_id"]
        plan["backend_id"] = backend_id
        if dry_run:
            plan["status"] = "planned"
            plan["action"] = f"afb reconstruction install --backend {backend_id} --output artifacts/{backend_id}-install.json"
            return plan
        report = install_backend(backend_id, default_install_root(backend_id))
        plan["status"] = report.get("status", "blocked")
        plan["detail"] = report
        return plan
    plan["status"] = "manual"
    plan["action"] = option.get("note", "manual installation required")
    return plan


def environment_capability_probe(params: dict[str, Any]) -> ToolResult:
    registry_path = str(params.get("registry_path") or REGISTRY_PATH)
    report = probe_capabilities(registry_path)
    artefacts: list[str] = []
    output = params.get("output")
    if output:
        target = Path(str(output))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        artefacts.append(target.as_posix())
    warnings = [
        f"{item['capability_id']} is blocked"
        for item in report["capabilities"]
        if item["status"] != "ready"
    ] + [f"{cid} is running on a fallback option" for cid in report["running_on_fallback"]]
    return ToolResult(
        success=report["blocked_count"] == 0,
        data=report,
        warnings=warnings,
        artefacts=artefacts,
        validation_status="validated" if report["blocked_count"] == 0 else "review_required",
    )
