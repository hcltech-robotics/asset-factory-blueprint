from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from asset_factory_blueprint.execution import atomic_write_json
from asset_factory_blueprint.utils.checksums import sha256_file
from asset_factory_blueprint.utils.package_fingerprint import package_inventory_fingerprint


__all__ = [
    "OmniAssetValidatorConfig",
    "attest_official_profile_report",
    "normalise_official_profile_report",
    "run_official_profile_validation",
    "verify_official_profile_report_attestation",
    "write_official_profile_report",
]


VALIDATOR_ID = "nvidia-omni-asset-validator"
VALIDATOR_DOCUMENTATION_URI = (
    "https://docs.omniverse.nvidia.com/kit/docs/asset-validator/latest/source/python/docs/cli.html"
)
PASS_STATUSES = {"pass", "passed", "validated", "conformant"}
PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
PROFILE_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9A-Za-z-]+){1,3}(?:[-+][0-9A-Za-z.-]+)?$")
VERSION_SEARCH_PATTERN = re.compile(
    r"(?<![0-9])((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)(?![0-9])"
)
ATTESTATION_SCHEMA_VERSION = "1.0.0"
ATTESTATION_ALGORITHM = "HMAC-SHA256"
ATTESTATION_FIELDS = {"schema_version", "algorithm", "key_id", "payload_sha256", "signature"}
ATTESTATION_KEY_ID_CONTEXT = b"asset-factory-validation-key-id-v1"
ATTESTATION_SIGNATURE_CONTEXT = b"asset-factory-official-validator-report-v1"


@dataclass(frozen=True)
class OmniAssetValidatorConfig:
    """Administrator-controlled execution limits for the official validator."""

    executable: str
    executable_sha256: str = ""
    attestation_secret: str = field(default="", repr=False)
    timeout_seconds: float = 600.0
    max_process_output_bytes: int = 1_048_576
    max_report_bytes: int = 16_777_216

    @classmethod
    def from_environment(cls) -> OmniAssetValidatorConfig:
        return cls(
            executable=os.environ.get("AFB_ASSET_VALIDATOR_EXECUTABLE", "").strip(),
            executable_sha256=os.environ.get("AFB_ASSET_VALIDATOR_EXECUTABLE_SHA256", "").strip(),
            attestation_secret=os.environ.get("AFB_VALIDATION_ATTESTATION_SECRET", ""),
            timeout_seconds=_environment_float("AFB_ASSET_VALIDATOR_TIMEOUT_SECONDS", 600.0),
            max_process_output_bytes=_environment_int("AFB_ASSET_VALIDATOR_MAX_OUTPUT_BYTES", 1_048_576),
            max_report_bytes=_environment_int("AFB_ASSET_VALIDATOR_MAX_REPORT_BYTES", 16_777_216),
        )


@dataclass(frozen=True)
class _ProcessResult:
    return_code: int | None
    stdout: bytes
    stderr: bytes
    observed_output_bytes: int
    timed_out: bool
    output_limit_exceeded: bool
    report_limit_exceeded: bool
    launch_error: str


class _BoundedCapture:
    def __init__(self, limit: int, process: subprocess.Popen[bytes]) -> None:
        self._limit = limit
        self._process = process
        self._lock = threading.Lock()
        self._buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self.observed_bytes = 0
        self.limit_exceeded = False

    def consume(self, stream_name: str, chunk: bytes) -> None:
        should_kill = False
        with self._lock:
            previous_total = self.observed_bytes
            self.observed_bytes += len(chunk)
            remaining = max(0, self._limit - previous_total)
            if remaining:
                self._buffers[stream_name].extend(chunk[:remaining])
            if self.observed_bytes > self._limit and not self.limit_exceeded:
                self.limit_exceeded = True
                should_kill = True
        if should_kill:
            _kill_process(self._process)

    def value(self, stream_name: str) -> bytes:
        with self._lock:
            return bytes(self._buffers[stream_name])


def _environment_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _environment_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


def _attestation_secret_bytes(secret: str) -> bytes:
    if not isinstance(secret, str):
        raise ValueError("validation attestation secret must be a string")
    secret_bytes = secret.encode("utf-8")
    if len(secret_bytes) < 32:
        raise ValueError("AFB_VALIDATION_ATTESTATION_SECRET must contain at least 32 UTF-8 bytes")
    return secret_bytes


def _canonical_attestation_payload(payload: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "attestation"}
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _attestation_key_id(secret_bytes: bytes) -> str:
    digest = hmac.new(secret_bytes, ATTESTATION_KEY_ID_CONTEXT, hashlib.sha256).hexdigest()
    return "afb-validation-" + digest[:32]


