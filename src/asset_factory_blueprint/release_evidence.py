from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import tomllib
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asset_factory_blueprint import __version__
from asset_factory_blueprint.config import ROOT
from asset_factory_blueprint.execution import atomic_write_json
from asset_factory_blueprint.utils.checksums import sha256_file


_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")
_PUBLIC_URL = re.compile(r"(?i)\b(?:https?|s3|hf)://[^\s\"'<>]+")
_FILE_URI = re.compile(r"(?i)(?<![A-Za-z0-9])file:(?://|[\\/])")
_WINDOWS_ABSOLUTE = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
_UNC_ABSOLUTE = re.compile(
    r"(?i)(?<![A-Za-z0-9\\/])(?:\\\\[^\\/\s]+[\\/][^\\/\s]+|//[^/\s]+/[^/\s]+)"
)
_POSIX_ABSOLUTE = re.compile(
    r"(?<![A-Za-z0-9:#/])/(?!/)[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~@+-]+)*"
)
_SECRET_KEY = re.compile(r"(?:api[_-]?key|password|secret|token|credential)", re.IGNORECASE)
_SCHEMA_VERSION = re.compile(r"/schemas/(?P<version>v[1-9][0-9]*(?:\.[0-9]+)*)/")
_DOCKER_BASE_IMAGE = re.compile(r"(?m)^ARG PYTHON_IMAGE=(?P<reference>\S+)\s*$")


def _normalise_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _requirement_name(value: str) -> str:
    match = _NAME_PATTERN.match(value.strip())
    return _normalise_name(match.group(0)) if match else ""


