from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from asset_factory_blueprint.cli import main
from asset_factory_blueprint.physics_evidence import (
    PHYSICS_EVIDENCE_SECRET_ENV,
    attest_physics_evidence,
    canonical_physics_evidence_payload,
    physics_evidence_secret_from_environment,
    verify_physics_evidence_attestation,
)
from asset_factory_blueprint.services.asset_authoring import (
    _materialise_physics_evidence,
    _normalise_physics_evidence,
)


SECRET = "physics-evidence-test-secret-32-bytes-minimum"


def _unsigned_record(project: Path) -> dict[str, Any]:
    evidence_path = project / "evidence" / "scale-reading.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text('{"mass_kg":2.75}\n', encoding="utf-8")
    digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    return {
        "status": "accepted",
        "prim_path": "/asset_id",
        "mass": 2.75,
        "center_of_mass": [0.0, 0.0, 0.04],
        "diagonal_inertia": [0.08, 0.09, 0.05],
        "principal_axes": [1.0, 0.0, 0.0, 0.0],
        "method": "measured",
        "unit_policy": {"mass": "kg", "length": "m", "inertia": "kg*m^2"},
        "uncertainty": {"mass": 0.02, "diagonal_inertia": [0.005, 0.005, 0.005]},
        "source_evidence_ids": ["scale-reading"],
        "evidence": [
            {
                "evidence_id": "scale-reading",
                "path": "evidence/scale-reading.json",
                "sha256": digest,
            }
        ],
        "approval": {
            "status": "accepted",
            "decision_id": "physics-review-001",
            "reviewer": "operator@example.org",
            "decided_at": "2026-07-09T12:00:00Z",
        },
    }


def test_attestation_binds_the_complete_accepted_payload(tmp_path: Path) -> None:
    unsigned = _unsigned_record(tmp_path)
    sealed = attest_physics_evidence(unsigned, SECRET)

    canonical = canonical_physics_evidence_payload(sealed)
    payload_sha256 = hashlib.sha256(canonical).hexdigest()
    assert sealed["evidence_fingerprint"] == "sha256:" + payload_sha256
    assert sealed["attestation"]["payload_sha256"] == payload_sha256
    assert sealed["attestation"]["algorithm"] == "HMAC-SHA256"
    assert sealed["attestation"]["key_id"].startswith("afb-physics-")
    assert verify_physics_evidence_attestation(sealed, SECRET) == []

    mutations = [
        ("mass", 2.8),
        ("unit_policy", {"mass": "kg", "length": "cm", "inertia": "kg*m^2"}),
        ("uncertainty", {"mass": 0.03, "diagonal_inertia": [0.005, 0.005, 0.005]}),
        ("source_evidence_ids", ["other-reading"]),
        ("approval", {**sealed["approval"], "reviewer": "other@example.org"}),
    ]
    for field, value in mutations:
        tampered = copy.deepcopy(sealed)
        tampered[field] = value
        problems = verify_physics_evidence_attestation(tampered, SECRET)
        assert "physics evidence attestation payload digest does not match" in problems
        assert "physics evidence attestation signature does not match" in problems

    tampered_digest = copy.deepcopy(sealed)
    tampered_digest["evidence"][0]["sha256"] = "0" * 64
    assert verify_physics_evidence_attestation(tampered_digest, SECRET)

    malformed_key_id = copy.deepcopy(sealed)
    malformed_key_id["attestation"]["key_id"] = "non-ascii-\u00e9"
    assert "physics evidence attestation key ID does not match the configured secret" in (
        verify_physics_evidence_attestation(malformed_key_id, SECRET)
    )


def test_secret_is_required_and_has_a_minimum_utf8_length() -> None:
    with pytest.raises(ValueError, match="at least 32 UTF-8 bytes"):
        physics_evidence_secret_from_environment({PHYSICS_EVIDENCE_SECRET_ENV: "too-short"})
    assert physics_evidence_secret_from_environment({PHYSICS_EVIDENCE_SECRET_ENV: SECRET}) == SECRET


def test_sealed_record_matches_the_run_request_contract(tmp_path: Path) -> None:
    schema_path = Path(__file__).parents[1] / "schemas" / "run-request.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    physics_schema = schema["properties"]["constraints"]["properties"]["physics_evidence"]
    errors = list(
        Draft202012Validator(physics_schema).iter_errors(attest_physics_evidence(_unsigned_record(tmp_path), SECRET))
    )
    assert errors == []


def test_cli_seals_a_strict_json_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unsigned_path = tmp_path / "physics-evidence.unsigned.json"
    sealed_path = tmp_path / "physics-evidence.sealed.json"
    unsigned_path.write_text(json.dumps(_unsigned_record(tmp_path)), encoding="utf-8")
    monkeypatch.setenv(PHYSICS_EVIDENCE_SECRET_ENV, SECRET)

    assert main(["physics-evidence", "seal", "--input", str(unsigned_path), "--output", str(sealed_path)]) == 0
    sealed = json.loads(sealed_path.read_text(encoding="utf-8"))
    assert verify_physics_evidence_attestation(sealed, SECRET) == []


def test_authoring_requires_valid_attestation_and_packages_exact_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PHYSICS_EVIDENCE_SECRET_ENV, SECRET)
    sealed = attest_physics_evidence(_unsigned_record(tmp_path), SECRET)

    accepted, problems = _normalise_physics_evidence(tmp_path, "asset_id", sealed)
    assert problems == []
    assert accepted == sealed

    package = tmp_path / "package"
    paths = _materialise_physics_evidence(tmp_path, package, {"accepted_evidence": accepted})
    binding_path = package / "evidence" / "physics-evidence-binding.json"
    assert binding_path in paths
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    assert binding["attested_evidence"] == sealed
    assert binding["evidence_fingerprint"] == sealed["evidence_fingerprint"]
    assert verify_physics_evidence_attestation(binding["attested_evidence"], SECRET) == []

    monkeypatch.delenv(PHYSICS_EVIDENCE_SECRET_ENV)
    accepted, problems = _normalise_physics_evidence(tmp_path, "asset_id", sealed)
    assert accepted is None
    assert f"{PHYSICS_EVIDENCE_SECRET_ENV} must contain at least 32 UTF-8 bytes" in problems

    monkeypatch.setenv(PHYSICS_EVIDENCE_SECRET_ENV, SECRET)
    tampered = copy.deepcopy(sealed)
    tampered["mass"] = 3.0
    accepted, problems = _normalise_physics_evidence(tmp_path, "asset_id", tampered)
    assert accepted is None
    assert "physics evidence attestation payload digest does not match" in problems


@pytest.mark.parametrize(
    ("field", "value", "expected_problem"),
    [
        (
            "diagonal_inertia",
            [0.01, 0.01, 0.03],
            "diagonal_inertia principal moments must satisfy the rigid-body triangle inequalities",
        ),
        (
            "principal_axes",
            [2.0, 0.0, 0.0, 0.0],
            "principal_axes must contain a finite unit quaternion",
        ),
    ],
)
def test_attestation_does_not_bypass_physical_validity_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: list[float],
    expected_problem: str,
) -> None:
    monkeypatch.setenv(PHYSICS_EVIDENCE_SECRET_ENV, SECRET)
    unsigned = _unsigned_record(tmp_path)
    unsigned[field] = value
    accepted, problems = _normalise_physics_evidence(
        tmp_path,
        "asset_id",
        attest_physics_evidence(unsigned, SECRET),
    )
    assert accepted is None
    assert expected_problem in problems