def attest_official_profile_report(payload: dict[str, Any], secret: str) -> dict[str, Any]:
    """Attach deterministic HMAC evidence to one final normalised report."""

    secret_bytes = _attestation_secret_bytes(secret)
    unsigned = {key: value for key, value in payload.items() if key != "attestation"}
    canonical_payload = _canonical_attestation_payload(unsigned)
    payload_sha256 = hashlib.sha256(canonical_payload).hexdigest()
    signature = hmac.new(
        secret_bytes,
        ATTESTATION_SIGNATURE_CONTEXT + b"\0" + canonical_payload,
        hashlib.sha256,
    ).hexdigest()
    return {
        **unsigned,
        "attestation": {
            "schema_version": ATTESTATION_SCHEMA_VERSION,
            "algorithm": ATTESTATION_ALGORITHM,
            "key_id": _attestation_key_id(secret_bytes),
            "payload_sha256": payload_sha256,
            "signature": signature,
        },
    }


def verify_official_profile_report_attestation(payload: dict[str, Any], secret: str) -> list[str]:
    """Return all HMAC-attestation problems without mutating the supplied report."""

    try:
        secret_bytes = _attestation_secret_bytes(secret)
    except ValueError as exc:
        return [str(exc)]
    attestation = payload.get("attestation")
    if not isinstance(attestation, dict):
        return ["official validator report attestation is missing"]
    problems = []
    if set(attestation) != ATTESTATION_FIELDS:
        problems.append("official validator report attestation has an unexpected shape")
    if attestation.get("schema_version") != ATTESTATION_SCHEMA_VERSION:
        problems.append("official validator report attestation schema version is unsupported")
    if attestation.get("algorithm") != ATTESTATION_ALGORITHM:
        problems.append("official validator report attestation algorithm is unsupported")
    expected_key_id = _attestation_key_id(secret_bytes)
    reported_key_id = str(attestation.get("key_id") or "")
    if not hmac.compare_digest(reported_key_id, expected_key_id):
        problems.append("official validator report attestation key ID does not match the configured secret")
    try:
        canonical_payload = _canonical_attestation_payload(payload)
    except (TypeError, ValueError) as exc:
        problems.append(f"official validator report cannot be canonicalised for attestation: {exc}")
        return problems
    expected_payload_sha256 = hashlib.sha256(canonical_payload).hexdigest()
    reported_payload_sha256 = str(attestation.get("payload_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", reported_payload_sha256) or not hmac.compare_digest(
        reported_payload_sha256,
        expected_payload_sha256,
    ):
        problems.append("official validator report attestation payload digest does not match")
    expected_signature = hmac.new(
        secret_bytes,
        ATTESTATION_SIGNATURE_CONTEXT + b"\0" + canonical_payload,
        hashlib.sha256,
    ).hexdigest()
    reported_signature = str(attestation.get("signature") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", reported_signature) or not hmac.compare_digest(
        reported_signature,
        expected_signature,
    ):
        problems.append("official validator report attestation signature does not match")
    return problems


def _attest_passing_report(payload: dict[str, Any], secret: str) -> dict[str, Any]:
    if payload.get("status") != "pass":
        return payload
    try:
        return attest_official_profile_report(payload, secret)
    except (TypeError, ValueError) as exc:
        blocked = {key: value for key, value in payload.items() if key != "attestation"}
        problems = list(blocked.get("problems") or [])
        _append_problem(problems, f"official validator report attestation failed: {exc}")
        blocked["status"] = "blocked"
        blocked["problems"] = problems
        blocked["reason"] = "; ".join(problems)
        return blocked


def _append_problem(problems: list[str], problem: str) -> None:
    if problem and problem not in problems:
        problems.append(problem)


def _status_passes(value: Any) -> bool:
    return str(value or "").strip().lower() in PASS_STATUSES


def _normalised_status(value: Any) -> str:
    return "pass" if _status_passes(value) else "blocked"


def _validate_profile_reference(profile_id: str, profile_version: str) -> list[str]:
    problems = []
    if not PROFILE_ID_PATTERN.fullmatch(profile_id):
        problems.append("Profile ID must be a non-empty versionable identifier")
    if not PROFILE_VERSION_PATTERN.fullmatch(profile_version) or profile_version.lower() in {
        "latest",
        "main",
        "unresolved",
    }:
        problems.append("Profile version must be an exact pinned version identifier")
    return problems


def _validate_config(config: OmniAssetValidatorConfig) -> list[str]:
    problems = []
    if not config.executable:
        problems.append("AFB_ASSET_VALIDATOR_EXECUTABLE is not configured")
    pinned_digest = config.executable_sha256.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", pinned_digest):
        problems.append("AFB_ASSET_VALIDATOR_EXECUTABLE_SHA256 must be an exact lowercase SHA-256 digest")
    try:
        _attestation_secret_bytes(config.attestation_secret)
    except ValueError as exc:
        problems.append(str(exc))
    else:
        secret_text = config.attestation_secret.strip()
        public_digests = {pinned_digest, f"sha256:{pinned_digest}"}
        if secret_text == config.executable or secret_text.lower() in public_digests:
            problems.append("validation attestation secret must be independent of executable configuration values")
    if not 1.0 <= config.timeout_seconds <= 3600.0:
        problems.append("validator timeout must be between 1 and 3600 seconds")
    if not 1 <= config.max_process_output_bytes <= 67_108_864:
        problems.append("validator process output limit must be between 1 byte and 64 MiB")
    if not 1 <= config.max_report_bytes <= 268_435_456:
        problems.append("validator report limit must be between 1 byte and 256 MiB")
    return problems


def _validate_package_binding(fingerprint: str, inventory: list[dict[str, str]]) -> list[str]:
    problems: list[str] = []
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint):
        problems.append("package dependency fingerprint is missing or malformed")
    if not inventory:
        problems.append("package inventory is missing")
        return problems
    normalised: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in inventory:
        path = str(item.get("path") or "")
        sha256 = str(item.get("sha256") or "").lower()
        if not path or Path(path).is_absolute() or ".." in Path(path).parts:
            problems.append("package inventory contains an invalid relative path")
            continue
        if path in seen:
            problems.append(f"package inventory path is duplicated: {path}")
            continue
        seen.add(path)
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            problems.append(f"package inventory digest is malformed: {path}")
            continue
        normalised.append({"path": path, "sha256": sha256})
    if normalised != sorted(normalised, key=lambda item: item["path"]):
        problems.append("package inventory must be sorted by relative path")
    digest = hashlib.sha256()
    for item in normalised:
        digest.update(item["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(item["sha256"].encode("ascii"))
        digest.update(b"\n")
    if fingerprint and fingerprint != f"sha256:{digest.hexdigest()}":
        problems.append("package dependency fingerprint does not match the declared inventory")
    return problems


def _portable_file_label(path: Path, fallback: str) -> str:
    label = path.name.strip()
    return label if label not in {"", ".", ".."} else fallback


def _resolve_executable(configured: str) -> tuple[Path | None, str]:
    if not configured:
        return None, "validator executable is not configured"
    has_path_separator = any(separator in configured for separator in ("/", "\\"))
    if has_path_separator or Path(configured).is_absolute():
        candidate = Path(configured).expanduser()
        if not candidate.exists():
            return None, "configured validator executable does not exist"
        resolved = candidate.resolve()
    else:
        located = shutil.which(configured)
        if not located:
            return None, "configured validator executable was not found on PATH"
        resolved = Path(located).resolve()
    if not resolved.is_file():
        return None, "configured validator executable is not a file"
    if os.name == "nt" and resolved.suffix.lower() not in {".exe", ".com"}:
        return None, "configured validator must be a native executable on Windows"
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        return None, "configured validator file is not executable"
    return resolved, ""


def _composition_fingerprint(path: Path) -> tuple[str, str]:
    try:
        from pxr import Usd
    except Exception as exc:
        return "", f"OpenUSD runtime is unavailable for composition fingerprinting: {exc}"
    stage = Usd.Stage.Open(str(path))
    if stage is None:
        return "", "composed USD root could not be opened for fingerprinting"
    layer_hashes = []
    unfingerprintable_layers = 0
    for layer in stage.GetUsedLayers():
        if bool(layer.anonymous):
            continue
        real_path = Path(str(layer.realPath or ""))
        if real_path.is_file():
            layer_hashes.append(sha256_file(real_path))
        else:
            unfingerprintable_layers += 1
    if unfingerprintable_layers:
        return "", f"composed USD layer stack contains {unfingerprintable_layers} unfingerprintable persistent layers"
    if not layer_hashes:
        return "", "composed USD layer stack has no fingerprintable files"
    digest = hashlib.sha256()
    for layer_hash in sorted(layer_hashes):
        digest.update(layer_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest(), ""


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        taskkill = system_root / "System32" / "taskkill.exe"
        if taskkill.is_file():
            try:
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5.0,
                    check=False,
                    shell=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def _read_process_stream(stream_name: str, stream: BinaryIO, capture: _BoundedCapture) -> None:
    try:
        try:
            while True:
                chunk = stream.read(65_536)
                if not chunk:
                    return
                capture.consume(stream_name, chunk)
        except OSError:
            return
    finally:
        stream.close()


def _validator_process_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("AFB_VALIDATION_ATTESTATION_SECRET", None)
    return environment


def _run_bounded_process(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    max_output_bytes: int,
    monitored_report: Path | None = None,
    max_report_bytes: int = 0,
) -> _ProcessResult:
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=_validator_process_environment(),
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        return _ProcessResult(None, b"", b"", 0, False, False, False, str(exc))
    if process.stdout is None or process.stderr is None:
        _kill_process(process)
        return _ProcessResult(None, b"", b"", 0, False, False, False, "validator output pipes were unavailable")

    capture = _BoundedCapture(max_output_bytes, process)
    stdout_reader = threading.Thread(
        target=_read_process_stream,
        args=("stdout", process.stdout, capture),
        name="asset-validator-stdout",
        daemon=True,
    )
    stderr_reader = threading.Thread(
        target=_read_process_stream,
        args=("stderr", process.stderr, capture),
        name="asset-validator-stderr",
        daemon=True,
    )
    stdout_reader.start()
    stderr_reader.start()

    started = time.monotonic()
    timed_out = False
    report_limit_exceeded = False
    while process.poll() is None:
        if time.monotonic() - started > timeout_seconds:
            timed_out = True
            _kill_process(process)
            break
        if monitored_report is not None and monitored_report.exists():
            try:
                if monitored_report.stat().st_size > max_report_bytes:
                    report_limit_exceeded = True
                    _kill_process(process)
                    break
            except OSError:
                pass
        time.sleep(0.02)

    try:
        return_code = process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        _kill_process(process)
        try:
            return_code = process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            return_code = process.poll()
    stdout_reader.join(timeout=5.0)
    stderr_reader.join(timeout=5.0)
    return _ProcessResult(
        return_code=return_code,
        stdout=capture.value("stdout"),
        stderr=capture.value("stderr"),
        observed_output_bytes=capture.observed_bytes,
        timed_out=timed_out,
        output_limit_exceeded=capture.limit_exceeded,
        report_limit_exceeded=report_limit_exceeded,
        launch_error="",
    )


def _extract_validator_version(result: _ProcessResult) -> str:
    text = (result.stdout + b"\n" + result.stderr).decode("utf-8", errors="replace")
    labelled_versions = []
    for line in text.splitlines():
        lowered = line.lower()
        if any(
            label in lowered
            for label in ("usd-validation-nvidia", "usd_validation_nvidia", "nvidia_usd_validate", "asset validator")
        ):
            labelled_versions.extend(VERSION_SEARCH_PATTERN.findall(line))
    if len(set(labelled_versions)) == 1:
        return labelled_versions[0]
    all_versions = set(VERSION_SEARCH_PATTERN.findall(text))
    return next(iter(all_versions)) if len(all_versions) == 1 else ""


def _sanitised_excerpt(data: bytes, replacements: dict[str, str], limit: int = 4096) -> str:
    text = data.decode("utf-8", errors="replace")
    for value, replacement in replacements.items():
        if value:
            text = text.replace(value, replacement)
    return text[:limit]


def _process_evidence(
    result: _ProcessResult,
    *,
    replacements: dict[str, str] | None = None,
) -> dict[str, Any]:
    replacements = replacements or {}
    return {
        "exit_code": result.return_code,
        "timed_out": result.timed_out,
        "output_limit_exceeded": result.output_limit_exceeded,
        "report_limit_exceeded": result.report_limit_exceeded,
        "observed_output_bytes": result.observed_output_bytes,
        "captured_stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
        "captured_stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
        "stdout_excerpt": _sanitised_excerpt(result.stdout, replacements),
        "stderr_excerpt": _sanitised_excerpt(result.stderr, replacements),
        "launch_error": result.launch_error,
    }


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _load_raw_report(payload: bytes) -> tuple[dict[str, Any] | None, str]:
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_json_object)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        return None, f"validator JSON report could not be decoded: {exc}"
    if not isinstance(value, dict):
        return None, "validator JSON report root must be an object"
    return value, ""


def _read_file_bounded(path: Path, maximum_bytes: int) -> tuple[bytes, str]:
    try:
        with path.open("rb") as stream:
            payload = stream.read(maximum_bytes + 1)
    except OSError as exc:
        return b"", f"validator JSON report could not be read: {exc}"
    if len(payload) > maximum_bytes:
        return b"", "validator JSON report exceeds its size limit"
    return payload, ""


def normalise_official_profile_report(
    raw_report: dict[str, Any] | None,
    *,
    profile_id: str,
    profile_version: str,
    validator_version: str,
    usd_path: str,
    usd_sha256: str,
    composition_fingerprint: str,
    raw_report_path: str,
    raw_report_sha256: str,
    execution: dict[str, Any],
    preflight_problems: list[str] | None = None,
    package_dependency_fingerprint: str = "",
    package_inventory: list[dict[str, str]] | None = None,
    package_problems: list[str] | None = None,
) -> dict[str, Any]:
    """Convert NVIDIA's Profile tree into the strict evidence contract used by the factory."""

    package_inventory = list(package_inventory or [])
    problems = [
        *list(preflight_problems or []),
        *list(package_problems or []),
        *_validate_package_binding(package_dependency_fingerprint, package_inventory),
    ]
    feature_results: list[dict[str, Any]] = []
    requirement_records: dict[tuple[str, str], dict[str, Any]] = {}
    versions_by_requirement: dict[str, set[str]] = {}

    if not validator_version:
        _append_problem(problems, "validator version is missing")
    if raw_report is None:
        _append_problem(problems, "validator JSON report is unavailable")
    else:
        if not _status_passes(raw_report.get("status")):
            _append_problem(problems, "validator report status is not pass")
        profiles = raw_report.get("profiles")
        if not isinstance(profiles, list):
            profiles = []
            _append_problem(problems, "validator report has no per-Profile evidence")
        matches = [
            item
            for item in profiles
            if isinstance(item, dict)
            and str(item.get("id") or "") == profile_id
            and str(item.get("version") or "") == profile_version
        ]
        if len(matches) != 1:
            _append_problem(
                problems,
                "validator report must contain exactly one result for the requested Profile ID and version",
            )
        else:
            profile_result = matches[0]
            if not _status_passes(profile_result.get("status")):
                _append_problem(problems, "requested Profile did not pass")
            features = profile_result.get("features")
            if not isinstance(features, list) or not features:
                features = []
                _append_problem(problems, "requested Profile has no per-Feature evidence")
            seen_features: set[tuple[str, str]] = set()
            for feature in features:
                if not isinstance(feature, dict):
                    _append_problem(problems, "Profile contains a malformed Feature result")
                    continue
                feature_id = str(feature.get("id") or "")
                feature_version = str(feature.get("version") or "")
                feature_key = (feature_id, feature_version)
                if not feature_id or not feature_version:
                    _append_problem(problems, "Feature result is missing its ID or version")
                if feature_key in seen_features:
                    _append_problem(problems, f"Feature result is duplicated: {feature_id}@{feature_version}")
                seen_features.add(feature_key)
                feature_status = _normalised_status(feature.get("status"))
                if feature_status != "pass":
                    _append_problem(problems, f"Feature did not pass: {feature_id or '<missing-id>'}")
                raw_requirements = feature.get("requirements")
                if not isinstance(raw_requirements, list) or not raw_requirements:
                    raw_requirements = []
                    _append_problem(
                        problems,
                        f"Feature has no per-Requirement evidence: {feature_id or '<missing-id>'}",
                    )
                requirement_ids = []
                for requirement in raw_requirements:
                    if not isinstance(requirement, dict):
                        _append_problem(problems, "Feature contains a malformed Requirement result")
                        continue
                    requirement_id = str(requirement.get("code") or requirement.get("id") or "")
                    requirement_version = str(requirement.get("version") or "")
                    if not requirement_id or not requirement_version:
                        _append_problem(problems, "Requirement result is missing its ID or version")
                    requirement_status = _normalised_status(requirement.get("status"))
                    if requirement_status != "pass":
                        _append_problem(
                            problems,
                            f"Requirement did not pass: {requirement_id or '<missing-id>'}",
                        )
                    requirement_ids.append(requirement_id)
                    versions_by_requirement.setdefault(requirement_id, set()).add(requirement_version)
                    key = (requirement_id, requirement_version)
                    existing = requirement_records.get(key)
                    if existing is None:
                        requirement_records[key] = {
                            "requirement_id": requirement_id,
                            "requirement_version": requirement_version,
                            "status": requirement_status,
                            "validator": VALIDATOR_ID,
                            "official_validator": True,
                            "feature_ids": [feature_id] if feature_id else [],
                        }
                    else:
                        if feature_id and feature_id not in existing["feature_ids"]:
                            existing["feature_ids"].append(feature_id)
                        if existing["status"] != requirement_status:
                            existing["status"] = "blocked"
                            _append_problem(
                                problems,
                                f"Requirement has conflicting results: {requirement_id}@{requirement_version}",
                            )
                feature_results.append(
                    {
                        "feature_id": feature_id,
                        "feature_version": feature_version,
                        "status": feature_status,
                        "requirement_ids": sorted(set(requirement_ids)),
                    }
                )

    for requirement_id, versions in versions_by_requirement.items():
        if len(versions) > 1:
            _append_problem(
                problems,
                f"Requirement has multiple versions in the requested Profile: {requirement_id}",
            )
    feature_results.sort(
        key=lambda item: (
            item["feature_id"],
            item["feature_version"],
            item["status"],
            tuple(item["requirement_ids"]),
        )
    )
    requirement_results = sorted(
        requirement_records.values(),
        key=lambda item: (item["requirement_id"], item["requirement_version"]),
    )
    for item in requirement_results:
        item["feature_ids"].sort()
    status = "pass" if not problems else "blocked"
    executable_evidence = execution.get("validator_executable")
    if not isinstance(executable_evidence, dict):
        executable_evidence = {}
    return {
        "schema_version": "1.0.0",
        "status": status,
        "validator_id": VALIDATOR_ID,
        "validator_version": validator_version,
        "validator": {
            "validator_id": VALIDATOR_ID,
            "validator_version": validator_version,
            "documentation_uri": VALIDATOR_DOCUMENTATION_URI,
            "executable_name": str(executable_evidence.get("name") or ""),
            "executable_sha256": str(executable_evidence.get("sha256") or ""),
        },
        "profile_id": profile_id,
        "profile_version": profile_version,
        "profile": {"profile_id": profile_id, "profile_version": profile_version},
        "usd_path": usd_path,
        "usd_sha256": usd_sha256,
        "composition_fingerprint": composition_fingerprint,
        "package_dependency_fingerprint": package_dependency_fingerprint,
        "package_inventory": package_inventory,
        "raw_report_path": raw_report_path,
        "raw_report_sha256": raw_report_sha256,
        "features": feature_results,
        "requirements": requirement_results,
        "execution": execution,
        "problems": problems,
        "reason": "; ".join(problems),
    }


def _blocked_without_execution(
    *,
    usd_path: Path,
    raw_report_path: Path,
    profile_id: str,
    profile_version: str,
    problems: list[str],
    usd_sha256: str = "",
    composition_fingerprint: str = "",
    package_dependency_fingerprint: str = "",
    package_inventory: list[dict[str, str]] | None = None,
    package_problems: list[str] | None = None,
) -> dict[str, Any]:
    return normalise_official_profile_report(
        None,
        profile_id=profile_id,
        profile_version=profile_version,
        validator_version="",
        usd_path=_portable_file_label(usd_path, "asset.usd"),
        usd_sha256=usd_sha256,
        composition_fingerprint=composition_fingerprint,
        raw_report_path=_portable_file_label(raw_report_path, "validator.raw.json"),
        raw_report_sha256="",
        execution={
            "command_contract": [
                "<validator>",
                "--profile",
                f"{profile_id}@{profile_version}",
                "--no-fix",
                "--no-stamp",
                "--json-output",
                "<raw-report>",
                "<asset>",
            ],
            "version_probe": {},
            "validation": {},
        },
        preflight_problems=problems,
        package_dependency_fingerprint=package_dependency_fingerprint,
        package_inventory=package_inventory,
        package_problems=package_problems,
    )


def run_official_profile_validation(
    usd_path: str | Path,
    *,
    profile_id: str,
    profile_version: str,
    raw_report_path: str | Path,
    config: OmniAssetValidatorConfig,
) -> dict[str, Any]:
    """Run the configured NVIDIA validator and return normalised, asset-bound evidence."""

    usd_input = Path(usd_path)
    raw_input = Path(raw_report_path)
    profile_problems = _validate_profile_reference(profile_id, profile_version)
    config_problems = _validate_config(config)
    if not usd_input.exists() or not usd_input.is_file():
        profile_problems.append("composed USD root does not exist or is not a file")
    if profile_problems:
        return _blocked_without_execution(
            usd_path=usd_input,
            raw_report_path=raw_input,
            profile_id=profile_id,
            profile_version=profile_version,
            problems=profile_problems,
        )

    resolved_usd = usd_input.resolve()
    if raw_input.is_symlink():
        return _blocked_without_execution(
            usd_path=usd_input,
            raw_report_path=raw_input,
            profile_id=profile_id,
            profile_version=profile_version,
            problems=["raw validator report path must not be a symbolic link"],
        )
    resolved_raw = raw_input.resolve()
    if resolved_raw == resolved_usd:
        return _blocked_without_execution(
            usd_path=usd_input,
            raw_report_path=raw_input,
            profile_id=profile_id,
            profile_version=profile_version,
            problems=["raw validator report path must not overwrite the USD root"],
        )
    package_root = resolved_usd.parent
    try:
        resolved_raw.relative_to(package_root)
    except ValueError:
        pass
    else:
        return _blocked_without_execution(
            usd_path=usd_input,
            raw_report_path=raw_input,
            profile_id=profile_id,
            profile_version=profile_version,
            problems=["raw validator report path must be outside the immutable package directory"],
        )
    if resolved_raw.exists() and not resolved_raw.is_file():
        return _blocked_without_execution(
            usd_path=usd_input,
            raw_report_path=raw_input,
            profile_id=profile_id,
            profile_version=profile_version,
            problems=["raw validator report path exists and is not a file"],
        )
    resolved_raw.parent.mkdir(parents=True, exist_ok=True)
    resolved_raw.unlink(missing_ok=True)

    usd_sha256 = sha256_file(resolved_usd)
    package_evidence = package_inventory_fingerprint(package_root)
    if package_evidence["status"] != "pass":
        return _blocked_without_execution(
            usd_path=usd_input,
            raw_report_path=raw_input,
            profile_id=profile_id,
            profile_version=profile_version,
            problems=[],
            usd_sha256=usd_sha256,
            package_dependency_fingerprint=str(package_evidence["fingerprint"]),
            package_inventory=list(package_evidence["files"]),
            package_problems=list(package_evidence["blocked_reasons"]),
        )
    package_dependency_fingerprint = str(package_evidence["fingerprint"])
    package_inventory = list(package_evidence["files"])
    composition_fingerprint, fingerprint_error = _composition_fingerprint(resolved_usd)
    if fingerprint_error:
        return _blocked_without_execution(
            usd_path=usd_input,
            raw_report_path=raw_input,
            profile_id=profile_id,
            profile_version=profile_version,
            problems=[fingerprint_error],
            usd_sha256=usd_sha256,
            package_dependency_fingerprint=package_dependency_fingerprint,
            package_inventory=package_inventory,
        )
    if config_problems:
        return _blocked_without_execution(
            usd_path=usd_input,
            raw_report_path=raw_input,
            profile_id=profile_id,
            profile_version=profile_version,
            problems=config_problems,
            usd_sha256=usd_sha256,
            composition_fingerprint=composition_fingerprint,
            package_dependency_fingerprint=package_dependency_fingerprint,
            package_inventory=package_inventory,
        )

    executable, executable_error = _resolve_executable(config.executable)
    if executable is None:
        return _blocked_without_execution(
            usd_path=usd_input,
            raw_report_path=raw_input,
            profile_id=profile_id,
            profile_version=profile_version,
            problems=[executable_error],
            usd_sha256=usd_sha256,
            composition_fingerprint=composition_fingerprint,
            package_dependency_fingerprint=package_dependency_fingerprint,
            package_inventory=package_inventory,
        )
    try:
        executable_sha256 = sha256_file(executable)
    except OSError as exc:
        return _blocked_without_execution(
            usd_path=usd_input,
            raw_report_path=raw_input,
            profile_id=profile_id,
            profile_version=profile_version,
            problems=[f"configured validator executable could not be hashed: {exc}"],
            usd_sha256=usd_sha256,
            composition_fingerprint=composition_fingerprint,
            package_dependency_fingerprint=package_dependency_fingerprint,
            package_inventory=package_inventory,
        )
    if not hmac.compare_digest(executable_sha256, config.executable_sha256.strip()):
        return _blocked_without_execution(
            usd_path=resolved_usd,
            raw_report_path=resolved_raw,
            profile_id=profile_id,
            profile_version=profile_version,
            problems=["validator executable digest does not match AFB_ASSET_VALIDATOR_EXECUTABLE_SHA256"],
            usd_sha256=usd_sha256,
            composition_fingerprint=composition_fingerprint,
            package_dependency_fingerprint=package_dependency_fingerprint,
            package_inventory=package_inventory,
        )

    version_result = _run_bounded_process(
        [str(executable), "--version"],
        cwd=resolved_usd.parent,
        timeout_seconds=min(config.timeout_seconds, 30.0),
        max_output_bytes=min(config.max_process_output_bytes, 65_536),
    )
    validator_version = _extract_validator_version(version_result)
    version_problems = []
    if version_result.launch_error:
        version_problems.append("validator version probe could not launch")
    if version_result.timed_out:
        version_problems.append("validator version probe timed out")
    if version_result.output_limit_exceeded:
        version_problems.append("validator version probe exceeded its output limit")
    if version_result.return_code != 0:
        version_problems.append("validator version probe did not exit successfully")
    if not validator_version:
        version_problems.append("validator version probe did not report a semantic version")
    version_evidence = _process_evidence(version_result)
    if version_problems:
        execution = {
            "command_contract": [
                executable.name,
                "--profile",
                f"{profile_id}@{profile_version}",
                "--no-fix",
                "--no-stamp",
                "--json-output",
                "<raw-report>",
                "<asset>",
            ],
            "validator_executable": {"name": executable.name, "sha256": executable_sha256},
            "version_probe": version_evidence,
            "validation": {},
        }
        return normalise_official_profile_report(
            None,
            profile_id=profile_id,
            profile_version=profile_version,
            validator_version=validator_version,
            usd_path=_portable_file_label(resolved_usd, "asset.usd"),
            usd_sha256=usd_sha256,
            composition_fingerprint=composition_fingerprint,
            raw_report_path=_portable_file_label(resolved_raw, "validator.raw.json"),
            raw_report_sha256="",
            execution=execution,
            preflight_problems=version_problems,
            package_dependency_fingerprint=package_dependency_fingerprint,
            package_inventory=package_inventory,
        )

    raw_payload = b""
    raw_report: dict[str, Any] | None = None
    run_problems = []
    with tempfile.TemporaryDirectory(prefix=".asset-validator-", dir=resolved_raw.parent) as temporary_dir:
        temporary_raw = Path(temporary_dir) / "raw-report.json"
        command = [
            str(executable),
            "--profile",
            f"{profile_id}@{profile_version}",
            "--no-fix",
            "--no-stamp",
            "--json-output",
            str(temporary_raw),
            str(resolved_usd),
        ]
        validation_result = _run_bounded_process(
            command,
            cwd=resolved_usd.parent,
            timeout_seconds=config.timeout_seconds,
            max_output_bytes=config.max_process_output_bytes,
            monitored_report=temporary_raw,
            max_report_bytes=config.max_report_bytes,
        )
        if validation_result.launch_error:
            run_problems.append("validator could not launch")
        if validation_result.timed_out:
            run_problems.append("validator timed out")
        if validation_result.output_limit_exceeded:
            run_problems.append("validator exceeded its process output limit")
        if validation_result.report_limit_exceeded:
            run_problems.append("validator exceeded its JSON report limit")
        if validation_result.return_code != 0:
            run_problems.append("validator did not exit successfully")
        if not temporary_raw.exists():
            run_problems.append("validator did not produce a JSON report")
        elif not validation_result.report_limit_exceeded:
            raw_payload, raw_read_error = _read_file_bounded(temporary_raw, config.max_report_bytes)
            if raw_read_error:
                run_problems.append(raw_read_error)
        if raw_payload:
            _atomic_write_bytes(resolved_raw, raw_payload)
            raw_report, raw_error = _load_raw_report(raw_payload)
            if raw_error:
                run_problems.append(raw_error)

        replacements = {
            str(temporary_raw): "<raw-report>",
            str(resolved_usd): "<asset>",
        }
        validation_evidence = _process_evidence(validation_result, replacements=replacements)

    final_usd_sha256 = sha256_file(resolved_usd)
    final_composition_fingerprint, final_fingerprint_error = _composition_fingerprint(resolved_usd)
    final_package_evidence = package_inventory_fingerprint(package_root)
    if final_usd_sha256 != usd_sha256:
        run_problems.append("validator modified the composed USD root despite no-fix and no-stamp mode")
    if final_fingerprint_error or final_composition_fingerprint != composition_fingerprint:
        run_problems.append("validator changed or invalidated the composed USD layer stack")
    if (
        final_package_evidence["status"] != "pass"
        or final_package_evidence["fingerprint"] != package_dependency_fingerprint
        or final_package_evidence["files"] != package_inventory
    ):
        run_problems.append("validator changed or invalidated the immutable package inventory")
    try:
        final_executable_sha256 = sha256_file(executable)
    except OSError:
        final_executable_sha256 = ""
    if final_executable_sha256 != executable_sha256:
        run_problems.append("validator executable changed during execution")
    raw_report_sha256 = hashlib.sha256(raw_payload).hexdigest() if raw_payload else ""
    execution = {
        "command_contract": [
            executable.name,
            "--profile",
            f"{profile_id}@{profile_version}",
            "--no-fix",
            "--no-stamp",
            "--json-output",
            "<raw-report>",
            "<asset>",
        ],
        "validator_executable": {"name": executable.name, "sha256": executable_sha256},
        "version_probe": version_evidence,
        "validation": validation_evidence,
    }
    normalised = normalise_official_profile_report(
        raw_report,
        profile_id=profile_id,
        profile_version=profile_version,
        validator_version=validator_version,
        usd_path=_portable_file_label(resolved_usd, "asset.usd"),
        usd_sha256=usd_sha256,
        composition_fingerprint=composition_fingerprint,
        raw_report_path=_portable_file_label(resolved_raw, "validator.raw.json"),
        raw_report_sha256=raw_report_sha256,
        execution=execution,
        preflight_problems=run_problems,
        package_dependency_fingerprint=package_dependency_fingerprint,
        package_inventory=package_inventory,
    )
    return _attest_passing_report(normalised, config.attestation_secret)


def write_official_profile_report(path: str | Path, payload: dict[str, Any]) -> Path:
    """Atomically write one normalised Profile evidence report."""

    return atomic_write_json(path, payload)
