from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from asset_factory_blueprint.config import ROOT
from asset_factory_blueprint.execution import atomic_write_json
from asset_factory_blueprint.security import (
    confine_path,
    ensure_path_component,
    in_service_request,
    service_workspace_roots,
)
from asset_factory_blueprint.schemas.common import ProjectPaths
from asset_factory_blueprint.provenance import build_provenance
from asset_factory_blueprint.utils.ids import slugify


def project_paths(root: Path, slug: str) -> ProjectPaths:
    ensure_path_component(slug, "project ID")
    base = (root / slug).resolve(strict=False)
    root_resolved = root.resolve(strict=False)
    if base != root_resolved and root_resolved not in base.parents:
        raise ValueError("project path escapes the project root")
    return ProjectPaths(
        root=base,
        manifests=base / "manifests",
        evidence=base / "evidence",
        reports=base / "reports",
        snapshots=base / "snapshots",
        packaged=base / "packaged",
    )


def create_project(name: str, project_root: str | Path = "projects") -> dict:
    root = Path(project_root)
    if in_service_request():
        root = confine_path(root, service_workspace_roots(ROOT))
    slug = slugify(name)
    paths = project_paths(root, slug)
    for path in [paths.root, paths.manifests, paths.evidence, paths.reports, paths.snapshots, paths.packaged, paths.root / "runs"]:
        path.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    project_path = paths.root / "project.json"
    if project_path.exists():
        manifest = json.loads(project_path.read_text(encoding="utf-8"))
        manifest["name"] = name
        manifest["updated_at"] = now
    else:
        manifest = {
            "project_id": slug,
            "name": name,
            "created_at": now,
            "updated_at": now,
            "active_scene": "scene.usda",
            "policy_flags": {"review_required": True},
            "provenance": "provenance.json",
        }
    atomic_write_json(project_path, manifest)
    provenance_path = paths.root / "provenance.json"
    if not provenance_path.exists():
        atomic_write_json(provenance_path, build_provenance())
    scene_path = paths.root / "scene.usda"
    if not scene_path.exists():
        scene_path.write_text("#usda 1.0\n(defaultPrim = \"World\")\ndef Xform \"World\" {}\n", encoding="utf-8")
    return manifest


def open_project(slug: str, project_root: str | Path = "projects") -> dict:
    ensure_path_component(slug, "project ID")
    root = Path(project_root)
    if in_service_request():
        root = confine_path(root, service_workspace_roots(ROOT), must_exist=True)
    path = confine_path(root / slug / "project.json", (root.resolve(strict=False),), must_exist=True)
    return json.loads(path.read_text(encoding="utf-8"))


def list_projects(project_root: str | Path = "projects") -> list[str]:
    root = Path(project_root)
    if in_service_request():
        root = confine_path(root, service_workspace_roots(ROOT))
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if (path / "project.json").exists())


def snapshot_project(slug: str, name: str, project_root: str | Path = "projects") -> dict:
    ensure_path_component(slug, "project ID")
    manifest = open_project(slug, project_root)
    paths = project_paths(Path(project_root), slug)
    snapshot_id = slugify(name)
    record = {
        "snapshot_id": snapshot_id,
        "project_id": slug,
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_project_updated_at": manifest["updated_at"],
    }
    atomic_write_json(paths.snapshots / f"{snapshot_id}.json", record)
    return record
