"""Identities, attestation and trusted loading for RL lane evidence reports.

Probe reports are produced under the Isaac Lab runtime by ``scripts/rl/isaac_lab_probe.py``
and collision fidelity reports by ``scripts/rl/collision_fidelity.py``. Probe, fidelity
and import-receipt attestations use separate managed secrets and domain-separated
signature contexts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from asset_factory_blueprint.isaac_evidence import (
    ATTESTATION_ALGORITHM,
    ATTESTATION_FIELDS,
    ATTESTATION_SCHEMA_VERSION,
    MAX_REPORT_BYTES,
    canonical_report_bytes,
    parse_runtime_report_bytes,
)

PROBE_REPORT_ID = "asset-factory.rl-probe-evidence"
PROBE_REPORT_VERSION = "1.0"
PROBE_PROTOCOL_ID = "asset-factory.rl-probe-protocol"
PROBE_PROTOCOL_VERSION = "1.0"
FIDELITY_REPORT_ID = "asset-factory.rl-collision-fidelity"
FIDELITY_REPORT_VERSION = "1.0"
REPORT_ATTESTATION_SECRET_ENV = {
    PROBE_REPORT_ID: "AFB_RL_PROBE_ATTESTATION_SECRET",
    FIDELITY_REPORT_ID: "AFB_RL_FIDELITY_ATTESTATION_SECRET",
}
REPORT_PRODUCER_PIN_ENV = {
    PROBE_REPORT_ID: "AFB_RL_PROBE_PRODUCER_SHA256",
    FIDELITY_REPORT_ID: "AFB_RL_FIDELITY_PRODUCER_SHA256",
}
REPORT_ATTESTATION_SIGNATURE_CONTEXT = {
    PROBE_REPORT_ID: b"asset-factory-rl-probe-evidence-attestation-v1",
    FIDELITY_REPORT_ID: b"asset-factory-rl-fidelity-evidence-attestation-v1",
}
IMPORT_ATTESTATION_SECRET_ENV = "AFB_RL_IMPORT_ATTESTATION_SECRET"
IMPORT_ATTESTATION_SIGNATURE_CONTEXT = b"asset-factory-rl-evidence-import-receipt-attestation-v1"
PROBE_IDS = ("smoke", "oracle", "reset", "repeat", "gaming")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_GAMING_PATTERNS = frozenset(
    {"proximity_without_contact", "velocity_without_displacement", "oscillation", "early_termination"}
)
_VOLATILE_CONTRACT_FIELDS = frozenset(
    {
        "contract_sha256",
        "status",
        "gates",
        "blocked_reasons",
        "review_reasons",
        "known_gaps",
        "environment_card",
    }
)

__all__ = [
    "FIDELITY_REPORT_ID",
    "FIDELITY_REPORT_VERSION",
    "PROBE_IDS",
    "PROBE_PROTOCOL_ID",
    "PROBE_PROTOCOL_VERSION",
    "PROBE_REPORT_ID",
    "PROBE_REPORT_VERSION",
    "REPORT_PRODUCER_PIN_ENV",
    "attest_report",
    "attest_import_receipt",
    "attestation_key_id",
    "environment_contract_sha256",
    "environment_manifest_sha256",
    "import_attestation_secret",
    "load_attested_report",
    "producer_bundle_sha256",
    "report_attestation_secret",
    "verify_report",
    "verify_report_attestation",
    "verify_import_receipt",
    "verify_import_receipt_payload",
]


def environment_contract_sha256(rl_block: Mapping[str, Any]) -> str:
    """Digest the fields that determine the rendered environment and its probes."""

    contract = {field: value for field, value in rl_block.items() if field not in _VOLATILE_CONTRACT_FIELDS}
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def environment_manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Digest the stable environment source without evidence-derived review state."""

    canonical = json.loads(json.dumps(manifest, ensure_ascii=False))
    for field in ("status", "validation_status", "review_status", "blocked_reasons", "review_reasons"):
        canonical.pop(field, None)
    canonical["evidence"] = [
        item for item in canonical.get("evidence") or [] if not str(item.get("evidence_id") or "").startswith("rl_")
    ]
    canonical["validation_gates"] = [
        item for item in canonical.get("validation_gates") or [] if not str(item.get("gate_id") or "").startswith("rl-")
    ]
    extensions = canonical.get("extensions") if isinstance(canonical.get("extensions"), dict) else {}
    rl_block = extensions.get("rl") if isinstance(extensions.get("rl"), dict) else None
    if rl_block is not None:
        extensions["rl"] = {field: value for field, value in rl_block.items() if field not in _VOLATILE_CONTRACT_FIELDS}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def producer_bundle_sha256(files: list[dict[str, str]]) -> str:
    """Digest a canonical producer-file inventory."""

    digest = hashlib.sha256()
    for item in sorted(files, key=lambda value: value["path"]):
        digest.update(item["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(item["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _managed_secret(
    environment_name: str,
    environment: Mapping[str, str] | None = None,
) -> bytes:
    source = environment if environment is not None else os.environ
    secret = source.get(environment_name, "").encode("utf-8")
    if len(secret) < 32:
        raise ValueError(f"{environment_name} must contain at least 32 UTF-8 bytes")
    return secret


def _ensure_distinct_role_secrets(environment: Mapping[str, str] | None = None) -> None:
    source = environment if environment is not None else os.environ
    configured = {
        name: value.encode("utf-8")
        for name in (*REPORT_ATTESTATION_SECRET_ENV.values(), IMPORT_ATTESTATION_SECRET_ENV)
        if (value := source.get(name, ""))
    }
    names = sorted(configured)
    collisions = [
        f"{left} and {right}"
        for index, left in enumerate(names)
        for right in names[index + 1 :]
        if hmac.compare_digest(configured[left], configured[right])
    ]
    if collisions:
        raise ValueError("RL attestation role secrets must be distinct: " + "; ".join(collisions))


def report_attestation_secret(
    report_id: str,
    environment: Mapping[str, str] | None = None,
) -> bytes:
    try:
        environment_name = REPORT_ATTESTATION_SECRET_ENV[report_id]
    except KeyError as exc:
        raise ValueError(f"unsupported RL evidence report identity {report_id!r}") from exc
    secret = _managed_secret(environment_name, environment)
    _ensure_distinct_role_secrets(environment)
    return secret


def import_attestation_secret(environment: Mapping[str, str] | None = None) -> bytes:
    secret = _managed_secret(IMPORT_ATTESTATION_SECRET_ENV, environment)
    _ensure_distinct_role_secrets(environment)
    return secret


def attestation_key_id(secret: bytes, role: str) -> str:
    digest = hmac.new(secret, f"asset-factory-rl-{role}-key-id-v1".encode("ascii"), hashlib.sha256).hexdigest()
    return f"afb-rl-{role}-{digest[:20]}"


def _report_identity(payload: Mapping[str, Any]) -> str:
    identity = payload.get("report_identity")
    return str(identity.get("id") or "") if isinstance(identity, Mapping) else ""


def attest_report(
    payload: Mapping[str, Any],
    secret: bytes,
    report_id: str | None = None,
) -> dict[str, str]:
    selected_id = report_id if report_id is not None else _report_identity(payload)
    try:
        context = REPORT_ATTESTATION_SIGNATURE_CONTEXT[selected_id]
    except KeyError as exc:
        raise ValueError(f"unsupported RL evidence report identity {selected_id!r}") from exc
    canonical = canonical_report_bytes(payload)
    payload_digest = hashlib.sha256(canonical).hexdigest()
    signature = hmac.new(secret, context + b"\0" + canonical, hashlib.sha256).hexdigest()
    role = "probe" if selected_id == PROBE_REPORT_ID else "fidelity"
    return {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "status": "signed",
        "algorithm": ATTESTATION_ALGORITHM,
        "key_id": attestation_key_id(secret, role),
        "payload_digest": f"sha256:{payload_digest}",
        "signature": f"hmac-sha256:{signature}",
    }


def attest_import_receipt(payload: Mapping[str, Any], secret: bytes) -> dict[str, str]:
    canonical = canonical_report_bytes(payload)
    payload_digest = hashlib.sha256(canonical).hexdigest()
    signature = hmac.new(
        secret,
        IMPORT_ATTESTATION_SIGNATURE_CONTEXT + b"\0" + canonical,
        hashlib.sha256,
    ).hexdigest()
    return {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "status": "signed",
        "algorithm": ATTESTATION_ALGORITHM,
        "key_id": attestation_key_id(secret, "import"),
        "payload_digest": f"sha256:{payload_digest}",
        "signature": f"hmac-sha256:{signature}",
    }


def verify_report_attestation(payload: Mapping[str, Any], secret: bytes, expected_report_id: str) -> list[str]:
    attestation = payload.get("attestation")
    if not isinstance(attestation, Mapping):
        return ["rl evidence attestation is missing"]
    if attestation.get("status") == "unsigned":
        return ["rl evidence report is unsigned; the producer had no attestation secret"]
    expected = attest_report(payload, secret, expected_report_id)
    errors: list[str] = []
    if set(attestation) != ATTESTATION_FIELDS:
        errors.append("rl evidence attestation has an unexpected shape")
    for key in ("schema_version", "status", "algorithm", "key_id", "payload_digest", "signature"):
        if not hmac.compare_digest(str(attestation.get(key) or ""), expected[key]):
            errors.append(f"rl evidence attestation {key} does not match")
    return errors


def verify_import_attestation(payload: Mapping[str, Any], secret: bytes) -> list[str]:
    attestation = payload.get("attestation")
    if not isinstance(attestation, Mapping):
        return ["rl evidence import receipt attestation is missing"]
    expected = attest_import_receipt(payload, secret)
    errors: list[str] = []
    if set(attestation) != ATTESTATION_FIELDS:
        errors.append("rl evidence import receipt attestation has an unexpected shape")
    for key in ("schema_version", "status", "algorithm", "key_id", "payload_digest", "signature"):
        if not hmac.compare_digest(str(attestation.get(key) or ""), expected[key]):
            errors.append(f"rl evidence import receipt attestation {key} does not match")
    return errors


def verify_report(
    payload: Mapping[str, Any],
    expected_report_id: str,
    *,
    secret: bytes | None = None,
    expected_backend: str | None = None,
    expected_usd_sha256: str | None = None,
    expected_request_digest: str | None = None,
    expected_manifest_sha256: str | None = None,
    expected_contract_sha256: str | None = None,
    expected_package_fingerprint: str | None = None,
    expected_sim_dt: float | None = None,
    expected_decimation: int | None = None,
    expected_rendered_files: Mapping[str, str] | None = None,
) -> list[str]:
    """Structural and binding checks shared by the importer and the design service."""

    errors: list[str] = []
    identity = payload.get("report_identity") if isinstance(payload.get("report_identity"), Mapping) else {}
    if identity.get("id") != expected_report_id:
        errors.append(
            f"rl evidence report identity is {identity.get('id') or 'missing'}, expected {expected_report_id}"
        )
    if payload.get("status") not in {"pass", "blocked"}:
        errors.append("rl evidence status must be pass or blocked")
    expected_version = {
        PROBE_REPORT_ID: PROBE_REPORT_VERSION,
        FIDELITY_REPORT_ID: FIDELITY_REPORT_VERSION,
    }.get(expected_report_id)
    if identity.get("version") != expected_version:
        errors.append(f"rl evidence report version {identity.get('version') or 'missing'} is unsupported")
    if expected_backend is not None and str(payload.get("physics_backend") or "").lower() != expected_backend.lower():
        errors.append(
            f"rl evidence was produced under backend {payload.get('physics_backend') or 'unknown'}, expected {expected_backend}"
        )
    if expected_usd_sha256 is not None and str(payload.get("usd_sha256") or "") != expected_usd_sha256:
        errors.append("rl evidence usd_sha256 does not match the validated package")
    if expected_request_digest is not None and str(payload.get("request_digest") or "") != expected_request_digest:
        errors.append("rl evidence request_digest does not match the current run")
    if expected_manifest_sha256 is not None and str(payload.get("manifest_sha256") or "") != expected_manifest_sha256:
        errors.append("rl evidence manifest_sha256 does not match the source environment manifest")
    if expected_contract_sha256 is not None and str(payload.get("contract_sha256") or "") != expected_contract_sha256:
        errors.append("rl evidence contract_sha256 does not match the current environment contract")
    if (
        expected_package_fingerprint is not None
        and str(payload.get("package_dependency_fingerprint") or "") != expected_package_fingerprint
    ):
        errors.append("rl evidence package_dependency_fingerprint does not match the validated package closure")
    for field in ("usd_sha256", "manifest_sha256", "contract_sha256"):
        value = str(payload.get(field) or "")
        if value and not _SHA256.fullmatch(value):
            errors.append(f"rl evidence {field} must be a lowercase SHA-256")
    package_fingerprint = str(payload.get("package_dependency_fingerprint") or "")
    if package_fingerprint and not _FINGERPRINT.fullmatch(package_fingerprint):
        errors.append("rl evidence package_dependency_fingerprint must be a lowercase sha256 fingerprint")

    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), Mapping) else {}
    actual_dt = runtime.get("sim_dt")
    actual_decimation = runtime.get("decimation")
    if expected_sim_dt is not None and (
        not isinstance(actual_dt, (int, float))
        or isinstance(actual_dt, bool)
        or not math.isclose(float(actual_dt), float(expected_sim_dt), rel_tol=1e-9, abs_tol=1e-12)
    ):
        errors.append(f"rl evidence sim_dt {actual_dt!r} does not match {expected_sim_dt!r}")
    if expected_decimation is not None and actual_decimation != expected_decimation:
        errors.append(f"rl evidence decimation {actual_decimation!r} does not match {expected_decimation!r}")
    if expected_rendered_files is not None and payload.get("rendered_files") != dict(expected_rendered_files):
        errors.append("rl evidence rendered_files do not match the current generated package")

    if expected_report_id == PROBE_REPORT_ID:
        protocol = payload.get("protocol_identity") if isinstance(payload.get("protocol_identity"), Mapping) else {}
        if protocol != {"id": PROBE_PROTOCOL_ID, "version": PROBE_PROTOCOL_VERSION}:
            errors.append("rl probe protocol identity does not match the supported protocol")
        if payload.get("status") == "pass" and runtime.get("isaac_lab_available") is not True:
            errors.append("a passing RL probe report must record an available Isaac Lab runtime")
        if payload.get("status") == "pass" and runtime.get("isaac_lab_version") != "2.3.1":
            errors.append("a passing RL probe report must record Isaac Lab 2.3.1")
        if (
            runtime
            and str(runtime.get("physics_backend") or "").lower() != str(payload.get("physics_backend") or "").lower()
        ):
            errors.append("rl probe runtime backend differs from the report binding")
        probes = payload.get("probes") if isinstance(payload.get("probes"), list) else []
        probe_names = [str(item.get("probe") or "") for item in probes if isinstance(item, Mapping)]
        if len(probe_names) != len(set(probe_names)):
            errors.append("rl probe evidence repeats a probe identity")
        unknown = sorted(set(probe_names) - set(PROBE_IDS))
        if unknown:
            errors.append(f"rl probe evidence contains unsupported probes {unknown}")
        if payload.get("status") == "pass" and set(probe_names) != set(PROBE_IDS):
            missing = sorted(set(PROBE_IDS) - set(probe_names))
            errors.append(f"a passing RL probe report must contain every required probe; missing {missing}")
        passing_metrics: dict[str, Mapping[str, Any]] = {}
        for item in probes:
            if not isinstance(item, Mapping):
                errors.append("rl probe evidence contains a non-object probe")
                continue
            probe = str(item.get("probe") or "")
            if item.get("status") not in {"pass", "blocked"}:
                errors.append(f"{probe or 'unnamed'} probe status must be pass or blocked")
            if payload.get("status") == "pass" and item.get("status") != "pass":
                errors.append(f"a passing RL probe report contains a blocked {probe or 'unnamed'} probe")
            if item.get("status") != "pass":
                continue
            metrics = item.get("metrics") if isinstance(item.get("metrics"), Mapping) else {}
            passing_metrics[probe] = metrics
            if probe == "smoke":
                if (
                    not isinstance(metrics.get("steps"), int)
                    or isinstance(metrics.get("steps"), bool)
                    or metrics["steps"] < 1
                ):
                    errors.append("passing smoke probe has no positive step count")
                for field in ("zero_action_return_mean", "random_policy_return_mean", "random_policy_return_std"):
                    if not _finite_number(metrics.get(field)):
                        errors.append(f"passing smoke probe has no finite {field}")
            elif probe == "oracle":
                for field in ("oracle_return_mean", "oracle_success_rate", "reachable_grasp_fraction"):
                    if not _finite_number(metrics.get(field)):
                        errors.append(f"passing oracle probe has no finite {field}")
                if metrics.get("reachable_grasp_fraction") != 1.0:
                    errors.append("passing oracle probe did not reach every accepted grasp")
                if metrics.get("oracle_success_rate") != 1.0:
                    errors.append("passing oracle probe did not complete every environment")
                if metrics.get("limit_violations") != 0:
                    errors.append("passing oracle probe contains joint-limit violations")
                phase_steps = metrics.get("phase_steps") if isinstance(metrics.get("phase_steps"), Mapping) else {}
                if set(phase_steps) != {"approach", "descend", "close", "lift"} or any(
                    not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in phase_steps.values()
                ):
                    errors.append("passing oracle probe has an invalid phase-step contract")
                oracle_returns = (
                    metrics.get("oracle_returns") if isinstance(metrics.get("oracle_returns"), list) else []
                )
                if not oracle_returns or not all(_finite_number(value) for value in oracle_returns):
                    errors.append("passing oracle probe has no finite per-environment return baseline")
                elif not all(float(value) > 0 for value in oracle_returns):
                    errors.append("passing oracle probe return baselines must be strictly positive")
                per_grasp = metrics.get("per_grasp") if isinstance(metrics.get("per_grasp"), list) else []
                if not per_grasp:
                    errors.append("passing oracle probe has no per-grasp results")
                grasp_ids = [str(grasp.get("grasp_id") or "") for grasp in per_grasp if isinstance(grasp, Mapping)]
                if len(grasp_ids) != len(per_grasp) or any(not grasp_id for grasp_id in grasp_ids):
                    errors.append("passing oracle probe has a missing grasp identity")
                elif len(grasp_ids) != len(set(grasp_ids)):
                    errors.append("passing oracle probe repeats a grasp identity")
                for index, grasp in enumerate(per_grasp):
                    if not isinstance(grasp, Mapping):
                        errors.append(f"passing oracle probe grasp {index} is not an object")
                        continue
                    if grasp.get("reachable_fraction") != 1.0 or grasp.get("success_rate") != 1.0:
                        errors.append(f"passing oracle probe grasp {index} is not fully reachable and successful")
                    if grasp.get("limit_violations") != 0:
                        errors.append(f"passing oracle probe grasp {index} contains joint-limit violations")
                    grasp_returns = grasp.get("returns") if isinstance(grasp.get("returns"), list) else []
                    if len(grasp_returns) != len(oracle_returns) or not all(
                        _finite_number(value) for value in grasp_returns
                    ):
                        errors.append(f"passing oracle probe grasp {index} has an invalid return baseline")
                component_means = (
                    metrics.get("oracle_component_means")
                    if isinstance(metrics.get("oracle_component_means"), Mapping)
                    else {}
                )
                if not component_means or not all(_finite_number(value) for value in component_means.values()):
                    errors.append("passing oracle probe has no finite observed reward components")
                if metrics.get("component_dominance_status") != "pass":
                    errors.append("passing oracle probe has no passing component-dominance audit")
            elif probe == "reset":
                if metrics.get("penetration_measured") is not True:
                    errors.append("passing reset probe did not measure contact separation")
                for field in ("infeasible_reset_fraction", "worst_penetration_m", "worst_settled_speed"):
                    if not _finite_number(metrics.get(field)):
                        errors.append(f"passing reset probe has no finite {field}")
                if metrics.get("infeasible_reset_fraction") != 0:
                    errors.append("passing reset probe contains an infeasible reset")
                seeds = metrics.get("seeds") if isinstance(metrics.get("seeds"), list) else []
                if (
                    not seeds
                    or len(seeds) != len(set(seeds))
                    or any(not isinstance(seed, int) or isinstance(seed, bool) or seed < 0 for seed in seeds)
                ):
                    errors.append("passing reset probe has no valid reset seeds")
                if (
                    not isinstance(metrics.get("settle_steps"), int)
                    or isinstance(metrics.get("settle_steps"), bool)
                    or metrics["settle_steps"] < 1
                ):
                    errors.append("passing reset probe has no positive settle step count")
                settled_speed = metrics.get("settled_speed_metres_per_second")
                penetration_tolerance = metrics.get("penetration_tolerance_m")
                if not _finite_number(settled_speed) or float(settled_speed) <= 0:
                    errors.append("passing reset probe has no positive settled-speed limit")
                elif _finite_number(metrics.get("worst_settled_speed")) and float(
                    metrics["worst_settled_speed"]
                ) > float(settled_speed):
                    errors.append("passing reset probe exceeds its settled-speed limit")
                if not _finite_number(penetration_tolerance) or float(penetration_tolerance) <= 0:
                    errors.append("passing reset probe has no positive penetration tolerance")
                elif _finite_number(metrics.get("worst_penetration_m")) and float(
                    metrics["worst_penetration_m"]
                ) > float(penetration_tolerance):
                    errors.append("passing reset probe exceeds its penetration tolerance")
            elif probe == "repeat":
                for field in ("max_trajectory_deviation_m", "max_return_deviation"):
                    if not _finite_number(metrics.get(field)):
                        errors.append(f"passing repeat probe has no finite {field}")
                if (
                    not isinstance(metrics.get("steps"), int)
                    or isinstance(metrics.get("steps"), bool)
                    or metrics["steps"] < 1
                ):
                    errors.append("passing repeat probe has no positive step count")
                tolerance = metrics.get("tolerance_m")
                return_tolerance = metrics.get("return_tolerance")
                if not _finite_number(tolerance) or float(tolerance) <= 0:
                    errors.append("passing repeat probe has no positive trajectory tolerance")
                elif _finite_number(metrics.get("max_trajectory_deviation_m")) and float(
                    metrics["max_trajectory_deviation_m"]
                ) > float(tolerance):
                    errors.append("passing repeat probe exceeds its trajectory tolerance")
                if not _finite_number(return_tolerance) or float(return_tolerance) <= 0:
                    errors.append("passing repeat probe has no positive return tolerance")
                elif _finite_number(metrics.get("max_return_deviation")) and float(
                    metrics["max_return_deviation"]
                ) > float(return_tolerance):
                    errors.append("passing repeat probe exceeds its return tolerance")
            elif probe == "gaming":
                if (
                    not isinstance(metrics.get("steps"), int)
                    or isinstance(metrics.get("steps"), bool)
                    or metrics["steps"] < 1
                ):
                    errors.append("passing gaming probe has no positive step count")
                allowed_fraction = metrics.get("allowed_fraction_of_oracle")
                if not _finite_number(allowed_fraction) or not 0 <= float(allowed_fraction) < 1:
                    errors.append("passing gaming probe has an invalid oracle-return allowance")
                patterns = metrics.get("patterns") if isinstance(metrics.get("patterns"), Mapping) else {}
                if set(patterns) != _GAMING_PATTERNS:
                    errors.append("passing gaming probe does not contain every required gaming pattern")
                for pattern, result in patterns.items():
                    if not isinstance(result, Mapping) or result.get("status") != "pass":
                        errors.append(f"passing gaming probe contains a blocked {pattern} result")
                        continue
                    for field in ("return_mean", "fraction_of_oracle"):
                        if not _finite_number(result.get(field)):
                            errors.append(f"gaming pattern {pattern} has no finite {field}")
                    if result.get("gamed_environment_count") != 0:
                        errors.append(f"gaming pattern {pattern} contains a reward-gamed environment")
        if payload.get("status") == "pass":
            oracle_metrics = passing_metrics.get("oracle", {})
            gaming_metrics = passing_metrics.get("gaming", {})
            phase_steps = (
                oracle_metrics.get("phase_steps") if isinstance(oracle_metrics.get("phase_steps"), Mapping) else {}
            )
            gaming_steps = gaming_metrics.get("steps")
            if (
                set(phase_steps) == {"approach", "descend", "close", "lift"}
                and all(
                    isinstance(value, int) and not isinstance(value, bool) and value > 0
                    for value in phase_steps.values()
                )
                and isinstance(gaming_steps, int)
                and not isinstance(gaming_steps, bool)
                and gaming_steps != sum(int(value) for value in phase_steps.values())
            ):
                errors.append("passing gaming probe horizon differs from the scripted oracle horizon")
    elif expected_report_id == FIDELITY_REPORT_ID and payload.get("status") == "pass":
        tolerance = payload.get("tolerance_m")
        regions = payload.get("regions") if isinstance(payload.get("regions"), list) else []
        if (
            not isinstance(tolerance, (int, float))
            or isinstance(tolerance, bool)
            or not math.isfinite(float(tolerance))
            or float(tolerance) <= 0
        ):
            errors.append("a passing collision-fidelity report needs a finite positive tolerance_m")
        samples_per_region = payload.get("samples_per_region")
        if not isinstance(samples_per_region, int) or isinstance(samples_per_region, bool) or samples_per_region < 1:
            errors.append("a passing collision-fidelity report needs a positive samples_per_region")
        seed = payload.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            errors.append("a passing collision-fidelity report needs a non-negative seed")
        if not regions:
            errors.append("a passing collision-fidelity report needs at least one measured region")
        region_ids = [str(region.get("id") or "") for region in regions if isinstance(region, Mapping)]
        if len(region_ids) != len(regions) or any(not region_id for region_id in region_ids):
            errors.append("a passing collision-fidelity report has a missing region identity")
        elif len(region_ids) != len(set(region_ids)):
            errors.append("a passing collision-fidelity report repeats a region identity")
        for index, region in enumerate(regions):
            if not isinstance(region, Mapping):
                errors.append(f"collision-fidelity region {index} is not an object")
                continue
            value = region.get("shell_distance_p95_m")
            if region.get("status") != "pass":
                errors.append(f"collision-fidelity region {index} did not pass")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
                errors.append(f"collision-fidelity region {index} has no finite shell distance")
            elif isinstance(tolerance, (int, float)) and float(value) > float(tolerance):
                errors.append(f"collision-fidelity region {index} exceeds tolerance_m")
            if (
                region.get("visual_samples") != samples_per_region
                or region.get("collision_samples") != samples_per_region
            ):
                errors.append(f"collision-fidelity region {index} has incomplete surface sampling")
            directional = [
                region.get("visual_to_collision_p95_m"),
                region.get("visual_to_collision_max_m"),
                region.get("collision_to_visual_p95_m"),
                region.get("collision_to_visual_max_m"),
            ]
            if not all(_finite_number(item) and float(item) >= 0 for item in directional):
                errors.append(f"collision-fidelity region {index} has invalid directional distances")
            elif float(directional[0]) > float(directional[1]) or float(directional[2]) > float(directional[3]):
                errors.append(f"collision-fidelity region {index} has a p95 distance above its maximum")
            elif _finite_number(value) and not math.isclose(
                float(value), max(float(directional[0]), float(directional[2])), rel_tol=1e-9, abs_tol=1e-12
            ):
                errors.append(f"collision-fidelity region {index} shell distance is inconsistent")
    if payload.get("status") == "pass" and payload.get("errors"):
        errors.append("a passing RL evidence report must not contain errors")
    if secret is None:
        try:
            secret = report_attestation_secret(expected_report_id)
        except (KeyError, ValueError) as exc:
            errors.append(f"rl evidence cannot be verified: {exc}")
            return errors
    errors.extend(verify_report_attestation(payload, secret, expected_report_id))
    return errors


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def verify_import_receipt_payload(
    report_relative: str,
    report_bytes: bytes,
    report_payload: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    secret: bytes | None = None,
    environment: Mapping[str, str] | None = None,
) -> list[str]:
    """Verify a receipt against exact report bytes and the current trust policy."""

    raw_report = Path(report_relative)
    if raw_report.is_absolute() or ".." in raw_report.parts:
        return ["rl evidence report path must be project-relative"]
    expected_keys = {
        "receipt_identity",
        "kind",
        "report_path",
        "report_sha256",
        "producer_sha256",
        "producer_policy",
        "importer_identity",
        "imported_at",
        "attestation",
    }
    errors: list[str] = []
    if set(receipt) != expected_keys:
        errors.append("rl evidence import receipt has an unexpected shape")
    if receipt.get("receipt_identity") != {"id": "asset-factory.rl-evidence-import", "version": "1.1"}:
        errors.append("rl evidence import receipt identity is unsupported")
    if receipt.get("importer_identity") != {
        "id": "asset-factory.rl-evidence-importer",
        "version": "1.1",
        "policy_version": "1.1",
    }:
        errors.append("rl evidence import receipt importer identity is unsupported")
    report_identity = (
        report_payload.get("report_identity") if isinstance(report_payload.get("report_identity"), Mapping) else {}
    )
    expected_kind = {PROBE_REPORT_ID: "probe", FIDELITY_REPORT_ID: "fidelity"}.get(report_identity.get("id"))
    if expected_kind is None or receipt.get("kind") != expected_kind:
        errors.append("rl evidence import receipt kind differs from the report")
    expected_pin_environment = REPORT_PRODUCER_PIN_ENV.get(str(report_identity.get("id") or ""))
    source = environment if environment is not None else os.environ
    current_pin = source.get(expected_pin_environment or "", "").strip()
    producer_policy = receipt.get("producer_policy") if isinstance(receipt.get("producer_policy"), Mapping) else {}
    if expected_pin_environment is None:
        errors.append("rl evidence import receipt report identity has no producer-pin policy")
    elif not _SHA256.fullmatch(current_pin):
        errors.append(f"{expected_pin_environment} must be set to the exact lowercase producer-bundle SHA-256")
    elif producer_policy != {
        "environment": expected_pin_environment,
        "approved_sha256": current_pin,
    }:
        errors.append("rl evidence import receipt producer policy differs from the current approved pin")
    if receipt.get("report_path") != raw_report.as_posix():
        errors.append("rl evidence import receipt path differs from the canonical report")
    if receipt.get("report_sha256") != hashlib.sha256(report_bytes).hexdigest():
        errors.append("rl evidence import receipt checksum differs from the canonical report")
    execution = (
        report_payload.get("execution_identity")
        if isinstance(report_payload.get("execution_identity"), Mapping)
        else {}
    )
    if receipt.get("producer_sha256") != execution.get("producer_sha256"):
        errors.append("rl evidence import receipt producer differs from the report")
    if current_pin and receipt.get("producer_sha256") != current_pin:
        errors.append("rl evidence report producer differs from the current approved pin")
    if secret is None:
        try:
            secret = import_attestation_secret(environment)
        except (KeyError, ValueError) as exc:
            errors.append(f"rl evidence import receipt cannot be verified: {exc}")
            return errors
    else:
        try:
            _ensure_distinct_role_secrets(environment)
        except ValueError as exc:
            errors.append(f"rl evidence import receipt cannot be verified: {exc}")
            return errors
        configured_report_secrets = {
            name: value.encode("utf-8")
            for name in REPORT_ATTESTATION_SECRET_ENV.values()
            if (value := source.get(name, ""))
        }
        reused_by = [name for name, value in configured_report_secrets.items() if hmac.compare_digest(secret, value)]
        if reused_by:
            errors.append(
                "rl evidence import receipt cannot be verified: import secret reuses report role "
                + ", ".join(sorted(reused_by))
            )
            return errors
    errors.extend(verify_import_attestation(receipt, secret))
    return errors


