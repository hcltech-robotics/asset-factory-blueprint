from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from asset_factory_blueprint.config import ROOT
from asset_factory_blueprint.utils.checksums import sha256_file
from asset_factory_blueprint.utils.ids import content_id


PROVENANCE_SCHEMA_VERSION = "2.0"
NOT_RECORDED = "not_recorded"


def _git(args: list[str]) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)
    if result.returncode:
        return ""
    return result.stdout.strip()


def _safe_version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "not_installed"


def _first_version(*packages: str) -> str:
    for package in packages:
        version = _safe_version(package)
        if version != "not_installed":
            return version
    return "not_installed"


def _env(name: str, default: str = NOT_RECORDED) -> str:
    return os.environ.get(name, "").strip() or default


def _file_checksums(paths: list[Path]) -> dict[str, str]:
    checksums = {}
    for path in paths:
        if path.exists() and path.is_file():
            checksums[path.relative_to(ROOT).as_posix()] = sha256_file(path)
    return checksums


def _workspace_digest() -> str:
    digest = hashlib.sha256()
    for path in (ROOT / "pyproject.toml", ROOT / "uv.lock"):
        if path.is_file():
            digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
            digest.update(sha256_file(path).encode("utf-8"))
    roots = ["src", "schemas", "configs", "skills", "scripts", "docs"]
    for root_name in roots:
        root = ROOT / root_name
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if any(part in {"__pycache__", ".pytest_cache"} for part in path.parts):
                continue
            digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
            digest.update(sha256_file(path).encode("utf-8"))
    return digest.hexdigest()


def _tool_versions() -> dict[str, str]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "asset_factory_blueprint": _safe_version("asset-factory-blueprint"),
        "jsonschema": _safe_version("jsonschema"),
        "pydantic": _safe_version("pydantic"),
        "pytest": _safe_version("pytest"),
        "openusd": _first_version("usd-core", "openusd"),
        "materialx": _first_version("MaterialX", "materialx"),
        "isaac_sim": _first_version("isaacsim", "isaac-sim"),
    }


def _environment_bom(tool_versions: dict[str, str]) -> dict[str, Any]:
    uname = platform.uname()
    return {
        "operating_system": {
            "system": uname.system or NOT_RECORDED,
            "release": uname.release or NOT_RECORDED,
            "version": uname.version or NOT_RECORDED,
        },
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "abi": getattr(sys.implementation, "cache_tag", None) or NOT_RECORDED,
            "packages": {
                key: value
                for key, value in tool_versions.items()
                if key not in {"python", "platform"}
            },
        },
        "hardware": {
            "architecture": platform.machine() or NOT_RECORDED,
            "processor": platform.processor() or NOT_RECORDED,
            "gpu_vendor": _env("AFB_GPU_VENDOR"),
            "gpu_model": _env("AFB_GPU_MODEL"),
            "gpu_count": _env("AFB_GPU_COUNT"),
            "memory_bytes": _env("AFB_MEMORY_BYTES"),
        },
        "accelerator": {
            "cuda_version": _env("CUDA_VERSION"),
            "driver_version": _env("NVIDIA_DRIVER_VERSION"),
            "compute_capability": _env("AFB_CUDA_COMPUTE_CAPABILITY"),
        },
        "container": {
            "image": _env("AFB_CONTAINER_IMAGE"),
            "digest": _env("AFB_CONTAINER_DIGEST"),
            "runtime": _env("AFB_CONTAINER_RUNTIME"),
        },
        "simulation": {
            "openusd_version": _env("AFB_OPENUSD_VERSION", tool_versions["openusd"]),
            "isaac_sim_version": _env("AFB_ISAAC_SIM_VERSION", tool_versions["isaac_sim"]),
            "materialx_version": _env("AFB_MATERIALX_VERSION", tool_versions["materialx"]),
            "renderer": _env("AFB_RENDERER"),
            "physics_backend": _env("AFB_PHYSICS_BACKEND"),
        },
        "extensions": {},
    }


