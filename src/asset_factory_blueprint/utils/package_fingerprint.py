from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from asset_factory_blueprint.utils.checksums import sha256_file


def package_inventory_fingerprint(package_root: str | Path) -> dict[str, Any]:
    """Hash every regular package file by relative path and content digest."""

    root = Path(package_root).resolve(strict=False)
    blockers: list[str] = []
    records: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    if not root.is_dir():
        return {
            "status": "blocked",
            "fingerprint": "",
            "files": [],
            "blocked_reasons": ["package root is not a directory"],
        }
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if candidate.is_symlink() or bool(getattr(candidate, "is_junction", lambda: False)()):
            blockers.append(f"package contains a link or junction: {candidate.relative_to(root).as_posix()}")
            continue
        if not candidate.is_file():
            continue
        resolved = candidate.resolve(strict=True)
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError:
            blockers.append(f"package file escapes the package root: {candidate.as_posix()}")
            continue
        normalised = relative.casefold()
        if normalised in seen_paths:
            blockers.append(f"package contains a case-colliding path: {relative}")
            continue
        seen_paths.add(normalised)
        records.append({"path": relative, "sha256": sha256_file(resolved)})
    if not records:
        blockers.append("package inventory contains no regular files")
    digest = hashlib.sha256()
    for record in records:
        digest.update(record["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\n")
    return {
        "status": "pass" if not blockers else "blocked",
        "fingerprint": f"sha256:{digest.hexdigest()}" if records and not blockers else "",
        "files": records,
        "blocked_reasons": blockers,
    }
