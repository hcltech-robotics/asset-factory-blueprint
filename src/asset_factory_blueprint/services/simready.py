from __future__ import annotations

import hashlib
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from asset_factory_blueprint.isaac_evidence import (
    attestation_secret as isaac_attestation_secret,
    parse_runtime_report_bytes,
    producer_sha256_pin,
    verify_runtime_report_envelope,
)
from asset_factory_blueprint.manifests import validate_payload
from asset_factory_blueprint.physics_evidence import (
    PHYSICS_EVIDENCE_SECRET_ENV,
    verify_physics_evidence_attestation,
)
from asset_factory_blueprint.services.official_validator import (
    VALIDATOR_DOCUMENTATION_URI,
    VALIDATOR_ID,
    normalise_official_profile_report,
    verify_official_profile_report_attestation,
)
from asset_factory_blueprint.utils.checksums import sha256_file
from asset_factory_blueprint.utils.package_fingerprint import package_inventory_fingerprint


USD_REFERENCE = re.compile(r"@([^@]+)@")
SIMREADY_SPECIFICATION_URI = "https://docs.omniverse.nvidia.com/simready/latest/simready-faq.html"
SEMANTIC_LABEL_REQUIREMENT_URI = (
    "https://docs.omniverse.nvidia.com/kit/docs/asset-requirements/latest/"
    "capabilities/semantic_labels/requirements/semantic-label-schema.html"
)
KNOWN_ROBOTICS_PROFILES = {
    "Prop-Robotics-Neutral",
    "Prop-Robotics-PhysX",
    "Prop-Robotics-Isaac",
    "Robot-Body-Neutral",
    "Robot-Body-Runnable",
    "Robot-Body-Isaac",
}
LOCAL_REQUIREMENTS = {
    "UN.006": {"name": "Z-up stage axis", "version": "unresolved"},
    "VG.027": {"name": "Mesh normals", "version": "unresolved"},
    "RB.COL.001": {"name": "Rigid-body collision schemas", "version": "unresolved"},
    "SL.003": {"name": "SemanticsLabelsAPI schema", "version": "1.0.0"},
}
FEATURE_REQUIREMENTS = {
    "Minimal Placeable Visual": ["UN.006", "VG.027"],
    "Rigid Body Physics": ["RB.COL.001"],
    "Semantic Labels": ["SL.003"],
}
SHA256_PATTERN = re.compile(r"^[A-Fa-f0-9]{64}$")
PREFIXED_SHA256_PATTERN = re.compile(r"^sha256:[A-Fa-f0-9]{64}$")
SEMANTIC_VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
PROCESS_EVIDENCE_FIELDS = {
    "exit_code",
    "timed_out",
    "output_limit_exceeded",
    "report_limit_exceeded",
    "observed_output_bytes",
    "captured_stdout_sha256",
    "captured_stderr_sha256",
    "stdout_excerpt",
    "stderr_excerpt",
    "launch_error",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _result(
    gate_id: str,
    gate_type: str,
    status: str,
    evidence_path: str,
    repair_action: str = "",
    **details: Any,
) -> dict[str, Any]:
    result = {
        "gate_id": gate_id,
        "gate_type": gate_type,
        "status": status,
        "evidence_path": evidence_path,
        "repair_action": repair_action,
        "rerun_required": status == "blocked",
    }
    result.update(details)
    return result


def _references_resolve(path: Path) -> list[str]:
    text = _read(path)
    missing = []
    for ref in USD_REFERENCE.findall(text):
        if ref.startswith(("http://", "https://", "omniverse://")):
            missing.append(ref)
            continue
        if ref.startswith("#"):
            continue
        target = (path.parent / ref).resolve()
        if not target.exists():
            missing.append(ref)
    return missing


def _package_dependency_closure(package_path: Path, package_root: Path) -> dict[str, Any]:
    """Resolve the complete USD and MaterialX dependency closure for a package."""

    resolved_root = package_root.resolve(strict=False)
    dependencies: set[Path] = set()
    unresolved: set[str] = set()
    external: set[str] = set()
    try:
        from pxr import UsdUtils

        layers, assets, unresolved_paths = UsdUtils.ComputeAllDependencies(str(package_path))
        for layer in layers:
            raw_layer_path = str(layer.realPath or layer.identifier or "")
            if raw_layer_path:
                dependencies.add(Path(raw_layer_path).resolve(strict=False))
        for asset in assets:
            raw = str(getattr(asset, "path", asset) or "")
            if not raw:
                continue
            if raw.startswith(("http://", "https://", "omniverse://", "s3://")):
                external.add(raw)
                continue
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = package_path.parent / candidate
            dependencies.add(candidate.resolve(strict=False))
        unresolved.update(str(item) for item in unresolved_paths if str(item))
    except Exception:
        pending = [package_path]
        visited: set[Path] = set()
        while pending:
            current = pending.pop()
            current = current.resolve(strict=False)
            if current in visited:
                continue
            visited.add(current)
            dependencies.add(current)
            if not current.is_file() or current.suffix.lower() not in {".usd", ".usda", ".usdc"}:
                continue
            for reference in USD_REFERENCE.findall(_read(current)):
                if reference.startswith(("http://", "https://", "omniverse://", "s3://")):
                    external.add(reference)
                    continue
                candidate = (current.parent / reference).resolve(strict=False)
                pending.append(candidate)

    materialx_files = [path for path in dependencies if path.suffix.lower() == ".mtlx" and path.is_file()]
    for materialx_path in materialx_files:
        try:
            tree = ET.parse(materialx_path)
        except (ET.ParseError, OSError) as exc:
            unresolved.add(f"{materialx_path.name}: {exc}")
            continue
        for element in tree.iter():
            if element.tag.rsplit("}", 1)[-1] != "input" or element.attrib.get("type") != "filename":
                continue
            value = str(element.attrib.get("value") or "")
            if not value:
                continue
            if value.startswith(("http://", "https://", "omniverse://", "s3://")):
                external.add(value)
                continue
            dependencies.add((materialx_path.parent / value).resolve(strict=False))

    missing: list[str] = []
    escaping: list[str] = []
    records: list[dict[str, str]] = []
    for dependency in sorted(dependencies, key=lambda item: item.as_posix()):
        if dependency != resolved_root and resolved_root not in dependency.parents:
            escaping.append(dependency.as_posix())
            continue
        relative = dependency.relative_to(resolved_root).as_posix()
        if not dependency.is_file():
            missing.append(relative)
            continue
        records.append({"path": relative, "sha256": sha256_file(dependency)})
    package_binding = package_inventory_fingerprint(resolved_root)
    blockers = [
        *[f"unresolved dependency: {item}" for item in sorted(unresolved)],
        *[f"external dependency is not localised: {item}" for item in sorted(external)],
        *[f"package dependency is missing: {item}" for item in sorted(missing)],
        *[f"package dependency escapes the package root: {item}" for item in sorted(escaping)],
        *[f"package inventory: {item}" for item in package_binding["blocked_reasons"]],
    ]
    return {
        "status": "pass" if not blockers else "blocked",
        "root": package_path.relative_to(resolved_root).as_posix(),
        "files": records,
        "package_dependency_fingerprint": package_binding["fingerprint"],
        "package_inventory": package_binding["files"],
        "unresolved": sorted(unresolved),
        "external": sorted(external),
        "missing": sorted(missing),
        "escaping": sorted(escaping),
        "blocked_reasons": blockers,
    }


def _openusd_compliance_record(path: Path, evidence_path: str) -> dict[str, Any]:
    base = {
        "checker_id": "openusd-compliance-checker",
        "checker_version": "unavailable",
        "status": "blocked",
        "asset_path": evidence_path,
        "asset_sha256": sha256_file(path) if path.is_file() else "",
        "errors": [],
        "failed_checks": [],
        "warnings": [],
        "reason": "",
    }
    if not path.is_file():
        base["reason"] = "composed USD root does not exist"
        return base
    try:
        from pxr import Usd, UsdValidation
    except Exception as exc:
        base["reason"] = f"OpenUSD validation framework is unavailable: {exc}"
        return base
    base["checker_version"] = ".".join(str(item) for item in Usd.GetVersion())
    try:
        stage = Usd.Stage.Open(str(path))
        if stage is None:
            base["reason"] = "composed USD root could not be opened"
            return base
        registry = UsdValidation.ValidationRegistry()
        validators = registry.GetOrLoadAllValidators()
        if not validators:
            base["reason"] = "OpenUSD validation registry did not provide any validators"
            return base
        findings = UsdValidation.ValidationContext(validators).Validate(stage)
        replacements = {
            str(path.resolve(strict=False)): evidence_path,
            path.resolve(strict=False).as_posix(): evidence_path,
        }

        def sanitise(value: Any) -> str:
            output = str(value)
            for original, replacement in replacements.items():
                output = output.replace(original, replacement)
            return output

        finding_records = [
            {
                "identifier": str(finding.GetIdentifier()),
                "name": str(finding.GetName()),
                "severity": str(finding.GetType().displayName),
                "message": sanitise(finding.GetMessage()),
            }
            for finding in findings
            if not finding.HasNoError()
        ]
        base["framework"] = "UsdValidation"
        base["validator_count"] = len(validators)
        base["findings"] = finding_records
        base["errors"] = [
            sanitise(finding.GetErrorAsString())
            for finding in findings
            if finding.GetType() == UsdValidation.ValidationErrorType.Error
        ]
        base["failed_checks"] = sorted(
            {
                str(finding.GetIdentifier())
                for finding in findings
                if finding.GetType() == UsdValidation.ValidationErrorType.Error
            }
        )
        base["warnings"] = [
            sanitise(finding.GetErrorAsString())
            for finding in findings
            if finding.GetType() == UsdValidation.ValidationErrorType.Warn
        ]
    except Exception as exc:
        base["reason"] = f"OpenUSD validation framework failed: {exc}"
        return base
    if base["errors"] or base["failed_checks"]:
        base["reason"] = "OpenUSD composition or schema compliance checks failed"
    else:
        base["status"] = "pass"
    return base


def _articulation_schema_record(path: Path, evidence_path: str, *, required: bool) -> dict[str, Any]:
    record: dict[str, Any] = {
        "status": "skipped" if not required else "blocked",
        "required": required,
        "evidence_path": evidence_path,
        "articulation_root_paths": [],
        "joint_results": [],
        "reason": "asset is not declared articulated" if not required else "",
    }
    if not required:
        return record
    try:
        from pxr import Usd, UsdPhysics
    except Exception as exc:
        record["reason"] = f"OpenUSD physics schemas are unavailable: {exc}"
        return record
    stage = Usd.Stage.Open(str(path))
    if stage is None:
        record["reason"] = "composed USD root could not be opened"
        return record
    roots = [prim for prim in stage.Traverse() if prim.HasAPI(UsdPhysics.ArticulationRootAPI)]
    joints = [prim for prim in stage.Traverse() if prim.IsA(UsdPhysics.Joint)]
    record["articulation_root_paths"] = [str(prim.GetPath()) for prim in roots]
    default_prim = stage.GetDefaultPrim()
    asset_root_path = str(default_prim.GetPath()) if default_prim and default_prim.IsDefined() else ""
    problems: list[str] = []
    if not roots or any(not prim.IsDefined() or not prim.IsActive() for prim in roots):
        problems.append("ArticulationRootAPI is not applied")
    if not joints:
        problems.append("no real UsdPhysics joint prims are authored")
    for prim in joints:
        joint = UsdPhysics.Joint(prim)
        body0 = [str(item) for item in joint.GetBody0Rel().GetTargets()]
        body1 = [str(item) for item in joint.GetBody1Rel().GetTargets()]
        joint_problems: list[str] = []
        body_prims: dict[str, Any] = {}
        for label, targets in (("body0", body0), ("body1", body1)):
            if len(targets) != 1:
                joint_problems.append(f"{label} relationship must target exactly one defined prim")
                continue
            target_path = targets[0]
            target = stage.GetPrimAtPath(target_path)
            body_prims[label] = target
            if not target or not target.IsValid() or not target.IsDefined() or not target.IsActive():
                joint_problems.append(f"{label} relationship must target one defined active prim")
                continue
            if asset_root_path and target_path != asset_root_path and not target_path.startswith(asset_root_path + "/"):
                joint_problems.append(f"{label} target must remain beneath the asset root")
            if not target.HasAPI(UsdPhysics.RigidBodyAPI):
                joint_problems.append(f"{label} target must have RigidBodyAPI")
            elif UsdPhysics.RigidBodyAPI(target).GetRigidBodyEnabledAttr().Get() is not True:
                joint_problems.append(f"{label} target rigid body must be enabled")
            has_collider = any(
                descendant.HasAPI(UsdPhysics.CollisionAPI)
                and UsdPhysics.CollisionAPI(descendant).GetCollisionEnabledAttr().Get() is True
                for descendant in Usd.PrimRange(target)
            )
            if not has_collider:
                joint_problems.append(f"{label} target must contain an enabled collision schema")
        if len(body0) == 1 and len(body1) == 1 and body0[0] == body1[0]:
            joint_problems.append("body0 and body1 targets must be distinct")
        lower: float | None = None
        upper: float | None = None
        axis = ""
        if prim.IsA(UsdPhysics.RevoluteJoint):
            typed_joint = UsdPhysics.RevoluteJoint(prim)
            raw_lower = typed_joint.GetLowerLimitAttr().Get()
            raw_upper = typed_joint.GetUpperLimitAttr().Get()
            if raw_lower is None or raw_upper is None:
                joint_problems.append("joint limits are not authored")
            else:
                lower = float(raw_lower)
                upper = float(raw_upper)
            axis = str(typed_joint.GetAxisAttr().Get() or "")
        elif prim.IsA(UsdPhysics.PrismaticJoint):
            typed_joint = UsdPhysics.PrismaticJoint(prim)
            raw_lower = typed_joint.GetLowerLimitAttr().Get()
            raw_upper = typed_joint.GetUpperLimitAttr().Get()
            if raw_lower is None or raw_upper is None:
                joint_problems.append("joint limits are not authored")
            else:
                lower = float(raw_lower)
                upper = float(raw_upper)
            axis = str(typed_joint.GetAxisAttr().Get() or "")
        if lower is not None and upper is not None and (not math.isfinite(lower) or not math.isfinite(upper) or lower > upper):
            joint_problems.append("joint limits must be finite and ordered")
        if axis and axis not in {"X", "Y", "Z"}:
            joint_problems.append("joint axis is invalid")
        frame_unit = str(prim.GetAttribute("assetFactory:frameUnit").Get() or "")
        if frame_unit != "m":
            joint_problems.append("joint frame unit must be m")
        frame_values: dict[str, list[float]] = {}
        for frame_name, attribute in (
            ("local_pos0", joint.GetLocalPos0Attr()),
            ("local_pos1", joint.GetLocalPos1Attr()),
        ):
            raw_value = attribute.Get()
            try:
                values = [float(item) for item in raw_value]
            except (TypeError, ValueError):
                values = []
            frame_values[frame_name] = values
            if not attribute.HasAuthoredValueOpinion() or len(values) != 3 or not all(math.isfinite(item) for item in values):
                joint_problems.append(f"{frame_name} must be an authored finite three-component position")
        for frame_name, attribute in (
            ("local_rot0", joint.GetLocalRot0Attr()),
            ("local_rot1", joint.GetLocalRot1Attr()),
        ):
            raw_value = attribute.Get()
            try:
                imaginary = raw_value.GetImaginary()
                values = [float(raw_value.GetReal()), *(float(item) for item in imaginary)]
            except (AttributeError, TypeError, ValueError):
                values = []
            frame_values[frame_name] = values
            if (
                not attribute.HasAuthoredValueOpinion()
                or len(values) != 4
                or not all(math.isfinite(item) for item in values)
                or math.sqrt(sum(item * item for item in values)) <= 0.0
            ):
                joint_problems.append(f"{frame_name} must be an authored non-zero finite quaternion")
        source_evidence_ids = prim.GetAttribute("assetFactory:sourceEvidenceIds").Get()
        if not source_evidence_ids or not all(str(item) for item in source_evidence_ids):
            joint_problems.append("joint source evidence IDs are missing")

        drive_instances = sorted(
            str(schema).split(":", 1)[1]
            for schema in prim.GetAppliedSchemas()
            if str(schema).startswith("PhysicsDriveAPI:") and ":" in str(schema)
        )
        drive_results: list[dict[str, Any]] = []
        expected_drive_instance = "angular" if prim.IsA(UsdPhysics.RevoluteJoint) else (
            "linear" if prim.IsA(UsdPhysics.PrismaticJoint) else ""
        )
        if not expected_drive_instance and drive_instances:
            joint_problems.append("fixed joints must not carry a DriveAPI")
        if expected_drive_instance and drive_instances and drive_instances != [expected_drive_instance]:
            joint_problems.append(f"joint DriveAPI must use only the {expected_drive_instance} instance")
        for drive_instance in drive_instances:
            drive = UsdPhysics.DriveAPI(prim, drive_instance)
            drive_type = str(drive.GetTypeAttr().Get() or "")
            drive_problems: list[str] = []
            if drive_type not in {"force", "acceleration"}:
                drive_problems.append("drive type must be force or acceleration")
            parameters: dict[str, float | None] = {}
            for field, attribute in (
                ("stiffness", drive.GetStiffnessAttr()),
                ("damping", drive.GetDampingAttr()),
                ("max_force", drive.GetMaxForceAttr()),
            ):
                value = attribute.Get()
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    number = math.nan
                parameters[field] = number if math.isfinite(number) else None
                if not attribute.HasAuthoredValueOpinion() or not math.isfinite(number) or number < 0.0:
                    drive_problems.append(f"drive {field} must be authored, finite and non-negative")
            target_position_attr = drive.GetTargetPositionAttr()
            target_position = target_position_attr.Get()
            if target_position_attr.HasAuthoredValueOpinion():
                try:
                    target_position_value = float(target_position)
                except (TypeError, ValueError):
                    target_position_value = math.nan
                if not math.isfinite(target_position_value):
                    drive_problems.append("drive target position must be finite")
                elif lower is not None and upper is not None and not lower <= target_position_value <= upper:
                    drive_problems.append("drive target position must lie within joint limits")
                parameters["target_position"] = target_position_value
            target_velocity_attr = drive.GetTargetVelocityAttr()
            target_velocity = target_velocity_attr.Get()
            if target_velocity_attr.HasAuthoredValueOpinion():
                try:
                    target_velocity_value = float(target_velocity)
                except (TypeError, ValueError):
                    target_velocity_value = math.nan
                if not math.isfinite(target_velocity_value):
                    drive_problems.append("drive target velocity must be finite")
                parameters["target_velocity"] = target_velocity_value
            if drive_problems:
                joint_problems.extend(f"{drive_instance} drive: {problem}" for problem in drive_problems)
            drive_results.append(
                {
                    "instance": drive_instance,
                    "type": drive_type,
                    "parameters": parameters,
                    "status": "blocked" if drive_problems else "pass",
                    "problems": drive_problems,
                }
            )
        if joint_problems:
            problems.extend(f"{prim.GetPath()}: {problem}" for problem in joint_problems)
        record["joint_results"].append(
            {
                "prim_path": str(prim.GetPath()),
                "type_name": prim.GetTypeName(),
                "body0": body0,
                "body1": body1,
                "axis": axis,
                "lower_limit": lower,
                "upper_limit": upper,
                "frame_unit": frame_unit,
                "frames": frame_values,
                "source_evidence_ids": [str(item) for item in source_evidence_ids or []],
                "drives": drive_results,
                "status": "blocked" if joint_problems else "pass",
                "problems": joint_problems,
            }
        )
    record["status"] = "pass" if not problems else "blocked"
    record["reason"] = "; ".join(problems)
    return record


def _usd_opens(path: Path) -> tuple[bool, str]:
    try:
        from pxr import Usd
    except Exception:
        return False, "OpenUSD Python runtime unavailable"
    stage = Usd.Stage.Open(str(path))
    if stage is None:
        return False, "USD stage did not open"
    if not stage.GetDefaultPrim():
        return False, "USD stage has no default prim"
    return True, ""


def _material_surface_context_is_bound(path: Path, render_context: str) -> bool:
    try:
        from pxr import Usd, UsdShade
    except Exception:
        return False
    stage = Usd.Stage.Open(str(path))
    if stage is None:
        return False
    for prim in stage.Traverse():
        if not prim.IsA(UsdShade.Material):
            continue
        output = UsdShade.Material(prim).GetSurfaceOutput(render_context)
        if output and output.HasConnectedSource():
            return True
    return False


def _contains(path: Path, snippets: list[str]) -> bool:
    text = _read(path)
    return all(snippet in text for snippet in snippets)


def _requirement_result(
    requirement_id: str,
    status: str,
    evidence_path: str,
    message: str,
    validator: str = "local_openusd_schema_inspection",
) -> dict[str, Any]:
    definition = LOCAL_REQUIREMENTS[requirement_id]
    return {
        "requirement_id": requirement_id,
        "requirement_name": definition["name"],
        "requirement_version": definition["version"],
        "status": status,
        "validator": validator,
        "official_validator": False,
        "evidence_path": evidence_path,
        "message": message,
    }


def _blocked_requirement_results(evidence_path: str, reason: str) -> list[dict[str, Any]]:
    return [_requirement_result(requirement_id, "blocked", evidence_path, reason) for requirement_id in LOCAL_REQUIREMENTS]


def _physics_evidence_binding_problems(
    stage: Any,
    rigid_bodies: list[Any],
    package_root: Path | None,
    usd_physics: Any,
) -> list[str]:
    if package_root is None or not package_root.is_dir():
        return ["packaged physics evidence directory is unavailable"]
    resolved_package_root = package_root.resolve(strict=True)
    binding_path = resolved_package_root / "evidence" / "physics-evidence-binding.json"
    binding, binding_error = _load_json_report(binding_path)
    if binding is None:
        return ["packaged physics evidence binding is unavailable: " + binding_error]
    problems = []
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
        problems.append("packaged physics evidence binding has an unexpected shape")
    if len(rigid_bodies) != 1:
        problems.append(
            "physics evidence binding format 1.0.0 supports exactly one rigid body; author one binding per body before promotion"
        )
    if binding.get("schema_version") != "1.0.0" or binding.get("status") != "accepted":
        problems.append("packaged physics evidence binding is not an accepted 1.0.0 record")
    binding_fingerprint = str(binding.get("evidence_fingerprint") or "")
    if not PREFIXED_SHA256_PATTERN.fullmatch(binding_fingerprint):
        problems.append("packaged physics evidence fingerprint is missing or invalid")
    attested_evidence = binding.get("attested_evidence")
    if not isinstance(attested_evidence, dict):
        attested_evidence = {}
        problems.append("packaged physics evidence has no signed source record")
    else:
        problems.extend(
            verify_physics_evidence_attestation(
                attested_evidence,
                os.environ.get(PHYSICS_EVIDENCE_SECRET_ENV, ""),
            )
        )
        if attested_evidence.get("evidence_fingerprint") != binding_fingerprint:
            problems.append("signed physics evidence fingerprint differs from the package binding")
    prim_path = str(binding.get("prim_path") or "")
    rigid_body_paths = {str(prim.GetPath()) for prim in rigid_bodies}
    if prim_path not in rigid_body_paths:
        problems.append("packaged physics evidence binding does not target a rigid body")
    for prim in rigid_bodies:
        usd_fingerprint = str(prim.GetAttribute("assetFactory:physicsEvidenceFingerprint").Get() or "")
        if usd_fingerprint != binding_fingerprint:
            problems.append(f"USD physics evidence fingerprint does not match the binding: {prim.GetPath()}")

    evidence = binding.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        evidence = []
        problems.append("packaged physics evidence binding contains no materialised evidence")
    evidence_ids: set[str] = set()
    reduced_evidence = []
    for index, record in enumerate(evidence):
        if not isinstance(record, dict):
            problems.append(f"packaged physics evidence record {index} is malformed")
            continue
        evidence_id = str(record.get("evidence_id") or "")
        relative_path = Path(str(record.get("path") or ""))
        expected_sha256 = str(record.get("sha256") or "")
        if not evidence_id or evidence_id in evidence_ids:
            problems.append(f"packaged physics evidence record {index} has a missing or duplicate ID")
            continue
        evidence_ids.add(evidence_id)
        reduced_evidence.append({"evidence_id": evidence_id, "sha256": expected_sha256})
        if relative_path.is_absolute() or any(part == ".." for part in relative_path.parts):
            problems.append(f"packaged physics evidence path is not package-relative: {evidence_id}")
            continue
        candidate = resolved_package_root / relative_path
        current = resolved_package_root
        symbolic_link_found = False
        for part in relative_path.parts:
            current = current / part
            if current.is_symlink():
                symbolic_link_found = True
                break
        if symbolic_link_found:
            problems.append(f"packaged physics evidence path traverses a symbolic link: {evidence_id}")
            continue
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(resolved_package_root)
        except (OSError, ValueError):
            problems.append(f"packaged physics evidence file is missing or escapes the package: {evidence_id}")
            continue
        if not resolved.is_file() or not SHA256_PATTERN.fullmatch(expected_sha256):
            problems.append(f"packaged physics evidence file or digest is invalid: {evidence_id}")
        elif sha256_file(resolved) != expected_sha256:
            problems.append(f"packaged physics evidence digest does not match the materialised file: {evidence_id}")

    source_evidence_ids = [str(item) for item in binding.get("source_evidence_ids", []) if str(item)]
    if not source_evidence_ids or not set(source_evidence_ids).issubset(evidence_ids):
        problems.append("packaged physics source evidence IDs do not resolve")
    approval = binding.get("approval")
    if not isinstance(approval, dict) or approval.get("status") != "accepted" or any(
        not str(approval.get(field) or "") for field in ("decision_id", "reviewer", "decided_at")
    ):
        problems.append("packaged physics evidence approval is missing or not accepted")
    unit_policy = binding.get("unit_policy")
    if unit_policy != "si_m_kg_s" and unit_policy != {"mass": "kg", "length": "m", "inertia": "kg*m^2"}:
        problems.append("packaged physics evidence does not declare SI mass-property units")
    uncertainty = binding.get("uncertainty")
    if not isinstance(uncertainty, dict):
        problems.append("packaged physics evidence uncertainty is missing")
    else:
        mass_uncertainty = uncertainty.get("mass")
        inertia_uncertainty = uncertainty.get("diagonal_inertia")
        if (
            not isinstance(mass_uncertainty, (int, float))
            or isinstance(mass_uncertainty, bool)
            or not math.isfinite(float(mass_uncertainty))
            or float(mass_uncertainty) < 0.0
        ):
            problems.append("packaged physics mass uncertainty must be finite and non-negative")
        if (
            not isinstance(inertia_uncertainty, list)
            or len(inertia_uncertainty) != 3
            or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < 0.0
                for value in inertia_uncertainty
            )
        ):
            problems.append("packaged physics inertia uncertainty must contain three finite non-negative values")
    if binding.get("method") not in {"measured", "manufacturer_specification", "computed_from_measured_density"}:
        problems.append("packaged physics evidence method is not an accepted measurement or specification method")

    signed_identity_fields = (
        "status",
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
    )
    for field in signed_identity_fields:
        if binding.get(field) != attested_evidence.get(field):
            problems.append(f"packaged physics evidence {field} differs from the signed source record")
    signed_evidence = attested_evidence.get("evidence")
    signed_reduced_evidence: list[dict[str, str]] = []
    if not isinstance(signed_evidence, list):
        problems.append("signed physics evidence contains no evidence records")
    else:
        signed_ids: set[str] = set()
        for index, record in enumerate(signed_evidence):
            if not isinstance(record, dict):
                problems.append(f"signed physics evidence record {index} is malformed")
                continue
            evidence_id = str(record.get("evidence_id") or "")
            evidence_sha256 = str(record.get("sha256") or "")
            if not evidence_id or evidence_id in signed_ids or not SHA256_PATTERN.fullmatch(evidence_sha256):
                problems.append(f"signed physics evidence record {index} has an invalid identity or digest")
                continue
            signed_ids.add(evidence_id)
            signed_reduced_evidence.append({"evidence_id": evidence_id, "sha256": evidence_sha256})
    if signed_reduced_evidence != reduced_evidence:
        problems.append("materialised physics evidence identities or digests differ from the signed source record")

    if prim_path in rigid_body_paths:
        bound_prim = stage.GetPrimAtPath(prim_path)
        mass_api = usd_physics.MassAPI(bound_prim)
        usd_mass = mass_api.GetMassAttr().Get()
        usd_centre = mass_api.GetCenterOfMassAttr().Get()
        usd_inertia = mass_api.GetDiagonalInertiaAttr().Get()
        usd_axes = mass_api.GetPrincipalAxesAttr().Get()
        binding_mass = binding.get("mass")
        binding_centre = binding.get("center_of_mass")
        binding_inertia = binding.get("diagonal_inertia")
        binding_axes = binding.get("principal_axes")
        try:
            mass_matches = math.isclose(float(usd_mass), float(binding_mass), rel_tol=1e-6, abs_tol=1e-9)
            centre_matches = len(binding_centre) == 3 and all(
                math.isclose(float(usd_value), float(binding_value), rel_tol=1e-6, abs_tol=1e-9)
                for usd_value, binding_value in zip(usd_centre, binding_centre, strict=True)
            )
            inertia_matches = len(binding_inertia) == 3 and all(
                math.isclose(float(usd_value), float(binding_value), rel_tol=1e-6, abs_tol=1e-9)
                for usd_value, binding_value in zip(usd_inertia, binding_inertia, strict=True)
            )
            usd_axes_values = [float(usd_axes.GetReal()), *(float(value) for value in usd_axes.GetImaginary())]
            axes_match = len(binding_axes) == 4 and all(
                math.isclose(float(usd_value), float(binding_value), rel_tol=1e-6, abs_tol=1e-9)
                for usd_value, binding_value in zip(usd_axes_values, binding_axes, strict=True)
            )
            binding_inertia_values = [float(value) for value in binding_inertia]
            inertia_physical = all(value > 0.0 and math.isfinite(value) for value in binding_inertia_values) and not any(
                value
                > sum(binding_inertia_values) - value + max(1e-12, 1e-9 * sum(binding_inertia_values))
                for value in binding_inertia_values
            )
            binding_axes_values = [float(value) for value in binding_axes]
            axes_are_unit = math.isclose(
                math.sqrt(sum(value * value for value in binding_axes_values)),
                1.0,
                rel_tol=1e-6,
                abs_tol=1e-6,
            )
        except (AttributeError, TypeError, ValueError):
            mass_matches = False
            centre_matches = False
            inertia_matches = False
            axes_match = False
            inertia_physical = False
            axes_are_unit = False
        if not mass_matches or not centre_matches or not inertia_matches or not axes_match:
            problems.append("packaged physics evidence mass properties do not match the USD opinions")
        if not inertia_physical or not axes_are_unit:
            problems.append("packaged physics evidence contains physically invalid inertia or principal axes")
    return problems


