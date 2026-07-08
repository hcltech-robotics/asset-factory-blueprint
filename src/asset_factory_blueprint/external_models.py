from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asset_factory_blueprint.config import ROOT, load_json
from asset_factory_blueprint.execution import atomic_write_json
from asset_factory_blueprint.reconstruction_backends import ADAPTER_KIND, run_adapter_manifest
from asset_factory_blueprint.security import confine_path, external_io_roots


def list_models(config: str = "configs/external-models.json") -> list[dict[str, Any]]:
    return load_json(config).get("models", [])


def validate_config(config: str = "configs/external-models.json") -> list[str]:
    errors = []
    for index, item in enumerate(list_models(config)):
        if "command" in item and not isinstance(item["command"], list):
            errors.append(f"models[{index}].command must be an argument list")
        if not item.get("allowed_paths"):
            errors.append(f"models[{index}].allowed_paths is required")
    return errors


def run_manifest(manifest_path: str | Path, dry_run: bool = True) -> dict[str, Any]:
    path = Path(manifest_path)
    original = json.loads(path.read_text(encoding="utf-8"))
    if original.get("model_kind") == ADAPTER_KIND:
        return run_adapter_manifest(path, dry_run=dry_run)
    payload = dict(original)
    payload["status"] = "blocked"
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload.setdefault("logs_path", str(path.with_suffix(".log")))
    payload.setdefault("artefacts", [])
    payload["cancellable"] = True
    payload["input_manifest_mutated"] = False
    trusted_roots = external_io_roots(ROOT)
    for claimed_path in payload.get("allowed_paths", []):
        claimed = Path(claimed_path)
        if not claimed.is_absolute():
            claimed = ROOT / claimed
        confine_path(claimed, trusted_roots)
    output_roots = (*trusted_roots, path.resolve(strict=True).parent)
    confine_path(path, output_roots, must_exist=True)
    log_path = confine_path(Path(payload["logs_path"]), output_roots)
    blocked_reasons = list(payload.get("blocked_reasons", []))
    blocked_reasons.append(
        "generic external model manifests have no executable runner; use a registered adapter or endpoint runner"
    )
    payload["blocked_reasons"] = blocked_reasons
    payload["execution_status"] = "not_started"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        log_path,
        {
            "run_id": payload.get("run_id", payload.get("id")),
            "status": payload["status"],
            "dry_run": dry_run,
            "input_manifest": str(path),
            "input_manifest_mutated": False,
            "blocked_reasons": blocked_reasons,
        },
    )
    payload["log_written"] = log_path.as_posix()
    if not dry_run:
        result_path = path.with_name(f"{path.stem}.result.json")
        confine_path(result_path, output_roots)
        payload["result_manifest"] = result_path.as_posix()
        atomic_write_json(result_path, payload)
    return payload
