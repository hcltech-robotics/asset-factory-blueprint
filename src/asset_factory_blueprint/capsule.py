from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import shutil
import tempfile
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker

from asset_factory_blueprint.config import ROOT
from asset_factory_blueprint.execution import immutable_write_json, workspace_lease
from asset_factory_blueprint.isaac_evidence import (
    attestation_secret as isaac_attestation_secret,
    parse_runtime_report_bytes,
    producer_sha256_pin as isaac_producer_sha256_pin,
    verify_runtime_report_envelope,
)
from asset_factory_blueprint.manifests import validate_payload
from asset_factory_blueprint.physics_evidence import (
    physics_evidence_secret_from_environment,
    verify_physics_evidence_attestation,
)
from asset_factory_blueprint.schemas.common import RunPlan, RunRequest
from asset_factory_blueprint.security import ensure_path_component
from asset_factory_blueprint.services.official_validator import (
    normalise_official_profile_report,
    verify_official_profile_report_attestation,
)
from asset_factory_blueprint.services.simready import _revalidate_packaged_physics_evidence
from asset_factory_blueprint.utils.checksums import sha256_file, sha256_text
from asset_factory_blueprint.utils.ids import content_id, stage_attempt_id


CAPSULE_FORMAT_VERSION = "1.0"
CapsuleOutcome = Literal["positive", "negative"]
MAX_CAPSULE_FILES = 10_000
MAX_CAPSULE_BYTES = 20 * 1024 * 1024 * 1024
MAX_CAPSULE_FILE_BYTES = 4 * 1024 * 1024 * 1024
MAX_CAPSULE_JSON_BYTES = 64 * 1024 * 1024

