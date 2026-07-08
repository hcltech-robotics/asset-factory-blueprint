from __future__ import annotations

from pathlib import Path

import pytest

import asset_factory_blueprint.services.asset_authoring as asset_authoring
from asset_factory_blueprint.services.asset_authoring import _prepare_generated_package_root, compose_project_asset
from asset_factory_blueprint.utils.checksums import sha256_file


def test_generated_package_replacement_removes_stale_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package = project / "packaged" / "asset"
    stale_nested = package / "textures" / "variants" / "stale.png"
    stale_nested.parent.mkdir(parents=True)
    stale_nested.write_bytes(b"stale")
    (package / "injected.json").write_text("{}\n", encoding="utf-8")

    recreated = _prepare_generated_package_root(project, package)

    assert recreated == package
    assert recreated.is_dir()
    assert list(recreated.iterdir()) == []


def test_asset_composition_rebuilds_package_from_current_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / "source-assets" / "source.usda"
    source.parent.mkdir(parents=True)
    source.write_text(
        '#usda 1.0\n(defaultPrim = "World"\nmetersPerUnit = 1\nupAxis = "Z")\ndef Xform "World" {}\n',
        encoding="utf-8",
    )
    stale = project / "packaged" / "asset" / "textures" / "stale.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale")

    result = compose_project_asset(
        project,
        "asset",
        {
            "source_assets": [
                {
                    "status": "copied",
                    "project_copy_path": "source-assets/source.usda",
                    "copy_sha256": sha256_file(source),
                }
            ]
        },
        constraints={
            "simready_profile": {
                "profile_id": "Prop-Robotics-Neutral",
                "profile_version": "1.0",
            }
        },
    )

    package = project / "packaged" / "asset"
    assert result["package_path"] == "packaged/asset/asset.usda"
    assert (package / "asset.usda").is_file()
    assert not stale.exists()


def test_generated_package_replacement_rejects_escape_without_deleting_it(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    escaped = project / "outside" / "asset"
    escaped.mkdir(parents=True)
    sentinel = escaped / "preserve.txt"
    sentinel.write_text("preserve\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exactly beneath"):
        _prepare_generated_package_root(project, escaped)

    assert sentinel.read_text(encoding="utf-8") == "preserve\n"


def test_generated_package_replacement_rejects_linklike_package_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    package_root = project / "packaged"
    package = package_root / "asset"
    original = asset_authoring._is_linklike
    monkeypatch.setattr(
        asset_authoring,
        "_is_linklike",
        lambda path: path == package_root or original(path),
    )

    with pytest.raises(ValueError, match="symbolic link or junction"):
        _prepare_generated_package_root(project, package)

    assert not package.exists()


def test_generated_package_replacement_rejects_real_symlink_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package_root = project / "packaged"
    package_root.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "preserve.txt"
    sentinel.write_text("preserve\n", encoding="utf-8")
    package = package_root / "asset"
    try:
        package.symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(ValueError, match="resolves outside|symbolic link or junction"):
        _prepare_generated_package_root(project, package)

    assert sentinel.read_text(encoding="utf-8") == "preserve\n"
