from __future__ import annotations

import importlib.util
import json
import re
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONCEPT_DOI = "10.5281/zenodo.22201829"
SPEC = importlib.util.spec_from_file_location(
    "verify_zenodo_release",
    ROOT / "scripts" / "ci" / "verify_zenodo_release.py",
)
assert SPEC is not None and SPEC.loader is not None
VERIFY_ZENODO_RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY_ZENODO_RELEASE)
find_version_record = VERIFY_ZENODO_RELEASE.find_version_record
records_from_payload = VERIFY_ZENODO_RELEASE.records_from_payload
validate_record = VERIFY_ZENODO_RELEASE.validate_record
validate_source_metadata = VERIFY_ZENODO_RELEASE.validate_source_metadata
source_version_doi_state = VERIFY_ZENODO_RELEASE.source_version_doi_state
validate_source_record_doi = VERIFY_ZENODO_RELEASE.validate_source_record_doi


def _repository_metadata() -> dict[str, object]:
    return json.loads((ROOT / ".zenodo.json").read_text(encoding="utf-8"))


def _record() -> dict[str, object]:
    repository_metadata = _repository_metadata()
    return {
        "id": 22201830,
        "doi": "10.5281/zenodo.22201830",
        "conceptdoi": CONCEPT_DOI,
        "conceptrecid": "22201829",
        "metadata": {
            "title": repository_metadata["title"],
            "doi": "10.5281/zenodo.22201830",
            "version": "v1.1.0",
            "creators": repository_metadata["creators"],
            "keywords": repository_metadata["keywords"],
            "license": {"id": "mit-license"},
            "access_right": repository_metadata["access_right"],
            "resource_type": {"type": repository_metadata["upload_type"]},
            "related_identifiers": repository_metadata["related_identifiers"],
            "custom": {"code:codeRepository": "https://github.com/hcltech-robotics/asset-factory-blueprint"},
        },
        "files": [
            {
                "key": "hcltech-robotics/asset-factory-blueprint-v1.1.0.zip",
                "size": 5_421_626,
                "checksum": "md5:6a53fa1c5258c31ecc952ed564e28ae5",
            }
        ],
        "links": {"self_html": "https://zenodo.org/records/22201830"},
    }


def test_current_release_record_matches_repository_metadata() -> None:
    errors = validate_record(_record(), _repository_metadata(), tag="v1.1.0", concept_doi=CONCEPT_DOI)

    assert errors == []


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (lambda record: record.update(conceptdoi="10.5281/zenodo.1"), "concept DOI mismatch"),
        (lambda record: record["metadata"].update(doi="10.5281/zenodo.1"), "record/metadata DOI mismatch"),
        (lambda record: record.update(conceptrecid="1"), "concept record mismatch"),
        (lambda record: record["metadata"].update(version="v1.2.0"), "version mismatch"),
        (lambda record: record["metadata"].update(creators=[]), "creator identity/order mismatch"),
        (lambda record: record["metadata"].update(resource_type={"type": "dataset"}), "resource type mismatch"),
        (lambda record: record["metadata"].update(keywords=[]), "keyword values/order"),
        (lambda record: record.update(files=[]), "archived GitHub source file"),
    ],
)
def test_release_verifier_reports_identity_drift(mutation, expected_error: str) -> None:
    record = deepcopy(_record())
    mutation(record)

    errors = validate_record(record, _repository_metadata(), tag="v1.1.0", concept_doi=CONCEPT_DOI)

    assert any(expected_error in error for error in errors)


def test_versions_payload_and_tag_matching_accept_zenodo_v_prefix() -> None:
    record = _record()
    records = records_from_payload({"hits": {"hits": [record]}})

    assert find_version_record(records, "v1.1.0") == record


def _copy_source_metadata(tmp_path: Path) -> Path:
    source_root = tmp_path / "release-source"
    (source_root / "src" / "asset_factory_blueprint").mkdir(parents=True)
    for relative in ("pyproject.toml", "CITATION.cff", "codemeta.json", "mkdocs.yml"):
        shutil.copy2(ROOT / relative, source_root / relative)
    shutil.copy2(
        ROOT / "src" / "asset_factory_blueprint" / "__init__.py",
        source_root / "src" / "asset_factory_blueprint" / "__init__.py",
    )
    return source_root


