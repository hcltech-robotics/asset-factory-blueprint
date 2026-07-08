from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from asset_factory_blueprint.schemas.common import RunPlan, StageAttempt, StageAttemptIdentity, StagePlan
from asset_factory_blueprint.security import ensure_path_component
from asset_factory_blueprint.utils.checksums import sha256_file
from asset_factory_blueprint.utils.ids import stage_attempt_id


class WorkspaceBusyError(RuntimeError):
    """Raised when another process owns the project workspace lease."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: str | Path, payload: Any) -> Path:
    """Atomically replace a JSON file using a temporary file in the same directory."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=False, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def immutable_write_json(path: str | Path, payload: Any) -> Path:
    """Create an immutable-by-contract JSON record and refuse to replace it."""

    target = Path(path)
    if target.exists():
        raise FileExistsError(f"immutable record already exists: {target}")
    return atomic_write_json(target, payload)


def append_event(project_dir: str | Path, run_id: str, event_type: str, payload: dict[str, Any]) -> Path:
    """Append one durable event to the run journal.

    Callers hold the workspace lease, which serialises append operations.
    """

    ensure_path_component(run_id, "run ID")
    path = resolve_within(project_dir, Path("runs") / run_id / "events.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "event_id": f"{run_id}:{event_type}:{utc_now()}",
        "event_type": event_type,
        "occurred_at": utc_now(),
        "run_id": run_id,
        "payload": payload,
    }
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return path


