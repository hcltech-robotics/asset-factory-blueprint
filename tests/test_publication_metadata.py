from __future__ import annotations

from pathlib import Path

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
    assert {record["path"] for record in metadata["metadata_files"]} == {
        "pyproject.toml",
        "CITATION.cff",
        "codemeta.json",
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
