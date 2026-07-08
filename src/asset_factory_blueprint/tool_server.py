from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import signal
import sys
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.validators import validator_for

from asset_factory_blueprint import __version__
from asset_factory_blueprint.config import ROOT
from asset_factory_blueprint.dispatcher import list_tools
from asset_factory_blueprint.execution import atomic_write_json, immutable_write_json
from asset_factory_blueprint.security import (
    confine_path,
    service_request_context,
    service_source_roots,
    service_workspace_roots,
)
from asset_factory_blueprint.service_approval import (
    approval_token_digest,
    verify_approval_token,
)
from asset_factory_blueprint.tool_router import route_tool


JSON_MEDIA_TYPE = "application/json"
TERMINAL_JOB_STATES = {"cancelled", "failed", "succeeded"}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def _validate_json_shape(value: Any, *, max_depth: int = 16, max_nodes: int = 10_000) -> None:
    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            raise ValueError(f"JSON value exceeds the {max_nodes} node limit")
        if depth > max_depth:
            raise ValueError(f"JSON value exceeds the {max_depth} level nesting limit")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif item is not None and not isinstance(item, (bool, int, float, str)):
            raise ValueError(f"unsupported JSON value type: {type(item).__name__}")

    visit(value, 0)


def _validate_against_tool_schema(params: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate params against the exact schema published in the tool catalogue."""

    validator_class = validator_for(schema)
    try:
        validator_class.check_schema(schema)
        validator_class(schema, format_checker=FormatChecker()).validate(params)
    except SchemaError as exc:
        raise RuntimeError(f"invalid configured tool schema: {exc.message}") from exc
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path)
        prefix = f"tool param {location}: " if location else "tool params: "
        raise ValueError(prefix + exc.message) from exc


@dataclass(frozen=True)
class ToolServerConfig:
    host: str = "127.0.0.1"
    port: int = 8181
    catalogue_path: str = "/tools"
    max_request_bytes: int = 1_048_576
    max_result_bytes: int = 4_194_304
    max_http_threads: int = 32
    max_workers: int = 4
    max_retained_jobs: int = 256
    bearer_token: str | None = None
    audit_log: Path | None = None
    job_store: Path | None = None
    approval_secret: str | None = None
    allowed_tools: frozenset[str] | None = None
    max_retries: int = 1


@dataclass
class Job:
    id: str
    tool: str
    params: dict[str, Any]
    state: str = "queued"
    submitted_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    cancellation_requested: bool = False
    result: dict[str, Any] | None = None
    error: str | None = None
    attempt: int = 1
    parent_job_id: str | None = None
    approval_digest: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "state": self.state,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cancellation_requested": self.cancellation_requested,
            "result": self.result,
            "error": self.error,
            "attempt": self.attempt,
            "parent_job_id": self.parent_job_id,
        }


class AuditTrail:
    def __init__(self, path: Path | None, *, retained_events: int = 1_000) -> None:
        self.path = path
        self.events: deque[dict[str, Any]] = deque(maxlen=retained_events)
        self.lock = threading.Lock()

    def record(self, event: str, **fields: Any) -> None:
        entry = {"at": _now(), "event": event, **{key: _json_safe(value) for key, value in fields.items()}}
        line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        with self.lock:
            self.events.append(entry)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")

    def recent(self, limit: int) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.events)[-limit:]


_READ_ONLY_SERVICE_TOOLS = {
    "asset_programme_intake",
    "asset_library_search",
    "material_propose",
    "material_texture_defaults_validate",
    "scene_layout_validate",
}
_SERVICE_PATH_FIELDS = {
    "base_dir",
    "cache_dir",
    "checksums_path",
    "config",
    "image_path",
    "image_paths",
    "input_manifest",
    "library_path",
    "manifest",
    "manifest_path",
    "material_manifest",
    "output",
    "output_dir",
    "project",
    "project_root",
    "property_manifest",
    "policy_path",
    "registry",
    "registry_path",
    "report",
    "report_path",
    "request",
    "source_paths",
    "sources",
    "source_meshes",
    "usd_input_path",
    "usd_output_path",
}
_SECRET_PARAM_KEY = re.compile(r"(?:api[_-]?key|password|secret|token|credential)", re.IGNORECASE)


def tool_requires_approval(tool: Any, params: dict[str, Any]) -> bool:
    del params
    return tool.name not in _READ_ONLY_SERVICE_TOOLS


def _validate_service_paths(params: dict[str, Any]) -> None:
    roots = (
        *service_workspace_roots(ROOT),
        *service_source_roots(ROOT),
        (ROOT / "configs").resolve(strict=False),
        (ROOT / "library").resolve(strict=False),
    )
    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                if _SECRET_PARAM_KEY.search(child_key) and not child_key.lower().endswith("_env"):
                    if child not in (None, "", False, [], {}):
                        raise ValueError(f"service parameters must use environment handles, not secret field {child_key}")
                visit(child, child_key)
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key)
            return
        if key not in _SERVICE_PATH_FIELDS or not isinstance(value, (str, os.PathLike)) or not str(value).strip():
            return
        raw = str(value)
        if "://" in raw:
            raise ValueError(f"service path parameter {key} must not be a URL")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = ROOT / candidate
        confine_path(candidate, roots)

    visit(params)


def _selected_tool_map(allowed_tools: frozenset[str] | None) -> dict[str, Any]:
    available = {tool.name: tool for tool in list_tools()}
    if allowed_tools is None:
        return available
    unknown = sorted(allowed_tools - available.keys())
    if unknown:
        raise ValueError(f"allowed tool set contains unknown tools: {', '.join(unknown)}")
    return {name: available[name] for name in sorted(allowed_tools)}


class ApprovalLedger:
    """Verify capability tokens and consume each token at most once."""

    def __init__(self, secret: str | None, store: Path | None, audit: AuditTrail) -> None:
        if secret is not None and len(secret.encode("utf-8")) < 32:
            raise ValueError("approval secret must contain at least 32 UTF-8 bytes")
        self.secret = secret
        self.store = store
        self.audit = audit
        self.used: set[str] = set()
        self.lock = threading.Lock()
        if self.store is not None:
            self.store.mkdir(parents=True, exist_ok=True)
            if self.store.is_symlink():
                raise ValueError("approval ledger must not be a symbolic link")
            self.used.update(path.stem for path in self.store.glob("*.json"))

    @property
    def durability(self) -> str:
        return "persistent" if self.store is not None else "volatile"

    def consume(self, tool: Any, params: dict[str, Any], token: str | None) -> str | None:
        required = tool_requires_approval(tool, params)
        if not required:
            if token:
                raise PermissionError("approval token supplied for an operation that does not require approval")
            return None
        if self.secret is None:
            raise PermissionError("reviewed mutation is disabled because no approval secret is configured")
        if not token:
            raise PermissionError("reviewed mutation requires an approval token bound to the tool and parameters")
        try:
            approval = verify_approval_token(
                token,
                self.secret,
                tool=tool.name,
                params=params,
            )
        except ValueError as exc:
            self.audit.record("approval_rejected", tool=tool.name, reason=str(exc))
            raise PermissionError(str(exc)) from exc
        digest = approval_token_digest(token)
        with self.lock:
            if digest in self.used:
                raise PermissionError("approval token has already been consumed")
            if self.store is not None:
                immutable_write_json(
                    self.store / f"{digest}.json",
                    {
                        "approval_digest": digest,
                        "tool": tool.name,
                        "params_digest": approval["params_digest"],
                        "issued_at": approval["issued_at"],
                        "expires_at": approval["expires_at"],
                        "approved_by": approval["approved_by"],
                        "reason": approval["reason"],
                        "consumed_at": _now(),
                    },
                )
            self.used.add(digest)
        self.audit.record(
            "approval_consumed",
            approval_digest=digest,
            tool=tool.name,
            approved_by=approval["approved_by"],
            reason=approval["reason"],
        )
        return digest


class VolatileJobManager:
    """Bounded execution state with optional crash-evident disk persistence."""

    def __init__(self, config: ToolServerConfig, audit: AuditTrail) -> None:
        self.config = config
        self.audit = audit
        self.tool_map = _selected_tool_map(config.allowed_tools)
        self.jobs: dict[str, Job] = {}
        self.order: deque[str] = deque()
        self.futures: dict[str, Future[None]] = {}
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=config.max_workers, thread_name_prefix="afb-tool")
        configured_store = config.job_store
        if configured_store is not None and configured_store.exists() and configured_store.is_symlink():
            raise ValueError("job store must not be a symbolic link")
        self.store = configured_store.resolve(strict=False) if configured_store is not None else None
        if self.store is not None:
            (self.store / "jobs").mkdir(parents=True, exist_ok=True)
        self.approvals = ApprovalLedger(
            config.approval_secret,
            self.store / "approvals" if self.store is not None else None,
            audit,
        )
        if self.store is not None:
            self._load_persisted_jobs()

    @property
    def durability(self) -> str:
        return "persistent" if self.store is not None else "volatile"

    def _job_path(self, job_id: str) -> Path | None:
        return self.store / "jobs" / f"{job_id}.json" if self.store is not None else None

    def _persist(self, job: Job) -> None:
        path = self._job_path(job.id)
        if path is None:
            return
        atomic_write_json(path, asdict(job))
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _load_persisted_jobs(self) -> None:
        if self.store is None:
            return
        paths = sorted((self.store / "jobs").glob("*.json"), key=lambda item: item.stat().st_mtime)
        for path in paths[-self.config.max_retained_jobs :]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                job = Job(**payload)
            except (OSError, TypeError, json.JSONDecodeError):
                self.audit.record("persisted_job_invalid", path=path.name)
                continue
            if job.state not in TERMINAL_JOB_STATES:
                job.state = "failed"
                job.error = "server restarted before the job reached a terminal state"
                job.finished_at = _now()
                self._persist(job)
            self.jobs[job.id] = job
            self.order.append(job.id)
    def catalogue(self) -> list[dict[str, Any]]:
        records = []
        for tool in self.tool_map.values():
            record = asdict(tool)
            record["service_approval"] = "required" if tool_requires_approval(tool, {}) else "not_required"
            records.append(record)
        return records

    def submit(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        approval_token: str | None = None,
        attempt: int = 1,
        parent_job_id: str | None = None,
    ) -> Job:
        tool = self.tool_map.get(tool_name)
        if tool is None:
            raise KeyError(f"unknown tool: {tool_name}")
        _validate_json_shape(params)
        _validate_against_tool_schema(params, tool.input_schema)
        _validate_service_paths(params)
        with self.lock:
            self._make_capacity()
            approval_digest = self.approvals.consume(tool, params, approval_token)
            job = Job(
                id=secrets.token_hex(16),
                tool=tool_name,
                params=params,
                attempt=attempt,
                parent_job_id=parent_job_id,
                approval_digest=approval_digest,
            )
            self.jobs[job.id] = job
            self.order.append(job.id)
            self._persist(job)
            future = self.executor.submit(self._run, job.id)
            self.futures[job.id] = future
        self.audit.record("job_submitted", job_id=job.id, tool=tool_name)
        return job

    def _make_capacity(self) -> None:
        while len(self.jobs) >= self.config.max_retained_jobs:
            removable = next(
                (job_id for job_id in self.order if self.jobs[job_id].state in TERMINAL_JOB_STATES),
                None,
            )
            if removable is None:
                raise RuntimeError("job capacity reached; retry after an active job completes")
            self.order.remove(removable)
            self.jobs.pop(removable, None)
            self.futures.pop(removable, None)
            persisted = self._job_path(removable)
            if persisted is not None:
                persisted.unlink(missing_ok=True)

    def _run(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
            if job.cancellation_requested:
                job.state = "cancelled"
                job.finished_at = _now()
                self._persist(job)
                self.audit.record("job_cancelled", job_id=job.id, phase="queued")
                return
            job.state = "running"
            job.started_at = _now()
            self._persist(job)
        self.audit.record("job_started", job_id=job.id, tool=job.tool)
        try:
            with service_request_context():
                result = asdict(route_tool(job.tool, job.params))
            result_bytes = len(json.dumps(result, default=repr).encode("utf-8"))
            if result_bytes > self.config.max_result_bytes:
                raise ValueError(f"tool result exceeds the {self.config.max_result_bytes} byte limit")
            with self.lock:
                job.result = result
                job.state = "succeeded" if result.get("success") else "failed"
                job.error = result.get("error")
                job.finished_at = _now()
                self._persist(job)
            self.audit.record(
                "job_finished",
                job_id=job.id,
                state=job.state,
                cancellation_requested=job.cancellation_requested,
            )
        except Exception as exc:  # The boundary must convert tool failures into job state.
            with self.lock:
                job.state = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished_at = _now()
                self._persist(job)
            self.audit.record("job_finished", job_id=job.id, state="failed", error=job.error)

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> tuple[Job | None, bool]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None, False
            if job.state in TERMINAL_JOB_STATES:
                return job, False
            job.cancellation_requested = True
            future = self.futures.get(job_id)
            cancelled = future.cancel() if future is not None else False
            if cancelled:
                job.state = "cancelled"
                job.finished_at = _now()
            self._persist(job)
        self.audit.record("job_cancellation_requested", job_id=job_id, cancelled_before_start=cancelled)
        return job, cancelled

    def retry(self, job_id: str, *, approval_token: str | None = None) -> Job:
        with self.lock:
            original = self.jobs.get(job_id)
            if original is None:
                raise KeyError("job not found")
            if original.state not in {"failed", "cancelled"}:
                raise RuntimeError("only failed or cancelled jobs can be retried")
            if original.attempt > self.config.max_retries:
                raise RuntimeError("job retry limit has been exhausted")
            params = dict(original.params)
            tool = original.tool
            attempt = original.attempt + 1
        return self.submit(
            tool,
            params,
            approval_token=approval_token,
            attempt=attempt,
            parent_job_id=original.id,
        )

    def summary(self) -> dict[str, int]:
        with self.lock:
            counts = {state: 0 for state in ("queued", "running", "succeeded", "failed", "cancelled")}
            for job in self.jobs.values():
                counts[job.state] = counts.get(job.state, 0) + 1
            return counts

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


class ToolServerApplication:
    def __init__(self, config: ToolServerConfig) -> None:
        self.config = config
        self.audit = AuditTrail(config.audit_log)
        self.jobs = VolatileJobManager(config, self.audit)
        self.audit.record(
            "server_started",
            host=config.host,
            port=config.port,
            durability=self.jobs.durability,
            authenticated=config.bearer_token is not None,
            allowed_tool_count=len(self.jobs.tool_map),
        )

    def health(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "service": "asset-factory-tool-server",
            "version": __version__,
            "durability": self.jobs.durability,
            "durable": self.jobs.durability == "persistent",
            "jobs": self.jobs.summary(),
        }

    def close(self) -> None:
        self.audit.record("server_stopped")
        self.jobs.close()


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    request_queue_size = 64

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        max_threads: int,
    ) -> None:
        self.request_slots = threading.BoundedSemaphore(max_threads)
        super().__init__(server_address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        self.request_slots.acquire()
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


def _handler_for(app: ToolServerApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"AssetFactoryToolServer/{__version__}"
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, format_string: str, *args: Any) -> None:
            app.audit.record("http_access", client=self.client_address[0], message=format_string % args)

        def _authorised(self) -> bool:
            expected = app.config.bearer_token
            if expected is None:
                return True
            value = self.headers.get("Authorization", "")
            prefix = "Bearer "
            return value.startswith(prefix) and hmac.compare_digest(value[len(prefix) :], expected)

        def _send(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", f"{JSON_MEDIA_TYPE}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _require_auth(self) -> bool:
            if self._authorised():
                return True
            app.audit.record("authentication_failed", client=self.client_address[0], path=self.path)
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "valid bearer token required"})
            return False

        def _read_json(self) -> dict[str, Any] | None:
            media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media_type != JSON_MEDIA_TYPE:
                self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Content-Type must be application/json"})
                return None
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                self._send(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length is required"})
                return None
            try:
                length = int(raw_length)
            except ValueError:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length"})
                return None
            if length < 0 or length > app.config.max_request_bytes:
                self._send(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"error": f"request exceeds the {app.config.max_request_bytes} byte limit"},
                )
                return None
            try:
                payload = json.loads(self.rfile.read(length))
                _validate_json_shape(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self._send(HTTPStatus.BAD_REQUEST, {"error": f"invalid JSON request: {exc}"})
                return None
            if not isinstance(payload, dict):
                self._send(HTTPStatus.BAD_REQUEST, {"error": "request body must be a JSON object"})
                return None
            return payload

        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if path == "/healthz":
                self._send(HTTPStatus.OK, app.health())
                return
            if not self._require_auth():
                return
            catalogue_paths = {app.config.catalogue_path.rstrip("/"), "/v1/tools"}
            if path in catalogue_paths:
                self._send(HTTPStatus.OK, {"tools": app.jobs.catalogue()})
                return
            if path == "/v1/audit":
                self._send(
                    HTTPStatus.OK,
                    {"durability": app.jobs.durability, "events": app.audit.recent(100)},
                )
                return
            if path.startswith("/v1/jobs/"):
                job_id = path.removeprefix("/v1/jobs/")
                job = app.jobs.get(job_id)
                if job is None:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "job not found"})
                    return
                self._send(HTTPStatus.OK, {"job": job.public(), "durability": app.jobs.durability})
                return
            if path == "/":
                self._send(
                    HTTPStatus.OK,
                    {
                        "service": "asset-factory-tool-server",
                        "health": "/healthz",
                        "catalogue": app.config.catalogue_path,
                        "jobs": "/v1/jobs",
                        "durability": app.jobs.durability,
                    },
                )
                return
            self._send(HTTPStatus.NOT_FOUND, {"error": "endpoint not found"})

        def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            if not self._require_auth():
                return
            path = urlsplit(self.path).path.rstrip("/") or "/"
            if path == "/v1/jobs":
                payload = self._read_json()
                if payload is None:
                    return
                unknown = set(payload) - {"tool", "params", "approval_token"}
                if unknown:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": f"unknown request fields: {', '.join(sorted(unknown))}"})
                    return
                tool = payload.get("tool")
                params = payload.get("params", {})
                approval_token = payload.get("approval_token")
                if not isinstance(tool, str) or not tool:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": "tool must be a non-empty string"})
                    return
                if not isinstance(params, dict):
                    self._send(HTTPStatus.BAD_REQUEST, {"error": "params must be a JSON object"})
                    return
                if approval_token is not None and not isinstance(approval_token, str):
                    self._send(HTTPStatus.BAD_REQUEST, {"error": "approval_token must be a string"})
                    return
                try:
                    job = app.jobs.submit(tool, params, approval_token=approval_token)
                except KeyError as exc:
                    self._send(HTTPStatus.NOT_FOUND, {"error": str(exc).strip("'")})
                    return
                except ValueError as exc:
                    self._send(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                    return
                except PermissionError as exc:
                    self._send(HTTPStatus.FORBIDDEN, {"error": str(exc)})
                    return
                except RuntimeError as exc:
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                    return
                self._send(
                    HTTPStatus.ACCEPTED,
                    {"job": job.public(), "location": f"/v1/jobs/{job.id}", "durability": app.jobs.durability},
                )
                return
            if path.startswith("/v1/jobs/") and path.endswith("/retry"):
                payload = self._read_json()
                if payload is None:
                    return
                unknown = set(payload) - {"approval_token"}
                if unknown:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": f"unknown request fields: {', '.join(sorted(unknown))}"})
                    return
                approval_token = payload.get("approval_token")
                if approval_token is not None and not isinstance(approval_token, str):
                    self._send(HTTPStatus.BAD_REQUEST, {"error": "approval_token must be a string"})
                    return
                job_id = path.removeprefix("/v1/jobs/").removesuffix("/retry").rstrip("/")
                try:
                    job = app.jobs.retry(job_id, approval_token=approval_token)
                except KeyError:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "job not found"})
                    return
                except PermissionError as exc:
                    self._send(HTTPStatus.FORBIDDEN, {"error": str(exc)})
                    return
                except (RuntimeError, ValueError) as exc:
                    self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
                    return
                self._send(
                    HTTPStatus.ACCEPTED,
                    {"job": job.public(), "location": f"/v1/jobs/{job.id}", "durability": app.jobs.durability},
                )
                return
            if path.startswith("/v1/jobs/") and path.endswith("/cancel"):
                job_id = path.removeprefix("/v1/jobs/").removesuffix("/cancel").rstrip("/")
                job, cancelled = app.jobs.cancel(job_id)
                if job is None:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "job not found"})
                    return
                status = HTTPStatus.ACCEPTED if job.state not in TERMINAL_JOB_STATES or cancelled else HTTPStatus.CONFLICT
                self._send(
                    status,
                    {
                        "job": job.public(),
                        "cancelled_before_start": cancelled,
                        "cooperative_cancellation": False,
                    },
                )
                return
            self._send(HTTPStatus.NOT_FOUND, {"error": "endpoint not found"})

    return Handler


def serve_http(config: ToolServerConfig) -> None:
    app = ToolServerApplication(config)
    server = BoundedThreadingHTTPServer(
        (config.host, config.port),
        _handler_for(app),
        max_threads=config.max_http_threads,
    )
    server.daemon_threads = True
    print(
        json.dumps(
            {
                "service": "asset-factory-tool-server",
                "transport": "http",
                "host": config.host,
                "port": config.port,
                "catalogue": config.catalogue_path,
                "durability": app.jobs.durability,
                "authenticated": config.bearer_token is not None,
            }
        ),
        flush=True,
    )
    previous_handlers: dict[signal.Signals, Any] = {}

    def request_shutdown(_: int, __: Any) -> None:
        threading.Thread(target=server.shutdown, name="afb-http-shutdown", daemon=True).start()

    for signal_name in ("SIGINT", "SIGTERM"):
        candidate = getattr(signal, signal_name, None)
        if candidate is not None:
            previous_handlers[candidate] = signal.getsignal(candidate)
            signal.signal(candidate, request_shutdown)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        for candidate, previous in previous_handlers.items():
            signal.signal(candidate, previous)
        server.server_close()
        app.close()


def serve_stdio(config: ToolServerConfig) -> None:
    """Serve a bounded newline-delimited JSON protocol on stdin and stdout."""

    tools = _selected_tool_map(config.allowed_tools)
    audit = AuditTrail(config.audit_log)
    if config.job_store is not None and config.job_store.exists() and config.job_store.is_symlink():
        raise ValueError("job store must not be a symbolic link")
    approval_store = config.job_store.resolve(strict=False) / "approvals" if config.job_store is not None else None
    approvals = ApprovalLedger(config.approval_secret, approval_store, audit)
    for raw_line in sys.stdin.buffer:
        if len(raw_line) > config.max_request_bytes:
            response: dict[str, Any] = {"ok": False, "error": "request exceeds the configured byte limit"}
        else:
            try:
                payload = json.loads(raw_line)
                _validate_json_shape(payload)
                if not isinstance(payload, dict):
                    raise ValueError("request must be a JSON object")
                operation = payload.get("operation")
                if operation == "health":
                    response = {
                        "ok": True,
                        "service": "asset-factory-tool-server",
                        "version": __version__,
                        "transport": "stdio",
                        "approval_ledger": approvals.durability,
                    }
                elif operation == "catalogue":
                    response = {
                        "ok": True,
                        "tools": [
                            {
                                **asdict(tool),
                                "service_approval": (
                                    "required" if tool_requires_approval(tool, {}) else "not_required"
                                ),
                            }
                            for tool in tools.values()
                        ],
                    }
                elif operation == "invoke":
                    unknown = set(payload) - {"operation", "tool", "params", "approval_token"}
                    if unknown:
                        raise ValueError(f"unknown request fields: {', '.join(sorted(unknown))}")
                    tool_name = payload.get("tool")
                    params = payload.get("params", {})
                    approval_token = payload.get("approval_token")
                    if not isinstance(tool_name, str) or tool_name not in tools:
                        raise ValueError("unknown or missing tool")
                    if not isinstance(params, dict):
                        raise ValueError("params must be a JSON object")
                    if approval_token is not None and not isinstance(approval_token, str):
                        raise ValueError("approval_token must be a string")
                    _validate_against_tool_schema(params, tools[tool_name].input_schema)
                    _validate_service_paths(params)
                    approval_digest = approvals.consume(tools[tool_name], params, approval_token)
                    with service_request_context():
                        result = asdict(route_tool(tool_name, params))
                    result_bytes = len(json.dumps(result, default=repr).encode("utf-8"))
                    if result_bytes > config.max_result_bytes:
                        raise ValueError(f"tool result exceeds the {config.max_result_bytes} byte limit")
                    response = {"ok": True, "result": result, "approval_digest": approval_digest}
                else:
                    raise ValueError("operation must be health, catalogue or invoke")
            except (json.JSONDecodeError, PermissionError, ValueError) as exc:
                response = {"ok": False, "error": str(exc)}
            except Exception as exc:  # Keep protocol errors on stdout rather than terminating the agent process.
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        sys.stdout.write(json.dumps(response, sort_keys=True) + "\n")
        sys.stdout.flush()


def token_from_environment(variable: str) -> str | None:
    value = os.environ.get(variable)
    return value if value else None