def _model_bom(provider_model_ids: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for role, raw_value in sorted(provider_model_ids.items()):
        value = raw_value if isinstance(raw_value, dict) else {"model_id": str(raw_value)}
        records.append(
            {
                "role": role,
                "provider": str(value.get("provider") or NOT_RECORDED),
                "kind": str(value.get("kind") or NOT_RECORDED),
                "model_id": str(value.get("model_id") or "unresolved"),
                "revision": str(
                    value.get("revision")
                    or value.get("model_revision")
                    or value.get("commit")
                    or NOT_RECORDED
                ),
                "weights_checksum": str(
                    value.get("weights_checksum") or value.get("weights_sha256") or NOT_RECORDED
                ),
                "licence_expression": str(
                    value.get("licence_expression") or value.get("license_expression") or "NOASSERTION"
                ),
                "resolution_status": str(
                    value.get("model_resolution_status") or value.get("resolution_status") or "unresolved"
                ),
                "runtime": str(value.get("runtime") or NOT_RECORDED),
                "extensions": {
                    "model_env": str(value.get("model_env") or ""),
                    "blocked_reason": str(value.get("blocked_reason") or ""),
                },
            }
        )
    return records


def build_provenance(
    manifest_ids: list[str] | None = None,
    provider_model_ids: dict[str, Any] | None = None,
    source_assets: list[dict[str, Any]] | None = None,
    source_assets_mutated: bool = False,
    *,
    run_id: str | None = None,
    attempt_ids: list[str] | None = None,
    random_seeds: dict[str, int] | None = None,
) -> dict[str, Any]:
    git_top = _git(["rev-parse", "--show-toplevel"])
    git_sha = _git(["rev-parse", "HEAD"]) if git_top else ""
    git_dirty = bool(_git(["status", "--short"])) if git_top else False
    prompt_paths = sorted((ROOT / "src" / "asset_factory_blueprint" / "prompts").glob("*.md"))
    config_paths = [
        ROOT / "configs" / "agent-workflow.json",
        ROOT / "configs" / "provider-policy.json",
        ROOT / "configs" / "skill-registry.json",
        ROOT / "configs" / "validation-gates.json",
        ROOT / "configs" / "texture-defaults.json",
        ROOT / "configs" / "external-models.json",
        ROOT / "configs" / "stage-contracts.json",
        ROOT / "configs" / "runtime-config.example.json",
    ]
    model_handles = provider_model_ids or {}
    tool_versions = _tool_versions()
    lock_path = ROOT / "uv.lock"
    dependency_lock = {
        "status": "locked" if lock_path.is_file() else "missing",
        "path": "uv.lock",
        "sha256": sha256_file(lock_path) if lock_path.is_file() else "",
        "resolver": "uv",
    }
    record_core = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "run_id": run_id,
        "attempt_ids": sorted(attempt_ids or []),
        "repository": {
            "git_sha": git_sha or "unavailable",
            "git_dirty": git_dirty,
            "git_state": "git" if git_sha else "no_git_repository",
            "workspace_digest": _workspace_digest(),
            "dependency_lock": dependency_lock,
        },
        "environment_bom": _environment_bom(tool_versions),
        "model_bom": _model_bom(model_handles),
        "prompt_checksums": _file_checksums(prompt_paths),
        "config_checksums": _file_checksums(config_paths),
        "manifest_ids": sorted(manifest_ids or []),
        "source_assets": source_assets or [],
        "source_assets_mutated": source_assets_mutated,
        "reproducibility": {
            "random_seeds": random_seeds or {},
            "entrypoint": Path(sys.argv[0]).name or NOT_RECORDED,
            "secret_policy": "environment variables only",
        },
    }
    created_at = datetime.now(timezone.utc).isoformat()
    return {
        "provenance_id": content_id("prov", record_core, digest_length=32),
        "created_at": created_at,
        **record_core,
        # Compatibility views retained for v1 consumers.
        "tool_versions": tool_versions,
        "provider_model_ids": model_handles,
        "secret_policy": "environment variables only",
        "extensions": {},
    }


