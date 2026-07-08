from __future__ import annotations

from pathlib import Path
from typing import Any

from asset_factory_blueprint.skills.base import ToolResult
from asset_factory_blueprint.utils.checksums import sha256_file


def asset_source_inspect(params: dict[str, Any]) -> ToolResult:
    sources = params.get("sources") or params.get("source_paths") or []
    records = []
    warnings: list[str] = []
    for raw in sources:
        path = Path(raw)
        record: dict[str, Any] = {
            "path": str(path),
            "exists": path.exists(),
            "suffix": path.suffix.lower(),
            "kind": "directory" if path.is_dir() else "file",
            "checksum": None,
            "size_bytes": None,
        }
        if path.is_file():
            record["checksum"] = sha256_file(path)
            record["size_bytes"] = path.stat().st_size
        if not path.exists():
            warnings.append(f"source path does not exist: {path}")
        records.append(record)
    status = "blocked" if warnings else "proposal"
    return ToolResult(
        success=not warnings,
        data={"sources": records, "normalisation_allowed": False, "source_assets_mutated": False},
        warnings=warnings,
        validation_status=status,
    )