def verify_import_receipt(
    project_dir: Path,
    report_relative: str,
    report_payload: Mapping[str, Any],
    *,
    secret: bytes | None = None,
) -> list[str]:
    """Verify that a canonical report passed through the producer-pinning importer."""

    root = project_dir.resolve(strict=True)
    raw_report = Path(report_relative)
    if raw_report.is_absolute() or ".." in raw_report.parts:
        return ["rl evidence report path must be project-relative"]
    report_path = root / raw_report
    receipt_path = report_path.with_suffix(".import.json")
    for label, path in (("report", report_path), ("import receipt", receipt_path)):
        cursor = root
        for part in path.relative_to(root).parts:
            cursor /= part
            if cursor.is_symlink():
                return [f"rl evidence {label} must not traverse a symbolic link"]
    if not report_path.is_file():
        return [f"{raw_report.as_posix()} is missing"]
    if not receipt_path.is_file():
        return [f"{receipt_path.relative_to(root).as_posix()} is missing; import the RL evidence report"]
    try:
        report_bytes = report_path.read_bytes()
        raw_receipt = receipt_path.read_bytes()
        if len(raw_receipt) > MAX_REPORT_BYTES:
            return ["rl evidence import receipt exceeds the maximum report size"]
        receipt = parse_runtime_report_bytes(raw_receipt)
    except (OSError, ValueError) as exc:
        return [f"rl evidence import receipt is not trusted strict JSON: {exc}"]
    return verify_import_receipt_payload(
        report_relative,
        report_bytes,
        report_payload,
        receipt,
        secret=secret,
    )