def _git(args: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    return result.returncode == 0, result.stdout.strip()


def _repository_state() -> dict[str, Any]:
    commit_ok, commit = _git(["rev-parse", "HEAD"])
    status_ok, status = _git(["status", "--short"])
    commit_valid = commit_ok and re.fullmatch(r"[A-Fa-f0-9]{40,64}", commit) is not None
    cleanliness = "unknown" if not commit_valid or not status_ok else "dirty" if status else "clean"
    return {
        "commit": commit if commit_valid else "unavailable",
        "cleanliness": cleanliness,
        "clean": True if cleanliness == "clean" else False if cleanliness == "dirty" else None,
    }


def _metadata_sha256(distribution: importlib.metadata.Distribution) -> str:
    metadata = distribution.read_text("METADATA") or ""
    return hashlib.sha256(metadata.encode("utf-8")).hexdigest() if metadata else ""


def _licences(distribution: importlib.metadata.Distribution) -> list[dict[str, Any]]:
    expression = str(distribution.metadata.get("License-Expression") or "").strip()
    if expression:
        return [{"expression": expression}]
    name = str(distribution.metadata.get("License") or "NOASSERTION").strip() or "NOASSERTION"
    return [{"license": {"name": name}}]


def _component(distribution: importlib.metadata.Distribution) -> dict[str, Any]:
    name = str(distribution.metadata.get("Name") or "unknown")
    version = str(distribution.version or "unknown")
    component: dict[str, Any] = {
        "type": "library",
        "bom-ref": f"pkg:pypi/{_normalise_name(name)}@{version}",
        "name": name,
        "version": version,
        "purl": f"pkg:pypi/{_normalise_name(name)}@{version}",
        "licenses": _licences(distribution),
    }
    digest = _metadata_sha256(distribution)
    if digest:
        component["hashes"] = [{"alg": "SHA-256", "content": digest}]
    homepage = str(distribution.metadata.get("Home-page") or "").strip()
    if homepage.startswith(("https://", "http://")):
        component["externalReferences"] = [{"type": "website", "url": homepage}]
    return component


def _locked_ref(package: dict[str, Any]) -> str:
    return f"pkg:pypi/{_normalise_name(str(package['name']))}@{package['version']}"


def _locked_dependencies(
    dependency_records: list[dict[str, Any]],
    packages_by_name: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    resolved = []
    for dependency in dependency_records:
        name = _normalise_name(str(dependency.get("name") or ""))
        candidates = packages_by_name.get(name, [])
        version = str(dependency.get("version") or "")
        if version:
            candidates = [package for package in candidates if str(package.get("version")) == version]
        resolved.extend(candidates)
    return resolved


def _build_locked_sbom(lock_path: Path) -> dict[str, Any]:
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = [package for package in lock.get("package", []) if isinstance(package, dict)]
    root = next(
        (
            package
            for package in packages
            if _normalise_name(str(package.get("name") or "")) == "asset-factory-blueprint"
        ),
        None,
    )
    if root is None:
        raise ValueError("uv.lock does not contain the asset-factory-blueprint root package")
    packages_by_name: dict[str, list[dict[str, Any]]] = {}
    for package in packages:
        packages_by_name.setdefault(_normalise_name(str(package.get("name") or "")), []).append(package)
    runtime_keys: set[tuple[str, str]] = set()
    queue = _locked_dependencies(list(root.get("dependencies") or []), packages_by_name)
    while queue:
        package = queue.pop()
        key = (_normalise_name(str(package.get("name") or "")), str(package.get("version") or ""))
        if key in runtime_keys:
            continue
        runtime_keys.add(key)
        queue.extend(_locked_dependencies(list(package.get("dependencies") or []), packages_by_name))

    installed = {
        _normalise_name(str(distribution.metadata.get("Name") or "")): distribution
        for distribution in importlib.metadata.distributions()
        if distribution.metadata.get("Name")
    }
    component_packages = [package for package in packages if package is not root]
    components = []
    for package in sorted(
        component_packages,
        key=lambda item: (_normalise_name(str(item.get("name") or "")), str(item.get("version") or "")),
    ):
        name = str(package["name"])
        version = str(package["version"])
        normalised = _normalise_name(name)
        ref = _locked_ref(package)
        distribution = installed.get(normalised)
        licences = (
            _licences(distribution)
            if distribution is not None and str(distribution.version) == version
            else [{"license": {"name": "NOASSERTION"}}]
        )
        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": ref,
            "name": name,
            "version": version,
            "purl": ref,
            "scope": "required" if (normalised, version) in runtime_keys else "optional",
            "licenses": licences,
            "properties": [],
        }
        source = package.get("source") or {}
        if isinstance(source, dict) and source.get("registry"):
            component["externalReferences"] = [{"type": "distribution", "url": str(source["registry"])}]
        source_distribution = package.get("sdist") or {}
        source_hash = str(source_distribution.get("hash") or "") if isinstance(source_distribution, dict) else ""
        if source_hash.startswith("sha256:"):
            component["hashes"] = [{"alg": "SHA-256", "content": source_hash.removeprefix("sha256:")}]
        markers = sorted(
            {
                str(dependency.get("marker"))
                for dependency in package.get("dependencies") or []
                if isinstance(dependency, dict) and dependency.get("marker")
            }
        )
        if markers:
            component["properties"].append({"name": "afb:dependency-markers", "value": " | ".join(markers)})
        if not component["properties"]:
            component.pop("properties")
        components.append(component)

    application_ref = f"pkg:pypi/asset-factory-blueprint@{__version__}"
    dependencies = []
    root_dependencies = list(root.get("dependencies") or [])
    for group in (root.get("optional-dependencies") or {}).values():
        root_dependencies.extend(group or [])
    dependencies.append(
        {
            "ref": application_ref,
            "dependsOn": sorted({_locked_ref(package) for package in _locked_dependencies(root_dependencies, packages_by_name)}),
        }
    )
    for package in component_packages:
        dependencies.append(
            {
                "ref": _locked_ref(package),
                "dependsOn": sorted(
                    {
                        _locked_ref(dependency)
                        for dependency in _locked_dependencies(
                            list(package.get("dependencies") or []),
                            packages_by_name,
                        )
                    }
                ),
            }
        )
    dependencies.sort(key=lambda item: item["ref"])
    lock_sha256 = sha256_file(lock_path)
    identity = json.dumps(
        {"lock_sha256": lock_sha256, "components": components, "dependencies": dependencies},
        sort_keys=True,
        separators=(",", ":"),
    )
    timestamp = datetime.now(timezone.utc)
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if source_date_epoch:
        timestamp = datetime.fromtimestamp(int(source_date_epoch), tz=timezone.utc)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, identity)}",
        "version": 1,
        "metadata": {
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "asset-factory-blueprint SBOM generator",
                        "version": __version__,
                    }
                ]
            },
            "component": {
                "type": "application",
                "bom-ref": application_ref,
                "name": "asset-factory-blueprint",
                "version": __version__,
                "purl": application_ref,
                "licenses": [{"license": {"id": "MIT"}}],
            },
            "properties": [
                {"name": "afb:resolution", "value": "uv.lock"},
                {"name": "afb:dependency-lock-sha256", "value": lock_sha256},
                {"name": "afb:requires-python", "value": str(lock.get("requires-python") or "")},
            ],
        },
        "components": components,
        "dependencies": dependencies,
    }


