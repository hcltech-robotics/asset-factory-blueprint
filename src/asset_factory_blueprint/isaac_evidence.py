from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from typing import Any


REPORT_ID = "asset-factory.isaac-runtime-evidence"
REPORT_VERSION = "1.0"
PROTOCOL_ID = "asset-factory.isaac-runtime-validation"
PROTOCOL_VERSION = "1.0"
ATTESTATION_ALGORITHM = "hmac-sha256"
ATTESTATION_SCHEMA_VERSION = "1.0.0"
ATTESTATION_FIELDS = {
    "schema_version",
    "status",
    "algorithm",
    "key_id",
    "payload_digest",
    "signature",
}
ATTESTATION_SIGNATURE_CONTEXT = b"asset-factory-isaac-runtime-validation-attestation-v1"
ATTESTATION_SECRET_ENV = "AFB_ISAAC_ATTESTATION_SECRET"
PRODUCER_SHA256_ENV = "AFB_ISAAC_PRODUCER_SHA256"
MAX_REPORT_BYTES = 16 * 1024 * 1024
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def attestation_secret(environment: Mapping[str, str] | None = None) -> bytes:
    source = environment if environment is not None else os.environ
    value = source.get(ATTESTATION_SECRET_ENV, "")
    secret = value.encode("utf-8")
    if len(secret) < 32:
        raise ValueError(f"{ATTESTATION_SECRET_ENV} must contain at least 32 UTF-8 bytes")
    return secret


def producer_sha256_pin(environment: Mapping[str, str] | None = None) -> str:
    source = environment if environment is not None else os.environ
    value = source.get(PRODUCER_SHA256_ENV, "").strip()
    if not _LOWER_SHA256.fullmatch(value):
        raise ValueError(f"{PRODUCER_SHA256_ENV} must be an exact lowercase 64-character SHA-256")
    return value


def canonical_report_bytes(payload: Mapping[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "attestation"}
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def attestation_key_id(secret: bytes) -> str:
    digest = hmac.new(secret, b"asset-factory-isaac-attestation-key-id-v1", hashlib.sha256).hexdigest()
    return f"afb-isaac-{digest[:24]}"


def attest_runtime_report(payload: Mapping[str, Any], secret: bytes) -> dict[str, str]:
    canonical = canonical_report_bytes(payload)
    payload_digest = hashlib.sha256(canonical).hexdigest()
    signature = hmac.new(
        secret,
        ATTESTATION_SIGNATURE_CONTEXT + b"\0" + canonical,
        hashlib.sha256,
    ).hexdigest()
    return {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "status": "signed",
        "algorithm": ATTESTATION_ALGORITHM,
        "key_id": attestation_key_id(secret),
        "payload_digest": f"sha256:{payload_digest}",
        "signature": f"hmac-sha256:{signature}",
    }


def verify_runtime_report_attestation(payload: Mapping[str, Any], secret: bytes) -> list[str]:
    attestation = payload.get("attestation")
    if not isinstance(attestation, Mapping):
        return ["runtime report attestation is missing"]
    expected = attest_runtime_report(payload, secret)
    errors: list[str] = []
    if set(attestation) != ATTESTATION_FIELDS:
        errors.append("runtime report attestation has an unexpected shape")
    for key in ("schema_version", "status", "algorithm", "key_id", "payload_digest", "signature"):
        actual_value = str(attestation.get(key) or "")
        expected_value = expected[key]
        if not hmac.compare_digest(actual_value, expected_value):
            errors.append(f"runtime report attestation {key} does not match")
    return errors


def verify_runtime_report_envelope(
    payload: Mapping[str, Any],
    secret: bytes,
    producer_pin: str,
) -> list[str]:
    errors = verify_runtime_report_attestation(payload, secret)
    if payload.get("report_identity") != {"id": REPORT_ID, "version": REPORT_VERSION}:
        errors.append("runtime report identity does not match the supported producer contract")
    if payload.get("protocol_identity") != {"id": PROTOCOL_ID, "version": PROTOCOL_VERSION}:
        errors.append("runtime report protocol identity does not match the supported protocol")
    execution_identity = payload.get("execution_identity")
    if not isinstance(execution_identity, Mapping):
        errors.append("runtime report execution identity is missing")
    else:
        expected_identity = {
            "producer_id": "asset-factory-blueprint.isaac-load-check",
            "producer_version": REPORT_VERSION,
            "producer_sha256": f"sha256:{producer_pin}",
        }
        for field, expected in expected_identity.items():
            if execution_identity.get(field) != expected:
                errors.append(f"runtime report execution identity {field} does not match")
    return errors


def parse_runtime_report_bytes(raw: bytes) -> dict[str, Any]:
    if not raw:
        raise ValueError("runtime report is empty")
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError(f"runtime report exceeds the {MAX_REPORT_BYTES}-byte limit")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"runtime report contains duplicate key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise ValueError(f"runtime report contains non-finite number {value}")

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"runtime report is not strict UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("runtime report root must be a JSON object")
    return payload
