from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from asset_factory_blueprint.execution import atomic_write_json


__all__ = [
    "PHYSICS_EVIDENCE_SECRET_ENV",
    "attest_physics_evidence",
    "canonical_physics_evidence_payload",
    "physics_evidence_secret_from_environment",
    "seal_physics_evidence_file",
    "verify_physics_evidence_attestation",
]


PHYSICS_EVIDENCE_SECRET_ENV = "AFB_PHYSICS_EVIDENCE_SECRET"
ATTESTATION_SCHEMA_VERSION = "1.0.0"
ATTESTATION_ALGORITHM = "HMAC-SHA256"
ATTESTATION_FIELDS = {"schema_version", "algorithm", "key_id", "payload_sha256", "signature"}
ATTESTATION_KEY_ID_CONTEXT = b"asset-factory-physics-evidence-key-id-v1"
ATTESTATION_SIGNATURE_CONTEXT = b"asset-factory-physics-evidence-attestation-v1"
DERIVED_FIELDS = {"attestation", "evidence_fingerprint"}
MAX_PHYSICS_EVIDENCE_BYTES = 4 * 1024 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _secret_bytes(secret: str) -> bytes:
    if not isinstance(secret, str):
        raise ValueError("physics evidence attestation secret must be a string")
    secret_bytes = secret.encode("utf-8")
    if len(secret_bytes) < 32:
        raise ValueError(f"{PHYSICS_EVIDENCE_SECRET_ENV} must contain at least 32 UTF-8 bytes")
    return secret_bytes


def physics_evidence_secret_from_environment(environment: Mapping[str, str] | None = None) -> str:
    """Read and validate the independent physics-evidence signing secret."""

    source = os.environ if environment is None else environment
    secret = source.get(PHYSICS_EVIDENCE_SECRET_ENV, "")
    _secret_bytes(secret)
    return secret


def canonical_physics_evidence_payload(payload: Mapping[str, Any]) -> bytes:
    """Return canonical signed bytes, excluding only derived attestation fields."""

    if not isinstance(payload, Mapping):
        raise ValueError("physics evidence payload must be an object")
    if any(not isinstance(key, str) for key in payload):
        raise ValueError("physics evidence payload keys must be strings")
    unsigned = {key: value for key, value in payload.items() if key not in DERIVED_FIELDS}
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _attestation_key_id(secret_bytes: bytes) -> str:
    digest = hmac.new(secret_bytes, ATTESTATION_KEY_ID_CONTEXT, hashlib.sha256).hexdigest()
    return "afb-physics-" + digest[:32]


def _constant_time_text_equal(reported: Any, expected: str) -> bool:
    if not isinstance(reported, str):
        return False
    return hmac.compare_digest(reported.encode("utf-8"), expected.encode("utf-8"))


def attest_physics_evidence(payload: Mapping[str, Any], secret: str) -> dict[str, Any]:
    """Attach deterministic HMAC evidence and its derived content fingerprint."""

    secret_bytes = _secret_bytes(secret)
    canonical_physics_evidence_payload(payload)
    unsigned = {key: value for key, value in payload.items() if key not in DERIVED_FIELDS}
    canonical_payload = canonical_physics_evidence_payload(unsigned)
    payload_sha256 = hashlib.sha256(canonical_payload).hexdigest()
    signature = hmac.new(
        secret_bytes,
        ATTESTATION_SIGNATURE_CONTEXT + b"\0" + canonical_payload,
        hashlib.sha256,
    ).hexdigest()
    return {
        **unsigned,
        "evidence_fingerprint": "sha256:" + payload_sha256,
        "attestation": {
            "schema_version": ATTESTATION_SCHEMA_VERSION,
            "algorithm": ATTESTATION_ALGORITHM,
            "key_id": _attestation_key_id(secret_bytes),
            "payload_sha256": payload_sha256,
            "signature": signature,
        },
    }