def _revalidate_packaged_physics_evidence(package_path: Path) -> list[str]:
    """Revalidate one packaged physics binding against its composed USD opinions."""

    if not package_path.is_file():
        return ["released package root is unavailable"]
    try:
        from pxr import Usd, UsdPhysics
    except Exception as exc:
        return [f"OpenUSD runtime is unavailable for physics evidence revalidation: {exc}"]
    stage = Usd.Stage.Open(str(package_path))
    if stage is None:
        return ["released package root could not be opened"]
    rigid_bodies = [prim for prim in stage.Traverse() if prim.HasAPI(UsdPhysics.RigidBodyAPI)]
    return _physics_evidence_binding_problems(
        stage,
        rigid_bodies,
        package_path.parent,
        UsdPhysics,
    )


def _inspect_composed_requirements(
    path: Path,
    evidence_path: str,
    *,
    package_root: Path | None = None,
) -> list[dict[str, Any]]:
    if not path.exists():
        return _blocked_requirement_results(evidence_path, "composed USD root is missing")
    try:
        from pxr import Usd, UsdGeom, UsdPhysics, UsdShade
    except Exception:
        return _blocked_requirement_results(evidence_path, "OpenUSD Python runtime unavailable for schema inspection")
    stage = Usd.Stage.Open(str(path))
    if stage is None:
        return _blocked_requirement_results(evidence_path, "composed USD root could not be opened")

    prims = list(stage.Traverse())
    meshes = [prim for prim in prims if prim.IsA(UsdGeom.Mesh)]
    meshes_without_normals = [
        str(prim.GetPath())
        for prim in meshes
        if not UsdGeom.Mesh(prim).GetNormalsAttr().HasAuthoredValueOpinion()
        or not UsdGeom.Mesh(prim).GetNormalsAttr().Get()
    ]
    rigid_bodies = [prim for prim in prims if prim.HasAPI(UsdPhysics.RigidBodyAPI)]
    colliders = [prim for prim in prims if prim.HasAPI(UsdPhysics.CollisionAPI)]
    mass_prims = [prim for prim in prims if prim.HasAPI(UsdPhysics.MassAPI)]
    physics_materials = [prim for prim in prims if prim.HasAPI(UsdPhysics.MaterialAPI)]
    disabled_rigid_bodies = [
        str(prim.GetPath())
        for prim in rigid_bodies
        if UsdPhysics.RigidBodyAPI(prim).GetRigidBodyEnabledAttr().Get() is not True
    ]
    rigid_body_paths = {str(prim.GetPath()) for prim in rigid_bodies}
    mass_prim_paths = {str(prim.GetPath()) for prim in mass_prims}
    rigid_bodies_without_mass = sorted(rigid_body_paths - mass_prim_paths)
    invalid_mass_prims = []
    for prim in mass_prims:
        mass_api = UsdPhysics.MassAPI(prim)
        mass = mass_api.GetMassAttr().Get()
        diagonal_inertia = mass_api.GetDiagonalInertiaAttr().Get()
        principal_axes = mass_api.GetPrincipalAxesAttr().Get()
        try:
            mass_is_valid = math.isfinite(float(mass)) and float(mass) > 0.0
            inertia_values = [float(value) for value in diagonal_inertia]
            inertia_is_valid = len(inertia_values) == 3 and all(
                math.isfinite(value) and value > 0.0 for value in inertia_values
            )
            inertia_is_valid = inertia_is_valid and not any(
                value > sum(inertia_values) - value + max(1e-12, 1e-9 * sum(inertia_values))
                for value in inertia_values
            )
            axes_values = [float(principal_axes.GetReal()), *(float(value) for value in principal_axes.GetImaginary())]
            axes_norm = math.sqrt(sum(value * value for value in axes_values))
            axes_are_valid = all(math.isfinite(value) for value in axes_values) and math.isclose(
                axes_norm,
                1.0,
                rel_tol=1e-6,
                abs_tol=1e-6,
            )
        except (AttributeError, TypeError, ValueError):
            mass_is_valid = False
            inertia_is_valid = False
            axes_are_valid = False
        if not mass_is_valid or not inertia_is_valid or not axes_are_valid:
            invalid_mass_prims.append(str(prim.GetPath()))
    unaccepted_physics_evidence = [
        str(prim.GetPath())
        for prim in rigid_bodies
        if str(prim.GetAttribute("assetFactory:physicsEvidenceStatus").Get() or "").strip().lower() != "accepted"
    ]
    physics_binding_problems = _physics_evidence_binding_problems(
        stage,
        rigid_bodies,
        package_root,
        UsdPhysics,
    )
    physics_bindings = []
    for prim in prims:
        binding = UsdShade.MaterialBindingAPI(prim)
        relationship = binding.GetDirectBindingRel("physics")
        if relationship and relationship.GetTargets():
            physics_bindings.append(prim)
    semantic_prims = []
    invalid_semantic_prims = []
    for prim in prims:
        applied = [str(item) for item in prim.GetAppliedSchemas()]
        taxonomies = [item.split(":", 1)[1] for item in applied if item.startswith("SemanticsLabelsAPI:") and ":" in item]
        if not taxonomies:
            continue
        semantic_prims.append(prim)
        if any(
            not prim.GetAttribute(f"semantics:labels:{taxonomy}").HasAuthoredValueOpinion()
            or not prim.GetAttribute(f"semantics:labels:{taxonomy}").Get()
            for taxonomy in taxonomies
        ):
            invalid_semantic_prims.append(str(prim.GetPath()))

    up_axis_pass = str(UsdGeom.GetStageUpAxis(stage)).upper() == "Z"
    unit_scale_pass = abs(float(UsdGeom.GetStageMetersPerUnit(stage)) - 1.0) <= 1e-9
    physics_problems = []
    if not rigid_bodies:
        physics_problems.append("no rigid bodies found")
    if not colliders:
        physics_problems.append("no colliders found")
    if not mass_prims:
        physics_problems.append("no mass schemas found")
    if not physics_materials:
        physics_problems.append("no physics materials found")
    if not physics_bindings:
        physics_problems.append("no physics-purpose material bindings found")
    if disabled_rigid_bodies:
        physics_problems.append("disabled rigid bodies: " + ", ".join(disabled_rigid_bodies))
    if rigid_bodies_without_mass:
        physics_problems.append("rigid bodies without MassAPI: " + ", ".join(rigid_bodies_without_mass))
    if invalid_mass_prims:
        physics_problems.append(
            "MassAPI prims without finite positive physically valid inertia and unit principal axes: "
            + ", ".join(invalid_mass_prims)
        )
    if unaccepted_physics_evidence:
        physics_problems.append(
            "rigid bodies without accepted assetFactory:physicsEvidenceStatus: "
            + ", ".join(unaccepted_physics_evidence)
        )
    physics_problems.extend(physics_binding_problems)
    return [
        _requirement_result(
            "UN.006",
            "pass" if up_axis_pass and unit_scale_pass else "blocked",
            evidence_path,
            "stage uses metres and Z-up" if up_axis_pass and unit_scale_pass else "normalise the composed stage to metres and Z-up",
        ),
        _requirement_result(
            "VG.027",
            "pass" if meshes and not meshes_without_normals else "blocked",
            evidence_path,
            f"{len(meshes)} mesh prims carry authored normals"
            if meshes and not meshes_without_normals
            else "author normals on every non-subdivision mesh: " + ", ".join(meshes_without_normals or ["no mesh prims found"]),
        ),
        _requirement_result(
            "RB.COL.001",
            "pass" if not physics_problems else "blocked",
            evidence_path,
            (
                f"found {len(rigid_bodies)} rigid bodies, {len(colliders)} colliders, {len(mass_prims)} mass schemas, "
                f"{len(physics_materials)} physics materials and {len(physics_bindings)} physics-purpose bindings; "
                "all rigid bodies are enabled and carry accepted finite mass evidence"
            )
            if not physics_problems
            else "; ".join(physics_problems),
        ),
        _requirement_result(
            "SL.003",
            "pass" if semantic_prims and not invalid_semantic_prims else "blocked",
            evidence_path,
            f"found {len(semantic_prims)} prims using SemanticsLabelsAPI"
            if semantic_prims and not invalid_semantic_prims
            else "apply SemanticsLabelsAPI:<taxonomy> with non-empty token-array labels"
            + (": " + ", ".join(invalid_semantic_prims) if invalid_semantic_prims else ""),
        ),
    ]


