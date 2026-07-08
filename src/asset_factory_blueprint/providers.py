from __future__ import annotations

import json
import os
import re
import socket
import base64
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from asset_factory_blueprint.config import ROOT, load_json
from asset_factory_blueprint.security import (
    confine_path,
    in_service_request,
    media_hosts,
    open_bounded_url,
    provider_hosts,
    read_bounded,
    service_source_roots,
    service_workspace_roots,
    validate_provider_endpoint,
)


MAX_JSON_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_MEDIA_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_VISION_INPUT_BYTES = 32 * 1024 * 1024
_VALIDATED_PROVIDER_HOSTS: set[str] = set()
_GRADIO_SPACE_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    kind: str
    base_url_host: str
    model: str | None
    status: str
    message: str
    live: bool


@dataclass(frozen=True)
class ProviderCompletion:
    provider: str
    model: str
    base_url_host: str
    content: str
    request_payload_redacted: dict[str, Any]
    response_usage: dict[str, Any]
    proposal_status: str


@dataclass(frozen=True)
class ProviderImageGeneration:
    provider: str
    model: str
    base_url_host: str
    image_bytes: bytes
    output_format: str
    request_payload_redacted: dict[str, Any]
    response_usage: dict[str, Any]
    generation_status: str


def _host(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    return parsed.netloc or parsed.path


def _models_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/models"


def _chat_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


def _responses_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/responses"


def _images_generations_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/images/generations"


def _request_json(url: str, api_key: str | None, timeout: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if payload is not None else "GET")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    request.add_header("Accept", "application/json")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    response, _ = open_bounded_url(
        request,
        timeout=timeout,
        max_bytes=MAX_JSON_RESPONSE_BYTES,
        allowed_hosts=provider_hosts() | _VALIDATED_PROVIDER_HOSTS,
        allow_loopback_http=True,
    )
    with response:
        return json.loads(read_bounded(response, MAX_JSON_RESPONSE_BYTES).decode("utf-8"))


def _list_model_ids(base_url: str, api_key: str | None, timeout: int) -> list[str]:
    payload = _request_json(_models_url(base_url), api_key, timeout)
    data = payload.get("data", []) if isinstance(payload, dict) else []
    if data is None:
        data = []
    return [str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")]


def _select_model(model_ids: list[str], preferred: str | None = None) -> str | None:
    if preferred:
        return preferred
    candidate_terms = (
        "nemotron",
        "llama-3.1",
        "llama-3",
        "llama",
        "qwen",
        "mistral",
        "yi-large",
    )
    for term in candidate_terms:
        for model_id in model_ids:
            if term in model_id.lower():
                return model_id
    return model_ids[0] if model_ids else None


def _probe_models(base_url: str, api_key: str | None, timeout: int) -> tuple[bool, str, str | None]:
    try:
        payload = _request_json(_models_url(base_url), api_key, timeout)
    except urllib.error.HTTPError as exc:
        return False, f"http {exc.code}", None
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        return False, str(exc.reason if isinstance(exc, urllib.error.URLError) else exc), None
    except json.JSONDecodeError:
        return False, "models endpoint returned invalid JSON", None
    except ValueError as exc:
        return False, str(exc), None
    data = payload.get("data", []) if isinstance(payload, dict) else []
    if data is None:
        data = []
    model_ids = [str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")]
    return True, f"models endpoint returned {len(data)} model records", _select_model(model_ids)


def _provider_config(path: str, provider_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if in_service_request():
        path = confine_path(path, (ROOT / "configs",), must_exist=True).as_posix()
    policy = load_json(path)
    provider = policy["providers"].get(provider_name)
    if provider is None:
        raise ValueError(f"unknown provider: {provider_name}")
    return policy, provider


def _resolve_provider_static(
    path: str,
    provider_name: str,
    requested_model: str | None = None,
    default_model: str | None = None,
) -> tuple[dict[str, Any], str, str | None, str, int]:
    _, provider = _provider_config(path, provider_name)
    base_url = os.getenv(provider.get("base_url_env", ""), provider.get("default_base_url", ""))
    api_key = os.getenv(provider.get("api_key_env", ""), "")
    timeout = int(provider.get("timeout_seconds", 30))
    key_optional = bool(provider.get("api_key_optional", False))
    if not base_url:
        raise RuntimeError(f"{provider_name} base URL is not configured")
    validate_provider_endpoint(base_url, provider)
    _VALIDATED_PROVIDER_HOSTS.add((urllib.parse.urlparse(base_url).hostname or "").lower())
    if not api_key and not key_optional:
        raise RuntimeError(f"{provider_name} is missing environment variable {provider.get('api_key_env')}")
    image_model_env = str(provider.get("image_model_env") or "")
    selected_model = (
        requested_model
        or os.getenv("AFB_IMAGE_GENERATION_MODEL", "")
        or os.getenv("AFB_TEXTURE_MODEL", "")
        or os.getenv(image_model_env, "")
        or provider.get("default_image_model_id")
        or default_model
        or provider.get("default_model_id")
        or ""
    )
    if not selected_model:
        raise RuntimeError(f"{provider_name} image generation model is not configured")
    return provider, base_url, api_key or None, selected_model, timeout


def _resolve_gradio_provider_static(
    path: str,
    provider_name: str,
    requested_model: str | None = None,
) -> tuple[dict[str, Any], str, str | None, str, int]:
    _, provider = _provider_config(path, provider_name)
    space_env = str(provider.get("space_env") or "")
    space = os.getenv(space_env, "") or str(provider.get("default_space") or "")
    api_key = os.getenv(provider.get("api_key_env", ""), "")
    timeout = int(provider.get("timeout_seconds", 30))
    key_optional = bool(provider.get("api_key_optional", False))
    if not space:
        raise RuntimeError(f"{provider_name} space is not configured")
    if space.startswith(("http://", "https://")):
        validate_provider_endpoint(space, provider)
    elif not _GRADIO_SPACE_ID.fullmatch(space):
        raise RuntimeError(f"{provider_name} space must be an owner/name identifier or an allowed HTTPS endpoint")
    if not api_key and not key_optional:
        raise RuntimeError(f"{provider_name} is missing environment variable {provider.get('api_key_env')}")
    model_env = str(provider.get("model_env") or "")
    selected_model = requested_model or os.getenv(model_env, "") or provider.get("default_model_id") or space
    if not selected_model:
        raise RuntimeError(f"{provider_name} image generation model is not configured")
    return provider, space, api_key or None, str(selected_model), timeout


def _parse_image_size(size: str, provider: dict[str, Any]) -> tuple[int, int]:
    try:
        raw_width, raw_height = size.lower().split("x", 1)
        width = int(raw_width)
        height = int(raw_height)
    except (AttributeError, TypeError, ValueError):
        width = int(provider.get("default_width", 1024))
        height = int(provider.get("default_height", 1024))
    return max(256, width), max(256, height)


def _read_url_bytes(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "image/png,image/jpeg,image/webp,*/*")
    response, _ = open_bounded_url(
        request,
        timeout=timeout,
        max_bytes=MAX_MEDIA_RESPONSE_BYTES,
        allowed_hosts=media_hosts(),
        allow_loopback_http=True,
    )
    with response:
        return read_bounded(response, MAX_MEDIA_RESPONSE_BYTES)


def _extract_gradio_image_bytes(result: Any, timeout: int, download_root: Path) -> bytes:
    if isinstance(result, bytes):
        if len(result) > MAX_MEDIA_RESPONSE_BYTES:
            raise RuntimeError("gradio image response exceeds the byte limit")
        return result
    if isinstance(result, bytearray):
        return _extract_gradio_image_bytes(bytes(result), timeout, download_root)
    if isinstance(result, (list, tuple)):
        for item in result:
            try:
                return _extract_gradio_image_bytes(item, timeout, download_root)
            except RuntimeError:
                continue
    if isinstance(result, dict):
        for key in ("path", "url", "name", "image", "value", "data"):
            value = result.get(key)
            if value:
                return _extract_gradio_image_bytes(value, timeout, download_root)
    if hasattr(result, "path"):
        value = getattr(result, "path")
        if value:
            return _extract_gradio_image_bytes(value, timeout, download_root)
    if hasattr(result, "url"):
        value = getattr(result, "url")
        if value:
            return _extract_gradio_image_bytes(value, timeout, download_root)
    if hasattr(result, "save"):
        buffer = BytesIO()
        result.save(buffer, format="PNG")
        return _extract_gradio_image_bytes(buffer.getvalue(), timeout, download_root)
    if isinstance(result, str):
        if result.startswith(("http://", "https://")):
            return _read_url_bytes(result, timeout)
        path = Path(result)
        if path.exists() and path.is_file():
            path = confine_path(path, (download_root,), must_exist=True)
            if path.stat().st_size > MAX_MEDIA_RESPONSE_BYTES:
                raise RuntimeError("gradio image file exceeds the byte limit")
            return path.read_bytes()
    raise RuntimeError("gradio image generation response did not contain image bytes")


def _predict_gradio_space_image(
    *,
    space: str,
    api_key: str | None,
    prompt: str,
    width: int,
    height: int,
    steps: int,
    api_name: str,
    timeout: int,
) -> bytes:
    try:
        from gradio_client import Client
    except ImportError as exc:
        raise RuntimeError("gradio_client is required for gradio-space providers") from exc
    download_root = (Path(os.getenv("TEMP", os.getcwd())) / "gradio").resolve(strict=False)
    download_root.mkdir(parents=True, exist_ok=True)
    client = Client(
        space,
        hf_token=api_key or False,
        verbose=False,
        download_files=download_root,
    )
    result = client.predict(
        prompt,
        0,
        True,
        width,
        height,
        steps,
        api_name=api_name,
    )
    return _extract_gradio_image_bytes(result, timeout, download_root)


def _generate_image_gradio_space(
    provider_name: str,
    prompt: str,
    policy_path: str,
    model: str | None,
    size: str,
    output_format: str,
    quality: str,
) -> ProviderImageGeneration:
    provider, space, api_key, selected_model, timeout = _resolve_gradio_provider_static(policy_path, provider_name, model)
    width, height = _parse_image_size(size, provider)
    steps = int(provider.get("default_steps", 5))
    api_name = str(provider.get("predict_api_name") or "/infer")
    image_bytes = _predict_gradio_space_image(
        space=space,
        api_key=api_key,
        prompt=prompt,
        width=width,
        height=height,
        steps=steps,
        api_name=api_name,
        timeout=timeout,
    )
    return ProviderImageGeneration(
        provider=provider_name,
        model=selected_model,
        base_url_host=space,
        image_bytes=image_bytes,
        output_format=output_format,
        request_payload_redacted={
            "model": selected_model,
            "prompt_length": len(prompt),
            "size": size,
            "width": width,
            "height": height,
            "output_format": output_format,
            "quality": quality,
            "api_name": api_name,
            "space": space,
            "steps": steps,
        },
        response_usage={},
        generation_status="generated",
    )


def _resolve_provider(path: str, provider_name: str, requested_model: str | None = None) -> tuple[dict[str, Any], str, str | None, str, int]:
    _, provider = _provider_config(path, provider_name)
    base_url = os.getenv(provider.get("base_url_env", ""), provider.get("default_base_url", ""))
    api_key = os.getenv(provider.get("api_key_env", ""), "")
    timeout = int(provider.get("timeout_seconds", 30))
    key_optional = bool(provider.get("api_key_optional", False))
    if not base_url:
        raise RuntimeError(f"{provider_name} base URL is not configured")
    validate_provider_endpoint(base_url, provider)
    _VALIDATED_PROVIDER_HOSTS.add((urllib.parse.urlparse(base_url).hostname or "").lower())
    if not api_key and not key_optional:
        raise RuntimeError(f"{provider_name} is missing environment variable {provider.get('api_key_env')}")
    configured_model = requested_model or os.getenv(provider.get("model_env", ""), "") or provider.get("default_model_id") or None
    model_ids = _list_model_ids(base_url, api_key or None, timeout)
    model = _select_model(model_ids, configured_model)
    if not model:
        raise RuntimeError(f"{provider_name} returned no model records; set {provider.get('model_env')}")
    return provider, base_url, api_key or None, model, timeout


def generate_image(
    provider_name: str,
    prompt: str,
    policy_path: str = "configs/provider-policy.json",
    model: str | None = None,
    size: str = "1024x1024",
    output_format: str = "png",
    quality: str = "medium",
) -> ProviderImageGeneration:
    _, provider_config = _provider_config(policy_path, provider_name)
    if provider_config.get("kind") == "gradio-space":
        return _generate_image_gradio_space(provider_name, prompt, policy_path, model, size, output_format, quality)
    provider, base_url, api_key, selected_model, timeout = _resolve_provider_static(
        policy_path,
        provider_name,
        model,
        default_model="chatgpt-image-latest",
    )
    if provider.get("kind") not in {"openai", "openai-compatible"}:
        raise RuntimeError(f"{provider_name} does not expose an image generation endpoint")
    payload = {
        "model": selected_model,
        "prompt": prompt,
        "n": 1,
        "size": size,
    }
    if output_format:
        payload["output_format"] = output_format
    if quality:
        payload["quality"] = quality
    response = _request_json(_images_generations_url(base_url), api_key, timeout, payload)
    data = response.get("data", []) if isinstance(response, dict) else []
    if not data or not isinstance(data[0], dict) or not data[0].get("b64_json"):
        raise RuntimeError("image generation response did not contain base64 image data")
    image_bytes = base64.b64decode(str(data[0]["b64_json"]))
    return ProviderImageGeneration(
        provider=provider_name,
        model=selected_model,
        base_url_host=_host(base_url),
        image_bytes=image_bytes,
        output_format=str(response.get("output_format") or output_format),
        request_payload_redacted={
            "model": selected_model,
            "prompt_length": len(prompt),
            "size": size,
            "output_format": output_format,
            "quality": quality,
        },
        response_usage=response.get("usage", {}) if isinstance(response, dict) else {},
        generation_status="generated",
    )


def check_policy(path: str = "configs/provider-policy.json", live: bool = True) -> list[ProviderStatus]:
    payload = load_json(path)
    statuses = []
    for name, provider in payload["providers"].items():
        if provider.get("kind") == "gradio-space":
            space = os.getenv(provider.get("space_env", ""), provider.get("default_space", ""))
            api_key = os.getenv(provider.get("api_key_env", ""), "")
            model = os.getenv(provider.get("model_env", ""), "") or provider.get("default_model_id") or None
            key_optional = bool(provider.get("api_key_optional", False))
            if not space:
                statuses.append(ProviderStatus(name, provider["kind"], "", model, "blocked", "space is not configured", False))
                continue
            if space.startswith(("http://", "https://")):
                try:
                    validate_provider_endpoint(space, provider)
                except ValueError as exc:
                    statuses.append(ProviderStatus(name, provider["kind"], space, model, "blocked", str(exc), False))
                    continue
            elif not _GRADIO_SPACE_ID.fullmatch(space):
                statuses.append(
                    ProviderStatus(name, provider["kind"], space, model, "blocked", "space must be owner/name", False)
                )
                continue
            if not api_key and not key_optional:
                statuses.append(
                    ProviderStatus(
                        name,
                        provider["kind"],
                        space,
                        model,
                        "blocked",
                        f"missing environment variable {provider.get('api_key_env')}",
                        False,
                    )
                )
                continue
            statuses.append(
                ProviderStatus(
                    name,
                    provider["kind"],
                    space,
                    model,
                    "configured",
                    "gradio space configured; image generation is checked at request time",
                    False,
                )
            )
            continue
        base_url = os.getenv(provider.get("base_url_env", ""), provider.get("default_base_url", ""))
        api_key = os.getenv(provider.get("api_key_env", ""), "")
        model = os.getenv(provider.get("model_env", ""), "") or provider.get("default_model_id") or None
        key_optional = bool(provider.get("api_key_optional", False))
        if not base_url:
            statuses.append(ProviderStatus(name, provider["kind"], "", model, "blocked", "base URL is not configured", False))
            continue
        try:
            validate_provider_endpoint(base_url, provider)
        except ValueError as exc:
            statuses.append(ProviderStatus(name, provider["kind"], _host(base_url), model, "blocked", str(exc), False))
            continue
        if not api_key and not key_optional:
            statuses.append(
                ProviderStatus(
                    name,
                    provider["kind"],
                    _host(base_url),
                    model,
                    "blocked",
                    f"missing environment variable {provider.get('api_key_env')}",
                    False,
                )
            )
            continue
        if not live:
            statuses.append(ProviderStatus(name, provider["kind"], _host(base_url), model, "configured", "policy is valid", False))
            continue
        ok, message, discovered_model = _probe_models(base_url, api_key or None, int(provider.get("timeout_seconds", 30)))
        statuses.append(
            ProviderStatus(
                name,
                provider["kind"],
                _host(base_url),
                model or discovered_model,
                "ready" if ok else "blocked",
                message,
                ok,
            )
        )
    return statuses


def statuses_as_dict(statuses: list[ProviderStatus]) -> list[dict[str, Any]]:
    return [
        {
            "name": item.name,
            "kind": item.kind,
            "base_url_host": item.base_url_host,
            "model": item.model,
            "status": item.status,
            "message": item.message,
            "live": item.live,
        }
        for item in statuses
    ]


def complete_chat(
    provider_name: str,
    prompt: str,
    policy_path: str = "configs/provider-policy.json",
    model: str | None = None,
    max_tokens: int = 96,
    temperature: float = 0.0,
) -> ProviderCompletion:
    provider, base_url, api_key, selected_model, timeout = _resolve_provider(policy_path, provider_name, model)
    if provider.get("kind") == "openai":
        payload = {
            "model": selected_model,
            "instructions": "Return proposal text only. Do not claim validation or promotion.",
            "input": prompt,
            "max_output_tokens": max_tokens,
        }
        response = _request_json(_responses_url(base_url), api_key, timeout, payload)
        content = str(response.get("output_text") or "")
        if not content:
            output = response.get("output", []) if isinstance(response, dict) else []
            parts = []
            for item in output:
                for chunk in item.get("content", []) if isinstance(item, dict) else []:
                    if isinstance(chunk, dict) and chunk.get("text"):
                        parts.append(str(chunk["text"]))
            content = "".join(parts)
        return ProviderCompletion(
            provider=provider_name,
            model=selected_model,
            base_url_host=_host(base_url),
            content=content,
            request_payload_redacted={
                "model": selected_model,
                "input_count": 1,
                "max_output_tokens": max_tokens,
            },
            response_usage=response.get("usage", {}) if isinstance(response, dict) else {},
            proposal_status="proposal",
        )
    payload = {
        "model": selected_model,
        "messages": [
            {
                "role": "system",
                "content": "Return proposal text only. Do not claim validation or promotion.",
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    response = _request_json(_chat_url(base_url), api_key, timeout, payload)
    choices = response.get("choices", []) if isinstance(response, dict) else []
    content = ""
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message", {})
        if isinstance(message, dict):
            content = str(message.get("content") or "")
    return ProviderCompletion(
        provider=provider_name,
        model=selected_model,
        base_url_host=_host(base_url),
        content=content,
        request_payload_redacted={
            "model": selected_model,
            "message_count": len(payload["messages"]),
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        response_usage=response.get("usage", {}) if isinstance(response, dict) else {},
        proposal_status="proposal",
    )


def _image_content_data_url(path: str | Path) -> tuple[str, int]:
    image_path = Path(path).resolve()
    if in_service_request():
        image_path = confine_path(
            image_path,
            (*service_source_roots(ROOT), *service_workspace_roots(ROOT)),
            must_exist=True,
        )
    if image_path.stat().st_size > MAX_VISION_INPUT_BYTES:
        raise ValueError(f"vision input exceeds the {MAX_VISION_INPUT_BYTES} byte limit")
    image_bytes = image_path.read_bytes()
    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}", len(image_bytes)


def complete_vision(
    provider_name: str,
    prompt: str,
    image_paths: list[str | Path],
    policy_path: str = "configs/provider-policy.json",
    model: str | None = None,
    max_tokens: int = 768,
    temperature: float = 0.0,
) -> ProviderCompletion:
    provider, base_url, api_key, selected_model, timeout = _resolve_provider(policy_path, provider_name, model)
    image_payloads = [_image_content_data_url(path) for path in image_paths]
    image_urls = [item[0] for item in image_payloads]
    image_bytes_total = sum(item[1] for item in image_payloads)
    if image_bytes_total > MAX_VISION_INPUT_BYTES:
        raise ValueError(f"combined vision inputs exceed the {MAX_VISION_INPUT_BYTES} byte limit")
    if provider.get("kind") == "openai":
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        content.extend({"type": "input_image", "image_url": image_url} for image_url in image_urls)
        payload = {
            "model": selected_model,
            "instructions": "Return strict JSON only. Do not claim validation beyond the supplied images.",
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": max_tokens,
        }
        response = _request_json(_responses_url(base_url), api_key, timeout, payload)
        content_text = str(response.get("output_text") or "")
        if not content_text:
            output = response.get("output", []) if isinstance(response, dict) else []
            parts = []
            for item in output:
                for chunk in item.get("content", []) if isinstance(item, dict) else []:
                    if isinstance(chunk, dict) and chunk.get("text"):
                        parts.append(str(chunk["text"]))
            content_text = "".join(parts)
        return ProviderCompletion(
            provider=provider_name,
            model=selected_model,
            base_url_host=_host(base_url),
            content=content_text,
            request_payload_redacted={
                "model": selected_model,
                "input_count": 1,
                "image_count": len(image_urls),
                "image_bytes_total": image_bytes_total,
                "max_output_tokens": max_tokens,
            },
            response_usage=response.get("usage", {}) if isinstance(response, dict) else {},
            proposal_status="proposal",
        )
    payload = {
        "model": selected_model,
        "messages": [
            {
                "role": "system",
                "content": "Return strict JSON only. Do not claim validation beyond the supplied images.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    *({"type": "image_url", "image_url": {"url": image_url}} for image_url in image_urls),
                ],
            },
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    response = _request_json(_chat_url(base_url), api_key, timeout, payload)
    choices = response.get("choices", []) if isinstance(response, dict) else []
    content_text = ""
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message", {})
        if isinstance(message, dict):
            content_text = str(message.get("content") or "")
    return ProviderCompletion(
        provider=provider_name,
        model=selected_model,
        base_url_host=_host(base_url),
        content=content_text,
        request_payload_redacted={
            "model": selected_model,
            "message_count": len(payload["messages"]),
            "image_count": len(image_urls),
            "image_bytes_total": image_bytes_total,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        response_usage=response.get("usage", {}) if isinstance(response, dict) else {},
        proposal_status="proposal",
    )


def completion_as_dict(completion: ProviderCompletion) -> dict[str, Any]:
    return {
        "provider": completion.provider,
        "model": completion.model,
        "base_url_host": completion.base_url_host,
        "content": completion.content,
        "request_payload_redacted": completion.request_payload_redacted,
        "response_usage": completion.response_usage,
        "proposal_status": completion.proposal_status,
    }