def verify_physics_evidence_attestation(payload: Mapping[str, Any], secret: str) -> list[str]:
    """Return every attestation problem without mutating the supplied evidence."""

    try:
        secret_bytes = _secret_bytes(secret)
    except ValueError as exc:
        return [str(exc)]
    if not isinstance(payload, Mapping):
        return ["physics evidence payload must be an object"]

    attestation = payload.get("attestation")
    if not isinstance(attestation, dict):
        return ["physics evidence attestation is missing"]

    problems: list[str] = []
    if set(attestation) != ATTESTATION_FIELDS:
        problems.append("physics evidence attestation has an unexpected shape")
    if attestation.get("schema_version") != ATTESTATION_SCHEMA_VERSION:
        problems.append("physics evidence attestation schema version is unsupported")
    if attestation.get("algorithm") != ATTESTATION_ALGORITHM:
        problems.append("physics evidence attestation algorithm is unsupported")

    expected_key_id = _attestation_key_id(secret_bytes)
    reported_key_id = attestation.get("key_id")
    if not _constant_time_text_equal(reported_key_id, expected_key_id):
        problems.append("physics evidence attestation key ID does not match the configured secret")

    try:
        canonical_payload = canonical_physics_evidence_payload(payload)
    except (TypeError, ValueError) as exc:
        problems.append(f"physics evidence cannot be canonicalised for attestation: {exc}")
        return problems

    expected_payload_sha256 = hashlib.sha256(canonical_payload).hexdigest()
    reported_payload_sha256 = attestation.get("payload_sha256")
    if (
        not isinstance(reported_payload_sha256, str)
        or not SHA256_PATTERN.fullmatch(
            reported_payload_sha256,
        )
        or not _constant_time_text_equal(reported_payload_sha256, expected_payload_sha256)
    ):
        problems.append("physics evidence attestation payload digest does not match")

    reported_fingerprint = payload.get("evidence_fingerprint")
    expected_fingerprint = "sha256:" + expected_payload_sha256
    if not _constant_time_text_equal(reported_fingerprint, expected_fingerprint):
        problems.append("physics evidence fingerprint does not match the canonical payload")

    expected_signature = hmac.new(
        secret_bytes,
        ATTESTATION_SIGNATURE_CONTEXT + b"\0" + canonical_payload,
        hashlib.sha256,
    ).hexdigest()
    reported_signature = attestation.get("signature")
    if (
        not isinstance(reported_signature, str)
        or not SHA256_PATTERN.fullmatch(
            reported_signature,
        )
        or not _constant_time_text_equal(reported_signature, expected_signature)
    ):
        problems.append("physics evidence attestation signature does not match")
    return problems


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not permitted: {value}")


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _reject_link_chain(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.exists() and _is_link(candidate):
            raise ValueError(f"physics evidence paths must not traverse symbolic links or junctions: {candidate}")


def _read_evidence_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"physics evidence input is not a regular file: {path}")
    with path.open("rb") as stream:
        raw = stream.read(MAX_PHYSICS_EVIDENCE_BYTES + 1)
    if len(raw) > MAX_PHYSICS_EVIDENCE_BYTES:
        raise ValueError(f"physics evidence input exceeds the {MAX_PHYSICS_EVIDENCE_BYTES}-byte limit")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"physics evidence input is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("physics evidence input root must be an object")
    return payload


def seal_physics_evidence_file(
    input_path: str | Path,
    output_path: str | Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Seal one JSON record with the configured secret and write it atomically."""

    source = Path(input_path).absolute()
    target = Path(output_path).absolute()
    _reject_link_chain(source)
    _reject_link_chain(target)
    resolved_source = source.resolve(strict=True)
    if target.resolve(strict=False) == resolved_source:
        raise ValueError("physics evidence input and sealed output paths must be distinct")
    if target.exists() and not target.is_file():
        raise ValueError(f"physics evidence output is not a regular file: {target}")
    payload = _read_evidence_file(source)
    secret = physics_evidence_secret_from_environment(environment)
    sealed = attest_physics_evidence(payload, secret)
    atomic_write_json(target, sealed)
    attestation = sealed["attestation"]
    return {
        "status": "sealed",
        "output": str(target),
        "evidence_fingerprint": sealed["evidence_fingerprint"],
        "key_id": attestation["key_id"],
        "payload_sha256": attestation["payload_sha256"],
    }
