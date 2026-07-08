from __future__ import annotations

import ipaddress
import os
import re
import socket
import urllib.parse
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
DEFAULT_PROVIDER_HOSTS = {
    "api.openai.com",
    "integrate.api.nvidia.com",
    "localhost",
    "127.0.0.1",
    "::1",
}
DEFAULT_MEDIA_HOSTS = {
    "huggingface.co",
    "cdn-lfs.huggingface.co",
    "*.hf.space",
    "localhost",
    "127.0.0.1",
    "::1",
}
_SERVICE_REQUEST = ContextVar("afb_service_request", default=False)
_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so credentials and media fetches cannot change host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def ensure_path_component(value: str, label: str = "path component") -> str:
    if not _PATH_COMPONENT.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"{label} must be one safe filename component")
    return value


def _environment_hosts(name: str) -> set[str]:
    return {item.strip().lower() for item in os.environ.get(name, "").split(",") if item.strip()}


def _host_matches(host: str, allowed_hosts: Iterable[str]) -> bool:
    normalised = host.lower().rstrip(".")
    for allowed in allowed_hosts:
        candidate = allowed.lower().rstrip(".")
        if candidate.startswith("*."):
            suffix = candidate[1:]
            if normalised.endswith(suffix) and normalised != suffix[1:]:
                return True
        elif normalised == candidate:
            return True
    return False


def validate_endpoint(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    allow_loopback_http: bool = False,
    resolve_dns: bool = False,
) -> urllib.parse.ParseResult:
    """Validate scheme, credentials, host policy and optionally resolved addresses."""

    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.username or parsed.password:
        raise ValueError("endpoint URLs must not contain credentials")
    if not host:
        raise ValueError("endpoint URL has no host")
    if not _host_matches(host, allowed_hosts):
        raise ValueError(f"endpoint host is not allowed: {host}")
    if parsed.scheme == "http":
        if not allow_loopback_http or host not in LOOPBACK_HOSTS:
            raise ValueError("plain HTTP is allowed only for configured loopback endpoints")
    elif parsed.scheme != "https":
        raise ValueError(f"unsupported endpoint scheme: {parsed.scheme or 'missing'}")
    if resolve_dns:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        }
        if not addresses:
            raise ValueError(f"endpoint host did not resolve: {host}")
        for value in addresses:
            address = ipaddress.ip_address(value)
            if host in LOOPBACK_HOSTS:
                if not address.is_loopback:
                    raise ValueError("loopback endpoint resolved outside loopback")
            elif address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast:
                raise ValueError(f"remote endpoint resolved to a non-public address: {value}")
    return parsed


def validate_provider_endpoint(url: str, provider: dict, *, resolve_dns: bool = False) -> None:
    configured = {str(item).lower() for item in provider.get("allowed_hosts", [])}
    allowed = configured | _environment_hosts("AFB_ALLOWED_PROVIDER_HOSTS")
    if not allowed:
        allowed = set(DEFAULT_PROVIDER_HOSTS)
    validate_endpoint(
        url,
        allowed_hosts=allowed,
        allow_loopback_http=bool(provider.get("allow_loopback_http", False)),
        resolve_dns=resolve_dns,
    )


def open_bounded_url(
    request: urllib.request.Request,
    *,
    timeout: int,
    max_bytes: int,
    allowed_hosts: Iterable[str],
    allow_loopback_http: bool,
) -> tuple[BinaryIO, urllib.parse.ParseResult]:
    """Open one URL without redirects after host and address validation."""

    parsed = validate_endpoint(
        request.full_url,
        allowed_hosts=allowed_hosts,
        allow_loopback_http=allow_loopback_http,
        resolve_dns=True,
    )
    opener = urllib.request.build_opener(NoRedirectHandler())
    response = opener.open(request, timeout=timeout)
    content_length = response.headers.get("Content-Length")
    if content_length and int(content_length) > max_bytes:
        response.close()
        raise ValueError(f"response exceeds byte limit: {content_length} > {max_bytes}")
    return response, parsed


def read_bounded(stream: BinaryIO, max_bytes: int) -> bytes:
    data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"response exceeds byte limit: more than {max_bytes}")
    return data


def authorised_roots(allowed_paths: Iterable[str | Path], base_dir: str | Path) -> tuple[Path, ...]:
    base = Path(base_dir).resolve(strict=False)
    roots: list[Path] = []
    for value in allowed_paths:
        path = Path(value)
        roots.append((path if path.is_absolute() else base / path).resolve(strict=False))
    if not roots:
        raise ValueError("at least one authorised path root is required")
    return tuple(dict.fromkeys(roots))


def configured_roots(
    environment_name: str,
    base_dir: str | Path,
    defaults: Iterable[str | Path],
) -> tuple[Path, ...]:
    """Resolve administrator-controlled roots from defaults and an environment variable."""

    configured: list[str | Path] = list(defaults)
    configured.extend(item for item in os.environ.get(environment_name, "").split(os.pathsep) if item)
    return authorised_roots(configured, base_dir)


def external_io_roots(base_dir: str | Path) -> tuple[Path, ...]:
    return configured_roots(
        "AFB_EXTERNAL_ALLOWED_ROOTS",
        base_dir,
        ("projects", "artifacts", ".cache/afb"),
    )


def external_registry_roots(base_dir: str | Path) -> tuple[Path, ...]:
    return configured_roots("AFB_EXTERNAL_REGISTRY_ROOTS", base_dir, ("configs",))


def service_workspace_roots(base_dir: str | Path) -> tuple[Path, ...]:
    return configured_roots(
        "AFB_SERVICE_WORKSPACE_ROOTS",
        base_dir,
        ("projects", "artifacts", ".cache/afb"),
    )


def service_source_roots(base_dir: str | Path) -> tuple[Path, ...]:
    return configured_roots(
        "AFB_SERVICE_SOURCE_ROOTS",
        base_dir,
        ("projects", "artifacts", "examples"),
    )


@contextmanager
def service_request_context() -> Iterator[None]:
    token = _SERVICE_REQUEST.set(True)
    try:
        yield
    finally:
        _SERVICE_REQUEST.reset(token)


def in_service_request() -> bool:
    return bool(_SERVICE_REQUEST.get())


def confine_path(candidate: str | Path, roots: Iterable[Path], *, must_exist: bool = False) -> Path:
    """Resolve a path and require it to remain beneath one authorised root."""

    path = Path(candidate).resolve(strict=must_exist)
    for root in roots:
        if path == root or root in path.parents:
            return path
    raise ValueError(f"path is outside authorised roots: {candidate}")


def provider_hosts() -> set[str]:
    return set(DEFAULT_PROVIDER_HOSTS) | _environment_hosts("AFB_ALLOWED_PROVIDER_HOSTS")


def media_hosts() -> set[str]:
    return set(DEFAULT_MEDIA_HOSTS) | _environment_hosts("AFB_ALLOWED_MEDIA_HOSTS")
