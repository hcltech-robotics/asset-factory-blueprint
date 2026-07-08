from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from asset_factory_blueprint.config import ROOT


_SCHEMA_VERSION_PATTERN = re.compile(r"^[1-9][0-9]*\.[0-9]+(?:\.[0-9]+)?(?:[-+][0-9A-Za-z.-]+)?$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_SHA256_PATTERN = re.compile(r"^(?:sha256:)?[A-Fa-f0-9]{64}$")
_VALID_STATUSES = {"proposal", "validated", "review_required", "blocked", "released", "not_validated"}


@dataclass(frozen=True)
class ManifestValidationIssue:
    code: str
    path: str
    message: str

    def render(self) -> str:
        return f"{self.path}: {self.message} [{self.code}]"


def schema_dir() -> Path:
    return ROOT / "schemas"


def list_schemas() -> list[str]:
    return sorted(path.name for path in schema_dir().glob("*.schema.json"))


def load_schema(name: str) -> dict[str, Any]:
    file_name = name if name.endswith(".schema.json") else f"{name}.schema.json"
    path = schema_dir() / file_name
    if not path.exists():
        raise FileNotFoundError(f"unknown schema: {name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_local_ref(schema: dict[str, Any], root_schema: dict[str, Any]) -> dict[str, Any]:
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/"):
        return schema
    value: Any = root_schema
    for part in reference[2:].split("/"):
        value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def _example_string(schema: dict[str, Any]) -> str:
    pattern = str(schema.get("pattern") or "")
    if "prov_" in pattern and "{32}" in pattern:
        return "prov_" + ("0" * 32)
    if "attempt_" in pattern and "{32}" in pattern:
        return "attempt_" + ("0" * 32)
    if "{64}" in pattern:
        return "0" * 64
    if "[A-Z][A-Z0-9]*" in pattern:
        return "REQ.001"
    if schema.get("format") == "date-time":
        return "1970-01-01T00:00:00Z"
    if schema.get("format") in {"uri", "uri-reference"}:
        return "https://example.invalid/resource"
    if "[0-9]+\\.[0-9]+\\.[0-9]+" in pattern:
        return "1.0.0"
    if "[0-9]+" in pattern and "\\." in pattern:
        return "1.0"
    return "example"


def _value_for(schema: dict[str, Any], root_schema: dict[str, Any] | None = None) -> Any:
    root_schema = root_schema or schema
    schema = _resolve_local_ref(schema, root_schema)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "default" in schema:
        return copy.deepcopy(schema["default"])
    if "anyOf" in schema:
        return _value_for(schema["anyOf"][0], root_schema)
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), "null")
    if schema_type == "string":
        return _example_string(schema)
    if schema_type == "boolean":
        return True
    if schema_type == "number":
        return 0.0
    if schema_type == "integer":
        return int(schema.get("minimum", 0))
    if schema_type == "array":
        return [_value_for(schema.get("items", {"type": "string"}), root_schema)]
    if schema_type == "object":
        result = {}
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            result[key] = _value_for(props.get(key, {"type": "string"}), root_schema)
        return result
    return "example"


def skeleton(name: str) -> dict[str, Any]:
    schema = load_schema(name)
    result = _value_for(schema, schema)
    if isinstance(result, dict):
        result.setdefault("id", name.replace(".schema.json", ""))
        if "version" in schema.get("properties", {}):
            result["version"] = _value_for(schema["properties"]["version"], schema)
        if "status" in schema.get("properties", {}):
            statuses = schema["properties"]["status"].get("enum", [])
            if "proposal" in statuses:
                result["status"] = "proposal"
        if "evidence" in schema.get("properties", {}):
            result["evidence"] = []
        if "extensions" in schema.get("properties", {}):
            result["extensions"] = {}
    return copy.deepcopy(result)


def _json_path(parts: Any) -> str:
    suffix = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in parts)
    return "$" + suffix


def _major_version(payload: dict[str, Any]) -> int | None:
    version = str(payload.get("version") or "")
    if not _SCHEMA_VERSION_PATTERN.fullmatch(version):
        return None
    return int(version.split(".", 1)[0])


def _v2_contract_issues(schema: dict[str, Any], payload: dict[str, Any]) -> list[ManifestValidationIssue]:
    if _major_version(payload) != 2:
        return []
    issues: list[ManifestValidationIssue] = []
    known_properties = set(schema.get("properties", {})) | {"extensions"}
    for property_name in sorted(set(payload) - known_properties):
        issues.append(
            ManifestValidationIssue(
                code="unknown_property",
                path=f"$.{property_name}",
                message="v2 fields must be declared by the schema or placed under extensions",
            )
        )
    identifier = payload.get("id")
    if identifier is not None and not _IDENTIFIER_PATTERN.fullmatch(str(identifier)):
        issues.append(ManifestValidationIssue("invalid_identifier", "$.id", "identifier contains unsupported characters"))
    status = payload.get("status")
    if status is not None and status not in _VALID_STATUSES:
        issues.append(ManifestValidationIssue("invalid_status", "$.status", "status is not a recognised lifecycle state"))
    extensions = payload.get("extensions", {})
    if not isinstance(extensions, dict):
        issues.append(ManifestValidationIssue("invalid_extensions", "$.extensions", "extensions must be an object"))
    for index, evidence in enumerate(payload.get("evidence", [])):
        if not isinstance(evidence, dict):
            continue
        checksum = str(evidence.get("checksum") or "")
        if not _SHA256_PATTERN.fullmatch(checksum):
            issues.append(
                ManifestValidationIssue(
                    "invalid_checksum",
                    f"$.evidence[{index}].checksum",
                    "v2 evidence checksums must be SHA-256 values",
                )
            )
    return issues


def validate_payload(schema_name: str, payload: dict[str, Any]) -> list[ManifestValidationIssue]:
    schema = load_schema(schema_name)
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    issues = [
        ManifestValidationIssue(
            code=f"schema_{error.validator}",
            path=_json_path(error.absolute_path),
            message=error.message,
        )
        for error in sorted(validator.iter_errors(payload), key=lambda item: _json_path(item.absolute_path))
    ]
    issues.extend(_v2_contract_issues(schema, payload))
    return issues


def validate_manifest(schema_name: str, manifest_path: str | Path) -> list[str]:
    payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    return [issue.render() for issue in validate_payload(schema_name, payload)]
