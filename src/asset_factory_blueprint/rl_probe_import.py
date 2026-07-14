"""Verify and import attested RL runtime evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from asset_factory_blueprint.execution import (
    atomic_write_json,
    durable_replace,
    durable_unlink,
    workspace_lease,
)
from asset_factory_blueprint.isaac_evidence import MAX_REPORT_BYTES, parse_runtime_report_bytes
from asset_factory_blueprint.manifests import validate_payload
from asset_factory_blueprint.rl_evidence import (
    FIDELITY_REPORT_ID,
    PROBE_REPORT_ID,
    REPORT_PRODUCER_PIN_ENV,
    attest_import_receipt,
    environment_manifest_sha256,
    environment_contract_sha256,
    import_attestation_secret,
    producer_bundle_sha256,
    verify_import_receipt,
    verify_report,
)
from asset_factory_blueprint.rl_render import verify_rendered_package_sources
from asset_factory_blueprint.schemas.common import RunPlan, RunRequest
from asset_factory_blueprint.services.rl_environment import design_environment, merge_design_into_manifest
from asset_factory_blueprint.utils.checksums import sha256_file, sha256_text
from asset_factory_blueprint.utils.package_fingerprint import package_inventory_fingerprint
from asset_factory_blueprint.validation import build_project_checksum_inventory

PRODUCER_PIN_ENV = {
    "probe": REPORT_PRODUCER_PIN_ENV[PROBE_REPORT_ID],
    "fidelity": REPORT_PRODUCER_PIN_ENV[FIDELITY_REPORT_ID],
}
CANONICAL_PATH = {
    "probe": "reports/rl-probe-evidence.json",
    "fidelity": "reports/rl-collision-fidelity.json",
}
IMPORT_RECEIPT_PATH = {
    "probe": "reports/rl-probe-evidence.import.json",
    "fidelity": "reports/rl-collision-fidelity.import.json",
}
REPORT_ID = {"probe": PROBE_REPORT_ID, "fidelity": FIDELITY_REPORT_ID}
PRODUCER_FILES = {
    "probe": {
        "src/asset_factory_blueprint/rl_runtime_probe.py",
        "src/asset_factory_blueprint/rl_probes.py",
        "src/asset_factory_blueprint/rl_evidence.py",
        "src/asset_factory_blueprint/rl_render.py",
    },
    "fidelity": {
        "src/asset_factory_blueprint/rl_collision_fidelity.py",
        "src/asset_factory_blueprint/rl_fidelity.py",
        "src/asset_factory_blueprint/rl_evidence.py",
        "src/asset_factory_blueprint/rl_render.py",
    },
}
RENDERED_FILES = ("__init__.py", "rl_env_cfg.py", "custom_mdp.py", "rl_env_cfg.render.json")
_LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRANSACTION_AUXILIARY_PATHS = (
    "reports/rl-task-fitness-protocol.json",
    "reports/environment-card.md",
    "reports/rl-environment-design-report.json",
    "evidence/checksums.json",
)


def _inside(project_dir: Path, target: Path, label: str) -> Path:
    root = project_dir.resolve(strict=True)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the project workspace") from exc
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"{label} must not traverse a symbolic link")
    resolved = target.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the project workspace") from exc
    return resolved


def _project_file(project_dir: Path, value: str, label: str) -> Path:
    raw = Path(value)
    if not value or raw.is_absolute() or ".." in raw.parts:
        raise ValueError(f"{label} must be project-relative")
    target = _inside(project_dir, project_dir / raw, label)
    if not target.is_file():
        raise ValueError(f"{label} is missing")
    return target


def _read_report(report: str | Path) -> tuple[bytes, dict[str, Any]]:
    path = Path(report).resolve(strict=True)
    if not path.is_file() or path.is_symlink():
        raise ValueError("report must be a regular file")
    raw = path.read_bytes()
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("report exceeds the maximum size")
    return raw, parse_runtime_report_bytes(raw)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Atomically replace one file while preserving its exact bytes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        durable_replace(temporary, path)
    except BaseException:
        durable_unlink(temporary, missing_ok=True)
        raise


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=True) + "\n").encode("utf-8")


def _snapshot_files(paths: list[Path]) -> dict[Path, bytes | None]:
    snapshots: dict[Path, bytes | None] = {}
    for path in paths:
        if path.exists() and not path.is_file():
            raise ValueError(f"transaction target {path.name} must be a regular file")
        snapshots[path] = path.read_bytes() if path.is_file() else None
    return snapshots


def _restore_snapshot(path: Path, payload: bytes | None) -> None:
    if payload is None:
        if path.exists() and not path.is_file() and not path.is_symlink():
            raise ValueError(f"cannot remove non-file transaction target {path}")
        durable_unlink(path, missing_ok=True)
        return
    _atomic_write_bytes(path, payload)


def _rollback_files(snapshots: dict[Path, bytes | None], manifest_path: Path) -> None:
    """Restore project files, making the original manifest visible only after every dependency."""

    failures: list[str] = []
    for path, payload in snapshots.items():
        if path == manifest_path:
            continue
        try:
            _restore_snapshot(path, payload)
        except (OSError, ValueError) as exc:
            failures.append(f"{path}: {exc}")
    if failures:
        raise RuntimeError(
            "RL evidence import rollback was incomplete; the manifest remains non-validated: " + "; ".join(failures)
        )
    _restore_snapshot(manifest_path, snapshots[manifest_path])


def _pending_manifest(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    """Return a fail-closed manifest for the report-to-receipt publication window."""

    reason = f"{kind} evidence import is pending"
    payload["status"] = "not_validated"
    payload["validation_status"] = "not_validated"
    payload["review_status"] = "review_required"
    payload["blocked_reasons"] = []
    payload["review_reasons"] = [reason]
    pending_gate = {
        "gate_id": "rl-evidence-import",
        "status": "pending",
        "reasons": [reason],
        "evidence_ids": [],
    }
    payload["validation_gates"] = [
        gate
        for gate in payload.get("validation_gates", [])
        if isinstance(gate, dict) and not str(gate.get("gate_id") or "").startswith("rl-")
    ] + [pending_gate]
    extensions = payload.get("extensions") if isinstance(payload.get("extensions"), dict) else {}
    rl_block = extensions.get("rl") if isinstance(extensions.get("rl"), dict) else None
    if rl_block is not None:
        rl_block["status"] = "review_required"
        rl_block["gates"] = [pending_gate]
        rl_block["blocked_reasons"] = []
        rl_block["review_reasons"] = [reason]
    return payload


def _rendered_files(project_dir: Path, manifest: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    env_dir = project_dir / "envs"
    hashes = verify_rendered_package_sources(project_dir, manifest)
    record = json.loads((env_dir / "rl_env_cfg.render.json").read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("ready") is not True:
        raise ValueError("rendered environment record is not ready")
    if record.get("files") != {name: hashes[name] for name in RENDERED_FILES[:3]}:
        raise ValueError("rendered environment source hashes differ from the render record")
    return hashes, record


def _project_bindings(project_dir: Path) -> dict[str, Any]:
    request_path = _project_file(project_dir, "run-request.json", "run request")
    request = RunRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
    plan_path = _project_file(project_dir, "run-plan.json", "run plan")
    plan = RunPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    current_request_digest = "sha256:" + sha256_text(request.model_dump_json())
    if plan.request_digest != current_request_digest:
        raise ValueError("run plan digest does not identify the persisted run request")
    if plan.request_id != request.id or plan.asset_id != request.id:
        raise ValueError("run plan identity does not match the persisted run request")
    manifest_path = _project_file(project_dir, "manifests/rl-environment-manifest.json", "RL environment manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    block = ((manifest.get("extensions") or {}).get("rl") or {}) if isinstance(manifest, dict) else {}
    if environment_contract_sha256(block) != block.get("contract_sha256"):
        raise ValueError("RL environment contract SHA-256 is stale")
    runtime = block.get("runtime") if isinstance(block.get("runtime"), dict) else {}
    task = block.get("task") if isinstance(block.get("task"), dict) else {}
    embodiment = task.get("embodiment") if isinstance(task.get("embodiment"), dict) else {}
    robot_relative = str(embodiment.get("project_path") or "")
    robot_path = _project_file(project_dir, robot_relative, "embodiment URDF")
    robot_sha256 = sha256_file(robot_path)
    if robot_sha256 != embodiment.get("sha256"):
        raise ValueError("RL contract embodiment SHA-256 differs from the materialised URDF")
    simready_path = _project_file(project_dir, "manifests/simready-asset-manifest.json", "SimReady asset manifest")
    simready = json.loads(simready_path.read_text(encoding="utf-8"))
    usd_relative = str(simready.get("usd_root_path") or block.get("scene", {}).get("composed_root") or "")
    usd_path = _project_file(project_dir, usd_relative, "validated USD")
    package = package_inventory_fingerprint(usd_path.parent)
    if package["status"] != "pass":
        raise ValueError("validated package inventory cannot be recomputed")
    rendered, render_record = _rendered_files(project_dir, manifest)
    runtime_report_path = _project_file(project_dir, "reports/isaac-load-check.json", "Isaac runtime report")
    protocol_ref = block.get("task_fitness_protocol") if isinstance(block.get("task_fitness_protocol"), dict) else {}
    protocol_path = _project_file(project_dir, str(protocol_ref.get("path") or ""), "task-fitness protocol")
    if sha256_file(protocol_path) != protocol_ref.get("sha256"):
        raise ValueError("task-fitness protocol SHA-256 differs from the RL contract")
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    protocol_errors = validate_payload("task-fitness-protocol", protocol)
    if protocol_errors:
        raise ValueError("task-fitness protocol schema: " + "; ".join(issue.render() for issue in protocol_errors))
    if protocol.get("protocol_id") != protocol_ref.get("protocol_id"):
        raise ValueError("task-fitness protocol identity differs from the RL contract")
    protocol_rl = ((protocol.get("extensions") or {}).get("rl") or {}) if isinstance(protocol, dict) else {}
    if protocol_rl.get("request_digest") != current_request_digest:
        raise ValueError("task-fitness protocol belongs to another run request")
    grasp_ids = [str(item.get("id") or "") for item in task.get("grasp_frames") or [] if isinstance(item, dict)]
    if not grasp_ids or not all(grasp_ids) or len(grasp_ids) != len(set(grasp_ids)):
        raise ValueError("RL contract needs unique non-empty accepted grasp IDs")
    values = {
        "request_digest": current_request_digest,
        "manifest_sha256": environment_manifest_sha256(manifest),
        "manifest_path": "manifests/rl-environment-manifest.json",
        "contract_sha256": str(block.get("contract_sha256") or ""),
        "physics_backend": str(runtime.get("physics_backend") or ""),
        "sim_dt": runtime.get("sim_dt"),
        "decimation": runtime.get("decimation"),
        "runtime_source_report_sha256": sha256_file(runtime_report_path),
        "usd_path": usd_relative,
        "usd_sha256": sha256_file(usd_path),
        "package_dependency_fingerprint": package["fingerprint"],
        "rendered_files": rendered,
        "probe_parameters": protocol_rl.get("probe_parameters") or {},
        "fidelity_parameters": protocol_rl.get("collision_fidelity") or {},
        "settle_parameters": runtime.get("settle_parameters") or {},
        "num_envs": runtime.get("num_envs"),
        "grasp_ids": grasp_ids,
        "grasp_regions": [
            {
                "id": str(item.get("id") or ""),
                "centre": [float(value) for value in item.get("frame", [])[:3]],
            }
            for item in task.get("grasp_frames") or []
            if isinstance(item, dict)
        ],
    }
    if runtime.get("validated_usd_sha256") != values["usd_sha256"]:
        raise ValueError("RL contract USD SHA-256 differs from the materialised asset")
    if runtime.get("validated_package_dependency_fingerprint") != values["package_dependency_fingerprint"]:
        raise ValueError("RL contract package fingerprint differs from the materialised package")
    if (runtime.get("runtime_evidence") or {}).get("sha256") != values["runtime_source_report_sha256"]:
        raise ValueError("RL contract runtime evidence hash differs from the materialised report")
    for field in ("manifest_sha256", "contract_sha256", "usd_sha256", "runtime_source_report_sha256"):
        if not _LOWER_SHA256.fullmatch(str(values[field] or "")):
            raise ValueError(f"project binding {field} is not a lowercase SHA-256")
    if render_record.get("manifest_sha256") != values["manifest_sha256"]:
        raise ValueError("rendered environment was produced from a different manifest")
    if render_record.get("contract_sha256") != values["contract_sha256"]:
        raise ValueError("rendered environment was produced from a different contract")
    if render_record.get("package_dependency_fingerprint") != values["package_dependency_fingerprint"]:
        raise ValueError("rendered environment was produced from a different package closure")
    if render_record.get("robot_urdf") != robot_relative or render_record.get("robot_urdf_sha256") != robot_sha256:
        raise ValueError("rendered environment was produced from a different embodiment URDF")
    return values


def _producer_pin(kind: str, payload: dict[str, Any]) -> tuple[str | None, list[str]]:
    env_name = PRODUCER_PIN_ENV[kind]
    pin = os.environ.get(env_name, "").strip()
    if not _LOWER_SHA256.fullmatch(pin):
        return None, [f"{env_name} must be set to the exact lowercase producer-bundle SHA-256"]
    execution = payload.get("execution_identity") if isinstance(payload.get("execution_identity"), dict) else {}
    files = execution.get("producer_files") if isinstance(execution.get("producer_files"), list) else []
    paths = [str(item.get("path") or "") for item in files if isinstance(item, dict)]
    errors: list[str] = []
    if len(paths) != len(set(paths)) or set(paths) != PRODUCER_FILES[kind]:
        errors.append(f"report producer inventory must contain exactly {sorted(PRODUCER_FILES[kind])}")
    for item in files:
        if not isinstance(item, dict):
            errors.append("report producer inventory contains a non-object entry")
            continue
        path = Path(str(item.get("path") or ""))
        if path.is_absolute() or ".." in path.parts:
            errors.append("report producer inventory contains a non-portable path")
        if not _LOWER_SHA256.fullmatch(str(item.get("sha256") or "")):
            errors.append(f"report producer file {path} has no lowercase SHA-256")
    if errors:
        return pin, errors
    digest = producer_bundle_sha256(files)
    if execution.get("producer_sha256") != digest:
        errors.append("report producer_sha256 does not match its producer-file inventory")
    if digest != pin:
        errors.append(f"report producer bundle does not match {env_name}")
    return pin, errors


def _binding_errors(kind: str, payload: dict[str, Any], bindings: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in ("manifest_path", "usd_path", "rendered_files"):
        if payload.get(field) != bindings[field]:
            errors.append(f"report {field} does not match the current project")
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    if runtime.get("source_report_sha256") != bindings["runtime_source_report_sha256"]:
        errors.append("report runtime source hash does not match the validated Isaac report")
    if kind == "probe":
        probes = {
            str(item.get("probe") or ""): item.get("metrics") or {}
            for item in payload.get("probes") or []
            if isinstance(item, dict)
        }
        expected = bindings["probe_parameters"]
        comparisons = (
            ("smoke", "steps", "smoke_steps"),
            ("repeat", "steps", "repeat_steps"),
            ("gaming", "steps", "gaming_steps"),
            ("reset", "seeds", "reset_seeds"),
            ("reset", "penetration_tolerance_m", "penetration_tolerance_m"),
            ("oracle", "phase_steps", "oracle_phase_steps"),
            ("gaming", "allowed_fraction_of_oracle", "allowed_gaming_fraction"),
        )
        for probe, report_field, expected_field in comparisons:
            if probes.get(probe, {}).get(report_field) != expected.get(expected_field):
                errors.append(f"{probe} probe {report_field} differs from the task-fitness protocol")
        settle = bindings["settle_parameters"]
        if probes.get("reset", {}).get("settle_steps") != settle.get("settle_steps"):
            errors.append("reset probe settle_steps differs from the validated runtime report")
        if probes.get("reset", {}).get("settled_speed_metres_per_second") != settle.get(
            "settled_speed_metres_per_second"
        ):
            errors.append("reset probe settled-speed limit differs from the validated runtime report")
        if probes.get("repeat", {}).get("tolerance_m") != settle.get("repeatability_tolerance_metres"):
            errors.append("repeat probe tolerance differs from the validated runtime report")
        oracle = probes.get("oracle", {})
        oracle_returns = oracle.get("oracle_returns") if isinstance(oracle.get("oracle_returns"), list) else []
        if len(oracle_returns) != bindings["num_envs"]:
            errors.append("oracle return baseline does not cover every contracted environment")
        per_grasp = oracle.get("per_grasp") if isinstance(oracle.get("per_grasp"), list) else []
        actual_grasp_ids = [str(item.get("grasp_id") or "") for item in per_grasp if isinstance(item, dict)]
        if (
            len(actual_grasp_ids) != len(per_grasp)
            or len(actual_grasp_ids) != len(set(actual_grasp_ids))
            or set(actual_grasp_ids) != set(bindings["grasp_ids"])
        ):
            errors.append("oracle per-grasp results differ from the accepted grasp contract")
        for grasp in per_grasp:
            if not isinstance(grasp, dict):
                continue
            returns = grasp.get("returns") if isinstance(grasp.get("returns"), list) else []
            terminal_steps = grasp.get("terminal_steps") if isinstance(grasp.get("terminal_steps"), list) else []
            if len(returns) != bindings["num_envs"]:
                errors.append(
                    f"oracle grasp {grasp.get('grasp_id') or '<unnamed>'} returns do not cover every environment"
                )
            if len(terminal_steps) != bindings["num_envs"]:
                errors.append(
                    f"oracle grasp {grasp.get('grasp_id') or '<unnamed>'} terminal steps do not cover every environment"
                )
        gaming = probes.get("gaming", {})
        patterns = gaming.get("patterns") if isinstance(gaming.get("patterns"), dict) else {}
        for pattern, result in patterns.items():
            if not isinstance(result, dict):
                continue
            for field in ("terminal_steps", "terminal_success"):
                values = result.get(field)
                if not isinstance(values, list) or len(values) != bindings["num_envs"]:
                    errors.append(f"gaming pattern {pattern} {field} does not cover every environment")
    else:
        expected = bindings["fidelity_parameters"]
        for report_field, expected_field in (
            ("tolerance_m", "tolerance_m"),
            ("samples_per_region", "samples_per_region"),
            ("seed", "seed"),
        ):
            if payload.get(report_field) != expected.get(expected_field):
                errors.append(f"collision-fidelity {report_field} differs from the task-fitness protocol")
        radius = expected.get("region_radius_m")
        regions = payload.get("regions") or []
        actual_region_ids = [str(item.get("id") or "") for item in regions if isinstance(item, dict)]
        if len(actual_region_ids) != len(regions) or len(actual_region_ids) != len(set(actual_region_ids)):
            errors.append("collision-fidelity regions must have unique identities")
        actual_regions = {str(item.get("id") or ""): item for item in regions if isinstance(item, dict)}
        expected_regions = {item["id"]: item for item in bindings["grasp_regions"]}
        if set(actual_regions) != set(expected_regions):
            errors.append("collision-fidelity regions differ from the accepted grasp frames")
        for region_id, expected_region in expected_regions.items():
            actual = actual_regions.get(region_id, {})
            if actual.get("centre") != expected_region["centre"] or actual.get("radius_m") != radius:
                errors.append(f"collision-fidelity region {region_id} differs from the task-fitness protocol")
    return errors


def _redesign(project_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compute the refreshed design and manifest without committing the manifest."""

    manifest_path = project_dir / "manifests" / "rl-environment-manifest.json"
    stage_report_path = project_dir / "reports" / "simready-verification-report.json"
    validation_path = project_dir / "reports" / "generated-asset-validation-report.json"
    required = (
        project_dir / "run-request.json",
        project_dir / "run-plan.json",
        manifest_path,
        stage_report_path,
        validation_path,
    )
    missing = [path.relative_to(project_dir).as_posix() for path in required if not path.is_file()]
    if missing:
        raise ValueError(f"cannot refresh RL environment manifest; missing {missing}")
    request = RunRequest.model_validate_json((project_dir / "run-request.json").read_text(encoding="utf-8"))
    plan = RunPlan.model_validate_json((project_dir / "run-plan.json").read_text(encoding="utf-8"))
    stage_report = json.loads(stage_report_path.read_text(encoding="utf-8"))
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["report_sha256"] = sha256_file(validation_path)
    asset_package = stage_report.get("generated_asset") or {}
    if not asset_package:
        raise ValueError("cannot refresh RL environment manifest; SimReady report has no generated asset")
    design = design_environment(project_dir, request, plan, asset_package, validation)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    merge_design_into_manifest(manifest, design)
    schema_errors = validate_payload("rl-environment-manifest", manifest)
    if schema_errors:
        raise ValueError(
            "refreshed RL environment manifest schema: " + "; ".join(issue.render() for issue in schema_errors)
        )
    summary = {
        "status": design["status"],
        "gates": design["gates"],
        "blocked_reasons": design["blocked_reasons"],
    }
    return summary, manifest