def _declared_dependencies() -> list[str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return [
        name
        for requirement in pyproject.get("project", {}).get("dependencies", [])
        if (name := _requirement_name(str(requirement)))
    ]


def build_cyclonedx_sbom() -> dict[str, Any]:
    """Build a CycloneDX 1.6 SBOM from uv.lock, with an installed-graph fallback."""

    lock_path = ROOT / "uv.lock"
    if lock_path.is_file():
        return _build_locked_sbom(lock_path)

    distributions = {
        _normalise_name(str(distribution.metadata.get("Name") or "")): distribution
        for distribution in importlib.metadata.distributions()
        if distribution.metadata.get("Name")
    }
    roots = _declared_dependencies()
    queue = deque(roots)
    selected: dict[str, importlib.metadata.Distribution] = {}
    dependency_names: dict[str, list[str]] = {}
    while queue:
        name = queue.popleft()
        if not name or name in selected:
            continue
        distribution = distributions.get(name)
        if distribution is None:
            continue
        selected[name] = distribution
        children: list[str] = []
        for requirement in distribution.requires or []:
            if "extra ==" in requirement or "extra==" in requirement:
                continue
            child = _requirement_name(requirement)
            if child and child in distributions:
                children.append(child)
                queue.append(child)
        dependency_names[name] = sorted(set(children))
    components = [_component(selected[name]) for name in sorted(selected)]
    component_ref = {
        name: f"pkg:pypi/{name}@{selected[name].version}"
        for name in selected
    }
    application_ref = f"pkg:pypi/asset-factory-blueprint@{__version__}"
    dependencies = [
        {
            "ref": application_ref,
            "dependsOn": [component_ref[name] for name in roots if name in component_ref],
        },
        *[
            {
                "ref": component_ref[name],
                "dependsOn": [component_ref[child] for child in dependency_names.get(name, []) if child in component_ref],
            }
            for name in sorted(selected)
        ],
    ]
    identity = json.dumps({"components": components, "dependencies": dependencies}, sort_keys=True, separators=(",", ":"))
    timestamp = datetime.now(timezone.utc)
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if source_date_epoch:
        timestamp = datetime.fromtimestamp(int(source_date_epoch), tz=timezone.utc)
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, identity)}",
        "version": 1,
        "metadata": {
            "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "asset-factory-blueprint SBOM generator",
                        "version": __version__,
                    }
                ]
            },
            "component": {
                "type": "application",
                "bom-ref": application_ref,
                "name": "asset-factory-blueprint",
                "version": __version__,
                "purl": application_ref,
                "licenses": [{"license": {"id": "MIT"}}],
            },
            "properties": [
                {"name": "afb:resolution", "value": "installed Python environment"},
                {"name": "afb:python", "value": platform.python_version()},
            ],
        },
        "components": components,
        "dependencies": dependencies,
    }