def load_attested_report(
    project_dir: Path,
    relative_path: str,
    expected_report_id: str,
    *,
    expected_backend: str | None = None,
    expected_usd_sha256: str | None = None,
    expected_request_digest: str | None = None,
    expected_manifest_sha256: str | None = None,
    expected_contract_sha256: str | None = None,
    expected_package_fingerprint: str | None = None,
    expected_sim_dt: float | None = None,
    expected_decimation: int | None = None,
    expected_rendered_files: Mapping[str, str] | None = None,
    require_import_receipt: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return ``(payload, errors)``; an absent report is ``(None, [])`` so the gate can stay pending."""

    path = project_dir / relative_path
    if not path.exists():
        return None, []
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_REPORT_BYTES:
            return None, [f"{relative_path} exceeds the maximum report size"]
        payload = parse_runtime_report_bytes(raw)
    except (OSError, ValueError) as exc:
        return None, [f"{relative_path} is not trusted strict JSON: {exc}"]
    from asset_factory_blueprint.manifests import validate_payload

    schema_name = "rl-probe-evidence" if expected_report_id == PROBE_REPORT_ID else "rl-collision-fidelity"
    errors = [f"{schema_name} schema: {issue.render()}" for issue in validate_payload(schema_name, payload)]
    errors.extend(
        verify_report(
            payload,
            expected_report_id,
            expected_backend=expected_backend,
            expected_usd_sha256=expected_usd_sha256,
            expected_request_digest=expected_request_digest,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_contract_sha256=expected_contract_sha256,
            expected_package_fingerprint=expected_package_fingerprint,
            expected_sim_dt=expected_sim_dt,
            expected_decimation=expected_decimation,
            expected_rendered_files=expected_rendered_files,
        )
    )
    if require_import_receipt:
        errors.extend(verify_import_receipt(project_dir, relative_path, payload))
    return (payload if not errors else None), errors


def write_attested_report(path: Path, payload: dict[str, Any], secret: bytes) -> dict[str, Any]:
    """Attach an attestation and write canonical JSON; shared by the phase 2 scripts and the tests."""

    unsigned = {key: value for key, value in payload.items() if key != "attestation"}
    signed = {**unsigned, "attestation": attest_report(unsigned, secret)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(signed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return signed