def test_tagged_source_metadata_matches_release_identity() -> None:
    errors = validate_source_metadata(ROOT, _repository_metadata(), tag="v1.1.0")

    assert errors == []


def test_tagged_source_metadata_reports_version_and_orcid_drift(tmp_path: Path) -> None:
    source_root = _copy_source_metadata(tmp_path)
    runtime = source_root / "src" / "asset_factory_blueprint" / "__init__.py"
    runtime.write_text(runtime.read_text(encoding="utf-8").replace('"1.1.0"', '"1.2.0"'), encoding="utf-8")
    citation = source_root / "CITATION.cff"
    citation.write_text(citation.read_text(encoding="utf-8").replace("0000-0003-3131-0864", "0000-0000-0000-0000"), encoding="utf-8")

    errors = validate_source_metadata(source_root, _repository_metadata(), tag="v1.1.0")

    assert any("package runtime version mismatch" in error for error in errors)
    assert any("CITATION.cff ORCID identity/order mismatch" in error for error in errors)


def _make_pre_mint(source_root: Path) -> None:
    pyproject = source_root / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            'DOI = "https://doi.org/10.5281/zenodo.22201830"\n',
            "",
        ),
        encoding="utf-8",
    )
    citation = source_root / "CITATION.cff"
    citation_text = citation.read_text(encoding="utf-8").replace(
        '  - type: doi\n    value: "10.5281/zenodo.22201830"\n    description: "DOI for release v1.1.0"\n',
        "",
    )
    citation_text = citation_text.replace('  doi: "10.5281/zenodo.22201830"\n', "")
    citation.write_text(
        citation_text.replace(
            '  url: "https://doi.org/10.5281/zenodo.22201830"',
            '  url: "https://github.com/hcltech-robotics/asset-factory-blueprint/releases/tag/v1.1.0"',
        ),
        encoding="utf-8",
    )
    codemeta_path = source_root / "codemeta.json"
    codemeta = json.loads(codemeta_path.read_text(encoding="utf-8"))
    for key in ("identifier", "citation", "downloadUrl"):
        codemeta.pop(key)
    codemeta["sameAs"] = [f"https://doi.org/{CONCEPT_DOI}"]
    codemeta_path.write_text(json.dumps(codemeta, indent=2) + "\n", encoding="utf-8")
    mkdocs = source_root / "mkdocs.yml"
    mkdocs_text = mkdocs.read_text(encoding="utf-8")
    for key in ("version_doi", "archive_url", "archive_download_url"):
        mkdocs_text = re.sub(rf"(?m)^  {key}:.*$", f"  {key}:", mkdocs_text)
    mkdocs.write_text(mkdocs_text, encoding="utf-8")


def test_source_doi_state_accepts_complete_assigned_and_pre_mint_states(tmp_path: Path) -> None:
    assigned_doi, assigned_errors = source_version_doi_state(ROOT, CONCEPT_DOI)
    assert assigned_doi == "10.5281/zenodo.22201830"
    assert assigned_errors == []

    source_root = _copy_source_metadata(tmp_path)
    _make_pre_mint(source_root)
    pre_mint_doi, pre_mint_errors = source_version_doi_state(source_root, CONCEPT_DOI)
    assert pre_mint_doi is None
    assert pre_mint_errors == []


def test_source_doi_state_rejects_stale_urls_and_wrong_minted_record(tmp_path: Path) -> None:
    source_root = _copy_source_metadata(tmp_path)
    _make_pre_mint(source_root)
    codemeta_path = source_root / "codemeta.json"
    codemeta = json.loads(codemeta_path.read_text(encoding="utf-8"))
    codemeta["sameAs"].append("https://zenodo.org/records/22201830")
    codemeta_path.write_text(json.dumps(codemeta, indent=2) + "\n", encoding="utf-8")

    _, errors = source_version_doi_state(source_root, CONCEPT_DOI)

    assert any("pre-mint metadata retains exact-record" in error for error in errors)
    assert validate_source_record_doi("10.5281/zenodo.22201830", "10.5281/zenodo.99999999") == [
        "tagged source DOI '10.5281/zenodo.22201830' does not match minted record DOI "
        "'10.5281/zenodo.99999999'"
    ]
