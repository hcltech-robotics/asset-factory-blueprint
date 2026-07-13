from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asset_factory_blueprint.config import ROOT, load_json
from asset_factory_blueprint.execution import atomic_write_json, workspace_lease
from asset_factory_blueprint.security import (
    confine_path,
    ensure_path_component,
    external_io_roots,
    external_registry_roots,
)
from asset_factory_blueprint.utils.checksums import sha256_file, sha256_text
from asset_factory_blueprint.utils.ids import new_id


DEFAULT_REGISTRY = "configs/reconstruction-backends.json"
ADAPTER_KIND = "local-reconstruction-adapter"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_path(path: str | Path) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = ROOT / target
    return target


def _path_string(path: Path) -> str:
    return path.as_posix()


def _write_json_with_checksum(path: str | Path, payload: Any) -> tuple[Path, Path]:
    target = atomic_write_json(_resolve_path(path), payload)
    checksum_path = target.with_suffix(".sha256.json")
    atomic_write_json(
        checksum_path,
        {
            "algorithm": "sha256",
            "path": _path_string(target),
            "sha256": sha256_file(target),
        },
    )
    return target, checksum_path


def load_backend_registry(registry_path: str | Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    return load_json(registry_path)


def list_backend_specs(registry_path: str | Path = DEFAULT_REGISTRY) -> list[dict[str, Any]]:
    return list(load_backend_registry(registry_path).get("backends", []))


def _normalise_name(value: str) -> str:
    return value.lower().replace("_", "").replace("-", "").replace(".", "").replace(" ", "")


def resolve_backend(name: str, registry_path: str | Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    needle = _normalise_name(name)
    for spec in list_backend_specs(registry_path):
        names = [spec["id"], spec.get("display_name", ""), *spec.get("aliases", [])]
        if needle in {_normalise_name(item) for item in names if item}:
            return spec
    raise ValueError(f"unknown reconstruction backend: {name}")


def _candidate_roots(spec: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for env_name in spec.get("path_env_vars", []):
        value = os.environ.get(env_name, "")
        if value:
            candidates.append({"source": f"env:{env_name}", "path": value})
    for raw_path in spec.get("candidate_roots", []):
        candidates.append({"source": "registry", "path": raw_path})
    return candidates


def inspect_backend(backend: str, registry_path: str | Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    spec = resolve_backend(backend, registry_path)
    candidates = _candidate_roots(spec)
    checks: list[dict[str, Any]] = []
    selected_root = ""
    selected_source = ""
    blocked_reasons: list[str] = []
    for candidate in candidates:
        root = Path(candidate["path"])
        exists = root.exists()
        required = []
        if exists:
            required = [
                {
                    "path": _path_string(root / rel_path),
                    "exists": (root / rel_path).exists(),
                }
                for rel_path in spec.get("required_files", [])
            ]
        ready = exists and all(item["exists"] for item in required)
        checks.append(
            {
                "source": candidate["source"],
                "root": _path_string(root),
                "exists": exists,
                "required_files": required,
                "ready": ready,
            }
        )
        if ready and not selected_root:
            selected_root = _path_string(root)
            selected_source = candidate["source"]

    adapter_script = _resolve_path(spec.get("adapter_script", ""))
    adapter_ready = adapter_script.exists()
    weights = [
        {
            "path": _path_string(Path(raw_path)),
            "exists": Path(raw_path).exists(),
        }
        for raw_path in spec.get("weights_roots", [])
    ]
    if not selected_root:
        blocked_reasons.append(f"{spec['id']} backend root was not found with the required files")
    if not adapter_ready:
        blocked_reasons.append(f"{spec['id']} adapter script is not present")

    return {
        "backend_id": spec["id"],
        "display_name": spec.get("display_name", spec["id"]),
        "status": "ready" if selected_root and adapter_ready else "blocked",
        "selected_root": selected_root,
        "selected_source": selected_source,
        "adapter_script": _path_string(adapter_script),
        "adapter_script_ready": adapter_ready,
        "model_ref": spec.get("model_ref", ""),
        "candidate_roots": checks,
        "weights_roots": weights,
        "gpu_requirements": spec.get("gpu_requirements", {}),
        "blocked_reasons": blocked_reasons,
    }


def provision_backend(
    backend: str,
    registry_path: str | Path = DEFAULT_REGISTRY,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    spec = resolve_backend(backend, registry_path)
    status = inspect_backend(spec["id"], registry_path)
    report = {
        "id": f"{spec['id']}_backend_provision",
        "version": "1.0",
        "status": status["status"],
        "checked_at": _now(),
        "backend": {
            "backend_id": spec["id"],
            "display_name": spec.get("display_name", spec["id"]),
            "model_ref": spec.get("model_ref", ""),
            "provider": spec.get("provider", "local"),
        },
        "adapter": {
            "model_kind": ADAPTER_KIND,
            "runner": "scripts/reconstruction/run_reconstruction_backend.py",
            "adapter_script": spec.get("adapter_script", ""),
            "input_schema": "schemas/source-asset-manifest.schema.json",
            "output_schema": "schemas/reconstruction-manifest.schema.json",
        },
        "runtime": {
            "selected_root": status["selected_root"],
            "selected_source": status["selected_source"],
            "gpu_requirements": spec.get("gpu_requirements", {}),
            "timeout_seconds": spec.get("timeout_seconds", 0),
        },
        "checks": status,
        "blocked_reasons": status["blocked_reasons"],
        "artefacts": [],
    }
    if output_path:
        target = _resolve_path(output_path)
        report["report_path"] = _path_string(target)
        report["checksum_path"] = _path_string(target.with_suffix(".sha256.json"))
        report["artefacts"] = [
            {"kind": "provision_report", "uri": _path_string(target)},
            {"kind": "checksum", "uri": _path_string(target.with_suffix(".sha256.json"))},
        ]
        _write_json_with_checksum(target, report)
    return report


def _default_backend_output(backend_id: str) -> Path:
    return ROOT / "artifacts" / "reconstruction-backends" / backend_id / "external-model-run-manifest.json"


def build_backend_run_manifest(
    backend: str,
    output_path: str | Path | None = None,
    input_manifest: str = "projects/<project>/manifests/source-asset-manifest.json",
    output_manifest: str | None = None,
    registry_path: str | Path = DEFAULT_REGISTRY,
    asset_id: str = "",
    project_id: str = "",
) -> dict[str, Any]:
    spec = resolve_backend(backend, registry_path)
    status = inspect_backend(spec["id"], registry_path)
    target = _resolve_path(output_path) if output_path else _default_backend_output(spec["id"])
    output_manifest = output_manifest or target.with_name("reconstruction-manifest.json").as_posix()
    allowed_paths = list(spec.get("allowed_paths", ["projects", "artifacts", ".cache/afb"]))
    resolved_registry = _resolve_path(registry_path)
    log_path = target.with_suffix(".log")
    provision_path = target.with_name("provision-report.json")
    command = [
        "python",
        "scripts/reconstruction/run_reconstruction_backend.py",
        "--manifest",
        _path_string(target),
    ]
    payload = {
        "id": f"{spec['id']}_external_reconstruction",
        "version": "1.0",
        "status": "proposal" if status["status"] == "ready" else "blocked",
        "run_id": f"{spec['id']}_external_reconstruction",
        "asset_id": asset_id,
        "project_id": project_id,
        "model_id": spec["id"],
        "model_kind": ADAPTER_KIND,
        "input_manifest": input_manifest,
        "output_manifest": output_manifest,
        "input_schema": "schemas/source-asset-manifest.schema.json",
        "output_schema": "schemas/reconstruction-manifest.schema.json",
        "gpu_requirements": spec.get("gpu_requirements", {}),
        "runtime_env": {
            "AFB_ENV": os.environ.get("AFB_ENV", "local"),
            "AFB_RECONSTRUCTION_BACKEND": spec["id"],
            "AFB_RECONSTRUCTION_BACKEND_ROOT": status["selected_root"],
            "AFB_RECONSTRUCTION_REGISTRY": _path_string(resolved_registry),
        },
        "command_or_endpoint_redacted": command,
        "allowed_paths": allowed_paths,
        "registry_sha256": sha256_file(resolved_registry),
        "artefacts": [
            {"kind": "provision_report", "uri": _path_string(provision_path)},
            {"kind": "run_log", "uri": _path_string(log_path)},
            {"kind": "reconstruction_manifest", "uri": output_manifest},
        ],
        "logs_path": _path_string(log_path),
        "wandb_run_id": "",
        "evidence": [],
        "backend": {
            "backend_id": spec["id"],
            "display_name": spec.get("display_name", spec["id"]),
            "model_ref": spec.get("model_ref", ""),
            "adapter_script": spec.get("adapter_script", ""),
            "primary_output_name": spec.get("primary_output_name", "asset.glb"),
            "backbone": spec.get("backbone", {}),
        },
        "blocked_reasons": status["blocked_reasons"],
        "input_manifest_mutated": False,
        "cancellable": True,
    }
    payload["manifest_checksum"] = sha256_text(json.dumps(payload, sort_keys=True))
    written, checksum = _write_json_with_checksum(target, payload)
    payload["manifest_path"] = _path_string(written)
    payload["checksum_path"] = _path_string(checksum)
    return payload


def _backend_from_manifest(payload: dict[str, Any]) -> str:
    runtime_env = payload.get("runtime_env", {})
    backend = (
        payload.get("backend", {}).get("backend_id")
        or runtime_env.get("AFB_RECONSTRUCTION_BACKEND")
        or payload.get("model_id")
    )
    if not backend:
        raise ValueError("external reconstruction manifest does not name a backend")
    return str(backend)


def _format_native_command(
    spec: dict[str, Any],
    backend_root: str,
    input_asset: str,
    output_dir: Path,
    input_assets: list[str] | None = None,
) -> list[str]:
    values = {
        "backend_root": backend_root,
        "input_asset": input_asset,
        "output_dir": _path_string(output_dir),
        "model_ref": spec.get("model_ref", ""),
    }
    command: list[str] = []
    for item in spec.get("native_command", []):
        if item == "{input_assets}":
            command.extend(input_assets or ([input_asset] if input_asset else []))
            continue
        command.append(item.format(**values))
    # a bare "python" resolves to the spawning interpreter's own directory on
    # Windows, bypassing PATH and any venv, so it must be replaced explicitly:
    # env handle first, then the interpreter recorded at install time
    if command and command[0] == "python":
        recorded = Path(backend_root) / ".afb-interpreter"
        recorded_python = ""
        if recorded.exists():
            candidate = recorded.read_text(encoding="utf-8").strip()
            if candidate and Path(candidate).exists():
                recorded_python = candidate
        command[0] = os.environ.get("AFB_RECONSTRUCTION_PYTHON", "") or recorded_python or sys.executable
    return command


def trellis_reproducibility_arguments(payload: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    """Return the explicitly allowed TRELLIS controls recorded in a run manifest."""
    if spec.get("id") != "trellisv2":
        return []
    settings = payload.get("reproducibility", {})
    if not settings:
        return []
    if not isinstance(settings, dict):
        raise ValueError("reproducibility settings must be an object")

    command: list[str] = []
    seed = settings.get("seed")
    if seed is not None:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("TRELLIS reproducibility seed must be an integer")
        command.extend(["--seed", str(seed)])

    choices = {
        "pipeline_type": {"512", "1024", "1024_cascade", "1536_cascade"},
        "dtype": {"float32", "float16", "bfloat16"},
    }
    for key, allowed in choices.items():
        value = settings.get(key)
        if value is None:
            continue
        if value not in allowed:
            raise ValueError(f"unsupported TRELLIS reproducibility {key}: {value}")
        command.extend([f"--{key.replace('_', '-')}", value])

    positive_integer_flags = {
        "max_num_tokens": "--max-num-tokens",
        "decimation_target": "--decimation-target",
        "texture_size": "--texture-size",
    }
    for key, flag in positive_integer_flags.items():
        value = settings.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"TRELLIS reproducibility {key} must be a positive integer")
        command.extend([flag, str(value)])

    boolean_flags = {
        "deterministic": "--deterministic",
        "prune_unused_models": "--prune-unused-models",
        "cpu_image_cond": "--cpu-image-cond",
        "skip_preprocess": "--skip-preprocess",
    }
    for key, flag in boolean_flags.items():
        value = settings.get(key)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise ValueError(f"TRELLIS reproducibility {key} must be boolean")
        if value:
            command.append(flag)
    return command


def _run_native_backend(command: list[str], timeout_seconds: int, log_path: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=timeout_seconds or None,
            check=False,
        )
        native_log = {
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout[-6000:],
            "stderr": result.stderr[-6000:],
        }
    except subprocess.TimeoutExpired as exc:
        native_log = {
            "command": command,
            "returncode": -1,
            "timed_out": True,
            "timeout_seconds": timeout_seconds,
            "stdout": (exc.stdout or "")[-6000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-6000:] if isinstance(exc.stderr, str) else "",
        }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(native_log, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return native_log


def _optional_output_evidence(output_dir: Path) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    parts_manifest = output_dir / "parts-manifest.json"
    if parts_manifest.exists():
        evidence.append(
            {
                "evidence_id": "semantic_parts_manifest",
                "kind": "parts_manifest",
                "uri": _path_string(parts_manifest),
                "checksum": sha256_file(parts_manifest),
            }
        )
    return evidence


SUFFIX_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".avi": "video/avi",
}


def _inputs_from_source_manifest(
    manifest_ref: str,
    spec: dict[str, Any],
    trusted_roots: tuple[Path, ...] | None = None,
) -> list[str]:
    """Resolve backend inputs from the source manifest the run manifest cites,
    so a run needs no input env handles when the project already names its sources."""
    if not manifest_ref:
        return []
    manifest_path = _resolve_path(manifest_ref)
    if trusted_roots is not None:
        try:
            manifest_path = confine_path(manifest_path, trusted_roots, must_exist=True)
        except (OSError, ValueError):
            return []
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    project_dir = manifest_path.parent.parent
    accepted = set(spec.get("accepted_inputs", []))
    inputs: list[str] = []
    for record in manifest.get("source_assets", []):
        copy_path = str(record.get("project_copy_path") or "")
        if not copy_path:
            continue
        media_type = SUFFIX_MEDIA_TYPES.get(str(record.get("suffix") or Path(copy_path).suffix).lower(), "")
        if accepted and media_type not in accepted:
            continue
        resolved = project_dir / copy_path
        try:
            confined = confine_path(resolved, (project_dir.resolve(strict=False),), must_exist=True)
        except (OSError, ValueError):
            continue
        if confined.is_file():
            inputs.append(_path_string(confined))
    return inputs


def run_adapter_manifest(manifest_path: str | Path, dry_run: bool = True) -> dict[str, Any]:
    path = _resolve_path(manifest_path)
    original = json.loads(path.read_text(encoding="utf-8"))
    recorded_manifest_checksum = str(original.get("manifest_checksum") or "")
    checksum_payload = {key: value for key, value in original.items() if key != "manifest_checksum"}
    expected_manifest_checksum = sha256_text(json.dumps(checksum_payload, sort_keys=True))
    if not recorded_manifest_checksum or recorded_manifest_checksum != expected_manifest_checksum:
        raise ValueError("external reconstruction manifest checksum is missing or does not match")
    payload = dict(original)
    trusted_roots = external_io_roots(ROOT)
    for claimed_path in payload.get("allowed_paths", []):
        claimed = Path(claimed_path)
        if not claimed.is_absolute():
            claimed = ROOT / claimed
        confine_path(claimed, trusted_roots)
    output_roots = (*trusted_roots, path.resolve(strict=True).parent)
    confine_path(path, output_roots, must_exist=True)
    registry_path = _resolve_path(payload.get("runtime_env", {}).get("AFB_RECONSTRUCTION_REGISTRY", DEFAULT_REGISTRY))
    confine_path(registry_path, external_registry_roots(ROOT), must_exist=True)
    expected_registry_sha256 = str(payload.get("registry_sha256") or "")
    if not expected_registry_sha256 or expected_registry_sha256 != sha256_file(registry_path):
        raise ValueError("reconstruction registry checksum is missing or does not match the pinned registry")
    backend_id = _backend_from_manifest(payload)
    spec = resolve_backend(backend_id, registry_path)
    log_path = _resolve_path(payload.get("logs_path", path.with_suffix(".log")))
    output_manifest_path = _resolve_path(
        payload.get("output_manifest", log_path.with_name("reconstruction-manifest.json"))
    )
    provision_path = log_path.with_suffix(".provision.json")
    output_dir = output_manifest_path.with_suffix("").parent / "outputs"
    confine_path(log_path, output_roots)
    confine_path(output_manifest_path, output_roots)
    confine_path(provision_path, output_roots)
    confine_path(output_dir, output_roots)
    input_asset = (
        payload.get("input_asset")
        or payload.get("runtime_env", {}).get("AFB_RECONSTRUCTION_INPUT_ASSET", "")
        or os.environ.get("AFB_RECONSTRUCTION_INPUT_ASSET", "")
    )
    input_assets = [str(item) for item in payload.get("input_assets") or []]
    if not input_assets:
        raw_multi = payload.get("runtime_env", {}).get("AFB_RECONSTRUCTION_INPUT_ASSETS", "") or os.environ.get(
            "AFB_RECONSTRUCTION_INPUT_ASSETS", ""
        )
        input_assets = [item for item in raw_multi.split(os.pathsep) if item]
    if not input_asset and not input_assets:
        input_assets = _inputs_from_source_manifest(payload.get("input_manifest", ""), spec, trusted_roots)
    if not input_asset and input_assets:
        input_asset = input_assets[0]
    if input_asset:
        input_asset = _path_string(confine_path(_resolve_path(input_asset), trusted_roots, must_exist=True))
    input_assets = [
        _path_string(confine_path(_resolve_path(item), trusted_roots, must_exist=True)) for item in input_assets
    ]

    provision = provision_backend(spec["id"], registry_path=registry_path, output_path=provision_path)
    blocked_reasons = list(provision.get("blocked_reasons", []))
    native_log: dict[str, Any] | None = None
    generated_asset = output_dir / spec.get("primary_output_name", "asset.glb")
    status = "blocked"
    execution_status = "not_started"
    command: list[str] = []

    if dry_run:
        blocked_reasons.append("dry run requested; backend execution was not started")
    elif provision["status"] != "ready":
        blocked_reasons.append(f"{spec['id']} backend is not ready")
    elif not input_asset:
        blocked_reasons.append("input asset path is required for backend execution")
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        command = _format_native_command(
            spec, provision["runtime"]["selected_root"], input_asset, output_dir, input_assets or None
        )
        command.extend(trellis_reproducibility_arguments(payload, spec))
        native_log = _run_native_backend(command, int(spec.get("timeout_seconds", 0)), log_path)
        if native_log["returncode"] == 0 and generated_asset.exists():
            status = "proposal"
            execution_status = "completed"
        else:
            execution_status = "blocked"
            blocked_reasons.append(f"{spec['id']} backend command did not produce {generated_asset.name}")

    if native_log is None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(
                {
                    "run_id": payload.get("run_id", payload.get("id")),
                    "status": status,
                    "dry_run": dry_run,
                    "backend_id": spec["id"],
                    "input_manifest": payload.get("input_manifest", ""),
                    "output_manifest": _path_string(output_manifest_path),
                    "input_asset": input_asset,
                    "input_manifest_mutated": False,
                    "execution_status": execution_status,
                    "blocked_reasons": blocked_reasons,
                },
                indent=2,
                sort_keys=False,
            )
            + "\n",
            encoding="utf-8",
        )

    evidence = [
        {
            "evidence_id": "provision_report",
            "kind": "provision_report",
            "uri": _path_string(provision_path),
            "checksum": sha256_file(provision_path),
        },
        {
            "evidence_id": "run_log",
            "kind": "run_log",
            "uri": _path_string(log_path),
            "checksum": sha256_file(log_path),
        },
    ]
    if generated_asset.exists():
        evidence.append(
            {
                "evidence_id": "generated_reconstruction_asset",
                "kind": "model_gltf_binary",
                "uri": _path_string(generated_asset),
                "checksum": sha256_file(generated_asset),
            }
        )
    evidence.extend(_optional_output_evidence(output_dir))

    parts_manifest = output_dir / "parts-manifest.json"
    part_outputs: dict[str, Any] = {}
    if parts_manifest.exists():
        try:
            parts_payload = json.loads(parts_manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            parts_payload = {}
        part_outputs = {
            "parts_manifest": _path_string(parts_manifest),
            "semantic_part_count": len(parts_payload.get("parts", [])) if isinstance(parts_payload, dict) else 0,
            "backbone": parts_payload.get("backbone", "") if isinstance(parts_payload, dict) else "",
        }

    reconstruction = {
        "id": f"{payload.get('run_id', spec['id'])}_reconstruction",
        "version": "1.0",
        "status": status,
        "asset_id": payload.get("asset_id", ""),
        "project_id": payload.get("project_id", ""),
        "evidence": evidence,
        "provider_trace": [
            {
                "provider": "local",
                "model": spec.get("model_ref", spec["id"]),
                "role": "reconstruction",
                "prompt_checksum": "not_prompted",
            }
        ],
        "review_status": "review_required",
        "backend": {
            "backend_id": spec["id"],
            "model_ref": spec.get("model_ref", ""),
            "selected_root": provision["runtime"]["selected_root"],
            "adapter_script": spec.get("adapter_script", ""),
            "backbone": spec.get("backbone", {}),
        },
        "input_manifest": payload.get("input_manifest", ""),
        "input_asset": input_asset,
        "input_assets": input_assets,
        "output_dir": _path_string(output_dir),
        "generated_asset": _path_string(generated_asset) if generated_asset.exists() else "",
        "validation_status": status,
        "blocked_reasons": blocked_reasons,
        "reproducibility": payload.get("reproducibility", {}),
    }
    reconstruction.update(part_outputs)
    _write_json_with_checksum(output_manifest_path, reconstruction)

    payload["status"] = status
    payload["updated_at"] = _now()
    payload["logs_path"] = _path_string(log_path)
    payload["log_written"] = _path_string(log_path)
    payload["artefacts"] = [
        {"kind": "provision_report", "uri": _path_string(provision_path)},
        {"kind": "run_log", "uri": _path_string(log_path)},
        {"kind": "reconstruction_manifest", "uri": _path_string(output_manifest_path)},
        {"kind": "reconstruction_checksum", "uri": _path_string(output_manifest_path.with_suffix(".sha256.json"))},
    ]
    if parts_manifest.exists():
        payload["artefacts"].append({"kind": "parts_manifest", "uri": _path_string(parts_manifest)})
    payload["input_manifest_mutated"] = False
    payload["cancellable"] = True
    payload["blocked_reasons"] = blocked_reasons
    payload["execution_status"] = execution_status
    payload["native_command_redacted"] = command
    if execution_status == "completed":
        payload["project_landing"] = _land_run_in_project(
            payload,
            generated_asset,
            output_dir,
            input_asset,
            trusted_roots=trusted_roots,
            output_roots=output_roots,
        )
    if not dry_run:
        result_path = confine_path(path.with_name(f"{path.stem}.result.json"), output_roots)
        payload["result_manifest"] = _path_string(result_path)
        atomic_write_json(result_path, payload)
    return payload


def _land_run_in_project(
    payload: dict[str, Any],
    generated_asset: Path,
    output_dir: Path,
    input_asset: str,
    *,
    trusted_roots: tuple[Path, ...],
    output_roots: tuple[Path, ...],
) -> dict[str, Any]:
    """Land a completed run in the project workspace the input manifest belongs
    to: the mesh, preview renders and the run manifest itself, so the workflow
    can lift the reconstruction blocker without any manual copying."""
    manifest_ref = str(payload.get("input_manifest") or "")
    try:
        manifest_path = confine_path(_resolve_path(manifest_ref), trusted_roots, must_exist=True)
    except (OSError, ValueError) as exc:
        return {"status": "blocked", "reason": f"input manifest is not authorised: {exc}"}
    if manifest_path.parent.name != "manifests":
        return {"status": "blocked", "reason": "input manifest is not in a project manifests directory"}
    project_dir = manifest_path.parent.parent.resolve(strict=True)
    if not (project_dir / "project.json").is_file():
        return {"status": "blocked", "reason": "input manifest project has no project.json"}
    confine_path(project_dir, trusted_roots, must_exist=True)
    raw_asset_id = str(payload.get("asset_id") or project_dir.name)
    try:
        asset_id = ensure_path_component(raw_asset_id, "asset ID")
        generated_asset = confine_path(generated_asset, output_roots, must_exist=True)
        output_dir = confine_path(output_dir, output_roots, must_exist=True)
        if input_asset:
            input_asset = _path_string(confine_path(_resolve_path(input_asset), trusted_roots, must_exist=True))
    except (OSError, ValueError) as exc:
        return {"status": "blocked", "reason": str(exc)}

    with workspace_lease(project_dir, new_id("landing")):
        landing: dict[str, Any] = {"status": "landed", "project_dir": _path_string(project_dir)}
        asset_dir = confine_path(project_dir / "assets" / asset_id, (project_dir,))
        asset_dir.mkdir(parents=True, exist_ok=True)
        mesh_target = confine_path(asset_dir / generated_asset.name, (asset_dir,))
        shutil.copy2(generated_asset, mesh_target)
        landing["mesh_path"] = _path_string(mesh_target)

        renders_dir = confine_path(asset_dir / "renders", (asset_dir,))
        render_command = [
            sys.executable,
            _path_string(ROOT / "scripts" / "reconstruction" / "render_glb_mesh_preview.py"),
            "--parts-dir",
            _path_string(output_dir),
            "--output-dir",
            _path_string(renders_dir),
        ]
        if input_asset:
            render_command.extend(["--source-image", input_asset])
        render = subprocess.run(render_command, capture_output=True, text=True, timeout=1800, check=False)
        if render.returncode == 0:
            landing["renders_dir"] = _path_string(renders_dir)
        else:
            landing["render_warning"] = (render.stderr or render.stdout)[-400:]

        record_path = confine_path(project_dir / "manifests" / "external-model-run-manifest.json", (project_dir,))
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record = {key: value for key, value in payload.items() if key != "project_landing"}
        record["provenance"] = {
            "manifest_ids": [str(record.get("id") or "external-model-run"), "source-asset-manifest"],
            "recorded_by": "external-models run project landing",
            "input_manifest": manifest_ref,
            "generated_asset_sha256": sha256_file(mesh_target),
        }
        atomic_write_json(record_path, record)
        landing["run_manifest_path"] = _path_string(record_path)
        return landing