_MIT_LICENCE = "MIT"
_NOASSERTION = "NOASSERTION"
_HASH_PATTERN = re.compile(r"^[A-Fa-f0-9]{64}$")
_RESOLVED_MODEL_STATUSES = {"pinned", "resolved", "verified"}
_UNRESOLVED_MODEL_VALUES = {
    "",
    "n/a",
    "none",
    "not_recorded",
    "not-recorded",
    "noassertion",
    "unknown",
    "unresolved",
}
_FITNESS_TESTS_BY_SCOPE = {
    "visualisation": ("visual_render_acceptance",),
    "rigid_body_manipulation": ("manipulation_contact_fidelity",),
    "articulated_training": ("joint_task_fidelity",),
    "redistribution": ("consumer_install_reproduction",),
}
_POSITIVE_EVIDENCE_PATHS = {
    "task_fitness": "validation/task-fitness-evidence.json",
    "task_protocol": "validation/task-fitness-protocol.json",
    "official_normalised": "validation/official-validator-results.json",
    "official_raw": "validation/official-validator-raw.json",
    "openusd": "validation/openusd-compliance.json",
    "package_closure": "validation/package-dependency-closure.json",
}
_WINDOWS_PATH_PATTERN = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[a-z]:[\\/]|\\\\[^\\/\s]+[\\/])")
_POSIX_HOME_PATTERN = re.compile(r"(?:^|[\s\"'])/(?:home|Users)/[^/\s\"']+(?:/|$)")
_POSIX_SYSTEM_PATH_PATTERN = re.compile(
    r"(?:^|[\s\"'@])/(?:data|dev|etc|mnt|opt|private|proc|root|run|srv|sys|tmp|usr|var|workspace)(?:/|$)"
)
_PRIVATE_ENDPOINT_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9.-])(?:localhost|host\.docker\.internal|"
    r"127(?:\.[0-9]{1,3}){3}|10(?:\.[0-9]{1,3}){3}|192\.168(?:\.[0-9]{1,3}){2}|"
    r"172\.(?:1[6-9]|2[0-9]|3[01])(?:\.[0-9]{1,3}){2}|"
    r"[A-Za-z0-9.-]+\.(?:internal|local))(?::[0-9]{1,5})?(?![A-Za-z0-9.-])"
)
_TOKEN_PATTERN = re.compile(
    r"(?i)(?:\bBearer\s+[A-Za-z0-9._~+/=-]{12,}|\bsk-[A-Za-z0-9_-]{12,}|"
    r"\bnvapi-[A-Za-z0-9_-]{12,}|\bgh[opusr]_[A-Za-z0-9_]{12,}|"
    r"\bAKIA[A-Z0-9]{16}|\bhf_[A-Za-z0-9]{12,}|\bglpat-[A-Za-z0-9_-]{12,}|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)
_SECRET_KEY_PATTERN = re.compile(
    r"(?i)(?:^|_)(?:api_?key|access_?token|refresh_?token|auth(?:orization)?|password|passwd|"
    r"credential|client_?secret|private_?key|secret(?:_access)?_key|session_?token|hf_?token|"
    r"gitlab_?token|sas_?token|shared_?access_?signature|token|secret)(?:$|_)"
)
_NON_SECRET_KEYS = {"secret_policy", "secret_values_removed", "signed_urls_removed", "unsafe_keys_removed"}
_PATH_KEY_PATTERN = re.compile(r"(?i)(?:^|_)(?:path|file|directory|dir|root|workspace|cwd)(?:$|_)")
_SIGNED_QUERY_KEYS = {
    "access_token",
    "authorization",
    "credential",
    "expires",
    "signature",
    "sig",
    "token",
    "x-amz-credential",
    "x-amz-signature",
}
_TEXT_SUFFIXES = {".csv", ".json", ".jsonl", ".log", ".md", ".txt", ".usd", ".usda", ".gltf", ".mtlx", ".mdl"}
_OUTPUT_SUFFIXES = {
    ".bin",
    ".exr",
    ".glb",
    ".gltf",
    ".hdr",
    ".jpeg",
    ".jpg",
    ".json",
    ".mdl",
    ".mtlx",
    ".png",
    ".txt",
    ".usd",
    ".usda",
    ".usdc",
}
_SOURCE_MEDIA_SUFFIXES = {
    ".bin",
    ".exr",
    ".fbx",
    ".glb",
    ".gltf",
    ".hdr",
    ".jpeg",
    ".jpg",
    ".obj",
    ".ply",
    ".png",
    ".stl",
    ".tif",
    ".tiff",
    ".usd",
    ".usda",
    ".usdc",
}
_RIGHTS_EVIDENCE_SUFFIXES = {".json", ".md", ".pdf", ".txt"}
_TASK_FITNESS_EVIDENCE_SUFFIXES = {".csv", ".jpeg", ".jpg", ".json", ".jsonl", ".log", ".pdf", ".png", ".txt"}
_MEDIA_TYPES = {
    ".bin": "application/octet-stream",
    ".csv": "text/csv",
    ".exr": "image/x-exr",
    ".fbx": "application/octet-stream",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".hdr": "image/vnd.radiance",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".mdl": "text/plain",
    ".mtlx": "application/xml",
    ".obj": "model/obj",
    ".pdf": "application/pdf",
    ".ply": "application/octet-stream",
    ".png": "image/png",
    ".stl": "model/stl",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".txt": "text/plain",
    ".usd": "model/vnd.usd",
    ".usda": "model/vnd.usda",
    ".usdc": "model/vnd.usdc",
}
_CAPSULE_TOP_LEVEL = {
    "README.md",
    "attempts",
    "capsule.json",
    "checksums.sha256",
    "environment",
    "governance",
    "outputs",
    "request",
    "schemas",
    "source",
    "validation",
}
_STAGE_SCHEMAS = {
    "evaluation": "evaluation-manifest.schema.json",
    "governance": "governance-record.schema.json",
    "intake": "asset-programme-intake-manifest.schema.json",
    "material-inference": "material-inference-manifest.schema.json",
    "nonvisual-materials": "nonvisual-material-manifest.schema.json",
    "physics-articulation": "physics-articulation-manifest.schema.json",
    "reconstruction": "reconstruction-manifest.schema.json",
    "mesh-verification": "mesh-verification-record.schema.json",
    "rl-environment": "rl-environment-manifest.schema.json",
    "segmentation": "segmentation-manifest.schema.json",
    "simready-verification": "simready-asset-manifest.schema.json",
    "source-ingestion": "source-asset-manifest.schema.json",
    "texturing": "texturing-manifest.schema.json",
}


class CapsuleCreationError(ValueError):
    """Raised when a project cannot be exported without weakening its evidence boundary."""

    def __init__(self, blockers: Sequence[str]) -> None:
        self.blockers = tuple(dict.fromkeys(str(item) for item in blockers if str(item)))
        super().__init__("reference capsule creation blocked: " + "; ".join(self.blockers))


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    return path


def _safe_relative(value: str, label: str = "capsule path") -> PurePosixPath:
    if not value or "\\" in value:
        raise ValueError(f"{label} must use a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must not be absolute or traverse parents: {value}")
    return path


def _is_linklike(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _has_linklike_component(path: Path) -> bool:
    absolute = path.absolute()
    return any(_is_linklike(candidate) for candidate in (absolute, *absolute.parents) if candidate.exists())


def _reject_symlink_chain(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes the authorised root: {target}") from exc
    current = root
    if _is_linklike(current):
        raise ValueError(f"authorised root must not be a symlink: {root}")
    for part in relative.parts:
        current = current / part
        if _is_linklike(current):
            raise ValueError(f"symlinks are not permitted in capsule inputs: {current}")


def _project_file(project_root: Path, relative: str | PurePosixPath, *, required: bool = True) -> Path | None:
    path = _safe_relative(str(relative), "project-relative path")
    candidate = project_root.joinpath(*path.parts)
    _reject_symlink_chain(project_root, candidate)
    resolved = candidate.resolve(strict=False)
    if resolved != project_root and project_root not in resolved.parents:
        raise ValueError(f"project path escapes the authorised root: {relative}")
    if not candidate.exists():
        if required:
            raise FileNotFoundError(candidate)
        return None
    if not candidate.is_file():
        raise ValueError(f"capsule input is not a regular file: {relative}")
    return candidate


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapsuleCreationError([f"{label} is not readable JSON: {exc}"]) from exc
    if not isinstance(payload, dict):
        raise CapsuleCreationError([f"{label} must be a JSON object"])
    return payload


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_absolute_filesystem_path(value: str, key: str | None) -> bool:
    if (
        value.lower().startswith("file://")
        or _WINDOWS_PATH_PATTERN.search(value)
        or _POSIX_HOME_PATTERN.search(value)
        or _POSIX_SYSTEM_PATH_PATTERN.search(value)
    ):
        return True
    if value.startswith("/"):
        key_lower = (key or "").lower()
        if "prim" not in key_lower and key_lower not in {
            "articulation_root_paths",
            "joint_paths",
            "relationship_targets",
        }:
            return True
    if not key or not _PATH_KEY_PATTERN.search(key) or "prim" in key.lower():
        return False
    if value.startswith(("http://", "https://", "omniverse://", "s3://", "hf://", "redacted:")):
        return False
    return value.startswith("/")


def _unsafe_url_reason(value: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return None
    if parsed.username or parsed.password:
        return "credentialled_url"
    query_keys = {key.lower() for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)}
    if query_keys & _SIGNED_QUERY_KEYS:
        return "signed_url"
    if parsed.scheme == "omniverse":
        return "private_endpoint"
    if parsed.scheme not in {"http", "https", "s3", "hf"}:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in {"localhost", "host.docker.internal"} or host.endswith((".internal", ".local", ".localhost")):
        return "private_endpoint"
    if parsed.scheme in {"http", "https"} and "." not in host:
        return "private_endpoint"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
        return "private_endpoint"
    return None


def _redacted(value: str, reason: str, redactions: Counter[str]) -> str:
    redactions[reason] += 1
    return f"redacted:{reason}:sha256:{sha256_text(value)}"


def _unsafe_key_reason(value: str) -> str | None:
    if _TOKEN_PATTERN.search(value):
        return "secret_key_name"
    if _PRIVATE_ENDPOINT_PATTERN.search(value) or _unsafe_url_reason(value):
        return "endpoint_key_name"
    if _is_absolute_filesystem_path(value, None):
        return "path_key_name"
    return None


def _sanitise_json(
    value: Any,
    redactions: Counter[str],
    *,
    key: str | None = None,
) -> Any:
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for raw_key, child in value.items():
            child_key = str(raw_key)
            if key_reason := _unsafe_key_reason(child_key):
                redactions[key_reason] += 1
                child_key = f"redacted_key_{sha256_text(child_key)[:24]}"
            if child_key in clean:
                raise CapsuleCreationError(["JSON key sanitisation produced a duplicate key"])
            if _SECRET_KEY_PATTERN.search(child_key) and child_key.lower() not in _NON_SECRET_KEYS:
                clean[child_key] = _redacted(_canonical_json(child), "secret_value", redactions)
            else:
                clean[child_key] = _sanitise_json(child, redactions, key=child_key)
        return clean
    if isinstance(value, list):
        return [_sanitise_json(child, redactions, key=key) for child in value]
    if isinstance(value, tuple):
        return [_sanitise_json(child, redactions, key=key) for child in value]
    if not isinstance(value, str):
        return value
    if _TOKEN_PATTERN.search(value):
        return _redacted(value, "secret_value", redactions)
    if _PRIVATE_ENDPOINT_PATTERN.search(value):
        return _redacted(value, "private_endpoint", redactions)
    url_reason = _unsafe_url_reason(value)
    if url_reason:
        return _redacted(value, url_reason, redactions)
    if _is_absolute_filesystem_path(value, key):
        return _redacted(value, "absolute_path", redactions)
    return value


def _disclosure_errors_text(text: str) -> list[str]:
    errors: list[str] = []
    if (
        _WINDOWS_PATH_PATTERN.search(text)
        or _POSIX_HOME_PATTERN.search(text)
        or _POSIX_SYSTEM_PATH_PATTERN.search(text)
        or "file://" in text.lower()
    ):
        errors.append("absolute filesystem path")
    if _TOKEN_PATTERN.search(text):
        errors.append("secret-like token")
    if _PRIVATE_ENDPOINT_PATTERN.search(text):
        errors.append("private endpoint")
    for match in re.finditer(r"(?:https?|omniverse|s3|hf)://[^\s\"'<>]+", text, flags=re.IGNORECASE):
        if reason := _unsafe_url_reason(match.group(0)):
            errors.append(reason.replace("_", " "))
    return sorted(set(errors))


def _disclosure_errors_json(value: Any, *, key: str | None = None) -> list[str]:
    errors: set[str] = set()
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            child_key = str(raw_key)
            if key_reason := _unsafe_key_reason(child_key):
                errors.add(key_reason.replace("_", " "))
            if (
                _SECRET_KEY_PATTERN.search(child_key)
                and child_key.lower() not in _NON_SECRET_KEYS
                and not (isinstance(child, str) and child.startswith("redacted:secret_value:sha256:"))
            ):
                errors.add("secret value")
            errors.update(_disclosure_errors_json(child, key=child_key))
        return sorted(errors)
    if isinstance(value, list):
        for child in value:
            errors.update(_disclosure_errors_json(child, key=key))
        return sorted(errors)
    if not isinstance(value, str):
        return []
    if _TOKEN_PATTERN.search(value):
        errors.add("secret-like token")
    if _PRIVATE_ENDPOINT_PATTERN.search(value):
        errors.add("private endpoint")
    if reason := _unsafe_url_reason(value):
        errors.add(reason.replace("_", " "))
    if key != "pattern" and _is_absolute_filesystem_path(value, key):
        errors.add("absolute filesystem path")
    return sorted(errors)


def _disclosure_errors_file(path: Path) -> list[str]:
    if path.suffix.lower() in {".json", ".gltf"}:
        try:
            return _disclosure_errors_json(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return ["unreadable JSON evidence"]
    if path.suffix.lower() == ".jsonl":
        errors: set[str] = set()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                if line.strip():
                    errors.update(_disclosure_errors_json(json.loads(line)))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return ["unreadable JSONL evidence"]
        return sorted(errors)
    errors: set[str] = set()
    with path.open("rb") as stream:
        tail = b""
        while chunk := stream.read(1024 * 1024):
            sample = tail + chunk
            text = sample.decode("latin-1", errors="ignore")
            errors.update(_disclosure_errors_text(text))
            errors.update(_disclosure_errors_text(sample.decode("utf-16-le", errors="ignore")))
            errors.update(_disclosure_errors_text(sample.decode("utf-16-be", errors="ignore")))
            tail = sample[-1024:]
    return sorted(errors)


def _copy_sanitised_json(source: Path, destination: Path, redactions: Counter[str]) -> None:
    payload = json.loads(source.read_text(encoding="utf-8"))
    _write_json(destination, _sanitise_json(payload, redactions))


def _copy_sanitised_jsonl(source: Path, destination: Path, redactions: Counter[str]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CapsuleCreationError([f"invalid JSONL at {source.name}:{line_number}: {exc}"]) from exc
        lines.append(_canonical_json(_sanitise_json(payload, redactions)))
    destination.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _copy_exact_safe(source: Path, destination: Path) -> None:
    disclosure_errors = _disclosure_errors_file(source)
    if disclosure_errors:
        details = ", ".join(disclosure_errors)
        raise CapsuleCreationError([f"{source.name} contains prohibited disclosure material: {details}"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _licence_expression(rights: Sequence[Mapping[str, Any]]) -> str:
    licences = sorted(
        {
            str(item.get("licence_expression") or _NOASSERTION).strip()
            for item in rights
            if str(item.get("licence_expression") or "").strip()
        }
    )
    if not licences:
        return _NOASSERTION
    if len(licences) == 1:
        return licences[0]
    return " AND ".join(f"({item})" for item in licences)


def _rights_records(
    source_manifest: Mapping[str, Any], governance: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    del governance
    candidates = source_manifest.get("source_rights") or []
    return [item for item in candidates if isinstance(item, Mapping)] if isinstance(candidates, list) else []


def _rights_representation_blockers(
    source_manifest: Mapping[str, Any], governance: Mapping[str, Any]
) -> list[str]:
    source_rights = source_manifest.get("source_rights")
    governance_rights = governance.get("source_rights")
    if not isinstance(source_rights, list) or not isinstance(governance_rights, list):
        return ["source and governance snapshots must both carry structured rights records"]
    if _canonical_json(source_rights) != _canonical_json(governance_rights):
        return ["source and governance rights records do not match exactly"]
    return []


def _rights_coverage_blockers(
    source_manifest: Mapping[str, Any], rights: Sequence[Mapping[str, Any]]
) -> list[str]:
    source_assets = [
        item for item in source_manifest.get("source_assets") or [] if isinstance(item, Mapping)
    ]
    blockers: list[str] = []
    if len(source_assets) != len(rights):
        blockers.append(
            f"source rights coverage mismatch: {len(source_assets)} source assets and {len(rights)} rights records"
        )
    rights_by_id = {str(item.get("rights_id") or ""): item for item in rights if item.get("rights_id")}
    if len(rights_by_id) != len(rights):
        blockers.append("source rights identifiers must be present and unique")
    source_ids = [str(item.get("source_id") or "") for item in rights]
    if len(set(source_ids)) != len(source_ids) or any(not item for item in source_ids):
        blockers.append("source rights source identifiers must be present and unique")
    for index, source in enumerate(source_assets):
        source_path = str(source.get("project_copy_path") or f"source asset {index}")
        rights_id = str(source.get("rights_id") or "")
        if rights_id:
            if rights_id not in rights_by_id:
                blockers.append(f"{source_path}: referenced rights record {rights_id!r} is missing")
            continue
        if index >= len(rights):
            blockers.append(f"{source_path}: no positional rights record is present")
    return blockers


def _rights_blockers(
    rights: Sequence[Mapping[str, Any]],
    evaluated_at: datetime,
    evidence_records: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    blockers: list[str] = []
    if not rights:
        return ["structured source rights records are required before capsule publication"]
    evidence = {
        str(item.get("evidence_id") or ""): item
        for item in evidence_records
        if str(item.get("evidence_id") or "")
    }
    evidence_claims: dict[str, str] = {}
    for item in evidence_records:
        evidence_id = str(item.get("evidence_id") or "")
        if not evidence_id:
            continue
        claim = _canonical_json(item)
        if evidence_id in evidence_claims and evidence_claims[evidence_id] != claim:
            blockers.append(f"rights evidence identifier {evidence_id!r} has conflicting claims")
        evidence_claims[evidence_id] = claim
    for index, record in enumerate(rights):
        source_id = str(record.get("source_id") or f"source_{index}")
        if record.get("rights_status") != "cleared":
            blockers.append(f"{source_id}: rights are not cleared")
        if record.get("redistribution_allowed") is not True:
            blockers.append(f"{source_id}: redistribution is not permitted")
        permitted_uses = {str(item) for item in record.get("permitted_uses") or []}
        if "*" not in permitted_uses and "redistribution" not in permitted_uses:
            blockers.append(f"{source_id}: redistribution is not an explicitly permitted use")
        if str(record.get("licence_expression") or _NOASSERTION).upper() == _NOASSERTION and not record.get(
            "terms_uri"
        ):
            blockers.append(f"{source_id}: licence expression or terms URI is required")
        if record.get("privacy_status") not in {"cleared", "not_applicable"}:
            blockers.append(f"{source_id}: privacy status is not cleared")
        consent_ids = [str(item) for item in record.get("consent_evidence_ids") or []]
        if record.get("privacy_status") == "cleared" and not consent_ids:
            blockers.append(f"{source_id}: consent evidence is required for privacy clearance")
        rights_evidence_ids = [str(item) for item in record.get("evidence_ids") or []]
        if not rights_evidence_ids:
            blockers.append(f"{source_id}: content-addressed rights evidence is required")
        for evidence_id in [*rights_evidence_ids, *consent_ids]:
            item = evidence.get(evidence_id)
            checksum = str(item.get("checksum") or "").removeprefix("sha256:") if item else ""
            if not item or not item.get("uri") or not _HASH_PATTERN.fullmatch(checksum):
                blockers.append(f"{source_id}: rights evidence {evidence_id!r} is missing or not content-addressed")
        expiry = record.get("expires_at")
        if expiry:
            expires_at = _parse_time(expiry)
            if expires_at is None:
                blockers.append(f"{source_id}: rights expiry is invalid")
            elif expires_at <= evaluated_at:
                blockers.append(f"{source_id}: rights have expired")
    return blockers


def _rights_evidence_sources(
    project_root: Path,
    rights: Sequence[Mapping[str, Any]],
    evidence_records: Sequence[Mapping[str, Any]],
) -> dict[str, Path]:
    evidence = {
        str(item.get("evidence_id") or ""): item
        for item in evidence_records
        if str(item.get("evidence_id") or "")
    }
    sources: dict[str, Path] = {}
    for right in rights:
        for raw_evidence_id in [*(right.get("evidence_ids") or []), *(right.get("consent_evidence_ids") or [])]:
            evidence_id = str(raw_evidence_id)
            item = evidence.get(evidence_id)
            if item is None:
                continue
            uri = str(item.get("uri") or "")
            try:
                source = _project_file(project_root, uri)
            except (FileNotFoundError, ValueError) as exc:
                raise CapsuleCreationError(
                    [f"rights evidence {evidence_id!r} must be materialised inside the project: {exc}"]
                ) from exc
            assert source is not None
            if source.suffix.lower() not in _RIGHTS_EVIDENCE_SUFFIXES:
                raise CapsuleCreationError([f"rights evidence type is not allowlisted: {source.suffix or '<none>'}"])
            expected = str(item.get("checksum") or "").removeprefix("sha256:").lower()
            if not _HASH_PATTERN.fullmatch(expected) or sha256_file(source) != expected:
                raise CapsuleCreationError([f"rights evidence checksum differs: {evidence_id}"])
            sources[evidence_id] = source
    return sources


def _select_release_decision(
    governance: Mapping[str, Any], outcome: CapsuleOutcome, release_scope: str | None
) -> dict[str, Any]:
    raw_decisions = governance.get("release_decisions")
    decisions = [dict(item) for item in raw_decisions or [] if isinstance(item, Mapping)]
    expected_allowed = outcome == "positive"
    matching = [
        item
        for item in decisions
        if (release_scope is None or str(item.get("scope") or "") == release_scope)
        and item.get("release_allowed") is expected_allowed
    ]
    if not matching:
        scope_note = f" for scope {release_scope!r}" if release_scope else ""
        raise CapsuleCreationError([f"no {outcome} release decision is recorded{scope_note}"])
    decision = sorted(matching, key=lambda item: (str(item.get("scope") or ""), str(item.get("decision_id") or "")))[0]
    status = str(decision.get("release_status") or "")
    blockers = [str(item) for item in decision.get("blockers") or []]
    if any(not item.strip() for item in blockers):
        raise CapsuleCreationError(["release decision blockers must be non-empty strings"])
    if outcome == "positive" and (status != "approved" or blockers):
        raise CapsuleCreationError(["positive capsule requires an approved decision without blockers"])
    if outcome == "negative" and (status != "blocked" or not blockers):
        raise CapsuleCreationError(["negative capsule requires a blocked decision with explicit blockers"])
    expected_decision_id = content_id(
        "release",
        {
            "governance_id": governance.get("id"),
            "scope": decision.get("scope"),
            "policy_version": decision.get("policy_version"),
            "blockers": sorted(blockers),
        },
        digest_length=32,
    )
    if decision.get("decision_id") != expected_decision_id:
        raise CapsuleCreationError(["release decision identifier does not match its decision basis"])
    return decision


def _provenance_path(project_root: Path, run_root: Path) -> Path:
    candidates = list(run_root.glob("result-provenance*.json"))
    candidates.extend(path for path in (run_root / "provenance.json",) if path.exists())
    if not candidates:
        raise CapsuleCreationError(["run provenance record is missing"])
    ranked: list[tuple[int, str, Path]] = []
    for candidate in candidates:
        _reject_symlink_chain(project_root, candidate)
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        attempt_count = len(payload.get("attempt_ids") or []) if isinstance(payload, dict) else -1
        ranked.append((attempt_count, candidate.name, candidate))
    if not ranked:
        raise CapsuleCreationError(["run provenance records are not readable JSON"])
    return max(ranked)[2]


def _provenance_identity_blocker(provenance: Mapping[str, Any]) -> str | None:
    core_keys = (
        "schema_version",
        "run_id",
        "attempt_ids",
        "repository",
        "environment_bom",
        "model_bom",
        "prompt_checksums",
        "config_checksums",
        "manifest_ids",
        "source_assets",
        "source_assets_mutated",
        "reproducibility",
    )
    record_core = {key: provenance.get(key) for key in core_keys}
    expected = content_id("prov", record_core, digest_length=32)
    if provenance.get("provenance_id") != expected:
        return "provenance identifier does not match the immutable provenance record core"
    return None


def _stage_manifest_snapshot(
    project_root: Path,
    run_root: Path,
    stage_id: str,
    request_digest: str,
) -> Path:
    ensure_path_component(stage_id, "stage ID")
    stage_root = run_root / "attempts" / stage_id
    _reject_symlink_chain(project_root, stage_root)
    if not stage_root.is_dir():
        raise CapsuleCreationError([f"immutable {stage_id} attempt evidence is missing"])
    candidates: list[tuple[int, str, Path]] = []
    for attempt_root in sorted(stage_root.iterdir(), key=lambda item: item.name):
        _reject_symlink_chain(project_root, attempt_root)
        if not attempt_root.is_dir():
            continue
        ensure_path_component(attempt_root.name, "attempt ID")
        identity_path = attempt_root / f"{attempt_root.name}.json"
        manifest_path = attempt_root / "manifest.json"
        _reject_symlink_chain(project_root, identity_path)
        _reject_symlink_chain(project_root, manifest_path)
        if not identity_path.is_file() or not manifest_path.is_file():
            continue
        attempt = _load_json(identity_path, f"{stage_id} stage attempt")
        identity = attempt.get("identity")
        if not isinstance(identity, Mapping):
            continue
        if (
            identity.get("run_id") != run_root.name
            or identity.get("stage_id") != stage_id
            or identity.get("request_digest") != request_digest
        ):
            continue
        try:
            attempt_number = int(identity.get("attempt_number"))
            expected_attempt_id = stage_attempt_id(
                str(identity["run_id"]),
                str(identity["stage_id"]),
                attempt_number,
                str(identity["request_digest"]),
            )
        except (TypeError, ValueError):
            continue
        if identity.get("attempt_id") != expected_attempt_id or attempt_root.name != expected_attempt_id:
            continue
        if attempt.get("status") not in {"succeeded", "blocked"}:
            continue
        extensions = attempt.get("extensions")
        snapshots = extensions.get("snapshots") if isinstance(extensions, Mapping) else None
        manifest_snapshot = snapshots.get("manifest_path") if isinstance(snapshots, Mapping) else None
        expected_path = manifest_path.relative_to(project_root).as_posix()
        if not isinstance(manifest_snapshot, Mapping) or manifest_snapshot.get("path") != expected_path:
            continue
        expected_sha256 = str(manifest_snapshot.get("sha256") or "").removeprefix("sha256:")
        if not _HASH_PATTERN.fullmatch(expected_sha256) or sha256_file(manifest_path) != expected_sha256.lower():
            continue
        candidates.append((attempt_number, attempt_root.name, manifest_path))
    if not candidates:
        raise CapsuleCreationError([f"no complete immutable {stage_id} attempt is available"])
    return max(candidates)[2]


def _copy_schema_snapshots(staging: Path, schema_root: Path) -> list[dict[str, str]]:
    if _is_linklike(schema_root):
        raise CapsuleCreationError(["schema root must not be a symlink"])
    root = schema_root.resolve(strict=True)
    schemas: list[dict[str, str]] = []
    for source in sorted(root.glob("*.schema.json"), key=lambda item: item.name):
        _reject_symlink_chain(root, source)
        payload = _load_json(source, f"schema {source.name}")
        destination = staging / "schemas" / source.name
        _copy_exact_safe(source, destination)
        schemas.append(
            {
                "path": f"schemas/{source.name}",
                "schema_id": str(payload.get("$id") or ""),
                "sha256": sha256_file(destination),
            }
        )
    if not schemas:
        raise CapsuleCreationError(["no schema snapshots were found"])
    return schemas


def _schema_validation_results(staging: Path) -> dict[str, Any]:
    schema_map = {
        "environment/provenance.json": "provenance-record.schema.json",
        "governance/governance-record.json": "governance-record.schema.json",
        "request/run-request.json": "run-request.schema.json",
        "source/source-asset-manifest.json": "source-asset-manifest.schema.json",
    }
    if (staging / _POSITIVE_EVIDENCE_PATHS["task_fitness"]).is_file():
        schema_map[_POSITIVE_EVIDENCE_PATHS["task_fitness"]] = "task-fitness-evidence.schema.json"
    if (staging / _POSITIVE_EVIDENCE_PATHS["task_protocol"]).is_file():
        schema_map[_POSITIVE_EVIDENCE_PATHS["task_protocol"]] = "task-fitness-protocol.schema.json"
    runtime_relative = "validation/runtime-results.json"
    runtime_path = staging / runtime_relative
    if runtime_path.is_file():
        try:
            runtime_payload = json.loads(runtime_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            schema_map[runtime_relative] = "isaac-runtime-evidence.schema.json"
        else:
            if not isinstance(runtime_payload, Mapping) or runtime_payload.get("status") != "not_available":
                schema_map[runtime_relative] = "isaac-runtime-evidence.schema.json"
    results: list[dict[str, Any]] = []
    plan_path = staging / "request" / "run-plan.json"
    try:
        RunPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
        plan_errors: list[str] = []
    except (OSError, ValueError) as exc:
        plan_errors = [str(exc)]
    results.append(
        {
            "document": "request/run-plan.json",
            "schema": "python:asset_factory_blueprint.schemas.common.RunPlan",
            "status": "pass" if not plan_errors else "blocked",
            "errors": plan_errors,
        }
    )
    for relative, schema_name in sorted(schema_map.items()):
        target = staging / relative
        schema_path = staging / "schemas" / schema_name
        if not target.exists() or not schema_path.exists():
            results.append(
                {
                    "document": relative,
                    "schema": f"schemas/{schema_name}",
                    "status": "blocked",
                    "errors": ["document or schema snapshot is missing"],
                }
            )
            continue
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            results.append(
                {
                    "document": relative,
                    "schema": f"schemas/{schema_name}",
                    "status": "blocked",
                    "errors": [f"document or schema is unreadable: {exc}"],
                }
            )
            continue
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = [
            f"{'.'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
            for error in sorted(validator.iter_errors(payload), key=lambda item: list(item.absolute_path))
        ]
        results.append(
            {
                "document": relative,
                "schema": f"schemas/{schema_name}",
                "status": "pass" if not errors else "blocked",
                "errors": errors,
            }
        )
    attempt_schema = staging / "schemas" / "stage-attempt.schema.json"
    if attempt_schema.exists():
        try:
            schema = json.loads(attempt_schema.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            results.append(
                {
                    "document": "attempts",
                    "schema": "schemas/stage-attempt.schema.json",
                    "status": "blocked",
                    "errors": [f"attempt schema is unreadable: {exc}"],
                }
            )
            schema = None
        if schema is None:
            return {"status": "blocked", "results": results}
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for target in sorted((staging / "attempts").glob("*/*/*.json")):
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                results.append(
                    {
                        "document": target.relative_to(staging).as_posix(),
                        "schema": "schemas/stage-attempt.schema.json",
                        "status": "blocked",
                        "errors": [f"attempt evidence is unreadable: {exc}"],
                    }
                )
                continue
            if not isinstance(payload, dict) or "identity" not in payload:
                continue
            relative = target.relative_to(staging).as_posix()
            errors = [
                f"{'.'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
                for error in sorted(validator.iter_errors(payload), key=lambda item: list(item.absolute_path))
            ]
            results.append(
                {
                    "document": relative,
                    "schema": "schemas/stage-attempt.schema.json",
                    "status": "pass" if not errors else "blocked",
                    "errors": errors,
                }
            )
    for stage_id, schema_name in sorted(_STAGE_SCHEMAS.items()):
        schema_path = staging / "schemas" / schema_name
        if not schema_path.is_file():
            continue
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            results.append(
                {
                    "document": f"attempts/{stage_id}",
                    "schema": f"schemas/{schema_name}",
                    "status": "blocked",
                    "errors": [f"stage schema is unreadable: {exc}"],
                }
            )
            continue
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for target in sorted((staging / "attempts" / stage_id).glob("*/manifest.json")):
            relative = target.relative_to(staging).as_posix()
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                results.append(
                    {
                        "document": relative,
                        "schema": f"schemas/{schema_name}",
                        "status": "blocked",
                        "errors": [f"stage manifest is unreadable: {exc}"],
                    }
                )
                continue
            errors = [
                f"{'.'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
                for error in sorted(validator.iter_errors(payload), key=lambda item: list(item.absolute_path))
            ]
            results.append(
                {
                    "document": relative,
                    "schema": f"schemas/{schema_name}",
                    "status": "pass" if not errors else "blocked",
                    "errors": errors,
                }
            )
    return {
        "status": "pass" if results and all(item["status"] == "pass" for item in results) else "blocked",
        "results": results,
    }


def _profile_identity(profile_report: Mapping[str, Any]) -> dict[str, str]:
    profile = profile_report.get("simready_profile") or profile_report.get("profile") or {}
    if not isinstance(profile, Mapping):
        profile = {}
    return {
        "profile_id": str(profile.get("profile_id") or profile.get("id") or ""),
        "profile_version": str(profile.get("profile_version") or profile.get("version") or ""),
    }


def _declared_evidence_checksum(record: Mapping[str, Any], uri: str) -> str:
    for item in record.get("evidence") or []:
        if not isinstance(item, Mapping) or str(item.get("uri") or "") != uri:
            continue
        checksum = str(item.get("checksum") or "").removeprefix("sha256:")
        return checksum.lower() if _HASH_PATTERN.fullmatch(checksum) else ""
    return ""


def _runtime_report_checksum(profile_report: Mapping[str, Any]) -> str:
    conformance = profile_report.get("simready_conformance")
    runtime = conformance.get("runtime_validation") if isinstance(conformance, Mapping) else None
    checksum = str(runtime.get("report_sha256") or "").removeprefix("sha256:") if isinstance(runtime, Mapping) else ""
    return checksum.lower() if _HASH_PATTERN.fullmatch(checksum) else ""


def _generated_file_records(simready_manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for item in simready_manifest.get("evidence") or []:
        if not isinstance(item, Mapping) or not re.fullmatch(r"generated_asset_[0-9]+", str(item.get("evidence_id") or "")):
            continue
        path = str(item.get("uri") or "")
        checksum = str(item.get("checksum") or "").removeprefix("sha256:").lower()
        if not path or not _HASH_PATTERN.fullmatch(checksum):
            raise CapsuleCreationError(["SimReady file evidence contains a missing path or invalid checksum"])
        records.append({"path": path, "sha256": checksum})
    if not records:
        raise CapsuleCreationError(["selected run has no content-addressed SimReady file evidence"])
    paths = [item["path"] for item in records]
    if len(paths) != len(set(paths)):
        raise CapsuleCreationError(["SimReady file evidence contains duplicate paths"])
    return sorted(records, key=lambda item: (item["path"], item["sha256"]))


def _asset_fingerprint(simready_manifest: Mapping[str, Any]) -> str:
    files = _generated_file_records(simready_manifest)
    validation_sha256 = _declared_evidence_checksum(
        simready_manifest,
        "reports/generated-asset-validation-report.json",
    )
    if not validation_sha256:
        raise CapsuleCreationError(["SimReady evidence does not bind the generated-asset validation report"])
    return "sha256:" + sha256_text(
        json.dumps(
            {
                "files": files,
                "validation_report_sha256": validation_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _positive_evidence_blockers(
    governance: Mapping[str, Any],
    *,
    run_id: str,
    request_digest: str,
    release_scope: str,
    decision: Mapping[str, Any],
    profile_report: Mapping[str, Any],
    runtime_report: Mapping[str, Any],
    schema_results: Mapping[str, Any],
    evaluated_at: datetime,
) -> list[str]:
    blockers: list[str] = []
    profile = _profile_identity(profile_report)
    if not profile["profile_id"] or not profile["profile_version"]:
        blockers.append("positive capsule requires an exact SimReady Profile identity")
    if str(profile_report.get("status") or "").lower() not in {"pass", "passed", "validated"}:
        blockers.append("positive capsule requires a passed generated-asset validation report")
    conformance = profile_report.get("simready_conformance")
    if not isinstance(conformance, Mapping) or str(conformance.get("status") or "").lower() != "pass":
        blockers.append("positive capsule requires passed SimReady conformance evidence")
    else:
        requirements = [item for item in conformance.get("requirements") or [] if isinstance(item, Mapping)]
        features = [item for item in conformance.get("features") or [] if isinstance(item, Mapping)]
        official = conformance.get("official_validator")
        runtime_evidence = conformance.get("runtime_validation")
        if not requirements or any(str(item.get("status") or "").lower() != "pass" for item in requirements):
            blockers.append("positive capsule requires non-empty passed per-Requirement evidence")
        if not features or any(str(item.get("status") or "").lower() != "pass" for item in features):
            blockers.append("positive capsule requires non-empty passed per-Feature evidence")
        if not isinstance(official, Mapping) or str(official.get("status") or "").lower() != "pass":
            blockers.append("positive capsule requires a passed official validator record")
        if not isinstance(runtime_evidence, Mapping) or str(runtime_evidence.get("status") or "").lower() != "pass":
            blockers.append("positive capsule requires a passed runtime-validation record")
    if schema_results.get("status") != "pass":
        blockers.append("positive capsule requires all included contract documents to pass schema validation")
    required_gates = {str(item) for item in decision.get("required_gates") or []}
    if "isaac-load" in required_gates and str(runtime_report.get("status") or "").lower() != "pass":
        blockers.append("positive capsule scope requires passed Isaac runtime evidence")

    operator = governance.get("operator_decision")
    if not isinstance(operator, Mapping) or operator.get("decision") != "approve":
        blockers.append("positive capsule requires an approved operator decision")
        return blockers
    required_bindings = {
        "run_id": run_id,
        "request_digest": request_digest,
        "asset_fingerprint": str(governance.get("asset_fingerprint") or ""),
        "profile_id": profile["profile_id"],
        "profile_version": profile["profile_version"],
        "scope": release_scope,
    }
    for key, expected in required_bindings.items():
        if not expected or str(operator.get(key) or "") != expected:
            blockers.append(f"operator decision {key} is not bound to the positive capsule evidence")
    expires_at = _parse_time(operator.get("expires_at"))
    if expires_at is None or expires_at <= evaluated_at:
        blockers.append("operator decision expiry is missing, invalid or expired")
    if not operator.get("decided_by") or _parse_time(operator.get("decided_at")) is None:
        blockers.append("operator identity and decision time are required")
    return blockers


def _repository_publication_blockers(provenance: Mapping[str, Any]) -> list[str]:
    repository = provenance.get("repository")
    if not isinstance(repository, Mapping):
        return ["positive capsule requires repository provenance"]
    blockers: list[str] = []
    if repository.get("git_state") != "git":
        blockers.append("positive capsule requires a Git source revision")
    if repository.get("git_dirty") is not False:
        blockers.append("positive capsule requires a clean source revision")
    git_sha = str(repository.get("git_sha") or "")
    if not re.fullmatch(r"[A-Fa-f0-9]{40,64}", git_sha):
        blockers.append("positive capsule requires a concrete source commit SHA")
    return blockers


def _model_bom_publication_blockers(
    provenance: Mapping[str, Any],
    provider_assignments: Mapping[str, Any] | None = None,
) -> list[str]:
    model_bom = provenance.get("model_bom")
    if not isinstance(model_bom, list):
        return ["positive capsule requires a model BOM list"]
    blockers: list[str] = []
    compatibility_handles = provenance.get("provider_model_ids")
    roles_declared = provider_assignments is not None or isinstance(compatibility_handles, Mapping)
    expected_roles = set(provider_assignments or {})
    if isinstance(compatibility_handles, Mapping):
        expected_roles.update(str(role) for role in compatibility_handles)
    actual_roles = {
        str(record.get("role") or "")
        for record in model_bom
        if isinstance(record, Mapping) and record.get("role")
    }
    if roles_declared and actual_roles != expected_roles:
        blockers.append("model BOM roles do not exactly cover the run provider assignments")
    required_fields = ("role", "provider", "kind", "model_id", "runtime")
    for index, record in enumerate(model_bom):
        label = f"model BOM entry {index}"
        if not isinstance(record, Mapping):
            blockers.append(f"{label} must be an object")
            continue
        for field in required_fields:
            value = str(record.get(field) or "").strip()
            if value.lower() in _UNRESOLVED_MODEL_VALUES or value.startswith("redacted:"):
                blockers.append(f"{label} has unresolved {field}")
        resolution_status = str(record.get("resolution_status") or "").strip().lower()
        if resolution_status not in _RESOLVED_MODEL_STATUSES:
            blockers.append(f"{label} has unresolved resolution_status")
        revision = str(record.get("revision") or "").strip()
        if revision.lower() in _UNRESOLVED_MODEL_VALUES or revision.startswith("redacted:"):
            blockers.append(f"{label} has no pinned revision")
        weights_checksum = str(record.get("weights_checksum") or "").removeprefix("sha256:")
        if not _HASH_PATTERN.fullmatch(weights_checksum):
            blockers.append(f"{label} has no SHA-256 weights checksum")
        licence = str(record.get("licence_expression") or "").strip()
        if licence.lower() in _UNRESOLVED_MODEL_VALUES or licence.startswith("redacted:"):
            blockers.append(f"{label} has unresolved licence_expression")
    return blockers


def _write_readme(
    staging: Path,
    *,
    outcome: CapsuleOutcome,
    scope: str,
    run_id: str,
) -> None:
    content = f"""# Reference-run capsule

This capsule records a {outcome} decision for scope `{scope}` from run `{run_id}`.

Source media is excluded unless `capsule.json` records `source_media_included` as true. Portable evidence
representations and `capsule.json` are immutable records. Origin identities and digests preserve their run lineage. A
reproducer writes comparison results outside this directory.

## Verify

Run from an extracted source archive containing this version of `asset_factory_blueprint`:

```shell
python -c "import json,sys; from asset_factory_blueprint.capsule import validate_reference_capsule as v; r=v('.'); print(json.dumps(r, indent=2)); sys.exit(0 if r['valid'] else 1)"
```

Expected exit code: `0`.

`checksums.sha256` covers every capsule file except itself. `capsule.json` contains the deterministic payload inventory,
schema identities and release-decision identity.
"""
    (staging / "README.md").write_text(content, encoding="utf-8", newline="\n")


def _inventory_entry(
    staging: Path,
    path: Path,
    *,
    role: str,
    licence_expression: str,
    origin_sha256: str | None = None,
) -> dict[str, Any]:
    relative = path.relative_to(staging).as_posix()
    media_type = _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    entry: dict[str, Any] = {
        "path": relative,
        "role": role,
        "media_type": media_type,
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "licence_expression": licence_expression,
    }
    if origin_sha256:
        entry["origin_sha256"] = origin_sha256
    return entry


def _build_inventory(
    staging: Path,
    roles: Mapping[str, tuple[str, str, str | None]],
) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for path in sorted((item for item in staging.rglob("*") if item.is_file()), key=lambda item: item.relative_to(staging).as_posix()):
        relative = path.relative_to(staging).as_posix()
        if relative in {"capsule.json", "checksums.sha256"}:
            continue
        role, licence_expression, origin_sha256 = roles.get(relative, ("capsule_metadata", _MIT_LICENCE, None))
        inventory.append(
            _inventory_entry(
                staging,
                path,
                role=role,
                licence_expression=licence_expression,
                origin_sha256=origin_sha256,
            )
        )
    return inventory


def _write_checksums(staging: Path) -> Path:
    files = sorted(
        (path for path in staging.rglob("*") if path.is_file() and path.name != "checksums.sha256"),
        key=lambda item: item.relative_to(staging).as_posix(),
    )
    lines = [f"{sha256_file(path)}  {path.relative_to(staging).as_posix()}" for path in files]
    target = staging / "checksums.sha256"
    target.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\n")
    return target


def _copy_run_attempts(
    project_root: Path,
    run_root: Path,
    staging: Path,
    redactions: Counter[str],
    roles: dict[str, tuple[str, str, str | None]],
    licence_expression: str,
) -> list[str]:
    attempt_ids: list[str] = []
    attempts_root = run_root / "attempts"
    if attempts_root.exists():
        _reject_symlink_chain(project_root, attempts_root)
        for stage_root in sorted(attempts_root.iterdir(), key=lambda item: item.name):
            _reject_symlink_chain(project_root, stage_root)
            if not stage_root.is_dir():
                raise CapsuleCreationError([f"unexpected file in attempts root: {stage_root.name}"])
            ensure_path_component(stage_root.name, "attempt stage component")
            for attempt_root in sorted(stage_root.iterdir(), key=lambda item: item.name):
                _reject_symlink_chain(project_root, attempt_root)
                if not attempt_root.is_dir():
                    raise CapsuleCreationError([f"unexpected file in attempt stage: {attempt_root.name}"])
                ensure_path_component(attempt_root.name, "attempt ID")
                identity_path = attempt_root / f"{attempt_root.name}.json"
                if not identity_path.is_file() or _is_linklike(identity_path):
                    raise CapsuleCreationError(
                        [f"attempt directory has no matching identity record: {attempt_root.relative_to(attempts_root)}"]
                    )
        for source in sorted(attempts_root.rglob("*.json"), key=lambda item: item.relative_to(attempts_root).as_posix()):
            _reject_symlink_chain(project_root, source)
            if not source.is_file():
                continue
            relative = source.relative_to(attempts_root)
            if len(relative.parts) != 3:
                raise CapsuleCreationError([f"unexpected attempt evidence path: {relative.as_posix()}"])
            for part in relative.parts[:2]:
                ensure_path_component(part, "attempt path component")
            allowed_names = {f"{relative.parts[1]}.json", "manifest.json", "report.json"}
            if relative.name not in allowed_names:
                raise CapsuleCreationError([f"attempt evidence file is not allowlisted: {relative.as_posix()}"])
            destination = staging / "attempts" / relative
            _copy_sanitised_json(source, destination, redactions)
            capsule_relative = destination.relative_to(staging).as_posix()
            roles[capsule_relative] = ("stage_attempt_evidence", licence_expression, sha256_file(source))
            payload = json.loads(source.read_text(encoding="utf-8"))
            identity = payload.get("identity") if isinstance(payload, dict) else None
            if isinstance(identity, Mapping) and identity.get("attempt_id"):
                try:
                    expected_attempt_id = stage_attempt_id(
                        str(identity.get("run_id") or ""),
                        str(identity.get("stage_id") or ""),
                        int(identity.get("attempt_number") or 0),
                        str(identity.get("request_digest") or ""),
                    )
                except (TypeError, ValueError) as exc:
                    raise CapsuleCreationError([f"invalid stage attempt identity in {relative.as_posix()}: {exc}"]) from exc
                expected_relative = PurePosixPath(
                    str(identity.get("stage_id") or ""),
                    expected_attempt_id,
                    f"{expected_attempt_id}.json",
                )
                if identity.get("attempt_id") != expected_attempt_id or PurePosixPath(relative.as_posix()) != expected_relative:
                    raise CapsuleCreationError([f"stage attempt identity does not match its path: {relative.as_posix()}"])
                if expected_attempt_id in attempt_ids:
                    raise CapsuleCreationError([f"duplicate stage attempt identity: {expected_attempt_id}"])
                extensions = payload.get("extensions")
                snapshots = extensions.get("snapshots") if isinstance(extensions, Mapping) else None
                for snapshot_key, filename in (("manifest_path", "manifest.json"), ("report_path", "report.json")):
                    snapshot = snapshots.get(snapshot_key) if isinstance(snapshots, Mapping) else None
                    snapshot_file = source.parent / filename
                    if snapshot is None and not snapshot_file.exists():
                        continue
                    expected_snapshot_path = snapshot_file.relative_to(project_root).as_posix()
                    expected_snapshot_sha = sha256_file(snapshot_file) if snapshot_file.is_file() else ""
                    if (
                        not isinstance(snapshot, Mapping)
                        or snapshot.get("path") != expected_snapshot_path
                        or str(snapshot.get("sha256") or "").removeprefix("sha256:") != expected_snapshot_sha
                    ):
                        raise CapsuleCreationError(
                            [f"attempt snapshot does not match its immutable identity record: {relative.as_posix()}"]
                        )
                attempt_ids.append(expected_attempt_id)
    events = run_root / "events.jsonl"
    if events.exists():
        _reject_symlink_chain(project_root, events)
        destination = staging / "attempts" / "events.jsonl"
        _copy_sanitised_jsonl(events, destination, redactions)
        roles["attempts/events.jsonl"] = ("run_event_journal", licence_expression, sha256_file(events))
    return sorted(attempt_ids)


def _copy_outputs(
    project_root: Path,
    staging: Path,
    file_records: Sequence[Mapping[str, str]],
    source_hashes: set[str],
    roles: dict[str, tuple[str, str, str | None]],
    licence_expression: str,
    required_source_duplicate_paths: set[str] | None = None,
) -> tuple[int, int]:
    required_source_duplicate_paths = required_source_duplicate_paths or set()
    copied = 0
    excluded_sources = 0
    for record in file_records:
        project_relative = str(record.get("path") or "")
        if not project_relative.startswith("packaged/"):
            continue
        source = _project_file(project_root, project_relative)
        assert source is not None
        if source.suffix.lower() not in _OUTPUT_SUFFIXES:
            raise CapsuleCreationError([f"declared output type is not allowlisted: {source.suffix or '<none>'}"])
        source_sha256 = sha256_file(source)
        if source_sha256 != str(record.get("sha256") or "").lower():
            raise CapsuleCreationError([f"declared output checksum differs from project file: {project_relative}"])
        if source_sha256 in source_hashes and project_relative not in required_source_duplicate_paths:
            excluded_sources += 1
            continue
        relative = PurePosixPath(project_relative).relative_to("packaged")
        destination = staging / "outputs" / relative
        _copy_exact_safe(source, destination)
        if sha256_file(destination) != source_sha256:
            raise CapsuleCreationError([f"output changed while it was copied: {project_relative}"])
        capsule_relative = destination.relative_to(staging).as_posix()
        roles[capsule_relative] = ("released_output", licence_expression, source_sha256)
        copied += 1
    return copied, excluded_sources


def _package_closure_output_paths(profile_report: Mapping[str, Any]) -> set[str]:
    closure = profile_report.get("package_dependency_closure")
    package_path = str(profile_report.get("package_path") or "")
    if not isinstance(closure, Mapping) or closure.get("status") != "pass":
        return set()
    try:
        package = _safe_relative(package_path, "package path")
    except ValueError:
        return set()
    if package.parts[0] != "packaged":
        return set()
    result: set[str] = set()
    for record in closure.get("files") or []:
        if not isinstance(record, Mapping):
            continue
        try:
            dependency = _safe_relative(str(record.get("path") or ""), "package-closure path")
        except ValueError:
            continue
        result.add(PurePosixPath(package.parent, dependency).as_posix())
    return result


def _verify_generated_files(project_root: Path, file_records: Sequence[Mapping[str, str]]) -> None:
    blockers: list[str] = []
    for record in file_records:
        project_relative = str(record.get("path") or "")
        try:
            source = _project_file(project_root, project_relative)
        except (FileNotFoundError, ValueError) as exc:
            blockers.append(f"declared generated file is unavailable: {project_relative}: {exc}")
            continue
        assert source is not None
        if sha256_file(source) != str(record.get("sha256") or "").lower():
            blockers.append(f"declared generated file checksum differs: {project_relative}")
    if blockers:
        raise CapsuleCreationError(blockers)


def _copy_bound_json_evidence(
    project_root: Path,
    staging: Path,
    *,
    project_relative: str,
    expected_sha256: str,
    capsule_relative: str,
    role: str,
    licence_expression: str,
    redactions: Counter[str],
    roles: dict[str, tuple[str, str, str | None]],
    exact: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = expected_sha256.removeprefix("sha256:").lower()
    if not _HASH_PATTERN.fullmatch(expected):
        raise CapsuleCreationError([f"{role} has no valid bound SHA-256 digest"])
    try:
        source = _project_file(project_root, project_relative)
    except (FileNotFoundError, ValueError) as exc:
        raise CapsuleCreationError([f"{role} is unavailable at {project_relative}: {exc}"]) from exc
    assert source is not None
    actual = sha256_file(source)
    if actual != expected:
        raise CapsuleCreationError([f"{role} differs from its immutable source digest"])
    source_payload = _load_json(source, role)
    destination = staging.joinpath(*PurePosixPath(capsule_relative).parts)
    if exact:
        _copy_exact_safe(source, destination)
    else:
        _copy_sanitised_json(source, destination, redactions)
    portable_payload = _load_json(destination, f"portable {role}")
    roles[capsule_relative] = (role, licence_expression, actual)
    return source_payload, portable_payload


def _copy_bound_file_evidence(
    project_root: Path,
    staging: Path,
    *,
    project_relative: str,
    expected_sha256: str,
    capsule_relative: str,
    role: str,
    licence_expression: str,
    roles: dict[str, tuple[str, str, str | None]],
) -> None:
    expected = expected_sha256.removeprefix("sha256:").lower()
    if not _HASH_PATTERN.fullmatch(expected):
        raise CapsuleCreationError([f"{role} has no valid bound SHA-256 digest"])
    try:
        source = _project_file(project_root, project_relative)
    except (FileNotFoundError, ValueError) as exc:
        raise CapsuleCreationError([f"{role} is unavailable at {project_relative}: {exc}"]) from exc
    assert source is not None
    if source.suffix.lower() not in _TASK_FITNESS_EVIDENCE_SUFFIXES:
        raise CapsuleCreationError([f"{role} type is not allowlisted: {source.suffix or '<none>'}"])
    actual = sha256_file(source)
    if actual != expected:
        raise CapsuleCreationError([f"{role} differs from its immutable source digest"])
    destination = staging.joinpath(*PurePosixPath(capsule_relative).parts)
    _copy_exact_safe(source, destination)
    roles[capsule_relative] = (role, licence_expression, actual)


def _copy_positive_evidence_chain(
    project_root: Path,
    staging: Path,
    *,
    evaluation_manifest: Mapping[str, Any],
    profile_report: Mapping[str, Any],
    redactions: Counter[str],
    roles: dict[str, tuple[str, str, str | None]],
    licence_expression: str,
) -> None:
    task_fitness = evaluation_manifest.get("task_fitness")
    conformance = profile_report.get("simready_conformance")
    package_closure = profile_report.get("package_dependency_closure")
    if not isinstance(task_fitness, Mapping):
        raise CapsuleCreationError(["positive capsule requires immutable task-fitness stage evidence"])
    if not isinstance(conformance, Mapping):
        raise CapsuleCreationError(["positive capsule requires SimReady conformance evidence"])
    official = conformance.get("official_validator")
    openusd = conformance.get("openusd_compliance")
    if not isinstance(official, Mapping):
        raise CapsuleCreationError(["positive capsule requires an official validator evidence binding"])
    if not isinstance(openusd, Mapping):
        raise CapsuleCreationError(["positive capsule requires an OpenUSD evidence binding"])
    if not isinstance(package_closure, Mapping):
        raise CapsuleCreationError(["positive capsule requires a package-closure evidence binding"])

    task_source, _ = _copy_bound_json_evidence(
        project_root,
        staging,
        project_relative=str(task_fitness.get("report_path") or ""),
        expected_sha256=str(task_fitness.get("report_sha256") or ""),
        capsule_relative=_POSITIVE_EVIDENCE_PATHS["task_fitness"],
        role="task_fitness_evidence",
        licence_expression=licence_expression,
        redactions=redactions,
        roles=roles,
        exact=True,
    )
    protocol = task_source.get("protocol")
    if not isinstance(protocol, Mapping):
        raise CapsuleCreationError(["positive capsule task-fitness report has no protocol binding"])
    _copy_bound_json_evidence(
        project_root,
        staging,
        project_relative=str(protocol.get("path") or ""),
        expected_sha256=str(protocol.get("sha256") or ""),
        capsule_relative=_POSITIVE_EVIDENCE_PATHS["task_protocol"],
        role="task_fitness_protocol",
        licence_expression=licence_expression,
        redactions=redactions,
        roles=roles,
        exact=True,
    )
    evidence_records = [item for item in task_source.get("evidence") or [] if isinstance(item, Mapping)]
    if not evidence_records:
        raise CapsuleCreationError(["positive capsule task-fitness report has no measurement evidence"])
    seen_evidence_ids: set[str] = set()
    for record in evidence_records:
        evidence_id = str(record.get("evidence_id") or "")
        ensure_path_component(evidence_id, "task-fitness evidence ID")
        if evidence_id in seen_evidence_ids:
            raise CapsuleCreationError([f"duplicate task-fitness evidence ID: {evidence_id}"])
        seen_evidence_ids.add(evidence_id)
        project_relative = str(record.get("path") or "")
        suffix = PurePosixPath(project_relative).suffix.lower()
        capsule_relative = f"validation/fitness-evidence-files/{evidence_id}{suffix}"
        _copy_bound_file_evidence(
            project_root,
            staging,
            project_relative=project_relative,
            expected_sha256=str(record.get("sha256") or ""),
            capsule_relative=capsule_relative,
            role="task_fitness_measurement_evidence",
            licence_expression=licence_expression,
            roles=roles,
        )
    official_source, _ = _copy_bound_json_evidence(
        project_root,
        staging,
        project_relative=str(official.get("report_path") or ""),
        expected_sha256=str(official.get("report_sha256") or ""),
        capsule_relative=_POSITIVE_EVIDENCE_PATHS["official_normalised"],
        role="official_validator_normalised_evidence",
        licence_expression=licence_expression,
        redactions=redactions,
        roles=roles,
    )
    official_report_parent = PurePosixPath(str(official.get("report_path") or "")).parent
    raw_report_label = PurePosixPath(str(official_source.get("raw_report_path") or "")).name
    _copy_bound_json_evidence(
        project_root,
        staging,
        project_relative=(official_report_parent / raw_report_label).as_posix(),
        expected_sha256=str(official_source.get("raw_report_sha256") or ""),
        capsule_relative=_POSITIVE_EVIDENCE_PATHS["official_raw"],
        role="official_validator_raw_evidence",
        licence_expression=licence_expression,
        redactions=redactions,
        roles=roles,
        exact=True,
    )
    _copy_bound_json_evidence(
        project_root,
        staging,
        project_relative=str(openusd.get("report_path") or ""),
        expected_sha256=str(openusd.get("report_sha256") or ""),
        capsule_relative=_POSITIVE_EVIDENCE_PATHS["openusd"],
        role="openusd_compliance_evidence",
        licence_expression=licence_expression,
        redactions=redactions,
        roles=roles,
    )
    _copy_bound_json_evidence(
        project_root,
        staging,
        project_relative=str(package_closure.get("report_path") or ""),
        expected_sha256=str(package_closure.get("report_sha256") or ""),
        capsule_relative=_POSITIVE_EVIDENCE_PATHS["package_closure"],
        role="package_dependency_closure_evidence",
        licence_expression=licence_expression,
        redactions=redactions,
        roles=roles,
    )


def _copy_source_media(
    project_root: Path,
    staging: Path,
    source_manifest: Mapping[str, Any],
    roles: dict[str, tuple[str, str, str | None]],
    licence_expression: str,
) -> int:
    copied = 0
    for index, record in enumerate(source_manifest.get("source_assets") or []):
        if not isinstance(record, Mapping) or record.get("status") != "copied":
            continue
        raw_path = str(record.get("project_copy_path") or "")
        source = _project_file(project_root, raw_path)
        assert source is not None
        if source.suffix.lower() not in _SOURCE_MEDIA_SUFFIXES:
            raise CapsuleCreationError([f"source media type is not allowlisted: {source.suffix or '<none>'}"])
        expected = str(record.get("copy_sha256") or "").removeprefix("sha256:")
        actual = sha256_file(source)
        if not _HASH_PATTERN.fullmatch(expected) or expected.lower() != actual.lower():
            raise CapsuleCreationError([f"source media checksum does not match its manifest: {raw_path}"])
        filename = f"{index:04d}_{source.name}"
        ensure_path_component(filename, "source media filename")
        destination = staging / "source" / "redistributable-inputs" / filename
        _copy_exact_safe(source, destination)
        if sha256_file(destination) != actual:
            raise CapsuleCreationError([f"source media changed while it was copied: {raw_path}"])
        capsule_relative = destination.relative_to(staging).as_posix()
        roles[capsule_relative] = ("redistributable_source", licence_expression, actual)
        copied += 1
    return copied


def create_reference_capsule(
    project_dir: str | Path,
    destination: str | Path,
    *,
    outcome: CapsuleOutcome,
    run_id: str | None = None,
    release_scope: str | None = None,
    include_source_media: bool = False,
    include_outputs: bool | None = None,
) -> dict[str, Any]:
    """Create one immutable, sanitised reference-run capsule under a project snapshot lease."""

    project_input = Path(project_dir)
    if _has_linklike_component(project_input):
        raise CapsuleCreationError(["project directory must be a real directory, not a symlink"])
    project_root = project_input.resolve(strict=True)
    with workspace_lease(project_root, "capsule-export"):
        return _create_reference_capsule_locked(
            project_root,
            destination,
            outcome=outcome,
            run_id=run_id,
            release_scope=release_scope,
            include_source_media=include_source_media,
            include_outputs=include_outputs,
        )


def _create_reference_capsule_locked(
    project_dir: str | Path,
    destination: str | Path,
    *,
    outcome: CapsuleOutcome,
    run_id: str | None = None,
    release_scope: str | None = None,
    include_source_media: bool = False,
    include_outputs: bool | None = None,
) -> dict[str, Any]:
    """Create one immutable, sanitised reference-run capsule while the caller holds the project lease.

    Creation fails closed unless every source rights record permits
    redistribution. Positive and negative outcomes are bound to an exact
    governance decision. The destination must not exist, so a published
    capsule is never rewritten in place.
    """

    if outcome not in {"positive", "negative"}:
        raise ValueError("outcome must be 'positive' or 'negative'")
    if outcome == "positive" and include_outputs is False:
        raise CapsuleCreationError(["positive capsule cannot omit its released outputs"])
    outputs_requested = outcome == "positive" if include_outputs is None else include_outputs
    project_input = Path(project_dir)
    if _has_linklike_component(project_input):
        raise CapsuleCreationError(["project directory must be a real directory, not a symlink"])
    project_root = project_input.resolve(strict=True)
    if not project_root.is_dir():
        raise CapsuleCreationError(["project directory must be a real directory, not a symlink"])
    destination_path = Path(destination).resolve(strict=False)
    if destination_path.exists():
        raise FileExistsError(f"refusing to overwrite an existing capsule: {destination_path}")
    if destination_path == project_root or project_root in destination_path.parents:
        raise CapsuleCreationError(["capsule destination must be outside the source project"])

    project_manifest_path = _project_file(project_root, "project.json")
    assert project_manifest_path is not None
    project_manifest = _load_json(project_manifest_path, "project manifest")
    selected_run_id = run_id or str(project_manifest.get("active_run_id") or "")
    ensure_path_component(selected_run_id, "run ID")
    run_root = project_root / "runs" / selected_run_id
    _reject_symlink_chain(project_root, run_root)
    if not run_root.is_dir():
        raise CapsuleCreationError([f"run snapshot is missing: runs/{selected_run_id}"])

    request_source = _project_file(project_root, f"runs/{selected_run_id}/request.json")
    plan_source = _project_file(project_root, f"runs/{selected_run_id}/plan.json")
    assert request_source and plan_source
    plan = _load_json(plan_source, "run plan")
    request_digest = str(plan.get("request_digest") or "")
    if str(plan.get("run_id") or plan.get("id") or "") != selected_run_id:
        raise CapsuleCreationError(["run plan identity does not match the selected run"])
    try:
        request = RunRequest.model_validate_json(request_source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CapsuleCreationError([f"run request does not satisfy its typed contract: {exc}"]) from exc
    calculated_request_digest = "sha256:" + sha256_text(request.model_dump_json())
    if request_digest != calculated_request_digest:
        raise CapsuleCreationError(["run request content does not match the plan request digest"])
    source_manifest_source = _stage_manifest_snapshot(
        project_root,
        run_root,
        "source-ingestion",
        request_digest,
    )
    governance_source = _stage_manifest_snapshot(
        project_root,
        run_root,
        "governance",
        request_digest,
    )
    simready_manifest_source = (
        _stage_manifest_snapshot(
            project_root,
            run_root,
            "simready-verification",
            request_digest,
        )
        if outputs_requested
        else None
    )
    evaluation_manifest_source = (
        _stage_manifest_snapshot(
            project_root,
            run_root,
            "evaluation",
            request_digest,
        )
        if outcome == "positive"
        else None
    )
    provenance_source = _provenance_path(project_root, run_root)

    source_manifest = _load_json(source_manifest_source, "source asset manifest")
    governance = _load_json(governance_source, "governance record")
    simready_manifest = (
        _load_json(simready_manifest_source, "SimReady manifest") if simready_manifest_source is not None else {}
    )
    evaluation_manifest = (
        _load_json(evaluation_manifest_source, "evaluation manifest")
        if evaluation_manifest_source is not None
        else {}
    )
    provenance = _load_json(provenance_source, "run provenance")
    rights = _rights_records(source_manifest, governance)
    created_at = datetime.now(timezone.utc)
    raw_evidence = [
        item
        for collection in (source_manifest.get("evidence") or [], governance.get("evidence") or [])
        for item in collection
        if isinstance(item, Mapping)
    ]
    rights_evidence_sources = _rights_evidence_sources(project_root, rights, raw_evidence)
    rights_blockers = [
        *_rights_representation_blockers(source_manifest, governance),
        *_rights_coverage_blockers(source_manifest, rights),
        *_rights_blockers(rights, created_at, raw_evidence),
    ]
    if rights_blockers:
        raise CapsuleCreationError(rights_blockers)
    decision = _select_release_decision(governance, outcome, release_scope)
    selected_scope = str(decision.get("scope") or release_scope or "")
    if not selected_scope:
        raise CapsuleCreationError(["release decision scope is missing"])
    if outputs_requested:
        output_rights_blockers: list[str] = []
        required_uses = {str(item) for item in decision.get("required_uses") or []}
        for index, right in enumerate(rights):
            source_id = str(right.get("source_id") or f"source_{index}")
            if right.get("derivatives_allowed") is not True:
                output_rights_blockers.append(f"{source_id}: derivative outputs are not permitted")
            permitted_uses = {str(item) for item in right.get("permitted_uses") or []}
            for required_use in required_uses:
                if "*" not in permitted_uses and required_use not in permitted_uses and selected_scope not in permitted_uses:
                    output_rights_blockers.append(f"{source_id}: {required_use} output use is not permitted")
        if output_rights_blockers:
            raise CapsuleCreationError(output_rights_blockers)
    if str(provenance.get("run_id") or "") != selected_run_id:
        raise CapsuleCreationError(["provenance identity does not match the selected run"])
    if provenance_blocker := _provenance_identity_blocker(provenance):
        raise CapsuleCreationError([provenance_blocker])
    governance_provenance = governance.get("provenance")
    if not isinstance(governance_provenance, Mapping) or governance_provenance.get("run_id") != selected_run_id:
        raise CapsuleCreationError(["governance evidence is not bound to the selected run"])
    project_id = str(project_manifest.get("project_id") or "")
    if not project_id or any(
        str(record.get("project_id") or "") != project_id for record in (source_manifest, governance)
    ):
        raise CapsuleCreationError(["project, source and governance identities do not match"])
    generated_file_records: list[dict[str, str]] = []
    if outputs_requested:
        generated_file_records = _generated_file_records(simready_manifest)
        _verify_generated_files(project_root, generated_file_records)
        if _declared_evidence_checksum(
            simready_manifest,
            "reports/generated-asset-validation-report.json",
        ) != _declared_evidence_checksum(
            governance,
            "reports/generated-asset-validation-report.json",
        ):
            raise CapsuleCreationError(
                ["SimReady and governance evidence bind different generated-asset validation reports"]
            )
        if _asset_fingerprint(simready_manifest) != governance.get("asset_fingerprint"):
            raise CapsuleCreationError(["governance asset fingerprint does not match selected-run SimReady evidence"])

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination_path.name}.", dir=destination_path.parent))
    redactions: Counter[str] = Counter()
    roles: dict[str, tuple[str, str, str | None]] = {}
    aggregate_licence = _licence_expression(rights)
    try:
        copies = (
            (request_source, staging / "request" / "run-request.json", "run_request"),
            (plan_source, staging / "request" / "run-plan.json", "run_plan"),
            (source_manifest_source, staging / "source" / "source-asset-manifest.json", "source_manifest"),
            (governance_source, staging / "governance" / "governance-record.json", "governance_record"),
            (provenance_source, staging / "environment" / "provenance.json", "provenance_record"),
        )
        for source, target, role in copies:
            _copy_sanitised_json(source, target, redactions)
            roles[target.relative_to(staging).as_posix()] = (role, aggregate_licence, sha256_file(source))

        sanitised_request = RunRequest.model_validate_json(
            (staging / "request" / "run-request.json").read_text(encoding="utf-8")
        )
        capsule_request_digest = "sha256:" + sha256_text(sanitised_request.model_dump_json())
        sanitised_provenance = json.loads((staging / "environment" / "provenance.json").read_text(encoding="utf-8"))
        origin_provenance_id = str(provenance.get("provenance_id") or "")
        provenance_extensions = sanitised_provenance.get("extensions")
        if not isinstance(provenance_extensions, dict):
            provenance_extensions = {}
        provenance_extensions["capsule_origin"] = {
            "provenance_id": origin_provenance_id,
            "sha256": sha256_file(provenance_source),
            "representation": "sanitised",
        }
        sanitised_provenance["extensions"] = provenance_extensions
        provenance_core_keys = (
            "schema_version",
            "run_id",
            "attempt_ids",
            "repository",
            "environment_bom",
            "model_bom",
            "prompt_checksums",
            "config_checksums",
            "manifest_ids",
            "source_assets",
            "source_assets_mutated",
            "reproducibility",
        )
        sanitised_provenance["provenance_id"] = content_id(
            "prov",
            {key: sanitised_provenance.get(key) for key in provenance_core_keys},
            digest_length=32,
        )
        _write_json(staging / "environment" / "provenance.json", sanitised_provenance)
        capsule_provenance_id = str(sanitised_provenance["provenance_id"])
        runtime = {
            "environment_bom": sanitised_provenance.get("environment_bom", {}),
            "reproducibility": sanitised_provenance.get("reproducibility", {}),
        }
        software_bom = {
            "repository": sanitised_provenance.get("repository", {}),
            "tool_versions": sanitised_provenance.get("tool_versions", {}),
            "python_packages": sanitised_provenance.get("environment_bom", {}).get("python", {}).get("packages", {}),
        }
        models = {
            "model_bom": sanitised_provenance.get("model_bom", []),
            "provider_model_ids": sanitised_provenance.get("provider_model_ids", {}),
        }
        _write_json(staging / "environment" / "runtime.json", runtime)
        _write_json(staging / "environment" / "software-bom.json", software_bom)
        _write_json(staging / "environment" / "models.json", models)
        for relative, role in (
            ("environment/runtime.json", "runtime_environment"),
            ("environment/software-bom.json", "software_bom"),
            ("environment/models.json", "model_bom"),
        ):
            roles[relative] = (role, aggregate_licence, None)

        declared_evidence_ids = {
            str(evidence_id)
            for right in rights
            for evidence_id in [*(right.get("evidence_ids") or []), *(right.get("consent_evidence_ids") or [])]
        }
        declared_evidence = {
            str(item.get("evidence_id")): item
            for item in raw_evidence
            if str(item.get("evidence_id") or "") in declared_evidence_ids
        }
        portable_evidence: list[dict[str, Any]] = []
        for index, evidence_id in enumerate(sorted(declared_evidence)):
            source = rights_evidence_sources[evidence_id]
            filename = f"{index:04d}_{sha256_text(evidence_id)[:16]}{source.suffix.lower()}"
            destination = staging / "source" / "rights-evidence-files" / filename
            _copy_exact_safe(source, destination)
            expected_sha256 = sha256_file(source)
            if sha256_file(destination) != expected_sha256:
                raise CapsuleCreationError([f"rights evidence changed while it was copied: {evidence_id}"])
            capsule_relative = destination.relative_to(staging).as_posix()
            roles[capsule_relative] = ("rights_evidence_file", aggregate_licence, expected_sha256)
            portable_record = dict(declared_evidence[evidence_id])
            portable_record["capsule_path"] = capsule_relative
            portable_evidence.append(portable_record)
        rights_evidence = {
            "evaluation_time": "capsule.json:rights_gate.evaluated_at",
            "status": "cleared",
            "redistribution_allowed": True,
            "source_rights": _sanitise_json(list(rights), redactions),
            "declared_evidence": _sanitise_json(portable_evidence, redactions),
        }
        _write_json(staging / "source" / "rights-evidence.json", rights_evidence)
        roles["source/rights-evidence.json"] = ("rights_evidence", aggregate_licence, None)

        origin_release_decision_id = str(decision.get("decision_id") or "")
        capsule_decision = _sanitise_json(decision, redactions)
        capsule_decision["decision_id"] = content_id(
            "release",
            {
                "governance_id": governance.get("id"),
                "scope": capsule_decision.get("scope"),
                "policy_version": capsule_decision.get("policy_version"),
                "blockers": sorted(str(item) for item in capsule_decision.get("blockers") or []),
            },
            digest_length=32,
        )
        _write_json(staging / "governance" / "release-decision.json", capsule_decision)
        portable_governance_path = staging / "governance" / "governance-record.json"
        portable_governance = json.loads(portable_governance_path.read_text(encoding="utf-8"))
        portable_decisions = [
            capsule_decision
            if isinstance(item, Mapping) and item.get("decision_id") == origin_release_decision_id
            else item
            for item in portable_governance.get("release_decisions") or []
        ]
        portable_governance["release_decisions"] = portable_decisions
        portable_extensions = portable_governance.get("extensions")
        if not isinstance(portable_extensions, dict):
            portable_extensions = {}
        portable_extensions["capsule_origin"] = {"release_decision_id": origin_release_decision_id}
        portable_governance["extensions"] = portable_extensions
        _write_json(portable_governance_path, portable_governance)
        roles["governance/release-decision.json"] = ("release_decision", aggregate_licence, None)

        attempt_ids = _copy_run_attempts(
            project_root,
            run_root,
            staging,
            redactions,
            roles,
            aggregate_licence,
        )
        provenance_attempt_ids = sorted({str(item) for item in provenance.get("attempt_ids") or [] if str(item)})
        if attempt_ids != provenance_attempt_ids:
            raise CapsuleCreationError(
                [
                    "run attempt evidence is incomplete: copied attempt identities do not exactly match "
                    "the final provenance record"
                ]
            )
        schema_inventory = _copy_schema_snapshots(
            staging,
            ROOT / "schemas",
        )
        for item in schema_inventory:
            roles[item["path"]] = ("schema_snapshot", _MIT_LICENCE, None)

        report_sources = {
            "profile-results.json": "reports/generated-asset-validation-report.json",
            "runtime-results.json": "reports/isaac-load-check.json",
        }
        report_payloads: dict[str, dict[str, Any]] = {}
        for destination_name, project_relative in report_sources.items():
            source = _project_file(project_root, project_relative, required=False)
            destination_report = staging / "validation" / destination_name
            expected_checksum = (
                _declared_evidence_checksum(governance, project_relative)
                if destination_name == "profile-results.json"
                else _runtime_report_checksum(report_payloads.get("profile-results.json", {}))
            )
            if source is None or not expected_checksum:
                payload = {
                    "status": "not_available",
                    "source_report": project_relative,
                    "reason": (
                        "the project did not record this validation report"
                        if source is None
                        else "the selected run did not bind this report to a content digest"
                    ),
                }
                _write_json(destination_report, payload)
                roles[f"validation/{destination_name}"] = ("validation_evidence", aggregate_licence, None)
                report_payloads[destination_name] = payload
            else:
                actual_checksum = sha256_file(source)
                if actual_checksum.lower() != expected_checksum:
                    raise CapsuleCreationError(
                        [f"{project_relative} differs from the digest bound by the selected run"]
                    )
                _copy_sanitised_json(source, destination_report, redactions)
                roles[f"validation/{destination_name}"] = (
                    "validation_evidence",
                    aggregate_licence,
                    actual_checksum,
                )
                report_payloads[destination_name] = json.loads(destination_report.read_text(encoding="utf-8"))

        if outcome == "positive":
            _copy_positive_evidence_chain(
                project_root,
                staging,
                evaluation_manifest=evaluation_manifest,
                profile_report=report_payloads["profile-results.json"],
                redactions=redactions,
                roles=roles,
                licence_expression=aggregate_licence,
            )

        schema_results = _schema_validation_results(staging)
        _write_json(staging / "validation" / "schema-results.json", schema_results)
        roles["validation/schema-results.json"] = ("schema_validation", _MIT_LICENCE, None)
        if schema_results.get("status") != "pass":
            raise CapsuleCreationError(["capsule contract evidence does not pass its trusted schema snapshots"])

        source_hashes = {
            str(item.get("copy_sha256") or "").removeprefix("sha256:").lower()
            for item in source_manifest.get("source_assets") or []
            if isinstance(item, Mapping) and _HASH_PATTERN.fullmatch(str(item.get("copy_sha256") or "").removeprefix("sha256:"))
        }
        output_count = 0
        excluded_source_outputs = 0
        if outputs_requested:
            output_count, excluded_source_outputs = _copy_outputs(
                project_root,
                staging,
                generated_file_records,
                source_hashes,
                roles,
                aggregate_licence,
                required_source_duplicate_paths=(
                    _package_closure_output_paths(report_payloads["profile-results.json"])
                    if outcome == "positive"
                    else set()
                ),
            )
            if outcome == "positive" and output_count == 0:
                raise CapsuleCreationError(["positive capsule requires at least one allowlisted packaged output"])
        source_media_count = (
            _copy_source_media(project_root, staging, source_manifest, roles, aggregate_licence)
            if include_source_media
            else 0
        )
        if outcome == "positive":
            positive_blockers = _positive_evidence_blockers(
                governance,
                run_id=selected_run_id,
                request_digest=request_digest,
                release_scope=selected_scope,
                decision=decision,
                profile_report=report_payloads["profile-results.json"],
                runtime_report=report_payloads["runtime-results.json"],
                schema_results=schema_results,
                evaluated_at=created_at,
            )
            positive_blockers.extend(_repository_publication_blockers(sanitised_provenance))
            positive_blockers.extend(
                _model_bom_publication_blockers(sanitised_provenance, plan.get("provider_assignments"))
            )
            if positive_blockers:
                raise CapsuleCreationError(positive_blockers)

        capsule_basis = {
            "format_version": CAPSULE_FORMAT_VERSION,
            "created_at": created_at.isoformat(),
            "outcome": outcome,
            "project_id": project_id,
            "run_id": selected_run_id,
            "request_digest": str(plan.get("request_digest") or ""),
            "capsule_request_digest": capsule_request_digest,
            "release_scope": selected_scope,
            "release_decision_id": str(capsule_decision.get("decision_id") or ""),
            "origin_release_decision_id": origin_release_decision_id,
            "provenance_id": capsule_provenance_id,
            "origin_provenance_id": origin_provenance_id,
        }
        _write_readme(
            staging,
            outcome=outcome,
            scope=selected_scope,
            run_id=selected_run_id,
        )
        roles["README.md"] = ("reproduction_instructions", _MIT_LICENCE, None)

        inventory = _build_inventory(staging, roles)
        inventory_digest = sha256_text(_canonical_json(inventory))
        capsule_id = "capsule_" + sha256_text(
            _canonical_json({**capsule_basis, "payload_inventory_sha256": inventory_digest})
        )[:32]

        profile_identity = _profile_identity(report_payloads["profile-results.json"])
        manifest = {
            "capsule_id": capsule_id,
            "format_version": CAPSULE_FORMAT_VERSION,
            "created_at": created_at.isoformat(),
            "outcome": outcome,
            "project_id": project_id,
            "run_id": selected_run_id,
            "request_digest": str(plan.get("request_digest") or ""),
            "capsule_request_digest": capsule_request_digest,
            "provenance_id": capsule_provenance_id,
            "origin_provenance_id": origin_provenance_id,
            "origin_release_decision_id": origin_release_decision_id,
            "release_scope": selected_scope,
            "release_decision": {
                "decision_id": str(capsule_decision.get("decision_id") or ""),
                "scope": str(capsule_decision.get("scope") or ""),
                "policy_version": str(capsule_decision.get("policy_version") or ""),
                "release_status": str(capsule_decision.get("release_status") or ""),
                "release_allowed": capsule_decision.get("release_allowed") is True,
                "required_uses": [str(item) for item in capsule_decision.get("required_uses") or []],
                "required_gates": [str(item) for item in capsule_decision.get("required_gates") or []],
                "blockers": [str(item) for item in capsule_decision.get("blockers") or []],
            },
            "simready_profile": profile_identity,
            "attempt_ids": attempt_ids,
            "source_media_included": source_media_count > 0,
            "source_media_count": source_media_count,
            "outputs_included": output_count > 0,
            "output_file_count": output_count,
            "source_duplicates_excluded_from_outputs": excluded_source_outputs,
            "rights_gate": {
                "status": "pass",
                "evaluated_at": created_at.isoformat(),
                "source_count": len(rights),
                "redistribution_allowed": True,
                "licence_expression": aggregate_licence,
            },
            "sanitisation": {
                "absolute_paths_removed": redactions["absolute_path"],
                "private_endpoints_removed": redactions["private_endpoint"],
                "secret_values_removed": redactions["secret_value"],
                "signed_urls_removed": redactions["signed_url"] + redactions["credentialled_url"],
                "unsafe_keys_removed": sum(
                    redactions[key]
                    for key in ("secret_key_name", "endpoint_key_name", "path_key_name")
                ),
            },
            "schema_inventory": schema_inventory,
            "payload_inventory_sha256": inventory_digest,
            "inventory": inventory,
            "container_metadata": {
                "capsule.json": {"licence_expression": _MIT_LICENCE, "role": "immutable_capsule_manifest"},
                "checksums.sha256": {"licence_expression": _MIT_LICENCE, "role": "checksum_manifest"},
            },
        }
        immutable_write_json(staging / "capsule.json", manifest)
        checksum_path = _write_checksums(staging)

        validation = validate_reference_capsule(staging)
        if not validation["valid"]:
            raise CapsuleCreationError(
                [
                    f"self-validation failed at {item['path'] or '<capsule>'}: "
                    f"{item['code']}: {item['message']}"
                    for item in validation["errors"]
                ]
            )
        os.rename(staging, destination_path)
        return {
            "status": "created",
            "valid": True,
            "capsule_id": capsule_id,
            "outcome": outcome,
            "run_id": selected_run_id,
            "destination": str(destination_path),
            "capsule_manifest_sha256": sha256_file(destination_path / "capsule.json"),
            "checksums_sha256": sha256_file(destination_path / checksum_path.name),
            "file_count": sum(1 for path in destination_path.rglob("*") if path.is_file()),
            "validation": validation,
        }
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _validation_error(errors: list[dict[str, str]], code: str, message: str, path: str = "") -> None:
    errors.append({"code": code, "message": message, "path": path})


def _validate_capsule_path(relative: str) -> str | None:
    try:
        path = _safe_relative(relative)
    except ValueError as exc:
        return str(exc)
    if path.parts[0] not in _CAPSULE_TOP_LEVEL:
        return f"top-level path is not allowlisted: {path.parts[0]}"
    if len(path.parts) == 1:
        return None if relative in {"README.md", "capsule.json", "checksums.sha256"} else "unexpected top-level file"
    top_level = path.parts[0]
    if top_level == "request" and relative not in {"request/run-plan.json", "request/run-request.json"}:
        return "request evidence path is not allowlisted"
    if top_level == "environment" and relative not in {
        "environment/models.json",
        "environment/provenance.json",
        "environment/runtime.json",
        "environment/software-bom.json",
    }:
        return "environment evidence path is not allowlisted"
    if top_level == "governance" and relative not in {
        "governance/governance-record.json",
        "governance/release-decision.json",
    }:
        return "governance evidence path is not allowlisted"
    if top_level == "validation" and relative not in {
        "validation/official-validator-raw.json",
        "validation/official-validator-results.json",
        "validation/openusd-compliance.json",
        "validation/package-dependency-closure.json",
        "validation/profile-results.json",
        "validation/runtime-results.json",
        "validation/schema-results.json",
        "validation/task-fitness-evidence.json",
        "validation/task-fitness-protocol.json",
    } and path.parts[:2] != ("validation", "fitness-evidence-files"):
        return "validation evidence path is not allowlisted"
    if path.parts[:2] == ("validation", "fitness-evidence-files"):
        if len(path.parts) != 3 or Path(path.name).suffix.lower() not in _TASK_FITNESS_EVIDENCE_SUFFIXES:
            return "task-fitness measurement evidence path or type is not allowlisted"
    if top_level == "schemas" and (len(path.parts) != 2 or not path.name.endswith(".schema.json")):
        return "schema snapshot path is not allowlisted"
    if top_level == "attempts" and relative != "attempts/events.jsonl":
        if len(path.parts) != 4 or Path(path.name).suffix.lower() != ".json":
            return "stage-attempt evidence path is not allowlisted"
        if path.name not in {f"{path.parts[2]}.json", "manifest.json", "report.json"}:
            return "stage-attempt evidence filename is not allowlisted"
    if top_level == "source":
        if (
            relative not in {"source/rights-evidence.json", "source/source-asset-manifest.json"}
            and path.parts[:2] not in {
                ("source", "redistributable-inputs"),
                ("source", "rights-evidence-files"),
            }
        ):
            return "source evidence path is not allowlisted"
    if top_level == "outputs" and Path(path.name).suffix.lower() not in _OUTPUT_SUFFIXES:
        return f"output type is not allowlisted: {Path(path.name).suffix or '<none>'}"
    if path.parts[:2] == ("source", "redistributable-inputs") and Path(path.name).suffix.lower() not in _SOURCE_MEDIA_SUFFIXES:
        return f"source media type is not allowlisted: {Path(path.name).suffix or '<none>'}"
    if path.parts[:2] == ("source", "rights-evidence-files") and Path(path.name).suffix.lower() not in _RIGHTS_EVIDENCE_SUFFIXES:
        return f"rights evidence type is not allowlisted: {Path(path.name).suffix or '<none>'}"
    return None


def _parse_checksums(path: Path, errors: list[dict[str, str]]) -> dict[str, str]:
    checksums: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        _validation_error(errors, "checksums_unreadable", str(exc), "checksums.sha256")
        return checksums
    for line_number, line in enumerate(lines, start=1):
        match = re.fullmatch(r"([A-Fa-f0-9]{64})  (.+)", line)
        if not match:
            _validation_error(
                errors,
                "checksums_malformed",
                f"malformed checksum line {line_number}",
                "checksums.sha256",
            )
            continue
        digest, relative = match.groups()
        if path_error := _validate_capsule_path(relative):
            _validation_error(errors, "path_not_allowed", path_error, relative)
            continue
        if relative in checksums:
            _validation_error(errors, "duplicate_checksum_path", "path occurs more than once", relative)
            continue
        checksums[relative] = digest.lower()
    return checksums


def _validate_outcome(manifest: Mapping[str, Any], root: Path, errors: list[dict[str, str]]) -> None:
    outcome = manifest.get("outcome")
    decision = manifest.get("release_decision")
    if outcome not in {"positive", "negative"} or not isinstance(decision, Mapping):
        _validation_error(errors, "outcome_invalid", "outcome and release decision are required", "capsule.json")
        return
    allowed = decision.get("release_allowed") is True
    status = str(decision.get("release_status") or "")
    blockers = [str(item) for item in decision.get("blockers") or []]
    if any(not item.strip() for item in blockers):
        _validation_error(
            errors,
            "decision_blocker_invalid",
            "release decision blockers must be non-empty strings",
            "capsule.json",
        )
    if outcome == "positive" and (not allowed or status != "approved" or blockers):
        _validation_error(
            errors,
            "positive_outcome_mismatch",
            "positive capsule does not carry an approved blocker-free decision",
            "capsule.json",
        )
    if outcome == "negative" and (allowed or status != "blocked" or not blockers):
        _validation_error(
            errors,
            "negative_outcome_mismatch",
            "negative capsule does not carry a blocked decision with blockers",
            "capsule.json",
        )
    decision_path = root / "governance" / "release-decision.json"
    if decision_path.exists():
        try:
            recorded = json.loads(decision_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _validation_error(errors, "decision_unreadable", str(exc), "governance/release-decision.json")
            return
        fields = (
            "decision_id",
            "scope",
            "policy_version",
            "release_status",
            "release_allowed",
            "required_uses",
            "required_gates",
            "blockers",
        )
        if any(recorded.get(field) != decision.get(field) for field in fields):
            _validation_error(
                errors,
                "decision_identity_mismatch",
                "capsule decision summary differs from the recorded decision",
                "governance/release-decision.json",
            )
        try:
            governance = json.loads((root / "governance" / "governance-record.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            governance = {}
        expected_decision_id = content_id(
            "release",
            {
                "governance_id": governance.get("id") if isinstance(governance, Mapping) else None,
                "scope": recorded.get("scope"),
                "policy_version": recorded.get("policy_version"),
                "blockers": sorted(str(item) for item in recorded.get("blockers") or []),
            },
            digest_length=32,
        )
        if recorded.get("decision_id") != expected_decision_id:
            _validation_error(
                errors,
                "decision_id_mismatch",
                "release decision identifier does not match its decision basis",
                "governance/release-decision.json",
            )
        origin_id = manifest.get("origin_release_decision_id")
        portable_matches = [
            item
            for item in governance.get("release_decisions") or []
            if isinstance(item, Mapping)
            and item.get("decision_id") == recorded.get("decision_id")
            and _canonical_json(item) == _canonical_json(recorded)
        ] if isinstance(governance, Mapping) else []
        declared_origin = (
            governance.get("extensions", {}).get("capsule_origin", {}).get("release_decision_id")
            if isinstance(governance, Mapping)
            else None
        )
        if len(portable_matches) != 1 or declared_origin != origin_id:
            _validation_error(
                errors,
                "origin_decision_mismatch",
                "sanitised decision is not bound to exactly one governance origin decision",
                "governance/governance-record.json",
            )


def _validate_capsule_identity(manifest: Mapping[str, Any], errors: list[dict[str, str]]) -> None:
    decision = manifest.get("release_decision")
    decision_id = str(decision.get("decision_id") or "") if isinstance(decision, Mapping) else ""
    basis = {
        "format_version": manifest.get("format_version"),
        "created_at": manifest.get("created_at"),
        "outcome": manifest.get("outcome"),
        "project_id": manifest.get("project_id"),
        "run_id": manifest.get("run_id"),
        "request_digest": manifest.get("request_digest"),
        "capsule_request_digest": manifest.get("capsule_request_digest"),
        "release_scope": manifest.get("release_scope"),
        "release_decision_id": decision_id,
        "provenance_id": manifest.get("provenance_id"),
        "origin_provenance_id": manifest.get("origin_provenance_id"),
        "origin_release_decision_id": manifest.get("origin_release_decision_id"),
        "payload_inventory_sha256": manifest.get("payload_inventory_sha256"),
    }
    expected = "capsule_" + sha256_text(_canonical_json(basis))[:32]
    if manifest.get("capsule_id") != expected:
        _validation_error(
            errors,
            "capsule_id_mismatch",
            "capsule identifier is not derived from its run, decision and payload inventory",
            "capsule.json",
        )


def _validate_rights(root: Path, manifest: Mapping[str, Any], errors: list[dict[str, str]]) -> None:
    rights_path = root / "source" / "rights-evidence.json"
    try:
        payload = json.loads(rights_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _validation_error(errors, "rights_unreadable", str(exc), "source/rights-evidence.json")
        return
    evaluated_at = _parse_time(manifest.get("created_at"))
    if evaluated_at is None:
        _validation_error(errors, "created_at_invalid", "capsule creation time is invalid", "capsule.json")
        return
    rights = payload.get("source_rights") if isinstance(payload, Mapping) else None
    rights_records = [item for item in rights or [] if isinstance(item, Mapping)] if isinstance(rights, list) else []
    declared_evidence = payload.get("declared_evidence") if isinstance(payload, Mapping) else None
    evidence_records = (
        [item for item in declared_evidence if isinstance(item, Mapping)] if isinstance(declared_evidence, list) else []
    )
    try:
        source_manifest = json.loads((root / "source" / "source-asset-manifest.json").read_text(encoding="utf-8"))
        governance = json.loads((root / "governance" / "governance-record.json").read_text(encoding="utf-8"))
        if not isinstance(source_manifest, dict) or not isinstance(governance, dict):
            raise TypeError("source and governance rights context must be JSON objects")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        _validation_error(errors, "rights_context_unreadable", str(exc), "source/rights-evidence.json")
        return
    for blocker in _rights_representation_blockers(source_manifest, governance):
        _validation_error(errors, "rights_representation_mismatch", blocker, "source/rights-evidence.json")
    source_rights = _rights_records(source_manifest, governance)
    if _canonical_json(source_rights) != _canonical_json(rights_records):
        _validation_error(
            errors,
            "rights_evidence_mismatch",
            "rights evidence does not match the selected source snapshot",
            "source/rights-evidence.json",
        )
    for blocker in _rights_coverage_blockers(source_manifest, rights_records):
        _validation_error(errors, "rights_coverage_failed", blocker, "source/source-asset-manifest.json")
    for blocker in _rights_blockers(rights_records, evaluated_at, evidence_records):
        _validation_error(errors, "rights_gate_failed", blocker, "source/rights-evidence.json")
    for item in evidence_records:
        evidence_id = str(item.get("evidence_id") or "")
        capsule_path = str(item.get("capsule_path") or "")
        expected = str(item.get("checksum") or "").removeprefix("sha256:").lower()
        try:
            safe_path = _safe_relative(capsule_path, "rights evidence capsule path")
            if safe_path.parts[:2] != ("source", "rights-evidence-files"):
                raise ValueError("rights evidence is outside its allowlisted capsule directory")
            target = root.joinpath(*safe_path.parts)
            _reject_symlink_chain(root, target)
        except ValueError as exc:
            _validation_error(errors, "rights_evidence_path_invalid", str(exc), capsule_path)
            continue
        if not target.is_file() or not _HASH_PATTERN.fullmatch(expected) or sha256_file(target) != expected:
            _validation_error(
                errors,
                "rights_evidence_digest_mismatch",
                f"materialised rights evidence differs: {evidence_id}",
                capsule_path,
            )
    gate = manifest.get("rights_gate")
    if not isinstance(gate, Mapping) or gate.get("status") != "pass" or gate.get("redistribution_allowed") is not True:
        _validation_error(errors, "rights_gate_invalid", "manifest rights gate is not a pass", "capsule.json")
    elif (
        gate.get("source_count") != len(rights_records)
        or gate.get("licence_expression") != _licence_expression(rights_records)
    ):
        _validation_error(
            errors,
            "rights_gate_summary_mismatch",
            "manifest rights summary differs from the rights evidence",
            "capsule.json",
        )
    if manifest.get("outputs_included") is True:
        decision = manifest.get("release_decision")
        required_uses = {str(item) for item in decision.get("required_uses") or []} if isinstance(decision, Mapping) else set()
        release_scope = str(manifest.get("release_scope") or "")
        for index, right in enumerate(rights_records):
            source_id = str(right.get("source_id") or f"source_{index}")
            if right.get("derivatives_allowed") is not True:
                _validation_error(
                    errors,
                    "rights_derivatives_forbidden",
                    f"{source_id}: derivative outputs are not permitted",
                    "source/rights-evidence.json",
                )
            permitted_uses = {str(item) for item in right.get("permitted_uses") or []}
            for required_use in required_uses:
                if "*" not in permitted_uses and required_use not in permitted_uses and release_scope not in permitted_uses:
                    _validation_error(
                        errors,
                        "rights_output_use_forbidden",
                        f"{source_id}: {required_use} output use is not permitted",
                        "source/rights-evidence.json",
                    )


def _latest_capsule_stage_manifest(
    root: Path,
    stage_id: str,
    capsule_manifest: Mapping[str, Any],
    inventory: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    candidates: list[tuple[int, str, Path]] = []
    for attempt_root in (root / "attempts" / stage_id).glob("*"):
        identity_path = attempt_root / f"{attempt_root.name}.json"
        manifest_path = attempt_root / "manifest.json"
        if not identity_path.is_file() or not manifest_path.is_file():
            continue
        try:
            attempt = json.loads(identity_path.read_text(encoding="utf-8"))
            identity = attempt.get("identity") if isinstance(attempt, Mapping) else None
            attempt_number = int(identity.get("attempt_number")) if isinstance(identity, Mapping) else 0
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(identity, Mapping) or attempt.get("status") not in {"succeeded", "blocked"}:
            continue
        try:
            expected_attempt_id = stage_attempt_id(
                str(identity.get("run_id") or ""),
                str(identity.get("stage_id") or ""),
                attempt_number,
                str(identity.get("request_digest") or ""),
            )
        except ValueError:
            continue
        snapshot = attempt.get("extensions", {}).get("snapshots", {}).get("manifest_path", {})
        capsule_relative = f"attempts/{stage_id}/{expected_attempt_id}/manifest.json"
        entry = inventory.get(capsule_relative, {})
        expected_suffix = f"runs/{capsule_manifest.get('run_id')}/attempts/{stage_id}/{expected_attempt_id}/manifest.json"
        if (
            identity.get("attempt_id") != expected_attempt_id
            or identity.get("run_id") != capsule_manifest.get("run_id")
            or identity.get("request_digest") != capsule_manifest.get("request_digest")
            or identity.get("stage_id") != stage_id
            or attempt_root.name != expected_attempt_id
            or not isinstance(snapshot, Mapping)
            or not str(snapshot.get("path") or "").endswith(expected_suffix)
            or snapshot.get("sha256") != entry.get("origin_sha256")
        ):
            continue
        candidates.append((attempt_number, attempt_root.name, manifest_path))
    if not candidates:
        return None
    try:
        payload = json.loads(max(candidates)[2].read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _read_strict_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_strict_json_object)
    if not isinstance(payload, dict):
        raise TypeError("document must be a JSON object")
    return payload


def _task_fitness_revalidation_blockers(
    report: Mapping[str, Any],
    summary: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    governance: Mapping[str, Any],
) -> list[str]:
    scope = str(manifest.get("release_scope") or "")
    required_test_ids = list(_FITNESS_TESTS_BY_SCOPE.get(scope, ()))
    profile = manifest.get("simready_profile")
    profile = profile if isinstance(profile, Mapping) else {}
    expected_bindings = {
        "scope": scope,
        "run_id": str(manifest.get("run_id") or ""),
        "request_digest": str(manifest.get("request_digest") or ""),
        "asset_fingerprint": str(governance.get("asset_fingerprint") or ""),
        "profile_id": str(profile.get("profile_id") or ""),
        "profile_version": str(profile.get("profile_version") or ""),
    }
    blockers: list[str] = []
    if not required_test_ids:
        blockers.append("task-fitness release scope is unsupported")
    for field, expected in expected_bindings.items():
        if not expected or str(report.get(field) or "") != expected:
            blockers.append(f"task-fitness report {field} does not match the capsule")
    if summary.get("status") != "pass" or summary.get("blocked_reasons"):
        blockers.append("task-fitness stage summary is not a blocker-free pass")
    if list(summary.get("required_test_ids") or []) != required_test_ids:
        blockers.append("task-fitness stage summary has the wrong required tests")
    if summary.get("bindings") != expected_bindings:
        blockers.append("task-fitness stage bindings do not match the capsule")
    tests = [item for item in report.get("tests") or [] if isinstance(item, Mapping)]
    test_ids = [str(item.get("test_id") or "") for item in tests]
    tests_by_id = {str(item.get("test_id") or ""): item for item in tests}
    if len(test_ids) != len(set(test_ids)):
        blockers.append("task-fitness test IDs are not unique")
    if _canonical_json(summary.get("tests") or []) != _canonical_json(tests):
        blockers.append("task-fitness stage tests differ from the underlying report")
    for test_id in required_test_ids:
        test = tests_by_id.get(test_id)
        if test is None:
            blockers.append(f"required task-fitness test is missing: {test_id}")
            continue
        if test.get("status") != "pass":
            blockers.append(f"required task-fitness test did not pass: {test_id}")
        for metric in test.get("metric_results") or []:
            if not isinstance(metric, Mapping) or metric.get("status") != "pass":
                blockers.append(f"task-fitness metric did not pass in {test_id}")
                continue
            try:
                value = float(metric["value"])
                expected_min = float(metric["expected_min"])
                expected_max = float(metric["expected_max"])
                tolerance = float(metric["tolerance"])
            except (KeyError, TypeError, ValueError):
                blockers.append(f"task-fitness metric is malformed in {test_id}")
                continue
            if not all(math.isfinite(item) for item in (value, expected_min, expected_max, tolerance)):
                blockers.append(f"task-fitness metric is non-finite in {test_id}")
            elif expected_min > expected_max or tolerance < 0:
                blockers.append(f"task-fitness metric range is invalid in {test_id}")
            elif not expected_min - tolerance <= value <= expected_max + tolerance:
                blockers.append(f"task-fitness metric is outside tolerance in {test_id}")
    return blockers


def _official_execution_blockers(
    normalised: Mapping[str, Any],
    *,
    pinned_executable_sha256: str,
) -> list[str]:
    blockers: list[str] = []
    validator = normalised.get("validator")
    validator = validator if isinstance(validator, Mapping) else {}
    if not validator.get("executable_name"):
        blockers.append("official validator executable name is missing")
    reported_executable_sha256 = str(validator.get("executable_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", reported_executable_sha256):
        blockers.append("official validator executable digest is missing")
    configured_executable_sha256 = pinned_executable_sha256.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", configured_executable_sha256):
        blockers.append("administrator-pinned official validator executable digest is unavailable")
    elif reported_executable_sha256 != configured_executable_sha256:
        blockers.append("official validator executable digest differs from the administrator pin")
    execution = normalised.get("execution")
    if not isinstance(execution, Mapping):
        return [*blockers, "official validator execution evidence is missing"]
    command = execution.get("command_contract")
    required_arguments = {"--profile", "--no-fix", "--no-stamp", "--json-output", "<raw-report>", "<asset>"}
    if not isinstance(command, list) or not required_arguments.issubset({str(item) for item in command}):
        blockers.append("official validator command contract is incomplete")
    executable = execution.get("validator_executable")
    if not isinstance(executable, Mapping) or (
        executable.get("name") != validator.get("executable_name")
        or executable.get("sha256") != validator.get("executable_sha256")
    ):
        blockers.append("official validator executable evidence is inconsistent")
    for phase in ("version_probe", "validation"):
        record = execution.get(phase)
        if not isinstance(record, Mapping):
            blockers.append(f"official validator {phase} evidence is missing")
            continue
        if (
            record.get("exit_code") != 0
            or record.get("timed_out") is not False
            or record.get("output_limit_exceeded") is not False
            or record.get("report_limit_exceeded") is not False
            or record.get("launch_error") not in {"", None}
        ):
            blockers.append(f"official validator {phase} did not complete cleanly")
        for digest_field in ("captured_stdout_sha256", "captured_stderr_sha256"):
            if not _HASH_PATTERN.fullmatch(str(record.get(digest_field) or "")):
                blockers.append(f"official validator {phase} has no {digest_field}")
    return blockers


def _official_report_revalidation_blockers(
    normalised: Mapping[str, Any],
    raw: Mapping[str, Any],
    official_summary: Mapping[str, Any],
    *,
    raw_report_sha256: str,
    attestation_secret: str,
    pinned_executable_sha256: str,
) -> list[str]:
    blockers = _official_execution_blockers(
        normalised,
        pinned_executable_sha256=pinned_executable_sha256,
    )
    blockers.extend(verify_official_profile_report_attestation(dict(normalised), attestation_secret))
    if raw_report_sha256 != str(normalised.get("raw_report_sha256") or "").removeprefix("sha256:").lower():
        blockers.append("official raw report digest differs from the normalised report")
    for field, label in (("usd_path", "USD"), ("raw_report_path", "raw report")):
        value = str(normalised.get(field) or "")
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            blockers.append(f"official validator {label} label is not portable")
    execution = normalised.get("execution")
    if isinstance(execution, dict):
        recomputed = normalise_official_profile_report(
            dict(raw),
            profile_id=str(normalised.get("profile_id") or ""),
            profile_version=str(normalised.get("profile_version") or ""),
            validator_version=str(normalised.get("validator_version") or ""),
            usd_path=str(normalised.get("usd_path") or ""),
            usd_sha256=str(normalised.get("usd_sha256") or ""),
            composition_fingerprint=str(normalised.get("composition_fingerprint") or ""),
            raw_report_path=str(normalised.get("raw_report_path") or ""),
            raw_report_sha256=str(normalised.get("raw_report_sha256") or ""),
            execution=execution,
            package_dependency_fingerprint=str(normalised.get("package_dependency_fingerprint") or ""),
            package_inventory=[
                {"path": str(item.get("path") or ""), "sha256": str(item.get("sha256") or "")}
                for item in normalised.get("package_inventory") or []
                if isinstance(item, Mapping)
            ],
        )
        unsigned_normalised = {key: value for key, value in normalised.items() if key != "attestation"}
        if _canonical_json(recomputed) != _canonical_json(unsigned_normalised):
            blockers.append("official normalised report cannot be reproduced from the raw report")
    else:
        blockers.append("official normalised report has no execution record")
    official_expectations = {
        "status": "pass",
        "reported_validator_id": normalised.get("validator_id"),
        "reported_validator_version": normalised.get("validator_version"),
        "validated_usd_sha256": normalised.get("usd_sha256"),
        "validated_composition_fingerprint": normalised.get("composition_fingerprint"),
        "validated_package_dependency_fingerprint": normalised.get("package_dependency_fingerprint"),
        "validated_package_inventory": normalised.get("package_inventory"),
        "reported_profile_id": normalised.get("profile_id"),
        "reported_profile_version": normalised.get("profile_version"),
        "feature_results": normalised.get("features"),
        "requirement_results": normalised.get("requirements"),
    }
    for field, expected in official_expectations.items():
        if official_summary.get(field) != expected:
            blockers.append(f"official aggregate {field} differs from the normalised report")
    if normalised.get("status") != "pass" or normalised.get("problems") or normalised.get("reason"):
        blockers.append("official normalised report is not a blocker-free pass")
    return blockers


def _official_package_inventory_blockers(
    profile_report: Mapping[str, Any],
    normalised: Mapping[str, Any],
    closure: Mapping[str, Any],
    simready_manifest: Mapping[str, Any] | None,
) -> list[str]:
    blockers: list[str] = []
    official_inventory = normalised.get("package_inventory")
    closure_inventory = closure.get("package_inventory")
    if not isinstance(official_inventory, list) or not official_inventory:
        blockers.append("official validator package inventory is missing")
        official_inventory = []
    if not isinstance(closure_inventory, list) or not closure_inventory:
        blockers.append("package closure full inventory is missing")
        closure_inventory = []
    if _canonical_json(official_inventory) != _canonical_json(closure_inventory):
        blockers.append("official validator and package closure inventories differ")
    official_fingerprint = str(normalised.get("package_dependency_fingerprint") or "")
    closure_fingerprint = str(closure.get("package_dependency_fingerprint") or "")
    if official_fingerprint != closure_fingerprint:
        blockers.append("official validator and package closure fingerprints differ")

    official_by_path = {
        str(item.get("path") or ""): str(item.get("sha256") or "")
        for item in official_inventory
        if isinstance(item, Mapping)
    }
    for item in closure.get("files") or []:
        if not isinstance(item, Mapping):
            blockers.append("package closure dependency inventory is malformed")
            continue
        path = str(item.get("path") or "")
        if official_by_path.get(path) != str(item.get("sha256") or ""):
            blockers.append(f"package closure dependency differs from the official inventory: {path}")

    if simready_manifest is None:
        blockers.append("selected-run SimReady inventory is unavailable")
        return blockers
    package_path = str(profile_report.get("package_path") or "")
    try:
        project_package = _safe_relative(package_path, "package path")
        if project_package.parts[0] != "packaged":
            raise ValueError("package path is outside packaged/")
        generated_records = _generated_file_records(simready_manifest)
    except (ValueError, CapsuleCreationError) as exc:
        blockers.append(f"selected-run package inventory cannot be reconstructed: {exc}")
        return blockers
    project_package_root = project_package.parent
    selected_inventory: list[dict[str, str]] = []
    for record in generated_records:
        try:
            generated_path = _safe_relative(str(record.get("path") or ""), "generated package path")
            relative = generated_path.relative_to(project_package_root)
        except ValueError:
            continue
        if relative.as_posix() == ".":
            continue
        selected_inventory.append(
            {
                "path": relative.as_posix(),
                "sha256": str(record.get("sha256") or "").lower(),
            }
        )
    selected_inventory.sort(key=lambda item: (item["path"], item["sha256"]))
    if _canonical_json(selected_inventory) != _canonical_json(official_inventory):
        blockers.append("official validator inventory differs from selected-run generated package evidence")
    return blockers


def _runtime_report_revalidation_blockers(
    runtime_report: Mapping[str, Any],
    runtime_summary: Mapping[str, Any],
    profile_report: Mapping[str, Any],
    official_report: Mapping[str, Any],
    *,
    report_origin_sha256: str,
    environment: Mapping[str, str] | None = None,
) -> list[str]:
    blockers = [f"runtime report schema: {issue.render()}" for issue in validate_payload("isaac-runtime-evidence", runtime_report)]
    secret: bytes | None = None
    producer_pin: str | None = None
    try:
        secret = isaac_attestation_secret(environment)
    except ValueError as exc:
        blockers.append(str(exc))
    try:
        producer_pin = isaac_producer_sha256_pin(environment)
    except ValueError as exc:
        blockers.append(str(exc))
    if secret is not None and producer_pin is not None:
        blockers.extend(verify_runtime_report_envelope(runtime_report, secret, producer_pin))

    execution_identity = runtime_report.get("execution_identity")
    execution_identity = execution_identity if isinstance(execution_identity, Mapping) else {}
    try:
        started_at = datetime.fromisoformat(str(execution_identity.get("started_at") or "").replace("Z", "+00:00"))
        completed_at = datetime.fromisoformat(
            str(execution_identity.get("completed_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        blockers.append("runtime report execution timestamps are invalid")
    else:
        if (
            started_at.tzinfo is None
            or started_at.utcoffset() is None
            or completed_at.tzinfo is None
            or completed_at.utcoffset() is None
            or completed_at < started_at
        ):
            blockers.append("runtime report execution timestamps are unordered or lack a timezone")
    parameters = runtime_report.get("validation_parameters")
    parameters = parameters if isinstance(parameters, Mapping) else {}
    for field in ("width", "height", "min_seconds", "physics_dt"):
        if runtime_report.get(field) != parameters.get(field):
            blockers.append(f"runtime report {field} conflicts with validation_parameters")

    profile = _profile_identity(profile_report)
    if (
        runtime_report.get("profile_id") != profile["profile_id"]
        or runtime_report.get("profile_version") != profile["profile_version"]
    ):
        blockers.append("runtime report Profile identity differs from the positive capsule")
    package_path = str(profile_report.get("package_path") or "")
    try:
        safe_package_path = _safe_relative(package_path, "runtime package path").as_posix()
    except ValueError as exc:
        blockers.append(str(exc))
        safe_package_path = ""
    if runtime_report.get("usd_path") != safe_package_path:
        blockers.append("runtime report USD path differs from the selected package")
    expected_label = "project:///" + urllib.parse.quote(safe_package_path, safe="/-._~") if safe_package_path else ""
    if runtime_report.get("usd_label") != expected_label:
        blockers.append("runtime report portable USD label differs from the selected package")
    if runtime_report.get("usd_sha256") != official_report.get("usd_sha256"):
        blockers.append("runtime and official validator reports cover different USD content")
    if runtime_report.get("composition_fingerprint") != official_report.get("composition_fingerprint"):
        blockers.append("runtime and official validator reports cover different composed layer stacks")
    if runtime_report.get("package_dependency_fingerprint") != official_report.get("package_dependency_fingerprint"):
        blockers.append("runtime and official validator package fingerprints differ")
    if _canonical_json(runtime_report.get("package_inventory") or []) != _canonical_json(
        official_report.get("package_inventory") or []
    ):
        blockers.append("runtime and official validator package inventories differ")

    tests = runtime_report.get("behavioural_tests")
    tests = tests if isinstance(tests, list) else []
    tests_by_id = {
        str(item.get("test_id") or ""): item
        for item in tests
        if isinstance(item, Mapping) and item.get("test_id")
    }
    if len(tests_by_id) != len(tests):
        blockers.append("runtime report behavioural test identities are missing or duplicated")
    required_test_ids = (
        ["articulation_runtime_stability", "articulation_joint_sweep"]
        if profile["profile_id"].startswith("Robot-")
        else ["rigid_body_drop_and_settle", "rigid_body_impulse_response", "rigid_body_reset_repeatability"]
    )
    for test_id in required_test_ids:
        test = tests_by_id.get(test_id)
        if not isinstance(test, Mapping) or test.get("status") != "pass":
            blockers.append(f"required runtime behavioural test did not pass: {test_id}")
    runtime_availability = runtime_report.get("runtime_availability")
    runtime_availability = runtime_availability if isinstance(runtime_availability, Mapping) else {}
    runtime_identity = runtime_report.get("runtime_identity")
    runtime_identity = runtime_identity if isinstance(runtime_identity, Mapping) else {}
    if (
        runtime_report.get("status") != "pass"
        or runtime_report.get("loaded") is not True
        or runtime_availability.get("isaac_sim") is not True
        or runtime_report.get("errors") != []
        or not runtime_identity.get("version")
        or runtime_identity.get("version") != runtime_availability.get("isaac_sim_version")
    ):
        blockers.append("runtime report is not a complete blocker-free Isaac Sim pass")

    report_path = str(runtime_summary.get("report_path") or "")
    try:
        _safe_relative(report_path, "runtime report origin path")
    except ValueError as exc:
        blockers.append(str(exc))
    expected_summary = {
        "runtime_id": "isaac-sim",
        "status": "pass",
        "available": True,
        "executed": True,
        "report_path": report_path,
        "report_sha256": report_origin_sha256,
        "validated_usd_sha256": runtime_report.get("usd_sha256"),
        "validated_composition_fingerprint": runtime_report.get("composition_fingerprint"),
        "validated_package_dependency_fingerprint": runtime_report.get("package_dependency_fingerprint"),
        "validated_package_inventory": runtime_report.get("package_inventory"),
        "required_test_ids": required_test_ids,
        "behavioural_tests": tests,
        "reason": "",
        "reported_profile_id": runtime_report.get("profile_id"),
        "reported_profile_version": runtime_report.get("profile_version"),
        "runtime_version": runtime_availability.get("isaac_sim_version"),
        "producer_sha256": execution_identity.get("producer_sha256"),
    }
    if _canonical_json(runtime_summary) != _canonical_json(expected_summary):
        blockers.append("runtime aggregate differs from the attested runtime report")
    return blockers


def _physics_binding_revalidation_blockers(
    root: Path,
    profile_report: Mapping[str, Any],
    inventory: Mapping[str, Mapping[str, Any]],
    *,
    environment: Mapping[str, str] | None = None,
) -> list[str]:
    blockers: list[str] = []
    package_path = str(profile_report.get("package_path") or "")
    try:
        project_package = _safe_relative(package_path, "physics package path")
        if project_package.parts[0] != "packaged":
            raise ValueError("physics package path is outside packaged/")
    except ValueError as exc:
        return [str(exc)]
    capsule_package_root = PurePosixPath("outputs", *project_package.parent.parts[1:])
    binding_relative = (capsule_package_root / "evidence" / "physics-evidence-binding.json").as_posix()
    binding_path = root.joinpath(*PurePosixPath(binding_relative).parts)
    conformance = profile_report.get("simready_conformance")
    conformance = conformance if isinstance(conformance, Mapping) else {}
    requirements = [item for item in conformance.get("requirements") or [] if isinstance(item, Mapping)]
    features = [item for item in conformance.get("features") or [] if isinstance(item, Mapping)]
    profile_id = _profile_identity(profile_report)["profile_id"].lower()
    rigid_body_claimed = any(
        str(item.get("requirement_id") or item.get("id") or "") == "RB.COL.001"
        and item.get("status") == "pass"
        for item in requirements
    ) or any(
        str(item.get("feature_id") or item.get("id") or "") == "Rigid Body Physics"
        and item.get("status") == "pass"
        for item in features
    ) or any(token in profile_id for token in ("rigid", "physics", "runnable", "isaac"))
    if not binding_path.exists():
        return ["positive rigid-body evidence has no packaged physics evidence binding"] if rigid_body_claimed else []
    try:
        binding = _read_strict_json_object(binding_path)
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        return [f"physics evidence binding is not strict JSON: {exc}"]
    expected_binding_fields = {
        "schema_version",
        "status",
        "evidence_fingerprint",
        "prim_path",
        "mass",
        "center_of_mass",
        "diagonal_inertia",
        "principal_axes",
        "method",
        "unit_policy",
        "uncertainty",
        "source_evidence_ids",
        "evidence",
        "approval",
        "attested_evidence",
    }
    if set(binding) != expected_binding_fields:
        blockers.append("packaged physics evidence binding has an unexpected shape")
    attested = binding.get("attested_evidence")
    if not isinstance(attested, Mapping):
        return ["physics evidence binding has no nested attested evidence"]
    try:
        secret = physics_evidence_secret_from_environment(environment)
    except ValueError as exc:
        blockers.append(str(exc))
    else:
        blockers.extend(verify_physics_evidence_attestation(attested, secret))
    if attested.get("status") != "accepted":
        blockers.append("nested physics evidence status is not accepted")
    method = attested.get("method")
    if method not in {"measured", "manufacturer_specification", "computed_from_measured_density"}:
        blockers.append("nested physics evidence method is unsupported")
    unit_policy = attested.get("unit_policy")
    if unit_policy != "si_m_kg_s" and unit_policy != {
        "mass": "kg",
        "length": "m",
        "inertia": "kg*m^2",
    }:
        blockers.append("nested physics evidence does not declare SI mass-property units")
    mass = attested.get("mass")
    if (
        not isinstance(mass, (int, float))
        or isinstance(mass, bool)
        or not math.isfinite(float(mass))
        or float(mass) <= 0.0
    ):
        blockers.append("nested physics evidence mass is not finite and positive")
    centre = attested.get("center_of_mass")
    inertia = attested.get("diagonal_inertia")
    axes = attested.get("principal_axes")
    if (
        not isinstance(centre, list)
        or len(centre) != 3
        or any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) for value in centre)
    ):
        blockers.append("nested physics evidence centre of mass is invalid")
    valid_inertia = (
        isinstance(inertia, list)
        and len(inertia) == 3
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) > 0.0
            for value in inertia
        )
    )
    if not valid_inertia:
        blockers.append("nested physics evidence diagonal inertia is invalid")
    elif any(float(value) > sum(float(item) for item in inertia) - float(value) + 1e-12 for value in inertia):
        blockers.append("nested physics evidence diagonal inertia violates rigid-body triangle inequalities")
    valid_axes = (
        isinstance(axes, list)
        and len(axes) == 4
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            for value in axes
        )
    )
    if not valid_axes or not math.isclose(
        math.sqrt(sum(float(value) ** 2 for value in axes)) if isinstance(axes, list) else 0.0,
        1.0,
        rel_tol=1e-6,
        abs_tol=1e-6,
    ):
        blockers.append("nested physics evidence principal axes are not a unit quaternion")
    uncertainty = attested.get("uncertainty")
    uncertainty_inertia = uncertainty.get("diagonal_inertia") if isinstance(uncertainty, Mapping) else None
    uncertainty_mass = uncertainty.get("mass") if isinstance(uncertainty, Mapping) else None
    if (
        not isinstance(uncertainty, Mapping)
        or not isinstance(uncertainty_mass, (int, float))
        or isinstance(uncertainty_mass, bool)
        or not math.isfinite(float(uncertainty_mass))
        or float(uncertainty_mass) < 0.0
        or not isinstance(uncertainty_inertia, list)
        or len(uncertainty_inertia) != 3
        or any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or float(value) < 0.0
            for value in uncertainty_inertia
        )
    ):
        blockers.append("nested physics evidence uncertainty is invalid")
    approval = attested.get("approval")
    if not isinstance(approval, Mapping) or approval.get("status") != "accepted" or any(
        not isinstance(approval.get(field), str) or not str(approval.get(field)).strip()
        for field in ("decision_id", "reviewer", "decided_at")
    ):
        blockers.append("nested physics evidence approval is incomplete")
    if binding.get("schema_version") != "1.0.0" or binding.get("status") != "accepted":
        blockers.append("physics evidence binding is not an accepted 1.0.0 record")
    for field in (
        "status",
        "evidence_fingerprint",
        "prim_path",
        "mass",
        "center_of_mass",
        "diagonal_inertia",
        "principal_axes",
        "method",
        "unit_policy",
        "uncertainty",
        "source_evidence_ids",
        "approval",
    ):
        if binding.get(field) != attested.get(field):
            blockers.append(f"physics evidence binding {field} differs from the nested attestation")

    nested_records = attested.get("evidence")
    materialised_records = binding.get("evidence")
    nested_records = nested_records if isinstance(nested_records, list) else []
    materialised_records = materialised_records if isinstance(materialised_records, list) else []
    nested_by_id = {
        str(item.get("evidence_id") or ""): item
        for item in nested_records
        if isinstance(item, Mapping) and item.get("evidence_id")
    }
    materialised_by_id = {
        str(item.get("evidence_id") or ""): item
        for item in materialised_records
        if isinstance(item, Mapping) and item.get("evidence_id")
    }
    if (
        not nested_by_id
        or len(nested_by_id) != len(nested_records)
        or len(materialised_by_id) != len(materialised_records)
        or set(nested_by_id) != set(materialised_by_id)
    ):
        blockers.append("physics evidence materialisation does not exactly cover the attested records")
    source_evidence_ids = attested.get("source_evidence_ids")
    if (
        not isinstance(source_evidence_ids, list)
        or not source_evidence_ids
        or len(source_evidence_ids) != len(set(str(item) for item in source_evidence_ids))
        or any(not isinstance(item, str) or not item.strip() or item not in nested_by_id for item in source_evidence_ids)
    ):
        blockers.append("nested physics source evidence identities are missing, duplicated or unresolved")
    for evidence_id, nested_record in nested_by_id.items():
        try:
            _safe_relative(str(nested_record.get("path") or ""), "nested physics evidence path")
        except ValueError as exc:
            blockers.append(str(exc))
        if not re.fullmatch(r"[0-9a-f]{64}", str(nested_record.get("sha256") or "")):
            blockers.append(f"nested physics evidence digest is malformed: {evidence_id}")
        materialised = materialised_by_id.get(evidence_id)
        if not isinstance(materialised, Mapping):
            continue
        expected_sha256 = str(nested_record.get("sha256") or "")
        if materialised.get("sha256") != expected_sha256:
            blockers.append(f"materialised physics evidence digest differs: {evidence_id}")
            continue
        try:
            materialised_path = _safe_relative(
                str(materialised.get("path") or ""),
                "materialised physics evidence path",
            )
        except ValueError as exc:
            blockers.append(str(exc))
            continue
        if materialised_path.parts[:2] != ("evidence", "physics"):
            blockers.append(f"materialised physics evidence is outside evidence/physics: {evidence_id}")
            continue
        capsule_relative = (capsule_package_root / materialised_path).as_posix()
        entry = inventory.get(capsule_relative, {})
        target = root.joinpath(*PurePosixPath(capsule_relative).parts)
        if (
            not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or entry.get("sha256") != expected_sha256
            or not target.is_file()
            or sha256_file(target) != expected_sha256
        ):
            blockers.append(f"materialised physics evidence is missing or changed: {evidence_id}")
    binding_entry = inventory.get(binding_relative, {})
    if binding_entry.get("sha256") != sha256_file(binding_path):
        blockers.append("physics evidence binding differs from the immutable capsule inventory")
    capsule_package_path = root.joinpath(
        *PurePosixPath("outputs", *project_package.parts[1:]).parts,
    )
    blockers.extend(_revalidate_packaged_physics_evidence(capsule_package_path))
    return blockers


def _validate_positive_underlying_evidence(
    root: Path,
    manifest: Mapping[str, Any],
    governance: Mapping[str, Any],
    profile_report: Mapping[str, Any],
    runtime_report: Mapping[str, Any],
    inventory: Mapping[str, Mapping[str, Any]],
    errors: list[dict[str, str]],
) -> None:
    documents: dict[str, dict[str, Any]] = {}
    for name, relative in _POSITIVE_EVIDENCE_PATHS.items():
        try:
            documents[name] = _read_strict_json_object(root.joinpath(*PurePosixPath(relative).parts))
        except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
            _validation_error(errors, "positive_evidence_missing", str(exc), relative)
    if len(documents) != len(_POSITIVE_EVIDENCE_PATHS):
        return

    evaluation = _latest_capsule_stage_manifest(root, "evaluation", manifest, inventory)
    task_summary = evaluation.get("task_fitness") if isinstance(evaluation, Mapping) else None
    conformance = profile_report.get("simready_conformance")
    conformance = conformance if isinstance(conformance, Mapping) else {}
    official_summary = conformance.get("official_validator")
    official_summary = official_summary if isinstance(official_summary, Mapping) else {}
    openusd_summary = conformance.get("openusd_compliance")
    openusd_summary = openusd_summary if isinstance(openusd_summary, Mapping) else {}
    closure_summary = profile_report.get("package_dependency_closure")
    closure_summary = closure_summary if isinstance(closure_summary, Mapping) else {}
    bindings = (
        ("task_fitness", task_summary, "report_sha256"),
        ("task_protocol", documents["task_fitness"].get("protocol"), "sha256"),
        ("official_normalised", official_summary, "report_sha256"),
        ("official_raw", documents["official_normalised"], "raw_report_sha256"),
        ("openusd", openusd_summary, "report_sha256"),
        ("package_closure", closure_summary, "report_sha256"),
    )
    for name, summary, digest_field in bindings:
        relative = _POSITIVE_EVIDENCE_PATHS[name]
        expected = str(summary.get(digest_field) or "").removeprefix("sha256:").lower() if isinstance(summary, Mapping) else ""
        entry = inventory.get(relative, {})
        origin = str(entry.get("origin_sha256") or "").lower()
        exact_copy_changed = (
            name in {"task_fitness", "task_protocol", "official_raw"}
            and entry.get("sha256") != expected
        )
        if not _HASH_PATTERN.fullmatch(expected) or origin != expected or exact_copy_changed:
            _validation_error(
                errors,
                "positive_evidence_origin_mismatch",
                f"{name} is not bound to its immutable source digest",
                relative,
            )

    if not isinstance(task_summary, Mapping):
        _validation_error(
            errors,
            "task_fitness_revalidation_failed",
            "selected-run evaluation evidence is missing",
            _POSITIVE_EVIDENCE_PATHS["task_fitness"],
        )
    else:
        task_blockers = _task_fitness_revalidation_blockers(
            documents["task_fitness"],
            task_summary,
            manifest=manifest,
            governance=governance,
        )
        for blocker in task_blockers:
            _validation_error(
                errors,
                "task_fitness_revalidation_failed",
                blocker,
                _POSITIVE_EVIDENCE_PATHS["task_fitness"],
            )

    task_report = documents["task_fitness"]
    task_protocol = documents["task_protocol"]
    task_materialisation_blockers: list[str] = []
    expected_report_id = content_id(
        "task_fitness",
        {key: value for key, value in task_report.items() if key != "report_id"},
        digest_length=32,
    )
    if task_report.get("report_id") != expected_report_id:
        task_materialisation_blockers.append("task-fitness report ID is not content-derived")
    protocol_binding = task_report.get("protocol")
    protocol_binding = protocol_binding if isinstance(protocol_binding, Mapping) else {}
    if (
        task_protocol.get("status") != "approved"
        or task_protocol.get("scope") != manifest.get("release_scope")
        or task_protocol.get("protocol_id") != protocol_binding.get("protocol_id")
        or task_protocol.get("protocol_version") != protocol_binding.get("protocol_version")
    ):
        task_materialisation_blockers.append("task-fitness protocol is not the bound approved release protocol")
    report_tests = {
        str(item.get("test_id") or ""): item
        for item in task_report.get("tests") or []
        if isinstance(item, Mapping)
    }
    protocol_tests = {
        str(item.get("test_id") or ""): item
        for item in task_protocol.get("tests") or []
        if isinstance(item, Mapping)
    }
    if set(report_tests) != set(protocol_tests):
        task_materialisation_blockers.append("task-fitness report does not exactly cover its approved protocol")
    evidence_records = [item for item in task_report.get("evidence") or [] if isinstance(item, Mapping)]
    evidence_ids: set[str] = set()
    for record in evidence_records:
        evidence_id = str(record.get("evidence_id") or "")
        expected_evidence_id = content_id(
            "evidence",
            {
                "kind": str(record.get("kind") or ""),
                "path": str(record.get("path") or ""),
                "sha256": str(record.get("sha256") or ""),
            },
            digest_length=32,
        )
        if not evidence_id or evidence_id in evidence_ids or evidence_id != expected_evidence_id:
            task_materialisation_blockers.append("task-fitness evidence IDs are duplicated or not content-derived")
            continue
        evidence_ids.add(evidence_id)
        suffix = PurePosixPath(str(record.get("path") or "")).suffix.lower()
        capsule_relative = f"validation/fitness-evidence-files/{evidence_id}{suffix}"
        expected_sha256 = str(record.get("sha256") or "").removeprefix("sha256:").lower()
        entry = inventory.get(capsule_relative, {})
        if (
            not _HASH_PATTERN.fullmatch(expected_sha256)
            or entry.get("origin_sha256") != expected_sha256
            or entry.get("sha256") != expected_sha256
        ):
            task_materialisation_blockers.append(f"task-fitness evidence is missing or changed: {evidence_id}")
    for test_id, report_test in report_tests.items():
        protocol_test = protocol_tests.get(test_id)
        if not isinstance(protocol_test, Mapping):
            continue
        if report_test.get("scenario") != protocol_test.get("scenario"):
            task_materialisation_blockers.append(f"task-fitness scenario differs from the protocol: {test_id}")
        referenced_ids = [str(item) for item in report_test.get("evidence_ids") or []]
        if len(referenced_ids) != len(set(referenced_ids)) or any(item not in evidence_ids for item in referenced_ids):
            task_materialisation_blockers.append(f"task-fitness evidence references are unresolved: {test_id}")
        report_metrics = {
            str(item.get("metric_id") or ""): item
            for item in report_test.get("metric_results") or []
            if isinstance(item, Mapping)
        }
        protocol_metrics = {
            str(item.get("metric_id") or ""): item
            for item in protocol_test.get("metrics") or []
            if isinstance(item, Mapping)
        }
        if set(report_metrics) != set(protocol_metrics):
            task_materialisation_blockers.append(f"task-fitness metrics differ from the protocol: {test_id}")
        for metric_id, metric in report_metrics.items():
            protocol_metric = protocol_metrics.get(metric_id)
            if not isinstance(protocol_metric, Mapping):
                continue
            criteria = ("unit", "expected_min", "expected_max", "tolerance")
            if any(metric.get(field) != protocol_metric.get(field) for field in criteria):
                task_materialisation_blockers.append(
                    f"task-fitness metric criteria differ from the protocol: {test_id}/{metric_id}"
                )
    for blocker in task_materialisation_blockers:
        _validation_error(
            errors,
            "task_fitness_revalidation_failed",
            blocker,
            _POSITIVE_EVIDENCE_PATHS["task_fitness"],
        )

    normalised = documents["official_normalised"]
    raw = documents["official_raw"]
    raw_digest = sha256_file(root / _POSITIVE_EVIDENCE_PATHS["official_raw"])
    official_blockers = _official_report_revalidation_blockers(
        normalised,
        raw,
        official_summary,
        raw_report_sha256=raw_digest,
        attestation_secret=os.environ.get("AFB_VALIDATION_ATTESTATION_SECRET", ""),
        pinned_executable_sha256=os.environ.get("AFB_ASSET_VALIDATOR_EXECUTABLE_SHA256", ""),
    )
    for blocker in official_blockers:
        _validation_error(
            errors,
            "official_validator_revalidation_failed",
            blocker,
            _POSITIVE_EVIDENCE_PATHS["official_normalised"],
        )

    runtime_summary = conformance.get("runtime_validation")
    runtime_summary = runtime_summary if isinstance(runtime_summary, Mapping) else {}
    runtime_origin_sha256 = str(inventory.get("validation/runtime-results.json", {}).get("origin_sha256") or "")
    runtime_blockers = _runtime_report_revalidation_blockers(
        runtime_report,
        runtime_summary,
        profile_report,
        normalised,
        report_origin_sha256=runtime_origin_sha256,
    )
    for blocker in runtime_blockers:
        _validation_error(
            errors,
            "runtime_revalidation_failed",
            blocker,
            "validation/runtime-results.json",
        )

    openusd = documents["openusd"]
    portable_openusd_summary = {
        key: value for key, value in openusd_summary.items() if key not in {"report_path", "report_sha256"}
    }
    openusd_blockers: list[str] = []
    if _canonical_json(portable_openusd_summary) != _canonical_json(openusd):
        openusd_blockers.append("OpenUSD aggregate differs from the underlying report")
    if (
        openusd.get("status") != "pass"
        or openusd.get("checker_id") != "openusd-compliance-checker"
        or str(openusd.get("checker_version") or "").lower() in _UNRESOLVED_MODEL_VALUES
        or openusd.get("framework") != "UsdValidation"
        or not isinstance(openusd.get("validator_count"), int)
        or int(openusd.get("validator_count") or 0) < 1
        or openusd.get("errors")
        or openusd.get("failed_checks")
        or openusd.get("reason")
    ):
        openusd_blockers.append("OpenUSD report is not a complete blocker-free UsdValidation pass")

    released_hashes = {
        str(entry.get("sha256") or "")
        for relative, entry in inventory.items()
        if relative.startswith("outputs/")
    }
    validated_usd_sha256 = str(openusd.get("asset_sha256") or "")
    if not _HASH_PATTERN.fullmatch(validated_usd_sha256) or validated_usd_sha256 not in released_hashes:
        openusd_blockers.append("OpenUSD report is not bound to a released output")
    if validated_usd_sha256 != str(normalised.get("usd_sha256") or ""):
        openusd_blockers.append("OpenUSD and official validator reports cover different USD content")
    for blocker in openusd_blockers:
        _validation_error(
            errors,
            "openusd_revalidation_failed",
            blocker,
            _POSITIVE_EVIDENCE_PATHS["openusd"],
        )

    closure = documents["package_closure"]
    portable_closure_summary = {
        key: value for key, value in closure_summary.items() if key not in {"report_path", "report_sha256"}
    }
    closure_blockers: list[str] = []
    if _canonical_json(portable_closure_summary) != _canonical_json(closure):
        closure_blockers.append("package-closure aggregate differs from the underlying report")
    if (
        closure.get("status") != "pass"
        or closure.get("unresolved")
        or closure.get("external")
        or closure.get("missing")
        or closure.get("escaping")
        or closure.get("blocked_reasons")
    ):
        closure_blockers.append("package closure is not a blocker-free pass")
    package_path = str(profile_report.get("package_path") or "")
    try:
        project_package = _safe_relative(package_path, "package path")
        if project_package.parts[0] != "packaged":
            raise ValueError("package path is outside packaged/")
        capsule_package = PurePosixPath("outputs", *project_package.parts[1:])
    except ValueError as exc:
        closure_blockers.append(str(exc))
        capsule_package = PurePosixPath("outputs", "invalid")
    closure_files = [item for item in closure.get("files") or [] if isinstance(item, Mapping)]
    closure_paths = [str(item.get("path") or "") for item in closure_files]
    if not closure_files or len(closure_paths) != len(set(closure_paths)):
        closure_blockers.append("package closure has no unique file inventory")
    if str(closure.get("root") or "") not in closure_paths:
        closure_blockers.append("package closure does not inventory its root")
    for record in closure_files:
        relative = str(record.get("path") or "")
        expected = str(record.get("sha256") or "").lower()
        try:
            safe_relative = _safe_relative(relative, "package-closure path")
        except ValueError as exc:
            closure_blockers.append(str(exc))
            continue
        capsule_relative = PurePosixPath(capsule_package.parent, safe_relative).as_posix()
        entry = inventory.get(capsule_relative, {})
        if not _HASH_PATTERN.fullmatch(expected) or entry.get("sha256") != expected:
            closure_blockers.append(f"package-closure digest differs for {relative}")
    simready_manifest = _latest_capsule_stage_manifest(
        root,
        "simready-verification",
        manifest,
        inventory,
    )
    closure_blockers.extend(
        _official_package_inventory_blockers(
            profile_report,
            normalised,
            closure,
            simready_manifest,
        )
    )
    physics_blockers = _physics_binding_revalidation_blockers(
        root,
        profile_report,
        inventory,
    )
    for blocker in physics_blockers:
        _validation_error(
            errors,
            "physics_evidence_revalidation_failed",
            blocker,
            "outputs",
        )
    for blocker in closure_blockers:
        _validation_error(
            errors,
            "package_closure_revalidation_failed",
            blocker,
            _POSITIVE_EVIDENCE_PATHS["package_closure"],
        )


def _validate_cross_evidence(
    root: Path,
    manifest: Mapping[str, Any],
    inventory_entries: Sequence[Mapping[str, Any]],
    errors: list[dict[str, str]],
) -> None:
    required_documents = {
        "request": "request/run-request.json",
        "plan": "request/run-plan.json",
        "provenance": "environment/provenance.json",
        "governance": "governance/governance-record.json",
        "source": "source/source-asset-manifest.json",
        "profile": "validation/profile-results.json",
        "runtime": "validation/runtime-results.json",
        "schema_results": "validation/schema-results.json",
    }
    documents: dict[str, dict[str, Any]] = {}
    for name, relative in required_documents.items():
        target = root.joinpath(*PurePosixPath(relative).parts)
        try:
            payload = parse_runtime_report_bytes(target.read_bytes()) if name == "runtime" else _read_strict_json_object(target)
            documents[name] = payload
        except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
            _validation_error(errors, "cross_record_unreadable", str(exc), relative)
    if len(documents) != len(required_documents):
        return

    inventory = {str(item.get("path") or ""): item for item in inventory_entries}

    request = documents["request"]
    plan = documents["plan"]
    provenance = documents["provenance"]
    governance = documents["governance"]
    source = documents["source"]
    profile = documents["profile"]
    runtime = documents["runtime"]
    recorded_schema_results = documents["schema_results"]
    try:
        typed_request = RunRequest.model_validate(request)
        local_request_digest = "sha256:" + sha256_text(typed_request.model_dump_json())
    except ValueError as exc:
        _validation_error(errors, "request_contract_invalid", str(exc), "request/run-request.json")
        local_request_digest = ""
    if local_request_digest != manifest.get("capsule_request_digest"):
        _validation_error(
            errors,
            "request_digest_mismatch",
            "sanitised request digest differs from capsule manifest",
            "request/run-request.json",
        )
    if (
        str(plan.get("run_id") or plan.get("id") or "") != manifest.get("run_id")
        or plan.get("request_digest") != manifest.get("request_digest")
    ):
        _validation_error(
            errors,
            "run_plan_binding_mismatch",
            "run plan identity or request digest differs from capsule manifest",
            "request/run-plan.json",
        )
    if provenance_blocker := _provenance_identity_blocker(provenance):
        _validation_error(errors, "provenance_id_mismatch", provenance_blocker, "environment/provenance.json")
    if (
        provenance.get("run_id") != manifest.get("run_id")
        or provenance.get("provenance_id") != manifest.get("provenance_id")
    ):
        _validation_error(
            errors,
            "provenance_binding_mismatch",
            "provenance does not match capsule run and local identity",
            "environment/provenance.json",
        )
    origin = provenance.get("extensions", {}).get("capsule_origin", {})
    provenance_origin_sha = str(inventory.get("environment/provenance.json", {}).get("origin_sha256") or "")
    if (
        not isinstance(origin, Mapping)
        or origin.get("provenance_id") != manifest.get("origin_provenance_id")
        or origin.get("sha256") != provenance_origin_sha
        or not _HASH_PATTERN.fullmatch(provenance_origin_sha)
    ):
        _validation_error(
            errors,
            "origin_provenance_mismatch",
            "sanitised provenance does not declare the capsule origin identity",
            "environment/provenance.json",
        )

    project_id = str(manifest.get("project_id") or "")
    if any(str(record.get("project_id") or "") != project_id for record in (source, governance)):
        _validation_error(
            errors,
            "project_identity_mismatch",
            "source and governance records do not match the capsule project",
            "capsule.json",
        )
    governance_provenance = governance.get("provenance")
    if not isinstance(governance_provenance, Mapping) or governance_provenance.get("run_id") != manifest.get("run_id"):
        _validation_error(
            errors,
            "governance_run_mismatch",
            "governance evidence is not bound to the capsule run",
            "governance/governance-record.json",
        )

    actual_attempt_ids: list[str] = []
    actual_stage_ids: list[str] = []
    for attempt_root in (root / "attempts").glob("*/*"):
        if not attempt_root.is_dir():
            continue
        identity_path = attempt_root / f"{attempt_root.name}.json"
        if not identity_path.is_file():
            _validation_error(
                errors,
                "attempt_orphan_directory",
                "attempt directory has no matching identity record",
                attempt_root.relative_to(root).as_posix(),
            )
    for target in sorted((root / "attempts").glob("*/*/*.json")):
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        identity = payload.get("identity") if isinstance(payload, Mapping) else None
        if not isinstance(identity, Mapping):
            continue
        try:
            expected_attempt_id = stage_attempt_id(
                str(identity.get("run_id") or ""),
                str(identity.get("stage_id") or ""),
                int(identity.get("attempt_number") or 0),
                str(identity.get("request_digest") or ""),
            )
        except (TypeError, ValueError) as exc:
            _validation_error(errors, "attempt_identity_invalid", str(exc), target.relative_to(root).as_posix())
            continue
        expected_relative = PurePosixPath(
            "attempts",
            str(identity.get("stage_id") or ""),
            expected_attempt_id,
            f"{expected_attempt_id}.json",
        )
        if (
            identity.get("attempt_id") != expected_attempt_id
            or target.relative_to(root).as_posix() != expected_relative.as_posix()
            or identity.get("run_id") != manifest.get("run_id")
            or identity.get("request_digest") != manifest.get("request_digest")
        ):
            _validation_error(
                errors,
                "attempt_identity_mismatch",
                "attempt content identity, path or run binding differs",
                target.relative_to(root).as_posix(),
            )
            continue
        actual_attempt_ids.append(expected_attempt_id)
        actual_stage_ids.append(str(identity.get("stage_id") or ""))
        extensions = payload.get("extensions")
        snapshots = extensions.get("snapshots") if isinstance(extensions, Mapping) else None
        for snapshot_key, filename in (("manifest_path", "manifest.json"), ("report_path", "report.json")):
            snapshot = snapshots.get(snapshot_key) if isinstance(snapshots, Mapping) else None
            capsule_relative = (
                f"attempts/{identity.get('stage_id')}/{expected_attempt_id}/{filename}"
            )
            entry = inventory.get(capsule_relative)
            if snapshot is None and entry is None:
                continue
            snapshot_sha = str(snapshot.get("sha256") or "").removeprefix("sha256:") if isinstance(snapshot, Mapping) else ""
            snapshot_path = str(snapshot.get("path") or "") if isinstance(snapshot, Mapping) else ""
            expected_suffix = f"runs/{manifest.get('run_id')}/attempts/{identity.get('stage_id')}/{expected_attempt_id}/{filename}"
            if (
                entry is None
                or snapshot_sha != entry.get("origin_sha256")
                or not snapshot_path.endswith(expected_suffix)
            ):
                _validation_error(
                    errors,
                    "attempt_snapshot_mismatch",
                    "attempt snapshot origin path or digest differs from its identity record",
                    capsule_relative,
                )
    expected_attempt_ids = sorted({str(item) for item in provenance.get("attempt_ids") or [] if str(item)})
    manifest_attempt_ids = sorted({str(item) for item in manifest.get("attempt_ids") or [] if str(item)})
    if len(actual_attempt_ids) != len(set(actual_attempt_ids)) or sorted(actual_attempt_ids) != expected_attempt_ids:
        _validation_error(
            errors,
            "attempt_evidence_incomplete",
            "capsule attempts do not exactly match final provenance",
            "attempts",
        )
    if manifest_attempt_ids != expected_attempt_ids:
        _validation_error(
            errors,
            "attempt_manifest_mismatch",
            "capsule attempt summary differs from final provenance",
            "capsule.json",
        )
    planned_stage_ids = {
        str(item.get("id") or "")
        for item in plan.get("stages") or []
        if isinstance(item, Mapping) and item.get("id")
    }
    attempted_stage_ids = set(actual_stage_ids)
    if not attempted_stage_ids or attempted_stage_ids != planned_stage_ids:
        _validation_error(
            errors,
            "run_plan_stage_mismatch",
            "copied stage attempts do not cover the run plan exactly",
            "request/run-plan.json",
        )

    recalculated_schema_results = _schema_validation_results(root)
    if _canonical_json(recorded_schema_results) != _canonical_json(recalculated_schema_results):
        _validation_error(
            errors,
            "schema_results_mismatch",
            "recorded schema results differ from trusted-schema revalidation",
            "validation/schema-results.json",
        )
    if recalculated_schema_results.get("status") != "pass":
        _validation_error(
            errors,
            "schema_evidence_failed",
            "capsule contract evidence does not pass trusted schema validation",
            "validation/schema-results.json",
        )

    profile_origin = str(inventory.get("validation/profile-results.json", {}).get("origin_sha256") or "")
    expected_profile_origin = _declared_evidence_checksum(
        governance,
        "reports/generated-asset-validation-report.json",
    )
    if expected_profile_origin:
        if profile_origin != expected_profile_origin:
            _validation_error(
                errors,
                "profile_origin_mismatch",
                "profile report origin digest differs from governance evidence",
                "validation/profile-results.json",
            )
    elif profile.get("status") != "not_available":
        _validation_error(
            errors,
            "profile_origin_unbound",
            "profile report is not bound by governance evidence",
            "validation/profile-results.json",
        )
    runtime_origin = str(inventory.get("validation/runtime-results.json", {}).get("origin_sha256") or "")
    expected_runtime_origin = _runtime_report_checksum(profile)
    if expected_runtime_origin:
        if runtime_origin != expected_runtime_origin:
            _validation_error(
                errors,
                "runtime_origin_mismatch",
                "runtime report origin digest differs from profile evidence",
                "validation/runtime-results.json",
            )
    elif runtime.get("status") != "not_available":
        _validation_error(
            errors,
            "runtime_origin_unbound",
            "runtime report is not bound by profile evidence",
            "validation/runtime-results.json",
        )
    if manifest.get("simready_profile") != _profile_identity(profile):
        _validation_error(
            errors,
            "profile_identity_mismatch",
            "manifest Profile identity differs from profile evidence",
            "capsule.json",
        )
    if manifest.get("outputs_included") is True:
        simready = _latest_capsule_stage_manifest(
            root,
            "simready-verification",
            manifest,
            inventory,
        )
        if simready is None:
            _validation_error(
                errors,
                "simready_evidence_missing",
                "released outputs have no selected-run SimReady manifest",
                "attempts/simready-verification",
            )
        else:
            try:
                generated_records = _generated_file_records(simready)
                fingerprint = _asset_fingerprint(simready)
            except CapsuleCreationError as exc:
                for blocker in exc.blockers:
                    _validation_error(
                        errors,
                        "simready_evidence_invalid",
                        blocker,
                        "attempts/simready-verification",
                    )
                generated_records = []
                fingerprint = ""
            if fingerprint and fingerprint != governance.get("asset_fingerprint"):
                _validation_error(
                    errors,
                    "asset_fingerprint_mismatch",
                    "governance fingerprint differs from selected-run SimReady evidence",
                    "governance/governance-record.json",
                )
            if _declared_evidence_checksum(
                simready,
                "reports/generated-asset-validation-report.json",
            ) != _declared_evidence_checksum(
                governance,
                "reports/generated-asset-validation-report.json",
            ):
                _validation_error(
                    errors,
                    "profile_authority_mismatch",
                    "SimReady and governance evidence bind different profile reports",
                    "validation/profile-results.json",
                )
            source_hashes = {
                str(item.get("copy_sha256") or "").removeprefix("sha256:").lower()
                for item in source.get("source_assets") or []
                if isinstance(item, Mapping)
            }
            expected_outputs: dict[str, str] = {}
            excluded_source_count = 0
            required_source_duplicates = (
                _package_closure_output_paths(profile) if manifest.get("outcome") == "positive" else set()
            )
            for record in generated_records:
                project_relative = str(record.get("path") or "")
                if not project_relative.startswith("packaged/"):
                    continue
                checksum = str(record.get("sha256") or "").lower()
                if checksum in source_hashes and project_relative not in required_source_duplicates:
                    excluded_source_count += 1
                    continue
                capsule_relative = "outputs/" + PurePosixPath(project_relative).relative_to("packaged").as_posix()
                expected_outputs[capsule_relative] = checksum
            actual_outputs = {
                relative: entry
                for relative, entry in inventory.items()
                if relative.startswith("outputs/")
            }
            if set(actual_outputs) != set(expected_outputs):
                _validation_error(
                    errors,
                    "output_lineage_mismatch",
                    "released output inventory differs from selected-run SimReady evidence",
                    "outputs",
                )
            for relative, expected_checksum in expected_outputs.items():
                entry = actual_outputs.get(relative, {})
                if entry.get("sha256") != expected_checksum or entry.get("origin_sha256") != expected_checksum:
                    _validation_error(
                        errors,
                        "output_digest_mismatch",
                        "released output digest differs from SimReady evidence",
                        relative,
                    )
            if manifest.get("source_duplicates_excluded_from_outputs") != excluded_source_count:
                _validation_error(
                    errors,
                    "source_exclusion_count_mismatch",
                    "source-output exclusion count differs from SimReady evidence",
                    "capsule.json",
                )
    if manifest.get("outcome") == "positive":
        _validate_positive_underlying_evidence(
            root,
            manifest,
            governance,
            profile,
            runtime,
            inventory,
            errors,
        )
        decision = manifest.get("release_decision")
        positive_blockers = _positive_evidence_blockers(
            governance,
            run_id=str(manifest.get("run_id") or ""),
            request_digest=str(manifest.get("request_digest") or ""),
            release_scope=str(manifest.get("release_scope") or ""),
            decision=decision if isinstance(decision, Mapping) else {},
            profile_report=profile,
            runtime_report=runtime,
            schema_results=recalculated_schema_results,
            evaluated_at=_parse_time(manifest.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
        )
        positive_blockers.extend(_repository_publication_blockers(provenance))
        positive_blockers.extend(_model_bom_publication_blockers(provenance, plan.get("provider_assignments")))
        for blocker in positive_blockers:
            _validation_error(errors, "positive_evidence_failed", blocker, "capsule.json")


def validate_reference_capsule(capsule_dir: str | Path) -> dict[str, Any]:
    """Validate a capsule without modifying it and return a machine-readable report."""

    root_input = Path(capsule_dir)
    root = root_input.resolve(strict=False)
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    checks: dict[str, dict[str, Any]] = {}
    if _has_linklike_component(root_input):
        _validation_error(errors, "capsule_symlink", "capsule root must not be a symlink", ".")
        return {
            "format_version": CAPSULE_FORMAT_VERSION,
            "capsule_id": "",
            "valid": False,
            "status": "invalid",
            "checks": checks,
            "errors": errors,
            "warnings": warnings,
        }
    if not root.is_dir():
        _validation_error(errors, "capsule_missing", "capsule directory does not exist", str(capsule_dir))
        return {
            "format_version": CAPSULE_FORMAT_VERSION,
            "capsule_id": "",
            "valid": False,
            "status": "invalid",
            "checks": checks,
            "errors": errors,
            "warnings": warnings,
        }
    files: list[Path] = []
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if _is_linklike(path):
            _validation_error(errors, "symlink_rejected", "capsule contains a symlink", relative)
            continue
        if path.is_file():
            size = path.stat().st_size
            total_bytes += size
            limit_message = ""
            if len(files) + 1 > MAX_CAPSULE_FILES:
                limit_message = f"capsule exceeds file limit {MAX_CAPSULE_FILES}"
            elif total_bytes > MAX_CAPSULE_BYTES:
                limit_message = f"capsule exceeds aggregate byte limit {MAX_CAPSULE_BYTES}"
            elif size > MAX_CAPSULE_FILE_BYTES:
                limit_message = f"file exceeds byte limit {MAX_CAPSULE_FILE_BYTES}"
            elif path.suffix.lower() in {".json", ".jsonl", ".gltf"} and size > MAX_CAPSULE_JSON_BYTES:
                limit_message = f"JSON evidence exceeds byte limit {MAX_CAPSULE_JSON_BYTES}"
            if limit_message:
                _validation_error(errors, "capsule_limit_exceeded", limit_message, relative)
                return {
                    "format_version": CAPSULE_FORMAT_VERSION,
                    "capsule_id": "",
                    "valid": False,
                    "status": "invalid",
                    "checks": {"limits": {"status": "fail"}},
                    "errors": errors,
                    "warnings": warnings,
                }
            files.append(path)
            if path_error := _validate_capsule_path(relative):
                _validation_error(errors, "path_not_allowed", path_error, relative)

    manifest_path = root / "capsule.json"
    checksum_path = root / "checksums.sha256"
    manifest: dict[str, Any] = {}
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise TypeError("capsule manifest must be an object")
        manifest = loaded
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        _validation_error(errors, "manifest_unreadable", str(exc), "capsule.json")
    checks["manifest"] = {"status": "pass" if manifest else "fail"}

    checksums = _parse_checksums(checksum_path, errors) if checksum_path.exists() else {}
    if not checksum_path.exists():
        _validation_error(errors, "checksums_missing", "checksum manifest is missing", "checksums.sha256")
    actual_paths = {path.relative_to(root).as_posix() for path in files if path.name != "checksums.sha256"}
    if set(checksums) != actual_paths:
        missing = sorted(actual_paths - set(checksums))
        extra = sorted(set(checksums) - actual_paths)
        _validation_error(
            errors,
            "checksum_coverage_mismatch",
            f"missing entries={missing}; extra entries={extra}",
            "checksums.sha256",
        )
    for relative, expected in checksums.items():
        target = root.joinpath(*PurePosixPath(relative).parts)
        if target.is_file() and sha256_file(target).lower() != expected:
            _validation_error(errors, "checksum_mismatch", "file digest differs from checksum manifest", relative)
    checks["checksums"] = {
        "status": "pass" if not any(item["code"].startswith("checksum") for item in errors) else "fail",
        "covered_files": len(checksums),
    }

    inventory = manifest.get("inventory") if isinstance(manifest, Mapping) else None
    inventory_entries = [item for item in inventory or [] if isinstance(item, Mapping)] if isinstance(inventory, list) else []
    inventory_paths = [str(item.get("path") or "") for item in inventory_entries]
    expected_payload_paths = sorted(actual_paths - {"capsule.json"})
    if sorted(inventory_paths) != expected_payload_paths or len(set(inventory_paths)) != len(inventory_paths):
        _validation_error(
            errors,
            "inventory_coverage_mismatch",
            "payload inventory does not exactly cover the non-container payload",
            "capsule.json",
        )
    for entry in inventory_entries:
        relative = str(entry.get("path") or "")
        try:
            safe_path = _safe_relative(relative, "inventory path")
            if path_error := _validate_capsule_path(relative):
                raise ValueError(path_error)
            target = root.joinpath(*safe_path.parts)
            _reject_symlink_chain(root, target)
        except ValueError as exc:
            _validation_error(errors, "inventory_path_invalid", str(exc), relative)
            continue
        if not target.is_file():
            continue
        actual_sha = sha256_file(target)
        if entry.get("sha256") != actual_sha or entry.get("size_bytes") != target.stat().st_size:
            _validation_error(errors, "inventory_file_mismatch", "inventory metadata differs from file", relative)
        if not str(entry.get("licence_expression") or ""):
            _validation_error(errors, "inventory_licence_missing", "licence expression is missing", relative)
    calculated_inventory_digest = sha256_text(_canonical_json(inventory_entries))
    if manifest.get("payload_inventory_sha256") != calculated_inventory_digest:
        _validation_error(errors, "inventory_digest_mismatch", "payload inventory digest differs", "capsule.json")
    checks["inventory"] = {
        "status": "pass" if not any(item["code"].startswith("inventory") for item in errors) else "fail",
        "payload_files": len(inventory_entries),
    }
    source_media_paths = [
        relative for relative in actual_paths if relative.startswith("source/redistributable-inputs/")
    ]
    output_paths = [relative for relative in actual_paths if relative.startswith("outputs/")]
    source_media_included = manifest.get("source_media_included") is True
    outputs_included = manifest.get("outputs_included") is True
    if (
        source_media_included != bool(source_media_paths)
        or manifest.get("source_media_count") != len(source_media_paths)
    ):
        _validation_error(
            errors,
            "source_media_declaration_mismatch",
            "source-media declaration does not match capsule contents",
            "capsule.json",
        )
    if outputs_included != bool(output_paths) or manifest.get("output_file_count") != len(output_paths):
        _validation_error(
            errors,
            "output_declaration_mismatch",
            "output declaration does not match capsule contents",
            "capsule.json",
        )
    checks["content_declarations"] = {
        "status": "pass"
        if not any(item["code"].endswith("_declaration_mismatch") for item in errors)
        else "fail",
        "source_media_files": len(source_media_paths),
        "output_files": len(output_paths),
    }

    schema_inventory = manifest.get("schema_inventory") if isinstance(manifest, Mapping) else None
    if not isinstance(schema_inventory, list) or not schema_inventory:
        _validation_error(errors, "schema_inventory_missing", "schema identities and snapshots are required", "capsule.json")
    else:
        for item in schema_inventory:
            if not isinstance(item, Mapping):
                _validation_error(errors, "schema_inventory_invalid", "schema entry is not an object", "capsule.json")
                continue
            relative = str(item.get("path") or "")
            try:
                schema_path = root.joinpath(*_safe_relative(relative, "schema path").parts)
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                _validation_error(errors, "schema_snapshot_unreadable", str(exc), relative)
                continue
            if item.get("sha256") != sha256_file(schema_path) or item.get("schema_id") != schema.get("$id", ""):
                _validation_error(errors, "schema_identity_mismatch", "schema identity or digest differs", relative)
                continue
            trusted_schema = ROOT / "schemas" / schema_path.name
            if not trusted_schema.is_file():
                _validation_error(
                    errors,
                    "trusted_schema_missing",
                    "matching source-archive schema is unavailable",
                    relative,
                )
                continue
            try:
                trusted_payload = json.loads(trusted_schema.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                _validation_error(errors, "trusted_schema_unreadable", str(exc), relative)
                continue
            if sha256_file(trusted_schema) != item.get("sha256") or trusted_payload.get("$id", "") != item.get(
                "schema_id"
            ):
                _validation_error(
                    errors,
                    "trusted_schema_mismatch",
                    "capsule schema differs from the matching source archive",
                    relative,
                )
    checks["schemas"] = {
        "status": "pass"
        if not any(item["code"].startswith(("schema_", "trusted_schema_")) for item in errors)
        else "fail",
        "schema_count": len(schema_inventory) if isinstance(schema_inventory, list) else 0,
    }

    capsule_schema_path = root / "schemas" / "reference-run-capsule.schema.json"
    if manifest and capsule_schema_path.is_file():
        try:
            capsule_schema = json.loads(capsule_schema_path.read_text(encoding="utf-8"))
            validator = Draft202012Validator(capsule_schema, format_checker=FormatChecker())
            for error in sorted(validator.iter_errors(manifest), key=lambda item: list(item.absolute_path)):
                location = ".".join(str(part) for part in error.absolute_path) or "$"
                _validation_error(
                    errors,
                    "manifest_schema_violation",
                    f"{location}: {error.message}",
                    "capsule.json",
                )
        except (OSError, json.JSONDecodeError) as exc:
            _validation_error(errors, "manifest_schema_unreadable", str(exc), "schemas/reference-run-capsule.schema.json")
    elif manifest:
        _validation_error(
            errors,
            "manifest_schema_missing",
            "reference capsule schema snapshot is missing",
            "schemas/reference-run-capsule.schema.json",
        )
    checks["manifest_schema"] = {
        "status": "pass" if not any(item["code"].startswith("manifest_schema_") for item in errors) else "fail"
    }

    if manifest:
        cross_error_count = len(errors)
        _validate_cross_evidence(root, manifest, inventory_entries, errors)
    else:
        cross_error_count = len(errors)
    checks["cross_evidence"] = {
        "status": "pass" if len(errors) == cross_error_count else "fail"
    }

    for path in files:
        relative = path.relative_to(root).as_posix()
        for detail in _disclosure_errors_file(path):
            _validation_error(errors, "disclosure_detected", f"prohibited {detail} detected", relative)
    checks["disclosure_scan"] = {
        "status": "pass" if not any(item["code"] == "disclosure_detected" for item in errors) else "fail",
        "scanned_files": len(files),
    }

    if manifest:
        if manifest.get("format_version") != CAPSULE_FORMAT_VERSION:
            _validation_error(errors, "format_version_unsupported", "unsupported capsule format", "capsule.json")
        identity_error_count = len(errors)
        _validate_capsule_identity(manifest, errors)
        checks["identity"] = {"status": "pass" if len(errors) == identity_error_count else "fail"}
        outcome_error_count = len(errors)
        _validate_outcome(manifest, root, errors)
        checks["outcome"] = {"status": "pass" if len(errors) == outcome_error_count else "fail"}
        rights_error_count = len(errors)
        _validate_rights(root, manifest, errors)
        checks["rights"] = {"status": "pass" if len(errors) == rights_error_count else "fail"}
    else:
        checks["identity"] = {"status": "fail"}
        checks["outcome"] = {"status": "fail"}
        checks["rights"] = {"status": "fail"}

    errors.sort(key=lambda item: (item["path"], item["code"], item["message"]))
    valid = not errors
    return {
        "format_version": CAPSULE_FORMAT_VERSION,
        "capsule_id": str(manifest.get("capsule_id") or "") if manifest else "",
        "valid": valid,
        "status": "valid" if valid else "invalid",
        "outcome": manifest.get("outcome") if manifest else None,
        "checked_files": len(files),
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
    }
