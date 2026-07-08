from __future__ import annotations

from pathlib import Path
import json

import asset_factory_blueprint.agent_loop as agent_loop
from asset_factory_blueprint.schemas.common import RunRequest
from asset_factory_blueprint.cli import main
from asset_factory_blueprint.services.programme import asset_factory_start, asset_programme_intake


def _draft(source: str, outputs: list[str], constraints: dict | None = None) -> dict:
    return {
        "id": "intake_probe",
        "version": "1.0",
        "objective": "Prepare an asset through the requested governed route.",
        "sources": [source],
        "requested_outputs": outputs,
        "constraints": constraints or {},
        "extensions": {},
    }


def _missing_fields(result: object) -> set[str]:
    return {item["field"] for item in result.data["missing_inputs"]}


def test_empty_intake_returns_focused_questions() -> None:
    result = asset_programme_intake({"draft": {}})

    assert result.success
    assert result.validation_status == "blocked"
    assert result.data["ready"] is False
    assert _missing_fields(result) == {"id", "objective", "requested_outputs", "sources"}
    assert len(result.data["questions"]) == 4


def test_simready_intake_requires_exact_profile() -> None:
    result = asset_programme_intake(
        {"draft": _draft("examples/sources/photos/metal_jerrycan.png", ["simready"])}
    )

    assert result.success
    assert result.validation_status == "blocked"
    assert {
        "constraints.simready_profile.profile_id",
        "constraints.simready_profile.profile_version",
    }.issubset(_missing_fields(result))


def test_texture_and_physics_routes_do_not_require_profile() -> None:
    source = "examples/sources/photos/metal_jerrycan.png"
    texture = asset_programme_intake({"draft": _draft(source, ["texture"])})
    physics = asset_programme_intake({"draft": _draft(source, ["physics"])})

    assert texture.data["ready"] is True
    assert "texturing" in texture.data["routed_stages"]
    assert "physics-articulation" not in texture.data["routed_stages"]
    assert "simready-verification" not in texture.data["routed_stages"]

    assert physics.data["ready"] is True
    assert "physics-articulation" in physics.data["routed_stages"]
    assert "simready-verification" not in physics.data["routed_stages"]
    assert [item["field"] for item in physics.data["pending_evidence"]] == [
        "constraints.source_rights",
        "constraints.physics_evidence",
    ]


def test_native_cad_blocks_until_converted_source_is_supplied(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    cad = tmp_path / "housing.step"
    cad.write_text("cad source", encoding="utf-8")
    monkeypatch.setenv("AFB_SERVICE_SOURCE_ROOTS", str(tmp_path))

    result = asset_programme_intake({"draft": _draft(str(cad), ["physics"])})

    assert result.data["ready"] is False
    assert "native_cad_conversion_unavailable" in {
        item["code"] for item in result.data["missing_inputs"]
    }


def test_factory_start_passes_validated_request_to_agent_loop(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    project_root = tmp_path / "projects"
    monkeypatch.setenv("AFB_SERVICE_SOURCE_ROOTS", str(tmp_path))
    monkeypatch.setenv("AFB_SERVICE_WORKSPACE_ROOTS", str(tmp_path))
    captured: dict = {}

    def capture_run_agent_loop(
        request: RunRequest,
        project_root: Path,
        project_name: str | None,
        dry_run: bool,
        max_fix_attempts: int | None,
    ) -> dict:
        captured.update(
            {
                "request": request,
                "project_root": project_root,
                "project_name": project_name,
                "dry_run": dry_run,
                "max_fix_attempts": max_fix_attempts,
            }
        )
        project_dir = project_root / "probe"
        return {
            "project_id": "probe",
            "project_dir": project_dir.as_posix(),
            "run_id": "run_probe",
            "dry_run": dry_run,
            "workflow_status": "proposal",
            "reviewed_stages": 1,
            "approved_stages": [],
            "pending_stages": ["texturing"],
            "progress": (project_dir / "progress.json").as_posix(),
            "contact_sheet": (project_dir / "reports/contact-sheet.md").as_posix(),
            "agent_report": (project_dir / "reports/agent-run-report.json").as_posix(),
            "status": "review_required",
        }

    monkeypatch.setattr(agent_loop, "run_agent_loop", capture_run_agent_loop)
    result = asset_factory_start(
        {
            "run_request": _draft(str(source), ["texture"]),
            "project_root": str(project_root),
            "project_name": "Probe",
            "dry_run": True,
            "max_fix_attempts": 1,
        }
    )

    assert result.success
    assert result.validation_status == "review_required"
    assert isinstance(captured["request"], RunRequest)
    assert captured["project_root"] == project_root
    assert captured["project_name"] == "Probe"
    assert captured["dry_run"] is True
    assert captured["max_fix_attempts"] == 1
    assert result.data["run_request"]["requested_outputs"] == ["texture"]


def test_factory_start_does_not_run_when_intake_is_blocked(monkeypatch: object) -> None:
    called = False

    def fail_if_called(*args: object, **kwargs: object) -> dict:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(agent_loop, "run_agent_loop", fail_if_called)
    result = asset_factory_start({"run_request": {}})

    assert not result.success
    assert result.validation_status == "blocked"
    assert called is False


def test_agent_intake_cli_returns_questions(
    tmp_path: Path,
    capsys: object,
) -> None:
    draft = tmp_path / "request.json"
    draft.write_text(
        json.dumps(_draft("examples/sources/photos/metal_jerrycan.png", ["simready"])),
        encoding="utf-8",
    )

    exit_code = main(["agent", "intake", "--draft", str(draft)])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["validation_status"] == "blocked"
    assert "constraints.simready_profile.profile_version" in {
        item["field"] for item in payload["data"]["missing_inputs"]
    }
