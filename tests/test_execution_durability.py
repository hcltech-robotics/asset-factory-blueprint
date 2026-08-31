from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from asset_factory_blueprint import (
    execution,
    isaac_load,
    rl_collision_fidelity,
    rl_probe_import,
    rl_render,
    rl_runtime_probe,
)
from asset_factory_blueprint.execution import WorkspaceBusyError, workspace_lease
from asset_factory_blueprint.services.rl_environment import rl_route


def test_durable_replace_replaces_the_target_and_syncs_its_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.tmp"
    target = tmp_path / "target.json"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    synced: list[Path] = []
    if os.name != "nt":
        monkeypatch.setattr(execution, "fsync_directory", lambda path: synced.append(Path(path)))

    assert execution.durable_replace(source, target) == target

    assert target.read_bytes() == b"new"
    assert not source.exists()
    if os.name != "nt":
        assert synced == [tmp_path]


def test_durable_unlink_publishes_the_removed_directory_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "obsolete.json"
    target.write_bytes(b"obsolete")
    synced: list[Path] = []
    if os.name != "nt":
        monkeypatch.setattr(execution, "fsync_directory", lambda path: synced.append(Path(path)))

    execution.durable_unlink(target)

    assert not target.exists()
    if os.name != "nt":
        assert synced == [tmp_path]


def test_atomic_json_uses_the_durable_replacement_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "record.json"
    replacements: list[tuple[Path, Path]] = []
    replace = execution.durable_replace

    def recording_replace(source: str | Path, destination: str | Path) -> Path:
        replacements.append((Path(source), Path(destination)))
        return replace(source, destination)

    monkeypatch.setattr(execution, "durable_replace", recording_replace)
    execution.atomic_write_json(target, {"status": "pass"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "pass"}
    assert len(replacements) == 1
    assert replacements[0][0].parent == tmp_path
    assert replacements[0][1] == target


@pytest.mark.parametrize(
    ("module", "writer_name", "payload"),
    [
        (isaac_load, "_atomic_write_bytes", b"runtime"),
        (rl_probe_import, "_atomic_write_bytes", b"probe"),
        (rl_render, "_atomic_write", "rendered\n"),
    ],
)
def test_workspace_atomic_writers_use_durable_replace(
    module: object,
    writer_name: str,
    payload: bytes | str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "record"
    replacements: list[tuple[Path, Path]] = []

    def recording_replace(source: str | Path, destination: str | Path) -> Path:
        source_path = Path(source)
        destination_path = Path(destination)
        replacements.append((source_path, destination_path))
        os.replace(source_path, destination_path)
        return destination_path

    monkeypatch.setattr(module, "durable_replace", recording_replace)
    writer = getattr(module, writer_name)
    writer(target, payload)

    assert len(replacements) == 1
    assert replacements[0][1] == target
    assert target.read_bytes() == (payload.encode("utf-8") if isinstance(payload, str) else payload)


def test_public_rl_mutators_refuse_a_leased_workspace(tmp_path: Path) -> None:
    project = tmp_path / "project"
    manifest = project / "manifests" / "rl-environment-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}\n", encoding="utf-8")

    with workspace_lease(project, "lease-holder") as lock_path:
        assert lock_path.read_bytes().startswith(b"\0{")
        route_result = rl_route({"project": project})
        with pytest.raises(WorkspaceBusyError, match="lease-holder"):
            rl_render.render_to_directory(manifest, project / "envs")
        with pytest.raises(WorkspaceBusyError, match="lease-holder"):
            isaac_load.apply_isaac_load_report(project, project / "incoming.json")

    assert route_result.success is False
    assert "lease-holder" in str(route_result.error)


def test_rl_producers_refuse_to_publish_while_the_workspace_is_leased(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    probe_output = project / "reports" / "incoming" / "probe.json"
    fidelity_output = project / "reports" / "incoming" / "fidelity.json"

    probe_bindings = {
        "runtime_contract": {"sim_dt": 1.0 / 120.0, "decimation": 2},
        "runtime_source_report_sha256": "a" * 64,
    }
    monkeypatch.setattr(
        rl_runtime_probe,
        "parse_args",
        lambda: SimpleNamespace(
            project=str(project),
            manifest="manifests/rl-environment-manifest.json",
            output="reports/incoming/probe.json",
            probes=[],
        ),
    )
    monkeypatch.setattr(rl_runtime_probe, "report_attestation_secret", lambda _report_id: b"p" * 32)
    monkeypatch.setattr(rl_runtime_probe, "_producer_files", lambda: [])
    monkeypatch.setattr(rl_runtime_probe, "load_bindings", lambda _project, _manifest: probe_bindings)
    monkeypatch.setattr(rl_runtime_probe, "_output_path", lambda _project, _value: probe_output)

    def runtime_unavailable(*_args: object) -> None:
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(rl_runtime_probe.importlib.metadata, "version", runtime_unavailable)

    fidelity_bindings = {
        "usd_file": project / "asset.usd",
        "fidelity": {"tolerance_m": 0.001, "samples_per_region": 8, "seed": 1},
    }
    monkeypatch.setattr(
        rl_collision_fidelity,
        "parse_args",
        lambda: SimpleNamespace(
            project=str(project),
            manifest="manifests/rl-environment-manifest.json",
            output="reports/incoming/fidelity.json",
        ),
    )
    monkeypatch.setattr(rl_collision_fidelity, "report_attestation_secret", lambda _report_id: b"f" * 32)
    monkeypatch.setattr(rl_collision_fidelity, "_producer_files", lambda: [])
    monkeypatch.setattr(rl_collision_fidelity, "load_bindings", lambda _project, _manifest: fidelity_bindings)
    monkeypatch.setattr(rl_collision_fidelity, "_output_path", lambda _project, _value: fidelity_output)
    monkeypatch.setattr(rl_collision_fidelity, "meshes_from_usd", runtime_unavailable)

    with workspace_lease(project, "lease-holder"):
        assert rl_runtime_probe.main() == 1
        assert rl_collision_fidelity.main() == 1

    assert "lease-holder" in capsys.readouterr().err
    assert not probe_output.exists()
    assert not fidelity_output.exists()