@contextmanager
def workspace_lease(
    project_dir: str | Path,
    run_id: str,
    *,
    stale_after: timedelta = timedelta(hours=4),
) -> Iterator[Path]:
    """Hold an OS-backed exclusive lock for project mutations.

    The lock file is persistent and excluded from project checksums. The OS
    releases the advisory lock if the process exits, so no stale-file takeover
    or unlink race is required. ``stale_after`` is retained for API
    compatibility but is intentionally not used for lock ownership.
    """

    del stale_after
    root = Path(project_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".afb-workspace.lock"
    stream = lock_path.open("a+b")
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.seek(0)
            raw_owner = stream.read().lstrip(b"\0").decode("utf-8", errors="replace")
            try:
                owner_record = json.loads(raw_owner) if raw_owner else {}
            except json.JSONDecodeError:
                owner_record = {}
            owner = owner_record.get("run_id") or owner_record.get("pid") or "unknown"
            raise WorkspaceBusyError(f"project workspace is leased by {owner}") from exc
        token = {
            "run_id": run_id,
            "pid": os.getpid(),
            "created_at": utc_now(),
        }
        stream.seek(0)
        stream.truncate()
        stream.write(json.dumps(token, ensure_ascii=True).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
        yield lock_path
    finally:
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        stream.close()


def resolve_within(root: str | Path, candidate: str | Path, *, must_exist: bool = False) -> Path:
    """Resolve a path and reject parent traversal and symlink escapes."""

    root_path = Path(root).resolve(strict=False)
    raw = Path(candidate)
    joined = raw if raw.is_absolute() else root_path / raw
    resolved = joined.resolve(strict=must_exist)
    if resolved != root_path and root_path not in resolved.parents:
        raise ValueError(f"path escapes authorised root: {candidate}")
    return resolved


def write_run_snapshot(
    project_dir: str | Path,
    plan: RunPlan,
    request_payload: dict[str, Any],
    provenance: dict[str, Any],
) -> Path:
    """Write the immutable inputs and plan for one run."""

    ensure_path_component(plan.run_id, "run ID")
    run_dir = resolve_within(project_dir, Path("runs") / plan.run_id)
    if run_dir.exists():
        raise FileExistsError(f"run snapshot already exists: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=False)
    immutable_write_json(run_dir / "request.json", request_payload)
    immutable_write_json(run_dir / "plan.json", plan.model_dump(mode="json"))
    immutable_write_json(run_dir / "provenance.json", provenance)
    return run_dir


def _next_attempt_number(project_dir: Path, run_id: str, stage_id: str) -> int:
    ensure_path_component(run_id, "run ID")
    ensure_path_component(stage_id, "stage ID")
    attempts_dir = resolve_within(project_dir, Path("runs") / run_id / "attempts" / stage_id)
    if not attempts_dir.exists():
        return 1
    numbers: list[int] = []
    for path in attempts_dir.glob("*/*.json"):
        if path.stem != path.parent.name:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            numbers.append(int(payload["identity"]["attempt_number"]))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return max(numbers, default=0) + 1


def _snapshot_output(source: Path, destination: Path, project_root: Path) -> dict[str, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"immutable attempt output already exists: {destination}")
    temporary = destination.with_name(f".{destination.name}.tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)
    return {
        "path": destination.relative_to(project_root).as_posix(),
        "sha256": sha256_file(destination),
    }


def execute_stage_plan(
    project_dir: str | Path,
    plan: RunPlan,
    request_digest: str,
    producer: Callable[[StagePlan], dict[str, Any]],
    *,
    dry_run: bool,
    provenance_id: str | None = None,
) -> list[dict[str, Any]]:
    """Execute a typed stage plan and record immutable terminal attempts.

    The producer remains stage-specific. This function owns ordering,
    precondition diagnostics, attempt identity, output snapshots and the event
    journal. A blocked stage still emits its diagnostic manifest and attempt.
    """

    root = Path(project_dir)
    ensure_path_component(plan.run_id, "run ID")
    available = {"run-request"}
    results: list[dict[str, Any]] = []
    for planned_stage in plan.stages:
        ensure_path_component(planned_stage.id, "stage ID")
        attempt_number = _next_attempt_number(root, plan.run_id, planned_stage.id)
        attempt_id = stage_attempt_id(plan.run_id, planned_stage.id, attempt_number, request_digest)
        started_at = utc_now()
        missing_inputs = sorted(set(planned_stage.consumes) - available)
        stage = planned_stage.model_copy(deep=True)
        if missing_inputs:
            stage.blocked_reasons.extend(f"required deliverable is unavailable: {item}" for item in missing_inputs)
        if attempt_number > stage.max_attempts:
            stage.blocked_reasons.append(
                f"stage attempt limit exceeded: {attempt_number} requested, maximum is {stage.max_attempts}"
            )
        resource_checks: dict[str, dict[str, Any]] = {}
        for resource_name, environment_name in (("cpu", "AFB_AVAILABLE_CPUS"), ("gpu", "AFB_AVAILABLE_GPUS")):
            required = int(stage.resources.get(resource_name, 0) or 0)
            raw_available = os.environ.get(environment_name, "")
            available_count = int(raw_available) if raw_available.isdigit() else None
            resource_checks[resource_name] = {
                "required": required,
                "available": available_count,
                "status": "unknown" if available_count is None else "pass" if available_count >= required else "blocked",
            }
            if available_count is not None and available_count < required:
                stage.blocked_reasons.append(
                    f"insufficient {resource_name} resources: requires {required}, available {available_count}"
                )
        append_event(
            root,
            plan.run_id,
            "stage_attempt_started",
            {
                "attempt_id": attempt_id,
                "stage_id": stage.id,
                "attempt_number": attempt_number,
                "consumes": stage.consumes,
                "missing_inputs": missing_inputs,
            },
        )
        error_codes: list[str] = []
        attempt_limit_exceeded = attempt_number > stage.max_attempts
        try:
            result = (
                {
                    "stage_id": stage.id,
                    "status": "blocked",
                    "manifest_path": None,
                    "report_path": None,
                    "manifest_valid": True,
                    "blocked_reasons": list(stage.blocked_reasons),
                }
                if attempt_limit_exceeded
                else producer(stage)
            )
        except Exception as exc:
            result = {
                "stage_id": stage.id,
                "status": "blocked",
                "manifest_path": None,
                "report_path": None,
                "manifest_valid": False,
                "blocked_reasons": [f"stage producer failed: {type(exc).__name__}: {exc}"],
            }
            error_codes.append("stage_producer_exception")
        ensure_path_component(attempt_id, "attempt ID")
        attempt_dir = resolve_within(root, Path("runs") / plan.run_id / "attempts" / stage.id / attempt_id)
        snapshots: dict[str, dict[str, str]] = {}
        for key in ("manifest_path", "report_path"):
            raw_path = result.get(key)
            if not raw_path:
                continue
            try:
                source = resolve_within(root, str(raw_path), must_exist=True)
                snapshots[key] = _snapshot_output(
                    source,
                    attempt_dir / f"{key.removesuffix('_path')}.json",
                    root,
                )
            except (OSError, ValueError) as exc:
                error_codes.append("attempt_output_snapshot_failed")
                result.setdefault("blocked_reasons", []).append(f"attempt output snapshot failed: {exc}")
        raw_status = str(result.get("execution_status") or result.get("status") or "failed").lower()
        terminal_status = {
            "failed": "failed",
            "cancelled": "cancelled",
            "timed_out": "timed_out",
            "timeout": "timed_out",
            "blocked": "blocked",
            "proposal": "succeeded",
            "validated": "succeeded",
            "released": "succeeded",
            "pass": "succeeded",
            "passed": "succeeded",
            "success": "succeeded",
            "succeeded": "succeeded",
            "generated": "succeeded",
        }.get(raw_status, "failed")
        if terminal_status == "failed" and raw_status not in {"failed"}:
            error_codes.append("unknown_terminal_status")
        if not result.get("manifest_valid", True):
            terminal_status = "failed"
            error_codes.append("manifest_invalid")
        if "attempt_output_snapshot_failed" in error_codes:
            terminal_status = "failed"
        produced_ids = tuple(stage.produces) if terminal_status == "succeeded" else ()
        attempt = StageAttempt(
            identity=StageAttemptIdentity(
                attempt_id=attempt_id,
                run_id=plan.run_id,
                stage_id=stage.id,
                attempt_number=attempt_number,
                request_digest=request_digest,
            ),
            status=terminal_status,
            started_at=started_at,
            completed_at=utc_now(),
            consumed_ids=tuple(stage.consumes),
            produced_ids=produced_ids,
            evidence_ids=tuple(snapshots),
            error_codes=tuple(sorted(set(error_codes))),
            provenance_id=provenance_id,
            extensions={
                "dry_run": dry_run,
                "snapshots": snapshots,
                "blocked_reasons": result.get("blocked_reasons", stage.blocked_reasons),
                "validation_gates": stage.validation_gates,
                "precondition_results": {
                    "declared": stage.preconditions,
                    "missing_deliverables": missing_inputs,
                    "status": "blocked" if missing_inputs else "pass",
                },
                "resource_checks": resource_checks,
                "execution_mode": stage.execution_mode,
            },
        )
        immutable_write_json(attempt_dir / f"{attempt_id}.json", attempt.model_dump(mode="json"))
        if terminal_status == "succeeded":
            available.update(stage.produces)
        append_event(
            root,
            plan.run_id,
            "stage_attempt_completed",
            {
                "attempt_id": attempt_id,
                "stage_id": stage.id,
                "status": terminal_status,
                "produces": list(produced_ids),
                "error_codes": sorted(set(error_codes)),
            },
        )
        result["attempt_id"] = attempt_id
        result["attempt_path"] = (attempt_dir / f"{attempt_id}.json").relative_to(root).as_posix()
        result["execution_status"] = terminal_status
        results.append(result)
    return results
