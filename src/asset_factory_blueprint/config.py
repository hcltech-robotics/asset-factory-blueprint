from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


_INFERRED_ROOT = Path(__file__).resolve().parents[2]
ROOT = Path(os.environ.get("AFB_APPLIANCE_ROOT") or _INFERRED_ROOT).resolve(strict=False)
SOURCE_APPLIANCE_MARKERS = (
    "configs/agent-workflow.json",
    "configs/stage-contracts.json",
    "schemas/run-request.schema.json",
    "skills/asset-factory-orchestrator/SKILL.md",
)


def source_appliance_status() -> dict[str, Any]:
    missing = [relative for relative in SOURCE_APPLIANCE_MARKERS if not (ROOT / relative).is_file()]
    return {
        "root": str(ROOT),
        "ready": not missing,
        "missing": missing,
        "root_source": "AFB_APPLIANCE_ROOT" if os.environ.get("AFB_APPLIANCE_ROOT") else "inferred",
    }


def require_source_appliance() -> Path:
    status = source_appliance_status()
    if status["missing"]:
        raise RuntimeError(
            "the tagged source appliance is required but repository resources are unavailable; "
            "run from an extracted release checkout with an editable install or set AFB_APPLIANCE_ROOT "
            f"to that checkout; missing: {', '.join(status['missing'])}"
        )
    return ROOT


def repo_path(*parts: str) -> Path:
    return ROOT.joinpath(*parts)


def load_json(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.is_absolute():
        target = ROOT / target
    if not target.is_file():
        require_source_appliance()
        raise FileNotFoundError(f"repository JSON resource does not exist: {target}")
    return json.loads(target.read_text(encoding="utf-8"))


def write_json(path: str | Path, payload: Any) -> Path:
    target = Path(path)
    if not target.is_absolute():
        target = ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return target
