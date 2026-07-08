from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from typing import Any

from asset_factory_blueprint.utils.ids import canonical_json


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def params_digest(params: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(params).encode("utf-8")).hexdigest()


def _secret_bytes(secret: str) -> bytes:
    value = secret.encode("utf-8")
    if len(value) < 32:
        raise ValueError("approval secret must contain at least 32 UTF-8 bytes")
    return value


def issue_approval_token(
    secret: str,
    *,
    tool: str,
    params: dict[str, Any],
    expires_at: str,
    approved_by: str,
    reason: str,
    nonce: str | None = None,
) -> str:
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("approval expiry must be an ISO 8601 timestamp") from exc
    if expiry.tzinfo is None:
        raise ValueError("approval expiry must include a timezone")
    now = datetime.now(timezone.utc)
    expiry = expiry.astimezone(timezone.utc)
    lifetime = (expiry - now).total_seconds()
    if not 1 <= lifetime <= 86_400:
        raise ValueError("approval expiry must be between 1 second and 24 hours in the future")
    approved_by = approved_by.strip()
    reason = reason.strip()
    if not approved_by or len(approved_by) > 128:
        raise ValueError("approved_by must contain between 1 and 128 characters")
    if not reason or len(reason) > 500:
        raise ValueError("approval reason must contain between 1 and 500 characters")
    payload = {
        "version": 1,
        "tool": tool,
        "params_digest": params_digest(params),
        "issued_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": expiry.isoformat().replace("+00:00", "Z"),
        "approved_by": approved_by,
        "reason": reason,
        "nonce": nonce or secrets.token_hex(16),
    }
    encoded = _encode(canonical_json(payload).encode("utf-8"))
    signature = _encode(hmac.digest(_secret_bytes(secret), encoded.encode("ascii"), "sha256"))
    return f"{encoded}.{signature}"


def verify_approval_token(
    token: str,
    secret: str,
    *,
    tool: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = _encode(hmac.digest(_secret_bytes(secret), encoded.encode("ascii"), "sha256"))
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("approval token signature is invalid")
        payload = json.loads(_decode(encoded))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"approval token is invalid: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("approval token version is invalid")
    if payload.get("tool") != tool:
        raise ValueError("approval token is bound to a different tool")
    if payload.get("params_digest") != params_digest(params):
        raise ValueError("approval token is bound to different parameters")
    try:
        expiry = datetime.fromisoformat(str(payload.get("expires_at") or "").replace("Z", "+00:00"))
        issued_at = datetime.fromisoformat(str(payload.get("issued_at") or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("approval token time binding is invalid") from exc
    now = datetime.now(timezone.utc)
    if issued_at.tzinfo is None or issued_at.astimezone(timezone.utc) > now:
        raise ValueError("approval token issue time is invalid")
    if expiry.tzinfo is None or expiry.astimezone(timezone.utc) <= now:
        raise ValueError("approval token has expired")
    if (expiry.astimezone(timezone.utc) - issued_at.astimezone(timezone.utc)).total_seconds() > 86_400:
        raise ValueError("approval token lifetime exceeds 24 hours")
    approved_by = payload.get("approved_by")
    reason = payload.get("reason")
    if not isinstance(approved_by, str) or not 1 <= len(approved_by.strip()) <= 128:
        raise ValueError("approval token approver is invalid")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        raise ValueError("approval token reason is invalid")
    nonce = str(payload.get("nonce") or "")
    if len(nonce) < 16:
        raise ValueError("approval token nonce is invalid")
    return payload


def approval_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