def apply_rl_evidence_report(project: str | Path, report: str | Path, kind: str) -> dict[str, Any]:
    if kind not in REPORT_ID:
        raise ValueError("kind must be probe or fidelity")
    project_dir = Path(project).resolve(strict=True)
    if not project_dir.is_dir():
        raise ValueError("project must be a directory")
    with workspace_lease(project_dir, f"rl-evidence-{kind}-import"):
        raw, payload = _read_report(report)
        canonical = _inside(project_dir, project_dir / CANONICAL_PATH[kind], "canonical report target")
        receipt_path = _inside(project_dir, project_dir / IMPORT_RECEIPT_PATH[kind], "import receipt target")
        manifest_path = _project_file(
            project_dir,
            "manifests/rl-environment-manifest.json",
            "RL environment manifest",
        )
        auxiliary_paths = [
            _inside(project_dir, project_dir / relative, f"transaction target {relative}")
            for relative in _TRANSACTION_AUXILIARY_PATHS
        ]
        checksums_path = project_dir / "evidence" / "checksums.json"
        bindings = _project_bindings(project_dir)
        schema_name = "rl-probe-evidence" if kind == "probe" else "rl-collision-fidelity"
        errors = [f"{schema_name} schema: {issue.render()}" for issue in validate_payload(schema_name, payload)]
        producer_pin, producer_errors = _producer_pin(kind, payload)
        errors.extend(producer_errors)
        errors.extend(_binding_errors(kind, payload, bindings))
        errors.extend(
            verify_report(
                payload,
                REPORT_ID[kind],
                expected_backend=bindings["physics_backend"],
                expected_usd_sha256=bindings["usd_sha256"],
                expected_request_digest=bindings["request_digest"],
                expected_manifest_sha256=bindings["manifest_sha256"],
                expected_contract_sha256=bindings["contract_sha256"],
                expected_package_fingerprint=bindings["package_dependency_fingerprint"],
                expected_sim_dt=bindings["sim_dt"],
                expected_decimation=bindings["decimation"],
                expected_rendered_files=bindings["rendered_files"],
            )
        )
        if errors:
            raise ValueError("; ".join(dict.fromkeys(errors)))
        if producer_pin is None:
            raise RuntimeError("validated producer policy has no approved SHA-256")

        import_secret = import_attestation_secret()
        report_sha256 = hashlib.sha256(raw).hexdigest()
        receipt = {
            "receipt_identity": {"id": "asset-factory.rl-evidence-import", "version": "1.1"},
            "kind": kind,
            "report_path": CANONICAL_PATH[kind],
            "report_sha256": report_sha256,
            "producer_sha256": (payload.get("execution_identity") or {}).get("producer_sha256"),
            "producer_policy": {
                "environment": PRODUCER_PIN_ENV[kind],
                "approved_sha256": producer_pin,
            },
            "importer_identity": {
                "id": "asset-factory.rl-evidence-importer",
                "version": "1.1",
                "policy_version": "1.1",
            },
            "imported_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        signed_receipt = {**receipt, "attestation": attest_import_receipt(receipt, import_secret)}
        snapshots = _snapshot_files([manifest_path, canonical, receipt_path, *auxiliary_paths])

        try:
            manifest = json.loads(snapshots[manifest_path].decode("utf-8"))
            pending_manifest = _pending_manifest(manifest, kind)
            pending_errors = validate_payload("rl-environment-manifest", pending_manifest)
            if pending_errors:
                raise ValueError(
                    "pending RL environment manifest schema: " + "; ".join(issue.render() for issue in pending_errors)
                )
            atomic_write_json(manifest_path, pending_manifest)
            _atomic_write_bytes(canonical, raw)
            atomic_write_json(receipt_path, signed_receipt)

            redesign, final_manifest = _redesign(project_dir)
            current_bindings = _project_bindings(project_dir)
            if current_bindings != bindings:
                raise RuntimeError("project bindings changed during RL evidence import")
            if sha256_file(canonical) != report_sha256:
                raise RuntimeError("canonical RL evidence checksum changed during import")
            receipt_errors = verify_import_receipt(
                project_dir,
                CANONICAL_PATH[kind],
                payload,
                secret=import_secret,
            )
            if receipt_errors:
                raise RuntimeError("import receipt revalidation failed: " + "; ".join(receipt_errors))

            result = {
                "kind": kind,
                "report_path": CANONICAL_PATH[kind],
                "report_sha256": report_sha256,
                "report_status": payload.get("status"),
                "manifest_refreshed": True,
                "manifest_status": redesign["status"],
                "gates": redesign["gates"],
                "blocked_reasons": redesign["blocked_reasons"],
            }
            final_manifest_bytes = _json_bytes(final_manifest)
            if checksums_path.parent.exists():
                checksum_inventory = build_project_checksum_inventory(project_dir)
                manifest_record = next(
                    (
                        item
                        for item in checksum_inventory["files"]
                        if item["path"] == "manifests/rl-environment-manifest.json"
                    ),
                    None,
                )
                if manifest_record is None:
                    raise RuntimeError("project checksum inventory omitted the RL environment manifest")
                manifest_record["sha256"] = hashlib.sha256(final_manifest_bytes).hexdigest()
                atomic_write_json(checksums_path, checksum_inventory)
            _atomic_write_bytes(manifest_path, final_manifest_bytes)
        except BaseException as exc:
            try:
                _rollback_files(snapshots, manifest_path)
            except (OSError, RuntimeError, ValueError) as rollback_exc:
                raise RuntimeError(
                    f"RL evidence import failed ({type(exc).__name__}: {exc}) and rollback was incomplete"
                ) from rollback_exc
            raise
        return result