def _profile_record(asset_package: dict[str, Any]) -> dict[str, Any]:
    raw = asset_package.get("simready_profile") or {}
    if not isinstance(raw, dict):
        raw = {"profile_id": str(raw)}
    profile_id = str(raw.get("profile_id") or "Prop-Robotics-Neutral")
    profile_version = str(raw.get("profile_version") or "unresolved")
    return {
        "profile_id": profile_id,
        "profile_version": profile_version,
        "profile_version_status": str(
            raw.get("profile_version_status") or ("unresolved" if profile_version == "unresolved" else "pinned")
        ),
        "target_runtime": str(raw.get("target_runtime") or "runtime-neutral"),
        "specification_uri": str(raw.get("specification_uri") or SIMREADY_SPECIFICATION_URI),
        "profile_catalogue_status": "known_robotics_profile" if profile_id in KNOWN_ROBOTICS_PROFILES else "vendor_profile",
    }


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON numeric constant: {value}")


def _load_json_report(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.exists():
        return None, "report does not exist"
    if not path.is_file() or path.is_symlink():
        return None, "report must be a regular non-symbolic-link file"
    try:
        if path.stat().st_size > 16_777_216:
            return None, "report exceeds the 16 MiB limit"
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        return None, f"report could not be read: {exc}"
    if not isinstance(payload, dict):
        return None, "report root must be an object"
    return payload, ""


def _resolve_project_report_file(root: Path, raw_path: str) -> tuple[Path | None, str]:
    if not raw_path.strip():
        return None, "report path is missing"
    project_root = root.resolve(strict=True)
    supplied = Path(raw_path)
    if any(part == ".." for part in supplied.parts):
        return None, "report path must not contain parent traversal"
    candidates = [supplied] if supplied.is_absolute() else [project_root / supplied]
    missing_inside_project = False
    for candidate in candidates:
        try:
            candidate_absolute = candidate.absolute()
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(project_root)
        except (OSError, ValueError):
            continue
        missing_inside_project = True
        try:
            relative_parts = candidate_absolute.relative_to(project_root).parts
        except ValueError:
            relative_parts = resolved.relative_to(project_root).parts
        current = project_root
        symbolic_link_found = False
        for part in relative_parts:
            current = current / part
            if current.is_symlink() or bool(getattr(current, "is_junction", lambda: False)()):
                symbolic_link_found = True
                break
        if symbolic_link_found:
            return None, "report path must not traverse a link or junction"
        if resolved.is_file():
            return resolved, ""
    if missing_inside_project:
        return None, "report does not exist inside the project"
    return None, "report path resolves outside the project"


def _successful_process_evidence(value: Any, label: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label} execution evidence must be an object"]
    problems = []
    if set(value) != PROCESS_EVIDENCE_FIELDS:
        problems.append(f"{label} execution evidence has an unexpected shape")
    if value.get("exit_code") != 0 or isinstance(value.get("exit_code"), bool):
        problems.append(f"{label} did not exit successfully")
    for field in ("timed_out", "output_limit_exceeded", "report_limit_exceeded"):
        if value.get(field) is not False:
            problems.append(f"{label} {field.replace('_', ' ')}")
    observed_output_bytes = value.get("observed_output_bytes")
    if (
        not isinstance(observed_output_bytes, int)
        or isinstance(observed_output_bytes, bool)
        or observed_output_bytes < 0
    ):
        problems.append(f"{label} observed output byte count is invalid")
    for field in ("captured_stdout_sha256", "captured_stderr_sha256"):
        if not SHA256_PATTERN.fullmatch(str(value.get(field) or "")):
            problems.append(f"{label} {field.replace('_', ' ')} is invalid")
    for field in ("stdout_excerpt", "stderr_excerpt", "launch_error"):
        if not isinstance(value.get(field), str):
            problems.append(f"{label} {field.replace('_', ' ')} must be a string")
    if value.get("launch_error") != "":
        problems.append(f"{label} has a launch error")
    return problems


def _composition_fingerprint(path: Path) -> str:
    try:
        from pxr import Usd
    except Exception:
        return ""
    stage = Usd.Stage.Open(str(path))
    if stage is None:
        return ""
    layer_hashes = []
    for layer in stage.GetUsedLayers():
        real_path = Path(str(layer.realPath or ""))
        if real_path.is_file():
            layer_hashes.append(sha256_file(real_path))
    if not layer_hashes:
        return ""
    digest = hashlib.sha256()
    for layer_hash in sorted(layer_hashes):
        digest.update(layer_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _official_validator_record(root: Path, asset_package: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    relative_path = str(asset_package.get("official_validator_report_path") or "reports/simready-profile-validation.json")
    report_path, path_error = _resolve_project_report_file(root, relative_path)
    payload, read_error = _load_json_report(report_path) if report_path is not None else (None, path_error)
    base = {
        "validator_id": VALIDATOR_ID,
        "status": "blocked",
        "available": payload is not None,
        "executed": False,
        "report_path": relative_path,
        "report_sha256": sha256_file(report_path) if payload is not None and report_path is not None else "",
        "validated_usd_sha256": "",
        "validated_composition_fingerprint": "",
        "validated_package_dependency_fingerprint": "",
        "validated_package_inventory": [],
        "profile_id": profile["profile_id"],
        "profile_version": profile["profile_version"],
        "feature_results": [],
        "requirement_results": [],
        "reason": read_error or "official Profile validation has not run",
    }
    if payload is None:
        return base

    validator_identity = payload.get("validator") if isinstance(payload.get("validator"), dict) else {}
    reported_validator_id = str(payload.get("validator_id") or "")
    reported_validator_version = str(payload.get("validator_version") or "")
    validated_usd_sha256 = str(payload.get("usd_sha256") or "")
    validated_composition_fingerprint = str(payload.get("composition_fingerprint") or "")
    expected_usd_path_value = str(asset_package.get("package_path") or asset_package.get("usd_root_path") or "")
    usd_path, usd_path_error = _resolve_project_report_file(root, expected_usd_path_value)
    expected_usd_sha256 = sha256_file(usd_path) if usd_path is not None else ""
    expected_composition_fingerprint = _composition_fingerprint(usd_path) if usd_path is not None else ""
    expected_package_binding = (
        package_inventory_fingerprint(usd_path.parent)
        if usd_path is not None
        else {"status": "blocked", "fingerprint": "", "files": [], "blocked_reasons": [usd_path_error]}
    )
    reported_profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    reported_profile_id = str(payload.get("profile_id") or "")
    reported_profile_version = str(payload.get("profile_version") or "")
    requirement_results = payload.get("requirements") or []
    if not isinstance(requirement_results, list):
        requirement_results = []
    feature_results = payload.get("features") or []
    if not isinstance(feature_results, list):
        feature_results = []
    problems = []
    attestation_secret = os.environ.get("AFB_VALIDATION_ATTESTATION_SECRET", "")
    problems.extend(verify_official_profile_report_attestation(payload, attestation_secret))
    if profile["profile_version_status"] != "pinned":
        problems.append("target Profile version is unresolved")
    if payload.get("schema_version") != "1.0.0":
        problems.append("validator report schema version is not 1.0.0")
    if reported_validator_id != VALIDATOR_ID:
        problems.append("validator identity does not match the official bridge")
    if not SEMANTIC_VERSION_PATTERN.fullmatch(reported_validator_version):
        problems.append("validator version is not an exact semantic version")
    expected_validator_fields = {
        "validator_id",
        "validator_version",
        "documentation_uri",
        "executable_name",
        "executable_sha256",
    }
    if set(validator_identity) != expected_validator_fields:
        problems.append("validator identity record has an unexpected shape")
    if validator_identity.get("validator_id") != VALIDATOR_ID:
        problems.append("nested validator identity does not match the official bridge")
    if validator_identity.get("validator_version") != reported_validator_version:
        problems.append("nested validator version does not match the report")
    if validator_identity.get("documentation_uri") != VALIDATOR_DOCUMENTATION_URI:
        problems.append("validator documentation identity does not match the official bridge")
    executable_name = str(validator_identity.get("executable_name") or "")
    executable_sha256 = str(validator_identity.get("executable_sha256") or "")
    if not executable_name or executable_name != Path(executable_name).name or any(
        separator in executable_name for separator in ("/", "\\")
    ):
        problems.append("validator executable name is missing or invalid")
    if not SHA256_PATTERN.fullmatch(executable_sha256):
        problems.append("validator executable digest is missing or invalid")
    pinned_executable_sha256 = os.environ.get("AFB_ASSET_VALIDATOR_EXECUTABLE_SHA256", "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", pinned_executable_sha256):
        problems.append("AFB_ASSET_VALIDATOR_EXECUTABLE_SHA256 is not configured with an exact lowercase digest")
    elif executable_sha256 != pinned_executable_sha256:
        problems.append("validator executable digest does not match the administrator-pinned digest")
    if usd_path_error:
        problems.append("composed USD root is unavailable: " + usd_path_error)
    if not validated_usd_sha256 or validated_usd_sha256 != expected_usd_sha256:
        problems.append("validator report USD checksum does not match the composed root")
    if not validated_composition_fingerprint or validated_composition_fingerprint != expected_composition_fingerprint:
        problems.append("validator report composition fingerprint does not match the composed layer stack")
    reported_package_fingerprint = str(payload.get("package_dependency_fingerprint") or "")
    reported_package_inventory = payload.get("package_inventory")
    if not isinstance(reported_package_inventory, list):
        reported_package_inventory = []
        problems.append("validator report package inventory must be an array")
    if expected_package_binding["status"] != "pass":
        problems.extend(
            "materialised package inventory: " + str(item)
            for item in expected_package_binding["blocked_reasons"]
        )
    if (
        not PREFIXED_SHA256_PATTERN.fullmatch(reported_package_fingerprint)
        or reported_package_fingerprint != expected_package_binding["fingerprint"]
    ):
        problems.append("validator report package fingerprint does not match the materialised package")
    if reported_package_inventory != expected_package_binding["files"]:
        problems.append("validator report package inventory does not match the materialised package")
    if usd_path is None or str(payload.get("usd_path") or "") != usd_path.name:
        problems.append("validator report USD label does not match the composed package root")
    if reported_profile_id != profile["profile_id"]:
        problems.append("validator report Profile ID does not match the target")
    if reported_profile_version != profile["profile_version"]:
        problems.append("validator report Profile version does not match the target")
    if reported_profile != {"profile_id": reported_profile_id, "profile_version": reported_profile_version}:
        problems.append("nested Profile identity is not exact")
    if not requirement_results:
        problems.append("validator report has no per-Requirement findings")
    if not feature_results:
        problems.append("validator report has no per-Feature findings")
    if payload.get("status") != "pass" or payload.get("problems") != [] or payload.get("reason") != "":
        problems.append("validator report status is not pass")

    execution = payload.get("execution")
    execution_ok = isinstance(execution, dict)
    if not execution_ok or set(execution) != {"command_contract", "validator_executable", "version_probe", "validation"}:
        problems.append("validator execution evidence has an unexpected shape")
        execution = execution if isinstance(execution, dict) else {}
        execution_ok = False
    executable_evidence = execution.get("validator_executable")
    expected_executable_evidence = {"name": executable_name, "sha256": executable_sha256}
    if executable_evidence != expected_executable_evidence:
        problems.append("execution executable identity does not match the validator record")
        execution_ok = False
    expected_command_contract = [
        executable_name,
        "--profile",
        f"{profile['profile_id']}@{profile['profile_version']}",
        "--no-fix",
        "--no-stamp",
        "--json-output",
        "<raw-report>",
        "<asset>",
    ]
    if execution.get("command_contract") != expected_command_contract:
        problems.append("validator command contract does not match the read-only official bridge")
        execution_ok = False
    for key, label in (("version_probe", "validator version probe"), ("validation", "validator run")):
        process_problems = _successful_process_evidence(execution.get(key), label)
        if process_problems:
            problems.extend(process_problems)
            execution_ok = False

    raw_report_path_value = str(payload.get("raw_report_path") or "")
    expected_raw_report_path_value = str(
        asset_package.get("official_validator_raw_report_path") or "reports/simready-profile-validation.raw.json"
    )
    raw_report_path, raw_report_path_error = _resolve_project_report_file(root, expected_raw_report_path_value)
    raw_report = None
    actual_raw_report_sha256 = ""
    if raw_report_path is None:
        problems.append("raw validator report is unavailable: " + raw_report_path_error)
    else:
        if raw_report_path_value != raw_report_path.name:
            problems.append("raw validator report label does not match the canonical project report")
        raw_report, raw_report_error = _load_json_report(raw_report_path)
        if raw_report is None:
            problems.append("raw validator report is invalid: " + raw_report_error)
        actual_raw_report_sha256 = sha256_file(raw_report_path)
    reported_raw_report_sha256 = str(payload.get("raw_report_sha256") or "")
    if (
        not SHA256_PATTERN.fullmatch(reported_raw_report_sha256)
        or reported_raw_report_sha256 != actual_raw_report_sha256
    ):
        problems.append("raw validator report digest does not match the materialised report")

    reconstructed = normalise_official_profile_report(
        raw_report,
        profile_id=reported_profile_id,
        profile_version=reported_profile_version,
        validator_version=reported_validator_version,
        usd_path=str(payload.get("usd_path") or ""),
        usd_sha256=validated_usd_sha256,
        composition_fingerprint=validated_composition_fingerprint,
        raw_report_path=raw_report_path_value,
        raw_report_sha256=reported_raw_report_sha256,
        execution=execution,
        preflight_problems=[],
        package_dependency_fingerprint=reported_package_fingerprint,
        package_inventory=reported_package_inventory,
    )
    unsigned_payload = {key: value for key, value in payload.items() if key != "attestation"}
    if reconstructed != unsigned_payload:
        problems.append("validator report is not the exact normalised output of the official bridge")
    base.update(
        {
            "status": "pass" if not problems else "blocked",
            "executed": execution_ok,
            "reported_validator_id": reported_validator_id,
            "reported_validator_version": reported_validator_version,
            "validated_usd_sha256": validated_usd_sha256,
            "validated_composition_fingerprint": validated_composition_fingerprint,
            "validated_package_dependency_fingerprint": reported_package_fingerprint,
            "validated_package_inventory": reported_package_inventory,
            "reported_profile_id": reported_profile_id,
            "reported_profile_version": reported_profile_version,
            "feature_results": feature_results,
            "requirement_results": requirement_results,
            "reason": "; ".join(problems),
        }
    )
    return base


def _runtime_validation_record(root: Path, asset_package: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    relative_path = str(asset_package.get("runtime_validation_report_path") or "reports/isaac-load-check.json")
    report_path, path_error = _resolve_project_report_file(root, relative_path)
    payload: dict[str, Any] | None = None
    read_error = path_error
    if report_path is not None:
        try:
            payload = parse_runtime_report_bytes(report_path.read_bytes())
            read_error = ""
        except (OSError, ValueError) as exc:
            read_error = f"runtime report is not trusted strict JSON: {exc}"
    profile_id = profile["profile_id"]
    required_test_ids = (
        ["articulation_runtime_stability", "articulation_joint_sweep"]
        if profile_id.startswith("Robot-")
        else ["rigid_body_drop_and_settle", "rigid_body_impulse_response", "rigid_body_reset_repeatability"]
    )
    record = {
        "runtime_id": "isaac-sim",
        "status": "blocked",
        "available": payload is not None,
        "executed": payload is not None,
        "report_path": relative_path,
        "report_sha256": sha256_file(report_path) if payload is not None and report_path is not None else "",
        "validated_usd_sha256": "",
        "validated_composition_fingerprint": "",
        "validated_package_dependency_fingerprint": "",
        "validated_package_inventory": [],
        "required_test_ids": required_test_ids,
        "behavioural_tests": [],
        "reason": read_error or "Isaac runtime validation has not run",
    }
    if payload is None:
        return record
    trust_problems = [f"runtime report schema: {issue.render()}" for issue in validate_payload("isaac-runtime-evidence", payload)]
    secret: bytes | None = None
    producer_pin: str | None = None
    try:
        secret = isaac_attestation_secret()
    except ValueError as exc:
        trust_problems.append(str(exc))
    try:
        producer_pin = producer_sha256_pin()
    except ValueError as exc:
        trust_problems.append(str(exc))
    if secret is not None and producer_pin is not None:
        trust_problems.extend(verify_runtime_report_envelope(payload, secret, producer_pin))
    tests = payload.get("behavioural_tests") or []
    if not isinstance(tests, list):
        tests = []
    tests_by_id = {str(item.get("test_id")): item for item in tests if isinstance(item, dict)}
    missing = [test_id for test_id in required_test_ids if test_id not in tests_by_id]
    failed = [
        test_id
        for test_id in required_test_ids
        if test_id in tests_by_id and str(tests_by_id[test_id].get("status") or "").lower() != "pass"
    ]
    report_passed = payload.get("loaded") is True and str(payload.get("status") or "").lower() == "pass"
    reported_profile_id = str(payload.get("profile_id") or "")
    reported_profile_version = str(payload.get("profile_version") or "")
    expected_usd_path_value = str(asset_package.get("package_path") or asset_package.get("usd_root_path") or "")
    usd_path, usd_path_error = _resolve_project_report_file(root, expected_usd_path_value)
    expected_usd_sha256 = sha256_file(usd_path) if usd_path is not None else ""
    validated_usd_sha256 = str(payload.get("usd_sha256") or "")
    expected_composition_fingerprint = _composition_fingerprint(usd_path) if usd_path is not None else ""
    validated_composition_fingerprint = str(payload.get("composition_fingerprint") or "")
    expected_package_binding = (
        package_inventory_fingerprint(usd_path.parent)
        if usd_path is not None
        else {"status": "blocked", "fingerprint": "", "files": [], "blocked_reasons": [usd_path_error]}
    )
    validated_package_fingerprint = str(payload.get("package_dependency_fingerprint") or "")
    validated_package_inventory = payload.get("package_inventory")
    if not isinstance(validated_package_inventory, list):
        validated_package_inventory = []
    problems = list(trust_problems)
    if not report_passed:
        problems.append("runtime report did not pass")
    if reported_profile_id != profile["profile_id"] or reported_profile_version != profile["profile_version"]:
        problems.append("runtime report Profile ID or version does not match the target")
    if missing:
        problems.append("missing behavioural tests: " + ", ".join(missing))
    if failed:
        problems.append("behavioural tests did not pass: " + ", ".join(failed))
    if not validated_usd_sha256 or validated_usd_sha256 != expected_usd_sha256:
        problems.append("runtime report USD checksum does not match the composed root")
    if not validated_composition_fingerprint or validated_composition_fingerprint != expected_composition_fingerprint:
        problems.append("runtime report composition fingerprint does not match the composed layer stack")
    reported_usd_path, reported_usd_path_error = _resolve_project_report_file(root, str(payload.get("usd_path") or ""))
    if reported_usd_path_error or usd_path is None or reported_usd_path != usd_path:
        problems.append("runtime report USD path does not resolve to the composed root")
    if expected_package_binding["status"] != "pass":
        problems.extend(
            "materialised package inventory: " + str(item)
            for item in expected_package_binding["blocked_reasons"]
        )
    if (
        not PREFIXED_SHA256_PATTERN.fullmatch(validated_package_fingerprint)
        or validated_package_fingerprint != expected_package_binding["fingerprint"]
    ):
        problems.append("runtime report package fingerprint does not match the materialised package")
    if validated_package_inventory != expected_package_binding["files"]:
        problems.append("runtime report package inventory does not match the materialised package")
    record.update(
        {
            "status": "pass" if not problems else "blocked",
            "required_test_ids": required_test_ids,
            "behavioural_tests": tests,
            "reported_profile_id": reported_profile_id,
            "reported_profile_version": reported_profile_version,
            "validated_usd_sha256": validated_usd_sha256,
            "validated_composition_fingerprint": validated_composition_fingerprint,
            "validated_package_dependency_fingerprint": validated_package_fingerprint,
            "validated_package_inventory": validated_package_inventory,
            "runtime_version": str(
                (payload.get("runtime_availability") or {}).get("isaac_sim_version")
                if isinstance(payload.get("runtime_availability"), dict)
                else payload.get("runtime_version") or ""
            ),
            "producer_sha256": str((payload.get("execution_identity") or {}).get("producer_sha256") or "")
            if isinstance(payload.get("execution_identity"), dict)
            else "",
            "reason": "; ".join(problems),
        }
    )
    return record


def evaluate_runtime_validation(
    project_dir: str | Path,
    *,
    usd_root_path: str,
    package_path: str = "",
    report_path: str,
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate one runtime report against the exact composed asset and Profile."""

    return _runtime_validation_record(
        Path(project_dir),
        {
            "usd_root_path": usd_root_path,
            "package_path": package_path,
            "runtime_validation_report_path": report_path,
        },
        profile,
    )


def _merge_official_requirement_results(
    requirements: list[dict[str, Any]], official_validator: dict[str, Any]
) -> list[dict[str, Any]]:
    official_by_id = {
        str(item.get("requirement_id") or item.get("id")): item
        for item in official_validator.get("requirement_results", [])
        if isinstance(item, dict)
    }
    merged = []
    for requirement in requirements:
        item = dict(requirement)
        official = official_by_id.get(requirement["requirement_id"])
        if official:
            official_status = str(official.get("status") or "").lower()
            item["requirement_version"] = str(
                official.get("requirement_version") or official.get("version") or item["requirement_version"]
            )
            item["official_status"] = "pass" if official_status in {"pass", "passed", "validated"} else "blocked"
            item["official_validator"] = True
            if item["official_status"] != "pass":
                item["status"] = "blocked"
                item["message"] = str(official.get("message") or "official validator Requirement finding did not pass")
        else:
            item["official_status"] = "blocked"
        merged.append(item)
    local_ids = {item["requirement_id"] for item in requirements}
    for requirement_id, official in official_by_id.items():
        if not requirement_id or requirement_id in local_ids:
            continue
        official_status = str(official.get("status") or "").lower()
        merged.append(
            {
                "requirement_id": requirement_id,
                "requirement_name": str(official.get("requirement_name") or official.get("name") or requirement_id),
                "requirement_version": str(official.get("requirement_version") or official.get("version") or "unresolved"),
                "status": "pass" if official_status in {"pass", "passed", "validated"} else "blocked",
                "validator": official_validator["validator_id"],
                "official_validator": True,
                "official_status": "pass" if official_status in {"pass", "passed", "validated"} else "blocked",
                "evidence_path": official_validator["report_path"],
                "message": str(official.get("message") or "official Profile Requirement finding"),
            }
        )
    return merged


def _feature_records(
    requirements: list[dict[str, Any]], official_validator: dict[str, Any]
) -> list[dict[str, Any]]:
    statuses = {item["requirement_id"]: item["status"] for item in requirements}
    official_by_id = {
        str(item.get("feature_id") or item.get("id")): item
        for item in official_validator.get("feature_results", [])
        if isinstance(item, dict)
    }
    feature_ids = list(FEATURE_REQUIREMENTS)
    feature_ids.extend(feature_id for feature_id in official_by_id if feature_id and feature_id not in FEATURE_REQUIREMENTS)
    records = []
    for feature_id in feature_ids:
        official = official_by_id.get(feature_id, {})
        requirement_ids = list(official.get("requirement_ids") or FEATURE_REQUIREMENTS.get(feature_id, []))
        official_passed = str(official.get("status") or "").lower() in {"pass", "passed", "validated"}
        version = str(official.get("feature_version") or official.get("version") or "unresolved")
        local_checks_passed = all(statuses.get(requirement_id) == "pass" for requirement_id in requirement_ids)
        records.append(
            {
                "feature_id": feature_id,
                "feature_version": version,
                "status": "pass"
                if requirement_ids and local_checks_passed and official_passed and version != "unresolved"
                else "blocked",
                "requirement_ids": requirement_ids,
            }
        )
    return records


def validate_asset_package(
    project_dir: str | Path,
    asset_package: dict[str, Any],
    requested_outputs: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    root = Path(project_dir)
    requested = " ".join(item.lower() for item in requested_outputs)
    asset_id = str(asset_package.get("asset_id", ""))
    results: list[dict[str, Any]] = []
    blockers: list[str] = []

    def add(result: dict[str, Any]) -> None:
        results.append(result)
        if result["status"] == "blocked":
            blockers.append(f"{result['gate_id']}: {result['repair_action'] or result['evidence_path']}")

    root_path = root / asset_package.get("usd_root_path", "")
    scene_path = root / asset_package.get("scene_path", "scene.usda")
    package_path = root / asset_package.get("package_path", "")
    layer_stack = [root / item for item in asset_package.get("usd_layer_stack", [])]
    simready_profile = _profile_record(asset_package)
    requirement_root_path = package_path if package_path.is_file() else root_path
    requirement_evidence_path = str(
        asset_package.get("package_path") if package_path.is_file() else asset_package.get("usd_root_path", "")
    )
    local_requirement_results = _inspect_composed_requirements(
        requirement_root_path,
        requirement_evidence_path,
        package_root=package_path.parent if package_path.is_file() else None,
    )
    official_validator = _official_validator_record(root, asset_package, simready_profile)
    requirement_results = _merge_official_requirement_results(local_requirement_results, official_validator)
    feature_results = _feature_records(requirement_results, official_validator)
    runtime_validation = _runtime_validation_record(root, asset_package, simready_profile)
    openusd_compliance = _openusd_compliance_record(root_path, str(asset_package.get("usd_root_path") or ""))
    openusd_report_path = root / "reports" / "openusd-compliance.json"
    openusd_report_path.parent.mkdir(parents=True, exist_ok=True)
    openusd_report_path.write_text(
        json.dumps(openusd_compliance, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    articulation_required = simready_profile["profile_id"].startswith("Robot-") or (
        asset_package.get("articulation", {}).get("status") == "authored"
    )
    articulation_schema = _articulation_schema_record(
        root_path,
        str(asset_package.get("usd_root_path") or ""),
        required=articulation_required,
    )

    add(
        _result(
            "openusd-compliance",
            "usd",
            openusd_compliance["status"],
            openusd_report_path.relative_to(root).as_posix(),
            openusd_compliance["reason"],
            checker_id=openusd_compliance["checker_id"],
            checker_version=openusd_compliance["checker_version"],
        )
    )
    add(
        _result(
            "articulation-schema-conformance",
            "usd_physics",
            articulation_schema["status"],
            articulation_schema["evidence_path"],
            articulation_schema["reason"],
            required=articulation_required,
            joint_count=len(articulation_schema["joint_results"]),
        )
    )
    add(
        _result(
            "simready-profile-version-pinned",
            "simready_profile",
            "pass" if simready_profile["profile_version_status"] == "pinned" else "blocked",
            simready_profile["specification_uri"],
            "pin the exact target SimReady Profile version",
            profile_id=simready_profile["profile_id"],
            profile_version=simready_profile["profile_version"],
        )
    )
    for requirement in requirement_results:
        add(
            _result(
                "simready-requirement-" + requirement["requirement_id"].lower().replace(".", "-"),
                "simready_requirement",
                requirement["status"],
                requirement["evidence_path"],
                requirement["message"] if requirement["status"] == "blocked" else "",
                requirement_id=requirement["requirement_id"],
                requirement_version=requirement["requirement_version"],
                validator=requirement["validator"],
            )
        )
    rigid_body_requirement = next(
        (item for item in local_requirement_results if item.get("requirement_id") == "RB.COL.001"),
        None,
    )
    if rigid_body_requirement is not None:
        add(
            _result(
                "physics-authoring-plan",
                "physics",
                rigid_body_requirement["status"],
                rigid_body_requirement["evidence_path"],
                rigid_body_requirement["message"] if rigid_body_requirement["status"] == "blocked" else "",
                compatibility_alias_for="RB.COL.001",
            )
        )
    add(
        _result(
            "official-simready-profile-validation",
            "simready_profile",
            official_validator["status"],
            official_validator["report_path"],
            official_validator["reason"],
            validator_id=official_validator["validator_id"],
            profile_id=simready_profile["profile_id"],
            profile_version=simready_profile["profile_version"],
        )
    )
    add(
        _result(
            "simready-runtime-behaviour",
            "runtime",
            runtime_validation["status"],
            runtime_validation["report_path"],
            runtime_validation["reason"],
            runtime_id=runtime_validation["runtime_id"],
            required_test_ids=runtime_validation["required_test_ids"],
        )
    )

    required_paths = [root_path, scene_path, package_path, *layer_stack]
    missing_paths = [path for path in required_paths if not path.exists()]
    add(
        _result(
            "generated-usd-present",
            "usd",
            "blocked" if missing_paths else "pass",
            asset_package.get("usd_root_path", ""),
            "generate missing USD artefacts: " + ", ".join(str(path.relative_to(root)) for path in missing_paths) if missing_paths else "",
        )
    )

    for gate_id, path in [("asset-root-opens", root_path), ("scene-root-opens", scene_path), ("package-root-opens", package_path)]:
        if not path.exists():
            continue
        ok, reason = _usd_opens(path)
        add(_result(gate_id, "usd", "pass" if ok else "blocked", path.relative_to(root).as_posix(), reason))
    for path in layer_stack:
        if not path.exists():
            continue
        ok, reason = _usd_opens(path)
        add(
            _result(
                "owned-layer-opens",
                "usd",
                "pass" if ok else "blocked",
                path.relative_to(root).as_posix(),
                reason,
            )
        )

    for gate_id, path in [("asset-root-references-resolve", root_path), ("scene-references-resolve", scene_path), ("package-references-resolve", package_path)]:
        if not path.exists():
            continue
        missing = _references_resolve(path)
        add(
            _result(
                gate_id,
                "usd",
                "blocked" if missing else "pass",
                path.relative_to(root).as_posix(),
                "resolve missing references: " + ", ".join(missing) if missing else "",
            )
        )

    material_snippets = ["def Material", "material:binding"]
    if asset_package.get("texture_outputs"):
        material_snippets.extend(["UsdPrimvarReader_float2", "inputs:st.connect", 'inputs:sourceColorSpace = "raw"'])
    layer_checks = {
        "material-binding": ("mtl.usda", material_snippets),
        "semantic-metadata": (
            "sem.usda",
            ["semantic_label", "SemanticsLabelsAPI:label", "semantics:labels:class", "provenance_source_sha256"],
        ),
        "variant-sets": ("variants.usda", ["variantSet", "materialProfile", "domainRandomization"]),
        "contents-assembly": ("contents.usda", ["sourceGeometry", "normalised.usda"]),
        "normalised-source": ("source/normalised.usda", ["normalisation_status", "root_transform_policy"]),
    }
    authored_asset_dir = root / asset_package.get("asset_dir", "")
    asset_dir = package_path.parent if package_path.is_file() else authored_asset_dir
    for gate_id, (rel, snippets) in layer_checks.items():
        path = asset_dir / rel
        add(
            _result(
                gate_id,
                "layer",
                "pass" if path.exists() and _contains(path, snippets) else "blocked",
                path.relative_to(root).as_posix() if path.exists() else (asset_package.get("asset_dir", "") + "/" + rel),
                "author required owned-layer opinions",
            )
        )

    material_representations = asset_package.get("material_representations")
    if not isinstance(material_representations, dict):
        material_representations = {}
    packaged_adapter_path = asset_dir / "material-adapters.json"
    adapter_record_value = (
        packaged_adapter_path.relative_to(root).as_posix()
        if package_path.is_file()
        else str(
            material_representations.get("adapter_record")
            or (Path(str(asset_package.get("asset_dir") or "")) / "material-adapters.json").as_posix()
        )
    )
    adapter_record_path, adapter_path_error = _resolve_project_report_file(root, adapter_record_value)
    adapter_payload, adapter_read_error = (
        _load_json_report(adapter_record_path) if adapter_record_path is not None else (None, adapter_path_error)
    )
    adapters = adapter_payload.get("adapters") if isinstance(adapter_payload, dict) else []
    if not isinstance(adapters, list):
        adapters = []

    def adapter_for(render_context: str) -> dict[str, Any] | None:
        matches = [
            item
            for item in adapters
            if isinstance(item, dict) and str(item.get("render_context") or "") == render_context
        ]
        return matches[0] if len(matches) == 1 else None

    def adapter_artefact_matches(adapter: dict[str, Any] | None) -> bool:
        if adapter is None:
            return False
        relative = Path(str(adapter.get("path") or ""))
        if relative.is_absolute() or any(part == ".." for part in relative.parts):
            return False
        candidate = (asset_dir / relative).resolve(strict=False)
        try:
            candidate.relative_to(asset_dir.resolve(strict=False))
        except ValueError:
            return False
        return (
            candidate.is_file()
            and not candidate.is_symlink()
            and SHA256_PATTERN.fullmatch(str(adapter.get("sha256") or "")) is not None
            and sha256_file(candidate) == str(adapter.get("sha256") or "")
        )

    universal_adapter = adapter_for("universal")
    universal_passes = (
        universal_adapter is not None
        and universal_adapter.get("representation") == "UsdPreviewSurface"
        and universal_adapter.get("status") in {"authored", "validated"}
        and adapter_artefact_matches(universal_adapter)
        and _material_surface_context_is_bound(asset_dir / str(universal_adapter.get("path") or ""), "")
    )
    add(
        _result(
            "preview-surface-universal-adapter",
            "material_adapter",
            "pass" if universal_passes else "blocked",
            adapter_record_value,
            "author and checksum a universal UsdPreviewSurface adapter"
            if not universal_passes
            else "",
            adapter_status=str((universal_adapter or {}).get("status") or adapter_read_error),
        )
    )
    materialx_adapter = adapter_for("mtlx")
    canonical_material = adapter_payload.get("canonical") if isinstance(adapter_payload, dict) else {}
    materialx_semantic_validation = (
        adapter_payload.get("materialx_semantic_validation") if isinstance(adapter_payload, dict) else {}
    )
    materialx_semantics_pass = (
        isinstance(materialx_semantic_validation, dict)
        and materialx_semantic_validation.get("status") == "pass"
        and bool(materialx_semantic_validation.get("validator_version"))
    )
    materialx_intentionally_unbound = (
        isinstance(canonical_material, dict)
        and canonical_material.get("usd_bound") is False
        and materialx_adapter is not None
        and materialx_adapter.get("status") == "blocked_not_bound_to_usd_render_context"
        and adapter_artefact_matches(materialx_adapter)
        and materialx_semantics_pass
    )
    materialx_passes = (
        materialx_adapter is not None
        and materialx_adapter.get("representation") == "MaterialX"
        and materialx_adapter.get("status") in {"authored", "validated"}
        and adapter_artefact_matches(materialx_adapter)
        and materialx_semantics_pass
        and _material_surface_context_is_bound(asset_dir / str(materialx_adapter.get("path") or ""), "mtlx")
    )
    add(
        _result(
            "materialx-usd-render-context-adapter",
            "material_adapter",
            "pass" if materialx_passes else "skipped" if materialx_intentionally_unbound else "blocked",
            adapter_record_value,
            "bind the MaterialX document to an mtlx USD render context and record its checksum"
            if not materialx_passes and not materialx_intentionally_unbound
            else "",
            adapter_status=str((materialx_adapter or {}).get("status") or adapter_read_error),
            semantic_validation_status=str(
                materialx_semantic_validation.get("status")
                if isinstance(materialx_semantic_validation, dict)
                else "missing"
            ),
            applicability=(
                "applicable_bound_adapter"
                if materialx_passes
                else "not_applicable_unbound_sidecar"
                if materialx_intentionally_unbound
                else "claimed_or_ambiguous_adapter"
            ),
        )
    )
    if asset_package.get("mesh_deformation_requested"):
        path = asset_dir / "deform.usda"
        add(
            _result(
                "geometry-deformation-variants",
                "layer",
                "pass" if path.exists() and _contains(path, ["GeometryDeformations", "height_or_displacement_path"]) else "blocked",
                path.relative_to(root).as_posix() if path.exists() else (asset_package.get("asset_dir", "") + "/deform.usda"),
                "author geometry deformation requests and height or displacement map references",
            )
        )

    texture_outputs = [root / item for item in asset_package.get("texture_outputs", [])]
    appearance_segments = asset_package.get("appearance_segments", [])
    segment_masks = [asset_dir / str(segment.get("mask_path", "")) for segment in appearance_segments]
    missing_segment_masks = [path for path in segment_masks if not path.exists()]
    if appearance_segments:
        sem_path = asset_dir / "sem.usda"
        mtl_path = asset_dir / "mtl.usda"
        variants_path = asset_dir / "variants.usda"
        sem_text = _read(sem_path) if sem_path.exists() else ""
        mtl_text = _read(mtl_path) if mtl_path.exists() else ""
        variants_text = _read(variants_path) if variants_path.exists() else ""
        missing_semantics = [
            str(segment.get("segment_id", ""))
            for segment in appearance_segments
            if str(segment.get("segment_id", "")) not in sem_text or str(segment.get("semantic_class", "")) not in sem_text
        ]
        missing_material_targets = [
            str(segment.get("material_prim_path", ""))
            for segment in appearance_segments
            if str(segment.get("material_prim_path", "")) not in mtl_text + sem_text
        ]
        add(
            _result(
                "segmentation-segments",
                "segmentation",
                "blocked" if missing_segment_masks else "pass",
                asset_package.get("asset_dir", "") + "/textures/segments",
                "write material-region segment masks" if missing_segment_masks else "",
            )
        )
        add(
            _result(
                "semantic-segment-labels",
                "segmentation",
                "blocked" if missing_semantics else "pass",
                sem_path.relative_to(root).as_posix() if sem_path.exists() else asset_package.get("asset_dir", "") + "/sem.usda",
                "author SemanticsLabelsAPI token-array labels for segments: " + ", ".join(missing_semantics)
                if missing_semantics
                else "",
            )
        )
        add(
            _result(
                "material-region-bindings",
                "material",
                "blocked" if missing_material_targets or "rel material:binding" not in variants_text else "pass",
                mtl_path.relative_to(root).as_posix() if mtl_path.exists() else asset_package.get("asset_dir", "") + "/mtl.usda",
                "bind material targets for appearance segments" if missing_material_targets or "rel material:binding" not in variants_text else "",
            )
        )
    elif "texture" in requested:
        add(
            _result(
                "segmentation-segments",
                "segmentation",
                "blocked",
                asset_package.get("asset_dir", "") + "/textures/segments",
                "create material-region segments before texture synthesis",
            )
        )
    if "texture" in requested:
        missing_textures = [path for path in texture_outputs if not path.exists()]
        if not texture_outputs:
            missing_textures = [asset_dir / "textures"]
        real_texture_status = str(asset_package.get("texture_generation_status", "")) in {"generated", "validated"}
        texture_repair_action = ""
        if missing_textures:
            texture_repair_action = "generate texture map files after material binding and UV readiness"
        elif not real_texture_status:
            texture_repair_action = "run texture synthesis backend and record generated texture provenance"
        add(
            _result(
                "texture-assets",
                "texture",
                "blocked" if missing_textures or not real_texture_status else "pass",
                asset_package.get("asset_dir", "") + "/textures",
                texture_repair_action,
            )
        )
    elif texture_outputs:
        real_texture_status = str(asset_package.get("texture_generation_status", "")) in {"generated", "validated"}
        add(
            _result(
                "texture-assets",
                "texture",
                "pass" if real_texture_status else "blocked",
                asset_package.get("asset_dir", "") + "/textures",
                "" if real_texture_status else "run texture synthesis backend and record generated texture provenance",
            )
        )

    package_entry_records = [
        item
        for item in asset_package.get("files", [])
        if isinstance(item, dict) and str(item.get("path", "")).startswith("packaged/")
    ]
    missing_package_entries: list[Path] = []
    mismatched_package_entries: list[str] = []
    declared_package_inventory: dict[str, str] = {}
    package_prefix = package_path.parent.relative_to(root) if package_path.is_file() else None
    for item in package_entry_records:
        raw_path = Path(str(item.get("path") or ""))
        target = (root / raw_path).resolve(strict=False)
        try:
            target.relative_to(root.resolve(strict=True))
            relative_to_package = raw_path.relative_to(package_prefix).as_posix() if package_prefix is not None else ""
        except ValueError:
            mismatched_package_entries.append(str(item.get("path") or ""))
            continue
        declared_sha256 = str(item.get("sha256") or "")
        if not target.is_file():
            missing_package_entries.append(target)
            continue
        if not SHA256_PATTERN.fullmatch(declared_sha256) or sha256_file(target) != declared_sha256:
            mismatched_package_entries.append(raw_path.as_posix())
        if relative_to_package in declared_package_inventory:
            mismatched_package_entries.append(raw_path.as_posix())
        declared_package_inventory[relative_to_package] = declared_sha256
    package_dependency_closure = (
        _package_dependency_closure(package_path, package_path.parent)
        if package_path.is_file()
        else {
            "status": "blocked",
            "root": str(asset_package.get("package_path") or ""),
            "files": [],
            "package_dependency_fingerprint": "",
            "package_inventory": [],
            "unresolved": [],
            "external": [],
            "missing": [str(asset_package.get("package_path") or "")],
            "escaping": [],
            "blocked_reasons": ["packaged USD root does not exist"],
        }
    )
    closure_report_path = root / "reports" / "package-dependency-closure.json"
    closure_report_path.parent.mkdir(parents=True, exist_ok=True)
    closure_report_path.write_text(
        json.dumps(package_dependency_closure, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    package_blockers = [
        *[f"package entry is missing: {path.relative_to(root).as_posix()}" for path in missing_package_entries],
        *[f"package entry digest or path is invalid: {path}" for path in sorted(set(mismatched_package_entries))],
        *package_dependency_closure["blocked_reasons"],
    ]
    if package_path.is_file():
        expected_root_record = {"path": package_path.name, "sha256": sha256_file(package_path)}
        if expected_root_record not in package_dependency_closure["package_inventory"]:
            package_blockers.append("canonical package inventory does not include the packaged USD root")
        actual_package_inventory = {
            str(item.get("path") or ""): str(item.get("sha256") or "")
            for item in package_dependency_closure["package_inventory"]
            if isinstance(item, dict)
        }
        if declared_package_inventory != actual_package_inventory:
            package_blockers.append("declared packaged files do not exactly match the canonical package inventory")
    add(
        _result(
            "package-self-contained",
            "package",
            "blocked" if package_blockers else "pass",
            closure_report_path.relative_to(root).as_posix(),
            "; ".join(package_blockers) if package_blockers else "",
            dependency_count=len(package_dependency_closure["files"]),
        )
    )

    conformance_status = (
        "pass"
        if simready_profile["profile_version_status"] == "pinned"
        and all(item["status"] == "pass" for item in requirement_results)
        and all(item["status"] == "pass" for item in feature_results)
        and openusd_compliance["status"] == "pass"
        and (not articulation_required or articulation_schema["status"] == "pass")
        and official_validator["status"] == "pass"
        and runtime_validation["status"] == "pass"
        else "blocked"
    )
    simready_conformance = {
        "status": conformance_status,
        "certification_claimed": False,
        "claim_scope": "candidate conformance evidence only; not an official certification",
        "profile": simready_profile,
        "features": feature_results,
        "requirements": requirement_results,
        "official_validator": official_validator,
        "openusd_compliance": {
            **openusd_compliance,
            "report_path": openusd_report_path.relative_to(root).as_posix(),
            "report_sha256": sha256_file(openusd_report_path),
        },
        "articulation_schema": articulation_schema,
        "runtime_validation": runtime_validation,
        "semantic_label_requirement_uri": SEMANTIC_LABEL_REQUIREMENT_URI,
    }
    report = {
        "asset_id": asset_id,
        "status": "validated" if not blockers else "blocked",
        "simready_profile": simready_profile,
        "simready_conformance": simready_conformance,
        "validation_results": results,
        "blocked_reasons": blockers,
        "validated_file_count": len([path for path in required_paths if path.exists()]),
        "package_path": asset_package.get("package_path", ""),
        "package_dependency_closure": {
            **package_dependency_closure,
            "report_path": closure_report_path.relative_to(root).as_posix(),
            "report_sha256": sha256_file(closure_report_path),
        },
    }
    report_path = root / "reports" / "generated-asset-validation-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    report["report_path"] = report_path.relative_to(root).as_posix()
    report["report_sha256"] = sha256_file(report_path)
    return report
