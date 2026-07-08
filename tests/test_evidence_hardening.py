from __future__ import annotations

from typing import Any

import pytest

from asset_factory_blueprint.capsule import (
    _model_bom_publication_blockers,
    _task_fitness_revalidation_blockers,
)
from asset_factory_blueprint.release_evidence import _assert_publishable, _git, _repository_state


@pytest.mark.parametrize(
    "value",
    [
        r"C:\Users\operator\project\report.json",
        "copied from D:/models/weights.bin",
        r"\\server\share\report.json",
        "//server/share/report.json",
        "/home/operator/report.json",
        "copied from /opt/private/report.json",
        "file:///home/operator/report.json",
        "file://server/share/report.json",
    ],
)
def test_publishability_rejects_absolute_machine_paths(value: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        _assert_publishable({"value": value})


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/a/b",
        "https://example.com/home/operator/report.json",
        "https://example.com/C:/documentation",
        "documentation: https://example.com/a/b?version=1",
        "s3://public-bucket/releases/report.json",
        "pkg:pypi/jsonschema@4.25.1",
    ],
)
def test_publishability_allows_normal_urls_and_identifiers(value: str) -> None:
    _assert_publishable({"value": value})


@pytest.mark.parametrize(
    ("responses", "cleanliness", "clean"),
    [
        ([(True, "a" * 40), (True, "")], "clean", True),
        ([(True, "a" * 40), (True, " M file.py")], "dirty", False),
        ([(False, ""), (False, "")], "unknown", None),
        ([(True, "a" * 40), (False, "")], "unknown", None),
        ([(True, ""), (True, "")], "unknown", None),
    ],
)
def test_repository_cleanliness_is_tri_state(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[tuple[bool, str]],
    cleanliness: str,
    clean: bool | None,
) -> None:
    pending = iter(responses)
    monkeypatch.setattr("asset_factory_blueprint.release_evidence._git", lambda _args: next(pending))
    state = _repository_state()
    assert state["cleanliness"] == cleanliness
    assert state["clean"] is clean


def test_git_execution_failure_is_not_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError("git is unavailable")

    monkeypatch.setattr("asset_factory_blueprint.release_evidence.subprocess.run", unavailable)
    assert _git(["status", "--short"]) == (False, "")


def _resolved_model() -> dict[str, Any]:
    return {
        "role": "validator_judge",
        "provider": "nvidia_nim",
        "kind": "openai_compatible",
        "model_id": "example/model",
        "revision": "0123456789abcdef",
        "weights_checksum": "a" * 64,
        "licence_expression": "Apache-2.0",
        "resolution_status": "pinned",
        "runtime": "NVIDIA NIM",
    }


def test_positive_model_bom_requires_resolved_provenance() -> None:
    assert _model_bom_publication_blockers({"model_bom": [_resolved_model()]}) == []
    unresolved = _resolved_model()
    unresolved.update(
        {
            "revision": "not_recorded",
            "weights_checksum": "not_recorded",
            "licence_expression": "NOASSERTION",
            "resolution_status": "blocked_unresolved",
        }
    )
    blockers = _model_bom_publication_blockers({"model_bom": [unresolved]})
    assert any("revision" in blocker for blocker in blockers)
    assert any("weights checksum" in blocker for blocker in blockers)
    assert any("licence_expression" in blocker for blocker in blockers)
    assert any("resolution_status" in blocker for blocker in blockers)


def test_task_fitness_is_recomputed_from_metrics_and_bindings() -> None:
    manifest = {
        "release_scope": "redistribution",
        "run_id": "run_test",
        "request_digest": "sha256:" + "b" * 64,
        "simready_profile": {"profile_id": "Prop-Robotics-Neutral", "profile_version": "1.0"},
    }
    governance = {"asset_fingerprint": "sha256:" + "c" * 64}
    test = {
        "test_id": "consumer_install_reproduction",
        "status": "pass",
        "scenario": "install the released package in a clean consumer environment",
        "metric_results": [
            {
                "metric_id": "load_success",
                "value": 1,
                "unit": "boolean",
                "expected_min": 1,
                "expected_max": 1,
                "tolerance": 0,
                "status": "pass",
            }
        ],
        "evidence_ids": ["consumer_install_log"],
    }
    bindings = {
        "scope": "redistribution",
        "run_id": "run_test",
        "request_digest": "sha256:" + "b" * 64,
        "asset_fingerprint": "sha256:" + "c" * 64,
        "profile_id": "Prop-Robotics-Neutral",
        "profile_version": "1.0",
    }
    report = {**bindings, "tests": [test]}
    summary = {
        "status": "pass",
        "required_test_ids": ["consumer_install_reproduction"],
        "tests": [test],
        "bindings": bindings,
        "blocked_reasons": [],
    }
    assert _task_fitness_revalidation_blockers(report, summary, manifest=manifest, governance=governance) == []
    report["tests"][0]["metric_results"][0]["value"] = 0
    blockers = _task_fitness_revalidation_blockers(report, summary, manifest=manifest, governance=governance)
    assert any("outside tolerance" in blocker for blocker in blockers)
