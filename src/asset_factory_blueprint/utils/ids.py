from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any


_PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SHA256_PATTERN = re.compile(r"^(?:sha256:)?[A-Fa-f0-9]{64}$")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "asset_factory_project"


def canonical_json(value: Any) -> str:
    """Serialise a value deterministically for identifiers and signatures."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_id(prefix: str, value: Any, *, digest_length: int = 24) -> str:
    """Return a stable content identifier without exposing the input value."""

    if not _PREFIX_PATTERN.fullmatch(prefix):
        raise ValueError("identifier prefix must use lowercase ASCII letters, digits and underscores")
    if not 12 <= digest_length <= 64:
        raise ValueError("digest_length must be between 12 and 64")
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:digest_length]}"


def new_id(prefix: str, *, created_at: datetime | None = None, entropy: str | None = None) -> str:
    """Return a sortable, collision-resistant identifier for a new record."""

    if not _PREFIX_PATTERN.fullmatch(prefix):
        raise ValueError("identifier prefix must use lowercase ASCII letters, digits and underscores")
    timestamp = (created_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    timestamp_text = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    random_text = entropy or secrets.token_hex(8)
    if not re.fullmatch(r"[A-Za-z0-9]+", random_text):
        raise ValueError("identifier entropy must be ASCII alphanumeric")
    return f"{prefix}_{timestamp_text}_{random_text.lower()}"


def stage_attempt_id(run_id: str, stage_id: str, attempt_number: int, request_digest: str) -> str:
    """Derive the immutable identity of one stage attempt."""

    if attempt_number < 1:
        raise ValueError("attempt_number must be at least 1")
    if not run_id or not stage_id:
        raise ValueError("run_id and stage_id are required")
    if not _SHA256_PATTERN.fullmatch(request_digest):
        raise ValueError("request_digest must be a SHA-256 value")
    return content_id(
        "attempt",
        {
            "run_id": run_id,
            "stage_id": stage_id,
            "attempt_number": attempt_number,
            "request_digest": request_digest.removeprefix("sha256:"),
        },
        digest_length=32,
    )
