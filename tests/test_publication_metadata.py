from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import asset_factory_blueprint.release_evidence as release_evidence
from asset_factory_blueprint import __version__
from asset_factory_blueprint.config import ROOT
from asset_factory_blueprint.release_evidence import _publication_metadata, _schema_catalogue


def test_schema_catalogue_exactly_covers_versioned_public_schemas() -> None:
    catalogue = _schema_catalogue()
    expected_paths = sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "schemas").glob("*.schema.json"))

    assert catalogue["software"]["version"] == __version__
    assert catalogue["schema_count"] == len(expected_paths)
    assert [record["path"] for record in catalogue["schemas"]] == expected_paths
    assert all(record["schema_version"].startswith("v") for record in catalogue["schemas"])
    assert all(record["schema_draft"] == "https://json-schema.org/draft/2020-12/schema" for record in catalogue["schemas"])
    assert all(len(record["sha256"]) == 64 for record in catalogue["schemas"])


def test_publication_metadata_aligns_release_identity_and_container_recipe() -> None:
    metadata = _publication_metadata()

    assert set(metadata["version_alignment"].values()) == {__version__}
    assert metadata["repository"].endswith("/asset-factory-blueprint")
    assert metadata["documentation"].startswith("https://")
    assert metadata["authors"] == ["Chris von Csefalvay", "Tamas Foldi"]
    assert metadata["author_affiliations"] == [
        "HCLTech Robotics Intelligence CoE",
        "HCLTech Robotics Intelligence CoE",
    ]
    assert metadata["author_orcids"] == [
        "0000-0003-3131-0864",
        "0000-0001-9283-6865",
    ]
    assert metadata["keywords"] == [
        "OpenUSD",
        "SimReady",
        "robotics",
        "simulation",
        "asset pipeline",
        "reinforcement learning",
        "Isaac Lab",
        "provenance",
    ]
    assert metadata["concept_doi"] == "10.5281/zenodo.22201829"
    assert metadata["version_doi"] == "10.5281/zenodo.22201830"
    assert {record["path"] for record in metadata["metadata_files"]} == {
        "pyproject.toml",
        ".zenodo.json",
        "CITATION.cff",
        "codemeta.json",
        "mkdocs.yml",
        "references.bib",
        "CHANGELOG.md",
        "RELEASE.md",
        "MANIFEST.in",
        ".dockerignore",
    }
    recipe = metadata["container_recipe"]
    assert recipe["path"] == Path("deploy/Dockerfile").as_posix()
    assert recipe["declared_default_base_image"].endswith(
        "@sha256:" + recipe["declared_default_base_image_sha256"]
    )


def test_publication_metadata_allows_a_consistent_pre_mint_state(tmp_path: Path, monkeypatch) -> None:
    metadata_paths = (
        "pyproject.toml",
        ".zenodo.json",
        "CITATION.cff",
        "codemeta.json",
        "mkdocs.yml",
        "references.bib",
        "CHANGELOG.md",
        "RELEASE.md",
        "MANIFEST.in",
        ".dockerignore",
        "deploy/Dockerfile",
    )
    for relative in metadata_paths:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            'DOI = "https://doi.org/10.5281/zenodo.22201830"\n',
            "",
        ),
        encoding="utf-8",
    )
    citation = tmp_path / "CITATION.cff"
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
    codemeta_path = tmp_path / "codemeta.json"
    codemeta = json.loads(codemeta_path.read_text(encoding="utf-8"))
    for key in ("identifier", "citation", "downloadUrl"):
        codemeta.pop(key)
    codemeta["sameAs"] = ["https://doi.org/10.5281/zenodo.22201829"]
    codemeta_path.write_text(json.dumps(codemeta, indent=2) + "\n", encoding="utf-8")
    mkdocs = tmp_path / "mkdocs.yml"
    mkdocs_text = mkdocs.read_text(encoding="utf-8")
    for key in ("version_doi", "archive_url", "archive_download_url"):
        mkdocs_text = re.sub(rf"(?m)^  {key}:.*$", f"  {key}:", mkdocs_text)
    mkdocs.write_text(mkdocs_text, encoding="utf-8")

    monkeypatch.setattr(release_evidence, "ROOT", tmp_path)

    metadata = release_evidence._publication_metadata()

    assert metadata["concept_doi"] == "10.5281/zenodo.22201829"
    assert metadata["version_doi"] is None