def _schema_catalogue() -> dict[str, Any]:
    schemas = []
    for path in sorted((ROOT / "schemas").glob("*.schema.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        schema_id = str(payload.get("$id") or "")
        version_match = _SCHEMA_VERSION.search(schema_id)
        if version_match is None:
            raise ValueError(f"public schema identity is not versioned: {path.relative_to(ROOT).as_posix()}")
        schemas.append(
            {
                "path": path.relative_to(ROOT).as_posix(),
                "schema_id": schema_id,
                "schema_version": version_match.group("version"),
                "schema_draft": str(payload.get("$schema") or ""),
                "title": str(payload.get("title") or ""),
                "sha256": sha256_file(path),
            }
        )
    return {
        "format_version": "1.0",
        "software": {"name": "asset-factory-blueprint", "version": __version__},
        "schema_count": len(schemas),
        "schemas": schemas,
    }


def _normalise_repository_url(value: str) -> str:
    return value.strip().removesuffix(".git").rstrip("/")


def _cff_scalar(text: str, key: str) -> str:
    match = re.search(rf'(?m)^{re.escape(key)}:\s*"?(?P<value>[^"\r\n]+?)"?\s*$', text)
    return match.group("value").strip() if match else ""


def _publication_metadata() -> dict[str, Any]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject.get("project") or {}
    project_urls = project.get("urls") or {}
    cff_text = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    codemeta = json.loads((ROOT / "codemeta.json").read_text(encoding="utf-8"))

    versions = {
        "package": str(project.get("version") or ""),
        "runtime": __version__,
        "citation": _cff_scalar(cff_text, "version"),
        "codemeta": str(codemeta.get("version") or ""),
    }
    if any(value != __version__ for value in versions.values()):
        raise ValueError(f"publication metadata versions are not aligned: {versions}")

    repositories = {
        "package": _normalise_repository_url(str(project_urls.get("Repository") or "")),
        "citation": _normalise_repository_url(_cff_scalar(cff_text, "repository-code")),
        "codemeta": _normalise_repository_url(str(codemeta.get("codeRepository") or "")),
    }
    if not repositories["package"] or len(set(repositories.values())) != 1:
        raise ValueError(f"publication repository identities are not aligned: {repositories}")

    documentation_urls = {
        "package": str(project_urls.get("Documentation") or "").rstrip("/"),
        "citation": _cff_scalar(cff_text, "url").rstrip("/"),
        "codemeta": str(codemeta.get("softwareHelp") or "").rstrip("/"),
    }
    if not documentation_urls["package"] or len(set(documentation_urls.values())) != 1:
        raise ValueError(f"publication documentation URLs are not aligned: {documentation_urls}")

    metadata_paths = (
        "pyproject.toml",
        "CITATION.cff",
        "codemeta.json",
        "references.bib",
        "CHANGELOG.md",
        "RELEASE.md",
        "MANIFEST.in",
        ".dockerignore",
    )
    metadata_files = [
        {"path": relative, "sha256": sha256_file(ROOT / relative)}
        for relative in metadata_paths
    ]

    dockerfile = ROOT / "deploy" / "Dockerfile"
    docker_text = dockerfile.read_text(encoding="utf-8")
    base_match = _DOCKER_BASE_IMAGE.search(docker_text)
    if base_match is None:
        raise ValueError("deploy/Dockerfile must declare the default PYTHON_IMAGE")
    base_reference = base_match.group("reference")
    _, separator, base_digest = base_reference.rpartition("@sha256:")
    if not separator or re.fullmatch(r"[0-9a-f]{64}", base_digest) is None:
        raise ValueError("deploy/Dockerfile default PYTHON_IMAGE must be pinned by lowercase SHA-256")

    return {
        "version_alignment": versions,
        "repository": repositories["package"],
        "documentation": documentation_urls["package"],
        "metadata_files": metadata_files,
        "container_recipe": {
            "path": dockerfile.relative_to(ROOT).as_posix(),
            "sha256": sha256_file(dockerfile),
            "declared_default_base_image": base_reference,
            "declared_default_base_image_sha256": base_digest,
        },
    }


def _configuration_catalogue() -> list[dict[str, str]]:
    return [
        {"path": path.relative_to(ROOT).as_posix(), "sha256": sha256_file(path)}
        for path in sorted((ROOT / "configs").glob("*.json"))
    ]


def _absolute_machine_path_kind(value: str) -> str | None:
    if _FILE_URI.search(value):
        return "file URI"
    without_public_urls = _PUBLIC_URL.sub("", value)
    if _WINDOWS_ABSOLUTE.search(without_public_urls):
        return "Windows path"
    if _UNC_ABSOLUTE.search(without_public_urls):
        return "UNC path"
    if _POSIX_ABSOLUTE.search(without_public_urls):
        return "POSIX path"
    return None


def _assert_publishable(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key)) and child not in (None, "", False) and child != [] and child != {}:
                raise ValueError(f"release evidence contains a secret-like field at {path}.{key}")
            _assert_publishable(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_publishable(child, f"{path}[{index}]")
    elif isinstance(value, str) and (path_kind := _absolute_machine_path_kind(value)):
        raise ValueError(f"release evidence contains an absolute {path_kind} at {path}")


def write_release_evidence(output_dir: str | Path) -> dict[str, Any]:
    """Write SBOM, schema catalogue and content-addressed release evidence."""

    root = Path(output_dir).resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("release evidence output must not be a symbolic link")
    lock_path = ROOT / "uv.lock"
    if not lock_path.is_file():
        raise ValueError("release evidence requires the checked-in uv.lock dependency resolution")
    sbom = build_cyclonedx_sbom()
    schemas = _schema_catalogue()
    _assert_publishable(sbom)
    _assert_publishable(schemas)
    sbom_path = atomic_write_json(root / "sbom.cdx.json", sbom)
    schema_path = atomic_write_json(root / "schema-catalogue.json", schemas)
    release = {
        "format_version": "1.0",
        "software": {"name": "asset-factory-blueprint", "version": __version__},
        "repository": _repository_state(),
        "publication": _publication_metadata(),
        "dependency_lock": {
            "path": "uv.lock",
            "sha256": sha256_file(lock_path),
            "resolver": "uv",
            "verified": True,
        },
        "configuration": _configuration_catalogue(),
        "artefacts": [
            {"path": sbom_path.relative_to(root).as_posix(), "sha256": sha256_file(sbom_path)},
            {"path": schema_path.relative_to(root).as_posix(), "sha256": sha256_file(schema_path)},
        ],
        "claims": {
            "signed_tag_verified": False,
            "doi_assigned": False,
            "official_nvidia_certification": False,
            "dependency_lock_verified": True,
        },
    }
    _assert_publishable(release)
    release_path = atomic_write_json(root / "release-evidence.json", release)
    checksum_records = [
        {"path": path.name, "sha256": sha256_file(path)}
        for path in sorted((sbom_path, schema_path, release_path), key=lambda item: item.name)
    ]
    checksum_path = atomic_write_json(root / "release-checksums.json", {"files": checksum_records})
    bundle_digest = hashlib.sha256(
        "".join(f"{item['path']}:{item['sha256']}\n" for item in checksum_records).encode("utf-8")
    ).hexdigest()
    return {
        "output_dir": str(root),
        "sbom": sbom_path.name,
        "schema_catalogue": schema_path.name,
        "release_evidence": release_path.name,
        "checksums": checksum_path.name,
        "bundle_sha256": bundle_digest,
    }
