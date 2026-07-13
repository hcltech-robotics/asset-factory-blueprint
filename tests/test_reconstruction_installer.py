from __future__ import annotations

import sys

from asset_factory_blueprint.reconstruction_installers import BACKEND_INSTALLERS, run_step


def test_trellis_installer_does_not_start_an_interactive_conda_environment() -> None:
    command = BACKEND_INSTALLERS["trellisv2"]["install_command"]

    assert "--new-env" not in command
    assert "--basic" not in command
    assert command[:2] == ["bash", "setup.sh"]


def test_run_step_uses_an_explicit_environment() -> None:
    result = run_step(
        [sys.executable, "-c", "import os; print(os.environ['AFB_TEST_MARKER'])"],
        env={"AFB_TEST_MARKER": "present"},
    )

    assert result["returncode"] == 0
    assert result["stdout_tail"].strip() == "present"
