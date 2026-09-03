#!/usr/bin/env python3
"""Verify that a GitHub release was archived in the existing Zenodo DOI series."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_SEED_RECORD_ID = "22201830"
DEFAULT_CONCEPT_DOI = "10.5281/zenodo.22201829"
REPOSITORY_URL = "https://github.com/hcltech-robotics/asset-factory-blueprint"
SEMVER_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
ZENODO_DOI = re.compile(r"^10\.5281/zenodo\.[0-9]+$")
LEGACY_INCOMPLETE_SOURCE_METADATA = {"v1.1.0"}


def normalise_version(value: str) -> str:
    """Return a release version without its conventional leading ``v``."""

    return value.strip().removeprefix("v")


def records_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    hits = payload.get("hits")
    if not isinstance(hits, dict) or not isinstance(hits.get("hits"), list):
        raise ValueError("Zenodo versions response does not contain hits.hits")
    return [record for record in hits["hits"] if isinstance(record, dict)]


def find_version_record(records: list[dict[str, Any]], tag: str) -> dict[str, Any] | None:
    expected = normalise_version(tag)
    for record in records:
        metadata = record.get("metadata") or {}
        if normalise_version(str(metadata.get("version") or "")) == expected:
            return record
    return None


def _normalise_licence(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold()).removesuffix("license")


def _normalise_repository_url(value: str) -> str:
    return value.strip().removesuffix(".git").rstrip("/")


def _orcid_id(value: str) -> str:
    return value.strip().removeprefix("https://orcid.org/").removeprefix("http://orcid.org/")


def _cff_scalar(text: str, key: str) -> str:
    match = re.search(rf'(?m)^{re.escape(key)}:\s*"?(?P<value>[^"\r\n]+?)"?\s*$', text)
    return match.group("value").strip() if match else ""


def _cff_preferred_scalar(text: str, key: str) -> str:
    match = re.search(rf'(?m)^  {re.escape(key)}:\s*"?(?P<value>[^"\r\n]+?)"?\s*$', text)
    return match.group("value").strip() if match else ""


def _cff_authors(text: str) -> list[dict[str, str]]:
    authors: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    in_authors = False
    for line in text.splitlines():
        if line == "authors:":
            in_authors = True
            continue
        if not in_authors:
            continue
        if line and not line.startswith("  "):
            break
        author_start = re.match(r'^  - (?P<key>[a-z-]+):\s*"?(?P<value>[^"\r\n]+?)"?\s*$', line)
        if author_start:
            current = {author_start.group("key"): author_start.group("value").strip()}
            authors.append(current)
            continue
        author_field = re.match(r'^    (?P<key>[a-z-]+):\s*"?(?P<value>[^"\r\n]+?)"?\s*$', line)
        if author_field and current is not None:
            current[author_field.group("key")] = author_field.group("value").strip()
    return authors


def _zenodo_creator_names(repository_metadata: dict[str, Any]) -> list[str]:
    return [
        " ".join(reversed([part.strip() for part in str(creator.get("name") or "").split(",", 1)]))
        for creator in repository_metadata.get("creators") or []
    ]


def _doi_value(value: str) -> str:
    return value.strip().removeprefix("https://doi.org/").removeprefix("http://doi.org/")


def _doi_url_value(value: str) -> str:
    stripped = value.strip()
    return _doi_value(stripped) if stripped.startswith(("https://doi.org/", "http://doi.org/")) else ""


def _mkdocs_extra_scalar(text: str, key: str) -> str:
    match = re.search(rf'(?m)^  {re.escape(key)}:[ \t]*"?(?P<value>[^"\r\n]*?)"?[ \t]*$', text)
    return match.group("value").strip() if match else ""


def source_version_doi_state(source_root: Path, concept_doi: str) -> tuple[str | None, list[str]]:
    """Return the tagged exact-version DOI, enforcing assigned or pre-mint consistency."""

    errors: list[str] = []
    paths = {
        "pyproject": source_root / "pyproject.toml",
        "citation": source_root / "CITATION.cff",
        "codemeta": source_root / "codemeta.json",
        "mkdocs": source_root / "mkdocs.yml",
    }
    missing = [path.relative_to(source_root).as_posix() for path in paths.values() if not path.is_file()]
    if missing:
        return None, [f"tagged DOI metadata files are missing: {missing!r}"]

    try:
        pyproject = tomllib.loads(paths["pyproject"].read_text(encoding="utf-8"))
        cff_text = paths["citation"].read_text(encoding="utf-8")
        codemeta = json.loads(paths["codemeta"].read_text(encoding="utf-8"))
        mkdocs_text = paths["mkdocs"].read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return None, [f"tagged DOI metadata could not be parsed: {exc}"]

    cff_dois = set(re.findall(r'(?m)^\s+value:\s*"?(10\.5281/zenodo\.[0-9]+)"?\s*$', cff_text))
    cff_version_dois = cff_dois - {concept_doi}
    if len(cff_version_dois) > 1:
        errors.append(f"CITATION.cff declares multiple version DOIs: {sorted(cff_version_dois)!r}")
    cff_identifier_doi = next(iter(cff_version_dois), "")
    project = pyproject.get("project") or {}
    surfaces = {
        "pyproject.toml DOI": _doi_value(str(((project.get("urls") or {}).get("DOI") or ""))),
        "CITATION.cff preferred DOI": _doi_value(_cff_preferred_scalar(cff_text, "doi")),
        "CITATION.cff version identifier": cff_identifier_doi,
        "CITATION.cff preferred URL": _doi_url_value(_cff_preferred_scalar(cff_text, "url")),
        "codemeta.json identifier": _doi_url_value(str(codemeta.get("identifier") or "")),
        "codemeta.json citation": _doi_url_value(str(codemeta.get("citation") or "")),
        "mkdocs.yml version_doi": _doi_value(_mkdocs_extra_scalar(mkdocs_text, "version_doi")),
    }
    present = {value for value in surfaces.values() if value}
    version_doi = next(iter(present), None)
    if present and (len(present) != 1 or any(not value for value in surfaces.values())):
        errors.append(f"version DOI surfaces are partially populated or disagree: {surfaces!r}")
    if version_doi is not None and (not ZENODO_DOI.fullmatch(version_doi) or version_doi == concept_doi):
        errors.append(f"tagged exact-version DOI is invalid: {version_doi!r}")

    same_as = [str(value) for value in codemeta.get("sameAs") or []]
    codemeta_record_urls = [value for value in same_as if re.fullmatch(r"https://zenodo\.org/records/[0-9]+", value)]
    codemeta_download_url = str(codemeta.get("downloadUrl") or "")
    mkdocs_archive_url = _mkdocs_extra_scalar(mkdocs_text, "archive_url")
    mkdocs_download_url = _mkdocs_extra_scalar(mkdocs_text, "archive_download_url")
    if version_doi is None:
        stale_urls = codemeta_record_urls + [
            value for value in (codemeta_download_url, mkdocs_archive_url, mkdocs_download_url) if value
        ]
        if stale_urls:
            errors.append(f"pre-mint metadata retains exact-record or download URLs: {stale_urls!r}")
    else:
        record_id = version_doi.rsplit(".", 1)[-1]
        expected_record_url = f"https://zenodo.org/records/{record_id}"
        if codemeta_record_urls != [expected_record_url]:
            errors.append(f"codemeta.json exact record mismatch: {codemeta_record_urls!r}")
        if mkdocs_archive_url != expected_record_url:
            errors.append(f"mkdocs.yml exact record mismatch: {mkdocs_archive_url!r}")
        for surface, url in {
            "codemeta.json downloadUrl": codemeta_download_url,
            "mkdocs.yml archive_download_url": mkdocs_download_url,
        }.items():
            if f"/records/{record_id}/" not in url:
                errors.append(f"{surface} does not belong to DOI {version_doi!r}: {url!r}")

    return version_doi, errors


def validate_source_record_doi(source_version_doi: str | None, record_doi: str) -> list[str]:
    """Require an assigned tagged DOI to identify the record that Zenodo minted."""

    if source_version_doi is not None and source_version_doi != record_doi:
        return [f"tagged source DOI {source_version_doi!r} does not match minted record DOI {record_doi!r}"]
    return []


def validate_source_metadata(
    source_root: Path,
    repository_metadata: dict[str, Any],
    *,
    tag: str,
    concept_doi: str = DEFAULT_CONCEPT_DOI,
) -> list[str]:
    """Validate version and creator identity in the exact tagged source tree."""

    errors: list[str] = []
    required_paths = {
        "pyproject": source_root / "pyproject.toml",
        "runtime": source_root / "src" / "asset_factory_blueprint" / "__init__.py",
        "citation": source_root / "CITATION.cff",
        "codemeta": source_root / "codemeta.json",
    }
    missing = [path.relative_to(source_root).as_posix() for path in required_paths.values() if not path.is_file()]
    if missing:
        return [f"tagged source metadata files are missing: {missing!r}"]

    try:
        pyproject = tomllib.loads(required_paths["pyproject"].read_text(encoding="utf-8"))
        runtime_text = required_paths["runtime"].read_text(encoding="utf-8")
        cff_text = required_paths["citation"].read_text(encoding="utf-8")
        codemeta = json.loads(required_paths["codemeta"].read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        return [f"tagged source metadata could not be parsed: {exc}"]

    project = pyproject.get("project") or {}
    runtime_match = re.search(r'(?m)^__version__\s*=\s*["\'](?P<version>[^"\']+)["\']\s*$', runtime_text)
    versions = {
        "pyproject.toml": str(project.get("version") or ""),
        "package runtime": runtime_match.group("version") if runtime_match else "",
        "CITATION.cff": _cff_scalar(cff_text, "version"),
        "codemeta.json": str(codemeta.get("version") or ""),
    }
    expected_version = normalise_version(tag)
    for surface, version in versions.items():
        if normalise_version(version) != expected_version:
            errors.append(f"tagged {surface} version mismatch: expected {expected_version!r}, got {version!r}")

    expected_names = _zenodo_creator_names(repository_metadata)
    cff_authors = _cff_authors(cff_text)
    author_surfaces = {
        "CITATION.cff": [
            " ".join((author.get("given-names", ""), author.get("family-names", ""))).strip()
            for author in cff_authors
        ],
        "codemeta.json": [
            " ".join((str(author.get("givenName") or ""), str(author.get("familyName") or ""))).strip()
            for author in codemeta.get("author") or []
        ],
    }
    if tag not in LEGACY_INCOMPLETE_SOURCE_METADATA:
        author_surfaces["pyproject.toml"] = [
            str(author.get("name") or "") for author in project.get("authors") or []
        ]
    for surface, authors in author_surfaces.items():
        if authors != expected_names:
            errors.append(f"tagged {surface} creator identity/order mismatch: {authors!r} != {expected_names!r}")

    expected_orcids = [_orcid_id(str(creator.get("orcid") or "")) for creator in repository_metadata.get("creators") or []]
    orcid_surfaces = {
        "CITATION.cff": [_orcid_id(author.get("orcid", "")) for author in cff_authors],
        "codemeta.json": [_orcid_id(str(author.get("@id") or "")) for author in codemeta.get("author") or []],
    }
    for surface, orcids in orcid_surfaces.items():
        if orcids != expected_orcids or any(not value for value in orcids):
            errors.append(f"tagged {surface} ORCID identity/order mismatch: {orcids!r} != {expected_orcids!r}")

    if tag not in LEGACY_INCOMPLETE_SOURCE_METADATA:
        expected_affiliations = [
            str(creator.get("affiliation") or "") for creator in repository_metadata.get("creators") or []
        ]
        affiliation_surfaces = {
            "CITATION.cff": [author.get("affiliation", "") for author in cff_authors],
            "codemeta.json": [
                str((author.get("affiliation") or {}).get("name") or "")
                for author in codemeta.get("author") or []
            ],
        }
        for surface, affiliations in affiliation_surfaces.items():
            if affiliations != expected_affiliations:
                errors.append(
                    f"tagged {surface} affiliation/order mismatch: "
                    f"{affiliations!r} != {expected_affiliations!r}"
                )

    repositories = {
        "pyproject.toml": str(((project.get("urls") or {}).get("Repository") or "")),
        "CITATION.cff": _cff_scalar(cff_text, "repository-code"),
        "codemeta.json": str(codemeta.get("codeRepository") or ""),
    }
    for surface, repository in repositories.items():
        if _normalise_repository_url(repository) != REPOSITORY_URL:
            errors.append(f"tagged {surface} repository mismatch: {repository!r}")

    _, doi_errors = source_version_doi_state(source_root, concept_doi)
    errors.extend(doi_errors)
    return errors


def validate_record(
    record: dict[str, Any],
    repository_metadata: dict[str, Any],
    *,
    tag: str,
    concept_doi: str,
) -> list[str]:
    """Return every release/metadata mismatch found in a Zenodo record."""

    errors: list[str] = []
    metadata = record.get("metadata") or {}
    record_doi = str(record.get("doi") or metadata.get("doi") or "")
    if not ZENODO_DOI.fullmatch(record_doi):
        errors.append(f"version DOI is missing or malformed: {record_doi!r}")
    elif record_doi == concept_doi:
        errors.append("version DOI must differ from the concept DOI")
    if str(metadata.get("doi") or "") != record_doi:
        errors.append(f"record/metadata DOI mismatch: {record_doi!r} != {metadata.get('doi')!r}")
    if str(record.get("conceptdoi") or "") != concept_doi:
        errors.append(
            f"concept DOI mismatch: expected {concept_doi!r}, got {record.get('conceptdoi')!r}"
        )
    if str(record.get("conceptrecid") or "") != concept_doi.rsplit(".", 1)[-1]:
        errors.append(
            "concept record mismatch: "
            f"expected {concept_doi.rsplit('.', 1)[-1]!r}, got {record.get('conceptrecid')!r}"
        )
    if normalise_version(str(metadata.get("version") or "")) != normalise_version(tag):
        errors.append(f"version mismatch: expected {tag!r}, got {metadata.get('version')!r}")
    if metadata.get("title") != repository_metadata.get("title"):
        errors.append(
            f"title mismatch: expected {repository_metadata.get('title')!r}, got {metadata.get('title')!r}"
        )
    expected_type = str(repository_metadata.get("upload_type") or "")
    actual_type_value = metadata.get("resource_type") or ""
    if isinstance(actual_type_value, dict):
        actual_type_value = actual_type_value.get("type") or ""
    if str(actual_type_value) != expected_type:
        errors.append(f"resource type mismatch: expected {expected_type!r}, got {actual_type_value!r}")
    if list(metadata.get("keywords") or []) != list(repository_metadata.get("keywords") or []):
        errors.append("keyword values/order do not match .zenodo.json")

    expected_creators = repository_metadata.get("creators") or []
    actual_creators = metadata.get("creators") or []
    creator_fields = ("name", "affiliation", "orcid")
    expected_identity = [
        {field: str(creator.get(field) or "") for field in creator_fields}
        for creator in expected_creators
    ]
    actual_identity = [
        {field: str(creator.get(field) or "") for field in creator_fields}
        for creator in actual_creators
    ]
    if actual_identity != expected_identity:
        errors.append(f"creator identity/order mismatch: expected {expected_identity!r}, got {actual_identity!r}")

    expected_licence = _normalise_licence(str(repository_metadata.get("license") or ""))
    actual_licence_value = metadata.get("license") or ""
    if isinstance(actual_licence_value, dict):
        actual_licence_value = actual_licence_value.get("id") or actual_licence_value.get("title") or ""
    actual_licence = _normalise_licence(str(actual_licence_value))
    if expected_licence != actual_licence:
        errors.append(
            f"licence mismatch: expected {repository_metadata.get('license')!r}, got {actual_licence_value!r}"
        )

    if metadata.get("access_right") != repository_metadata.get("access_right"):
        errors.append(
            "access-right mismatch: "
            f"expected {repository_metadata.get('access_right')!r}, got {metadata.get('access_right')!r}"
        )

    expected_relations = {
        (str(item.get("relation") or ""), str(item.get("identifier") or ""))
        for item in repository_metadata.get("related_identifiers") or []
    }
    actual_relations = {
        (str(item.get("relation") or ""), str(item.get("identifier") or ""))
        for item in metadata.get("related_identifiers") or []
    }
    missing_relations = expected_relations - actual_relations
    if missing_relations:
        errors.append(f"related identifiers missing from Zenodo metadata: {sorted(missing_relations)!r}")

    code_repository = str((metadata.get("custom") or {}).get("code:codeRepository") or "").rstrip("/")
    if code_repository != REPOSITORY_URL:
        errors.append(f"code repository mismatch: expected {REPOSITORY_URL!r}, got {code_repository!r}")

    expected_archive_suffix = f"asset-factory-blueprint-{tag}.zip"
    source_archives = [
        item
        for item in record.get("files") or []
        if str(item.get("key") or "").endswith(expected_archive_suffix)
    ]
    if not source_archives:
        errors.append(f"archived GitHub source file ending in {expected_archive_suffix!r} is missing")
    elif any(not str(item.get("checksum") or "") or int(item.get("size") or 0) <= 0 for item in source_archives):
        errors.append("archived GitHub source file is missing its checksum or size")

    return errors


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "asset-factory-blueprint-zenodo-verifier/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError(f"Zenodo returned a non-object payload for {url}")
    return payload


def fetch_versions(seed_record_id: str, timeout: float) -> list[dict[str, Any]]:
    url: str | None = f"https://zenodo.org/api/records/{seed_record_id}/versions?size=25"
    records: list[dict[str, Any]] = []
    page_count = 0
    while url:
        page_count += 1
        if page_count > 100:
            raise ValueError("Zenodo versions pagination exceeded 100 pages")
        payload = fetch_json(url, timeout)
        records.extend(records_from_payload(payload))
        links = payload.get("links") or {}
        next_url = links.get("next") if isinstance(links, dict) else None
        url = str(next_url) if next_url else None
    return records


def verify_with_polling(
    *,
    tag: str,
    seed_record_id: str,
    concept_doi: str,
    repository_metadata: dict[str, Any],
    attempts: int,
    interval: float,
    timeout: float,
) -> dict[str, Any]:
    last_problem = "release record was not found"
    for attempt in range(1, attempts + 1):
        try:
            record = find_version_record(fetch_versions(seed_record_id, timeout), tag)
            if record is None:
                last_problem = f"Zenodo has not exposed version {tag!r} in this DOI series"
            else:
                errors = validate_record(record, repository_metadata, tag=tag, concept_doi=concept_doi)
                if not errors:
                    return record
                last_problem = "; ".join(errors)
        except (OSError, TimeoutError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
            last_problem = f"Zenodo API check failed: {exc}"
        print(f"Attempt {attempt}/{attempts}: {last_problem}", file=sys.stderr, flush=True)
        if attempt < attempts:
            time.sleep(interval)
    raise RuntimeError(last_problem)


def _write_github_outputs(record: dict[str, Any]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not output_path:
        return
    doi = str(record.get("doi") or "")
    record_url = str((record.get("links") or {}).get("self_html") or "")
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"doi={doi}\n")
        output.write(f"doi_url=https://doi.org/{doi}\n")
        output.write(f"record_url={record_url}\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Published GitHub release tag, for example v1.1.0")
    parser.add_argument("--metadata", type=Path, default=Path(".zenodo.json"))
    parser.add_argument("--source-root", type=Path, default=Path("."))
    parser.add_argument("--seed-record-id", default=DEFAULT_SEED_RECORD_ID)
    parser.add_argument("--concept-doi", default=DEFAULT_CONCEPT_DOI)
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--interval", type=float, default=20.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not SEMVER_TAG.fullmatch(args.tag):
        raise SystemExit(f"release tag must be semantic and start with 'v': {args.tag!r}")
    if not ZENODO_DOI.fullmatch(args.concept_doi):
        raise SystemExit(f"concept DOI is not a supported Zenodo DOI: {args.concept_doi!r}")
    if args.attempts < 1:
        raise SystemExit("--attempts must be at least 1")
    if args.interval < 0 or args.timeout <= 0:
        raise SystemExit("--interval must be non-negative and --timeout must be positive")
    repository_metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    source_errors = validate_source_metadata(
        args.source_root,
        repository_metadata,
        tag=args.tag,
        concept_doi=args.concept_doi,
    )
    if source_errors:
        raise SystemExit("Tagged source metadata validation failed: " + "; ".join(source_errors))
    if args.tag in LEGACY_INCOMPLETE_SOURCE_METADATA:
        print(
            f"Warning: {args.tag} predates strict package-author and affiliation checks; "
            "CITATION.cff, CodeMeta and ORCID identities were still verified.",
            file=sys.stderr,
        )
    record = verify_with_polling(
        tag=args.tag,
        seed_record_id=args.seed_record_id,
        concept_doi=args.concept_doi,
        repository_metadata=repository_metadata,
        attempts=args.attempts,
        interval=args.interval,
        timeout=args.timeout,
    )
    doi = str(record["doi"])
    source_version_doi, _ = source_version_doi_state(args.source_root, args.concept_doi)
    source_record_errors = validate_source_record_doi(source_version_doi, doi)
    if source_record_errors:
        raise SystemExit("Tagged source DOI validation failed: " + "; ".join(source_record_errors))
    record_url = str((record.get("links") or {}).get("self_html") or f"https://doi.org/{doi}")
    print(f"Verified {args.tag}: {record_url}")
    print(f"Version DOI: {doi}")
    print(f"Concept DOI: {args.concept_doi}")
    _write_github_outputs(record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
