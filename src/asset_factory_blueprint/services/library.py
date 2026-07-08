"""Library backings: indexes over materials, textures, USD assets and knowledge.

The operator points the factory at existing locations (local folders, an
Omniverse content estate, a USD Search endpoint, remote pack sources) and the
library turns them into searchable indexes. Agents ground material, texture,
asset and property choices in these indexes instead of inventing prims.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asset_factory_blueprint import __version__
from asset_factory_blueprint.config import ROOT, load_json
from asset_factory_blueprint.skills.base import ToolResult
from asset_factory_blueprint.utils.checksums import sha256_file


REGISTRY_PATH = "configs/library-registry.json"
TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
NETWORK_TIMEOUT = 45
USER_AGENT = f"asset-factory-blueprint-library/{__version__}"
MAX_SCAN_FILES = 20000

USD_SUFFIXES = {".usd", ".usda", ".usdc", ".usdz"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".exr"}
CHANNEL_HINTS = {
    "base_color": ("basecolor", "base_color", "albedo", "diffuse", "color", "col"),
    "normal": ("normal", "normalgl", "normaldx", "nor", "nrm"),
    "roughness": ("roughness", "rough", "rgh"),
    "metallic": ("metallic", "metalness", "metal"),
    "ao": ("ambientocclusion", "occlusion", "ao"),
    "height": ("height", "displacement", "disp", "bump"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(value: str) -> list[str]:
    return TOKEN_PATTERN.findall(value.lower())


def load_registry(registry_path: str = REGISTRY_PATH) -> dict[str, Any]:
    return load_json(registry_path)


def _user_backings(registry: dict[str, Any]) -> list[dict[str, Any]]:
    path = ROOT / registry.get("user_backings_file", "library/local/backings.json")
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return [item for item in payload.get("backings", []) if isinstance(item, dict)]


def list_backings(registry_path: str = REGISTRY_PATH) -> list[dict[str, Any]]:
    registry = load_registry(registry_path)
    backings = [dict(item) for item in registry.get("backings", [])]
    backings.extend(dict(item) for item in _user_backings(registry))
    for backing in backings:
        backing["resolved_path"] = _resolve_backing_path(backing)
        backing["status"] = _backing_status(backing)
    return backings


def _resolve_backing_path(backing: dict[str, Any]) -> str:
    if backing.get("path"):
        path = Path(str(backing["path"]))
        return path.as_posix() if path.exists() else ""
    env_name = backing.get("path_env", "")
    if env_name and os.environ.get(env_name):
        path = Path(os.environ[env_name])
        return path.as_posix() if path.exists() else ""
    for candidate in backing.get("candidate_paths", []):
        path = Path(candidate).expanduser()
        if path.exists():
            return path.as_posix()
    return ""


def _backing_status(backing: dict[str, Any]) -> str:
    kind = backing.get("kind", "")
    if not backing.get("enabled", True):
        return "disabled"
    if kind == "local_folder" or kind == "manual_pack":
        return "ready" if backing.get("resolved_path") else "not_configured"
    if kind == "usd_search":
        return "ready" if os.environ.get(backing.get("url_env", ""), "") else "not_configured"
    if kind == "remote_pack_source":
        return "declared"
    return "declared"


def _load_index_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _iter_indexes(registry: dict[str, Any]) -> list[dict[str, Any]]:
    indexes: list[dict[str, Any]] = []
    for rel in registry.get("curated_indexes", []):
        payload = _load_index_file(ROOT / rel)
        if payload.get("items"):
            payload["index_path"] = rel
            indexes.append(payload)
    local_root = ROOT / registry.get("local_index_root", "library/local")
    if local_root.exists():
        for path in sorted(local_root.glob("*-index.json")):
            payload = _load_index_file(path)
            if payload.get("items"):
                payload["index_path"] = path.relative_to(ROOT).as_posix()
                indexes.append(payload)
    return indexes


def _score_item(item: dict[str, Any], query_tokens: list[str]) -> int:
    if not query_tokens:
        return 1
    tags = {tag.lower() for tag in item.get("tags", [])}
    haystack = " ".join(
        [
            item.get("name", ""),
            item.get("description", ""),
            item.get("material_class", ""),
            item.get("item_id", ""),
        ]
    ).lower()
    score = 0
    for token in query_tokens:
        if token in tags:
            score += 3
        elif any(token in tag for tag in tags):
            score += 2
        if token in haystack:
            score += 1
    return score


def search_library(
    query: str,
    domains: list[str] | None = None,
    limit: int = 12,
    registry_path: str = REGISTRY_PATH,
) -> list[dict[str, Any]]:
    registry = load_registry(registry_path)
    query_tokens = _tokens(query)
    wanted = {domain.lower() for domain in domains or []}
    hits: list[dict[str, Any]] = []
    for index in _iter_indexes(registry):
        for item in index.get("items", []):
            domain = str(item.get("domain", index.get("domain", "")))
            if wanted and domain not in wanted:
                continue
            score = _score_item(item, query_tokens)
            if score <= 0:
                continue
            hits.append(
                {
                    "item_id": item.get("item_id", ""),
                    "name": item.get("name", ""),
                    "domain": domain,
                    "source": item.get("source", index.get("backing_id", "curated")),
                    "index": index.get("index_path", ""),
                    "uri": item.get("uri", ""),
                    "tags": item.get("tags", []),
                    "licence": item.get("licence", ""),
                    "material_class": item.get("material_class", ""),
                    "score": score,
                }
            )
    hits.sort(key=lambda item: (-item["score"], item["item_id"]))
    return hits[: max(1, limit)]


def lookup_physical_properties(material_class: str, registry_path: str = REGISTRY_PATH) -> dict[str, Any] | None:
    registry = load_registry(registry_path)
    for index in _iter_indexes(registry):
        if index.get("domain") != "physical_properties":
            continue
        for item in index.get("items", []):
            if item.get("material_class") == material_class:
                return item
    return None


def _auth_headers(api_key: str) -> dict[str, str]:
    if not api_key:
        return {}
    mode = os.environ.get("AFB_USD_SEARCH_AUTH_MODE", "bearer").lower()
    if mode == "api-key":
        return {"X-API-Key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def _http_json(url: str, api_key: str = "", body: dict[str, Any] | None = None) -> Any:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    headers.update(_auth_headers(api_key))
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, headers=headers, data=data)
    with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def usd_search_query(query: str, limit: int = 10) -> dict[str, Any]:
    """Query a configured USD Search API endpoint.

    The deployed service accepts a POST to /search with a JSON body; older
    deployments answer a GET with query parameters. Both are attempted, the
    method is configurable through AFB_USD_SEARCH_METHOD, and a response the
    client does not recognise is reported as its own status instead of being
    silently treated as zero grounding.
    """
    base = os.environ.get("AFB_USD_SEARCH_URL", "")
    if not base:
        return {"status": "not_configured", "hits": [], "note": "set AFB_USD_SEARCH_URL to a USD Search API endpoint"}
    api_key = os.environ.get("AFB_USD_SEARCH_API_KEY", "")
    endpoint = base.rstrip("/") + "/search"
    method = os.environ.get("AFB_USD_SEARCH_METHOD", "post").lower()
    attempts = ["post", "get"] if method == "post" else ["get", "post"]
    payload: Any = None
    errors: list[str] = []
    for attempt in attempts:
        try:
            if attempt == "post":
                payload = _http_json(endpoint, api_key, body={"description": query, "limit": limit})
            else:
                payload = _http_json(endpoint + "?" + urllib.parse.urlencode({"description": query, "limit": limit}), api_key)
            break
        except Exception as exc:
            errors.append(f"{attempt}: {exc}")
            payload = None
    if payload is None:
        return {"status": "blocked", "hits": [], "error": "; ".join(errors)}
    raw_items: list[Any]
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        raw_items = payload.get("results") or payload.get("assets") or payload.get("hits") or payload.get("items") or []
        if not raw_items and payload:
            return {
                "status": "unrecognised_response",
                "hits": [],
                "response_keys": sorted(payload.keys())[:12],
                "note": "the endpoint answered with a schema this client does not recognise; check the service version",
            }
    else:
        return {"status": "unrecognised_response", "hits": [], "note": f"unexpected payload type {type(payload).__name__}"}
    hits = []
    for item in raw_items[:limit]:
        if not isinstance(item, dict):
            continue
        uri = str(item.get("url") or item.get("uri") or item.get("path") or "")
        hits.append(
            {
                "item_id": "usd_search_" + re.sub(r"[^a-z0-9]+", "_", uri.lower())[-60:],
                "name": uri.rsplit("/", 1)[-1] or uri,
                "domain": "usd_assets",
                "source": "usd_search",
                "uri": uri,
                "score": item.get("score", 0),
            }
        )
    if raw_items and not hits:
        return {"status": "unrecognised_response", "hits": [], "note": "result items carry none of the expected url, uri or path keys"}
    return {"status": "ready", "hits": hits}


def _classify_texture_channel(stem: str) -> tuple[str, str] | None:
    lowered = stem.lower()
    for channel, hints in CHANNEL_HINTS.items():
        for hint in hints:
            marker = lowered.rfind(hint)
            if marker >= 0 and marker + len(hint) >= len(lowered) - 4:
                prefix = lowered[:marker].rstrip("_- .")
                return channel, prefix
    return None


def _index_local_root(root: Path, backing: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    texture_sets: dict[str, dict[str, str]] = {}
    scanned = 0
    for path in root.rglob("*"):
        if scanned >= MAX_SCAN_FILES:
            break
        if not path.is_file():
            continue
        scanned += 1
        rel = path.relative_to(root).as_posix()
        suffix = path.suffix.lower()
        tokens = sorted(set(_tokens(rel)))
        if suffix == ".mdl":
            items.append(
                {
                    "item_id": f"{backing['backing_id']}_mdl_{re.sub(r'[^a-z0-9]+', '_', rel.lower())}"[:120],
                    "name": path.stem,
                    "domain": "materials",
                    "source": backing["backing_id"],
                    "uri": path.as_posix(),
                    "tags": tokens,
                    "format": "mdl",
                }
            )
        elif suffix in USD_SUFFIXES:
            items.append(
                {
                    "item_id": f"{backing['backing_id']}_usd_{re.sub(r'[^a-z0-9]+', '_', rel.lower())}"[:120],
                    "name": path.stem,
                    "domain": "usd_assets",
                    "source": backing["backing_id"],
                    "uri": path.as_posix(),
                    "tags": tokens,
                    "format": suffix.lstrip("."),
                }
            )
        elif suffix in IMAGE_SUFFIXES:
            classified = _classify_texture_channel(path.stem)
            if classified:
                channel, prefix = classified
                key = (path.parent.relative_to(root).as_posix() + "/" + prefix).strip("/")
                texture_sets.setdefault(key, {})[channel] = path.as_posix()
    for key, channels in texture_sets.items():
        if len(channels) < 2:
            continue
        name = key.rsplit("/", 1)[-1] or key
        items.append(
            {
                "item_id": f"{backing['backing_id']}_tex_{re.sub(r'[^a-z0-9]+', '_', key.lower())}"[:120],
                "name": name,
                "domain": "textures",
                "source": backing["backing_id"],
                "uri": (root / key).parent.as_posix(),
                "tags": sorted(set(_tokens(key))),
                "format": "pbr_texture_set",
                "channels": channels,
            }
        )
    return items


def build_local_index(backing_id: str, registry_path: str = REGISTRY_PATH) -> dict[str, Any]:
    registry = load_registry(registry_path)
    backing = next((item for item in list_backings(registry_path) if item["backing_id"] == backing_id), None)
    if backing is None:
        return {"status": "blocked", "error": f"unknown backing: {backing_id}"}
    if backing.get("kind") not in {"local_folder", "manual_pack"}:
        return {"status": "blocked", "error": f"backing {backing_id} is not a local folder"}
    resolved = backing.get("resolved_path", "")
    if not resolved:
        return {"status": "not_configured", "error": f"backing {backing_id} has no resolvable path; set {backing.get('path_env', 'its path')} "}
    items = _index_local_root(Path(resolved), backing)
    index = {
        "id": f"{backing_id}-index",
        "version": "1.0",
        "domain": "mixed",
        "backing_id": backing_id,
        "generated_at": _now(),
        "root": resolved,
        "items": items,
    }
    local_root = ROOT / registry.get("local_index_root", "library/local")
    local_root.mkdir(parents=True, exist_ok=True)
    target = local_root / f"{backing_id}-index.json"
    target.write_text(json.dumps(index, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return {"status": "indexed", "backing_id": backing_id, "root": resolved, "item_count": len(items), "index_path": target.relative_to(ROOT).as_posix()}


def _ambientcg_search(query: str, limit: int) -> list[dict[str, Any]]:
    url = "https://ambientcg.com/api/v2/full_json?" + urllib.parse.urlencode(
        {"q": query, "type": "Material", "limit": limit, "include": "downloadData,tagData"}
    )
    payload = _http_json(url)
    assets = payload.get("foundAssets", []) if isinstance(payload, dict) else []
    results = []
    for asset in assets[:limit]:
        asset_id = str(asset.get("assetId", ""))
        download_url = ""
        folders = asset.get("downloadFolders", {})
        if isinstance(folders, dict):
            for folder in folders.values():
                categories = folder.get("downloadFiletypeCategories", {}) if isinstance(folder, dict) else {}
                zips = categories.get("zip", {}).get("downloads", []) if isinstance(categories, dict) else []
                for entry in zips:
                    attribute = str(entry.get("attribute", ""))
                    if attribute.startswith("1K"):
                        download_url = str(entry.get("downloadLink", ""))
                        break
                if not download_url and zips:
                    download_url = str(zips[0].get("downloadLink", ""))
        results.append(
            {
                "item_id": f"ambientcg_{asset_id.lower()}",
                "name": asset.get("displayName", asset_id),
                "domain": "textures",
                "source": "ambientcg",
                "uri": f"https://ambientcg.com/view?id={asset_id}",
                "tags": [str(tag).lower() for tag in asset.get("tags", [])],
                "licence": "CC0",
                "download_url": download_url,
            }
        )
    return results


def _polyhaven_search(query: str, limit: int) -> list[dict[str, Any]]:
    payload = _http_json("https://api.polyhaven.com/assets?" + urllib.parse.urlencode({"t": "textures"}))
    if not isinstance(payload, dict):
        return []
    query_tokens = _tokens(query)
    scored = []
    for asset_id, meta in payload.items():
        if not isinstance(meta, dict):
            continue
        tags = [str(tag).lower() for tag in meta.get("tags", []) + meta.get("categories", [])]
        haystack = (str(meta.get("name", "")) + " " + " ".join(tags)).lower()
        score = sum(3 if token in tags else (1 if token in haystack else 0) for token in query_tokens)
        if score > 0:
            scored.append(
                (
                    score,
                    {
                        "item_id": f"polyhaven_{asset_id}",
                        "name": meta.get("name", asset_id),
                        "domain": "textures",
                        "source": "polyhaven",
                        "uri": f"https://polyhaven.com/a/{asset_id}",
                        "tags": tags,
                        "licence": "CC0",
                        "asset_id": asset_id,
                    },
                )
            )
    scored.sort(key=lambda entry: -entry[0])
    return [entry[1] for entry in scored[:limit]]


def search_remote_sources(query: str, sources: list[str] | None, limit: int) -> dict[str, Any]:
    wanted = set(sources or ["ambientcg", "polyhaven"])
    results: dict[str, Any] = {}
    for source in sorted(wanted):
        try:
            if source == "ambientcg":
                results[source] = {"status": "ready", "hits": _ambientcg_search(query, limit)}
            elif source == "polyhaven":
                results[source] = {"status": "ready", "hits": _polyhaven_search(query, limit)}
            else:
                results[source] = {"status": "blocked", "hits": [], "error": f"unknown remote source: {source}"}
        except Exception as exc:
            results[source] = {"status": "blocked", "hits": [], "error": str(exc)}
    return results


MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


def _safe_path_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value).strip("._")
    return cleaned or "item"


def _download_file(url: str, target: Path) -> dict[str, Any]:
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme != "https":
        raise ValueError(f"refusing non-https download url: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT * 4) as response, target.open("wb") as handle:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"download exceeds the {MAX_DOWNLOAD_BYTES >> 20} MB cap: {url}")
        while True:
            chunk = response.read(1 << 16)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_DOWNLOAD_BYTES:
                raise ValueError(f"download exceeded the {MAX_DOWNLOAD_BYTES >> 20} MB cap: {url}")
            handle.write(chunk)
    return {"path": target.as_posix(), "sha256": sha256_file(target), "size_bytes": target.stat().st_size}


def _polyhaven_download_plan(asset_id: str) -> list[dict[str, str]]:
    payload = _http_json(f"https://api.polyhaven.com/files/{asset_id}")
    if not isinstance(payload, dict):
        return []
    channel_map = {"Diffuse": "base_color", "nor_gl": "normal", "Rough": "roughness", "AO": "ao", "Displacement": "height", "Metal": "metallic"}
    plan = []
    for source_channel, channel in channel_map.items():
        entry = payload.get(source_channel, {})
        for resolution in ("1k", "2k"):
            candidate = entry.get(resolution, {}) if isinstance(entry, dict) else {}
            for fmt in ("jpg", "png"):
                record = candidate.get(fmt, {}) if isinstance(candidate, dict) else {}
                url = record.get("url", "") if isinstance(record, dict) else ""
                if url:
                    plan.append({"channel": channel, "url": url, "file_name": f"{asset_id}_{channel}_{resolution}.{fmt}"})
                    break
            if any(item["channel"] == channel for item in plan):
                break
    return plan


def fetch_from_source(
    source: str,
    query: str = "",
    item_ids: list[str] | None = None,
    limit: int = 5,
    dry_run: bool = True,
    registry_path: str = REGISTRY_PATH,
) -> dict[str, Any]:
    registry = load_registry(registry_path)
    download_root = ROOT / registry.get("download_root", "library/downloads")
    search_breadth = max(limit, 48 if item_ids else limit, 4 * len(item_ids or []))
    remote = search_remote_sources(query or "", [source], search_breadth)
    source_result = remote.get(source, {"status": "blocked", "hits": [], "error": "source unavailable"})
    if source_result["status"] != "ready":
        return {"status": "blocked", "source": source, "error": source_result.get("error", "source unavailable"), "downloads": []}
    hits = source_result["hits"]
    unresolved: list[str] = []
    if item_ids:
        wanted = set(item_ids)
        by_id = {hit["item_id"]: hit for hit in hits}
        hits = [by_id[item] for item in item_ids if item in by_id]
        unresolved = sorted(wanted - set(by_id))
    hits = hits[: max(limit, len(item_ids or []))]
    downloads = []
    index_items = []
    for hit in hits:
        record: dict[str, Any] = {"item_id": hit["item_id"], "name": hit["name"], "status": "planned"}
        safe_id = _safe_path_segment(hit["item_id"])
        if source == "ambientcg":
            record["url"] = hit.get("download_url", "")
            record["target"] = (download_root / "ambientcg" / f"{safe_id}.zip").as_posix()
            record["import_note"] = "unpack with scripts/texturing/import_ambientcg_pbr_textures.py to normalise channel naming"
            if not record["url"]:
                record["status"] = "blocked"
                record["error"] = "no download link in the API response"
            elif not dry_run:
                try:
                    result = _download_file(record["url"], Path(record["target"]))
                except Exception as exc:
                    record["status"] = "blocked"
                    record["error"] = str(exc)
                else:
                    record.update(result)
                    record["status"] = "cached"
        elif source == "polyhaven":
            safe_asset = _safe_path_segment(hit.get("asset_id", hit["item_id"].removeprefix("polyhaven_")))
            record["target"] = (download_root / "polyhaven" / safe_asset).as_posix()
            if dry_run:
                record["files"] = "channel map resolved at download time"
            else:
                try:
                    plan = _polyhaven_download_plan(hit.get("asset_id", safe_asset))
                except Exception as exc:
                    record["status"] = "blocked"
                    record["error"] = str(exc)
                    plan = []
                record["files"] = plan
                if plan:
                    stored = []
                    try:
                        for entry in plan:
                            stored.append({**entry, **_download_file(entry["url"], Path(record["target"]) / _safe_path_segment(entry["file_name"]))})
                    except Exception as exc:
                        record["status"] = "blocked"
                        record["error"] = str(exc)
                    else:
                        record["files"] = stored
                        record["status"] = "cached"
        downloads.append(record)
        if record["status"] == "cached":
            index_items.append(
                {
                    "item_id": hit["item_id"],
                    "name": hit["name"],
                    "domain": hit["domain"],
                    "source": source,
                    "uri": record.get("target", ""),
                    "tags": hit.get("tags", []),
                    "licence": hit.get("licence", ""),
                    "cached": True,
                }
            )
    if index_items:
        local_root = ROOT / registry.get("local_index_root", "library/local")
        local_root.mkdir(parents=True, exist_ok=True)
        target = local_root / f"{source}-cache-index.json"
        existing = _load_index_file(target)
        merged = {item["item_id"]: item for item in existing.get("items", [])}
        for item in index_items:
            merged[item["item_id"]] = item
        target.write_text(
            json.dumps(
                {
                    "id": f"{source}-cache-index",
                    "version": "1.0",
                    "domain": "textures",
                    "backing_id": source,
                    "generated_at": _now(),
                    "items": sorted(merged.values(), key=lambda item: item["item_id"]),
                },
                indent=2,
                sort_keys=False,
            )
            + "\n",
            encoding="utf-8",
        )
    status = "planned" if dry_run else "completed"
    if unresolved and not downloads:
        status = "blocked"
    return {
        "status": status,
        "source": source,
        "query": query,
        "dry_run": dry_run,
        "downloads": downloads,
        "unresolved_item_ids": unresolved,
        "cached_count": sum(1 for item in downloads if item["status"] == "cached"),
    }


def asset_library_search(params: dict[str, Any]) -> ToolResult:
    query = str(params.get("query") or "")
    if not query:
        return ToolResult(success=False, error="query is required", validation_status="blocked")
    domains = [str(item) for item in params.get("domains") or []]
    limit = int(params.get("limit") or 12)
    hits = search_library(query, domains or None, limit)
    data: dict[str, Any] = {"query": query, "domains": domains, "hits": hits}
    if params.get("include_usd_search", True) and (not domains or "usd_assets" in domains):
        data["usd_search"] = usd_search_query(query, limit)
    if params.get("include_remote"):
        data["remote_sources"] = search_remote_sources(query, params.get("remote_sources"), limit)
    grounded = bool(hits) or bool(data.get("usd_search", {}).get("hits"))
    warnings = [] if grounded else [f"no library grounding found for query: {query}"]
    return ToolResult(
        success=True,
        data=data,
        warnings=warnings,
        validation_status="proposal" if grounded else "review_required",
    )


def asset_library_index(params: dict[str, Any]) -> ToolResult:
    backing_id = str(params.get("backing_id") or "")
    results = []
    if backing_id:
        results.append(build_local_index(backing_id))
    else:
        for backing in list_backings():
            if backing.get("kind") in {"local_folder", "manual_pack"} and backing.get("resolved_path"):
                results.append(build_local_index(backing["backing_id"]))
        if not results:
            return ToolResult(
                success=False,
                data={"results": []},
                error="no local backings resolve to a path; set the backing environment handles or add user backings",
                validation_status="review_required",
            )
    indexed = [item for item in results if item.get("status") == "indexed"]
    return ToolResult(
        success=bool(indexed),
        data={"results": results, "indexed_count": len(indexed)},
        warnings=[item.get("error", "") for item in results if item.get("status") != "indexed" and item.get("error")],
        artefacts=[item["index_path"] for item in indexed if item.get("index_path")],
        validation_status="proposal" if indexed else "review_required",
    )


def asset_library_fetch(params: dict[str, Any]) -> ToolResult:
    source = str(params.get("source") or "ambientcg")
    result = fetch_from_source(
        source,
        query=str(params.get("query") or ""),
        item_ids=[str(item) for item in params.get("item_ids") or []] or None,
        limit=int(params.get("limit") or 5),
        dry_run=bool(params.get("dry_run", True)),
    )
    return ToolResult(
        success=result["status"] in {"planned", "completed"},
        data=result,
        error=result.get("error"),
        validation_status="proposal" if result["status"] in {"planned", "completed"} else "blocked",
    )