def provenance_to_jsonld(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the native record into a compact W3C PROV-O JSON-LD graph."""

    def urn(kind: str, value: Any) -> str:
        return f"urn:afb:{kind}:{quote(str(value or 'unresolved'), safe='._-')}"

    run_id = str(payload.get("run_id") or payload.get("provenance_id") or "unresolved")
    run_urn = urn("run", run_id)
    software_urn = urn("software", "asset-factory-blueprint")
    dependency_lock = payload.get("repository", {}).get("dependency_lock") or {}
    lock_node = (
        {
            "@id": urn("dependency-lock", dependency_lock.get("sha256")),
            "@type": "prov:Entity",
            "prov:label": str(dependency_lock.get("path") or "uv.lock"),
            "afb:sha256": str(dependency_lock.get("sha256") or ""),
            "afb:resolver": str(dependency_lock.get("resolver") or "uv"),
        }
        if dependency_lock.get("status") == "locked" and dependency_lock.get("sha256")
        else None
    )
    source_nodes: list[dict[str, Any]] = []
    source_refs: list[dict[str, str]] = []
    for index, source in enumerate(payload.get("source_assets") or []):
        source_id = str(source.get("source_id") or source.get("id") or f"source_{index}")
        source_urn = urn("source", source_id)
        node: dict[str, Any] = {
            "@id": source_urn,
            "@type": "prov:Entity",
            "prov:label": source_id,
        }
        checksum = source.get("copy_sha256") or source.get("sha256")
        if checksum:
            node["afb:sha256"] = str(checksum).removeprefix("sha256:")
        source_nodes.append(node)
        source_refs.append({"@id": source_urn})
    manifest_nodes = [
        {
            "@id": urn("manifest", manifest_id),
            "@type": "prov:Entity",
            "prov:label": str(manifest_id),
            "prov:wasGeneratedBy": {"@id": run_urn},
        }
        for manifest_id in payload.get("manifest_ids") or []
    ]
    model_nodes = []
    model_refs = []
    for index, model in enumerate(payload.get("model_bom") or []):
        model_id = str(model.get("model_id") or f"unresolved_{index}")
        model_urn = urn("model", f"{model.get('provider', 'unknown')}:{model_id}")
        model_nodes.append(
            {
                "@id": model_urn,
                "@type": ["prov:Agent", "prov:SoftwareAgent"],
                "prov:label": model_id,
                "afb:revision": str(model.get("revision") or "not_recorded"),
                "afb:licenceExpression": str(model.get("licence_expression") or "NOASSERTION"),
            }
        )
        model_refs.append({"@id": model_urn})
    attempt_nodes = [
        {
            "@id": urn("attempt", attempt_id),
            "@type": "prov:Activity",
            "prov:label": str(attempt_id),
            "prov:wasInformedBy": {"@id": run_urn},
            "prov:wasAssociatedWith": {"@id": software_urn},
        }
        for attempt_id in payload.get("attempt_ids") or []
    ]
    run_node: dict[str, Any] = {
        "@id": run_urn,
        "@type": "prov:Activity",
        "prov:label": run_id,
        "prov:generated": [{"@id": node["@id"]} for node in manifest_nodes],
        "prov:used": [*source_refs, *([{"@id": lock_node["@id"]}] if lock_node else [])],
        "prov:wasAssociatedWith": [{"@id": software_urn}, *model_refs],
        "afb:provenanceId": str(payload.get("provenance_id") or ""),
        "afb:workspaceDigest": str(payload.get("repository", {}).get("workspace_digest") or ""),
    }
    if payload.get("created_at"):
        run_node["prov:endedAtTime"] = {
            "@value": str(payload["created_at"]),
            "@type": "http://www.w3.org/2001/XMLSchema#dateTime",
        }
    return {
        "@context": {
            "prov": "http://www.w3.org/ns/prov#",
            "afb": "https://hcltech-robotics.github.io/asset-factory-blueprint/ns#",
        },
        "@graph": [
            {
                "@id": software_urn,
                "@type": ["prov:Agent", "prov:SoftwareAgent"],
                "prov:label": "asset-factory-blueprint",
                "afb:version": str(payload.get("tool_versions", {}).get("asset_factory_blueprint") or "not_recorded"),
            },
            run_node,
            *([lock_node] if lock_node else []),
            *source_nodes,
            *manifest_nodes,
            *model_nodes,
            *attempt_nodes,
        ],
    }


def write_prov_jsonld(path: str | Path, payload: dict[str, Any], *, immutable: bool = False) -> Path:
    """Write a W3C PROV-O JSON-LD projection, optionally refusing replacement."""

    target = Path(path)
    if immutable and target.exists():
        raise FileExistsError(f"refusing to overwrite immutable PROV-O record: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(provenance_to_jsonld(payload), indent=2, sort_keys=False, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def write_provenance(
    path: str | Path,
    manifest_ids: list[str] | None = None,
    provider_model_ids: dict[str, Any] | None = None,
    source_assets: list[dict[str, Any]] | None = None,
    source_assets_mutated: bool = False,
    *,
    run_id: str | None = None,
    attempt_ids: list[str] | None = None,
    random_seeds: dict[str, int] | None = None,
) -> dict[str, Any]:
    payload = build_provenance(
        manifest_ids,
        provider_model_ids,
        source_assets,
        source_assets_mutated,
        run_id=run_id,
        attempt_ids=attempt_ids,
        random_seeds=random_seeds,
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing.get("provenance_id") == payload["provenance_id"]:
            return existing
        raise FileExistsError(f"refusing to overwrite immutable provenance record: {target}")
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    temporary.replace(target)
    return payload
