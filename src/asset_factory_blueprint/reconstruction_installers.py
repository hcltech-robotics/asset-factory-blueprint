from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asset_factory_blueprint.config import ROOT
from asset_factory_blueprint.reconstruction_backends import provision_backend
from asset_factory_blueprint.utils.checksums import sha256_file


BACKEND_INSTALLERS = {
    "trellisv2": {
        "repo_url": "https://github.com/microsoft/TRELLIS.2.git",
        "checkout_name": "TRELLIS.2",
        "clone_recursive": True,
        "python_versions": ["3.10", "3.11"],
        "install_kind": "linux_setup_sh",
        "install_command": [
            "bash",
            "setup.sh",
            "--flash-attn",
            "--nvdiffrast",
            "--nvdiffrec",
            "--cumesh",
            "--o-voxel",
            "--flexgemm",
        ],
    },
    "hunyuan3d": {
        "repo_url": "https://github.com/Tencent-Hunyuan/Hunyuan3D-2.git",
        "checkout_name": "Hunyuan3D-2",
        "clone_recursive": False,
        "python_versions": ["3.10", "3.11", "3.12"],
        "install_kind": "pip_editable",
        "install_command": [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-e",
            ".",
        ],
    },
    "triposg": {
        "repo_url": "https://github.com/VAST-AI-Research/TripoSG.git",
        "checkout_name": "TripoSG",
        "clone_recursive": False,
        "python_versions": ["3.10", "3.11"],
        "install_kind": "pip_requirements",
        "install_command": [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            "requirements.txt",
        ],
    },
    "partcrafter": {
        "repo_url": "https://github.com/wgsxm/PartCrafter.git",
        "checkout_name": "PartCrafter",
        "clone_recursive": False,
        "python_versions": ["3.11"],
        "install_kind": "linux_setup_sh",
        "install_command": [
            "bash",
            "settings/setup.sh",
        ],
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalise_backend_id(value: str) -> str:
    lowered = value.lower().replace("-", "").replace("_", "").replace(".", "")
    if lowered in {"trellis", "trellis2", "trellisv2"}:
        return "trellisv2"
    if lowered in {"hy3d", "hunyuan", "hunyuan3d", "hunyuan3d2"}:
        return "hunyuan3d"
    if lowered in {"tripo", "triposg", "triposg1"}:
        return "triposg"
    if lowered in {"partcrafter", "part", "triposgpartcrafter", "tripopartcrafter", "structuredparts"}:
        return "partcrafter"
    raise ValueError(f"unknown backend: {value}")


def default_install_root(backend: str) -> Path:
    backend_id = normalise_backend_id(backend)
    base_raw = os.environ.get("AFB_BACKEND_INSTALL_ROOT", "")
    base = Path(base_raw).expanduser() if base_raw else ROOT / ".cache" / "afb" / "backends"
    return base / BACKEND_INSTALLERS[backend_id]["checkout_name"]


def run_step(
    command: list[str],
    cwd: Path | None = None,
    timeout: int = 1800,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        return {
            "command": command,
            "cwd": cwd.as_posix() if cwd else "",
            "returncode": result.returncode,
            "stdout_tail": result.stdout[-6000:],
            "stderr_tail": result.stderr[-6000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "cwd": cwd.as_posix() if cwd else "",
            "returncode": -1,
            "timed_out": True,
            "timeout_seconds": timeout,
            "stdout_tail": (exc.stdout or "")[-6000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-6000:] if isinstance(exc.stderr, str) else "",
        }


def torch_status() -> dict[str, Any]:
    try:
        import torch

        return {
            "available": True,
            "version": torch.__version__,
            "cuda_available": bool(torch.cuda.is_available()),
            "device_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "",
        }
    except Exception as exc:
        return {"available": False, "error": str(exc), "cuda_available": False, "device_name": ""}


def nvidia_smi_status() -> dict[str, Any]:
    if not shutil.which("nvidia-smi"):
        return {"available": False, "stdout": "", "stderr": "nvidia-smi not found"}
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free", "--format=csv,noheader"],
        text=True,
        capture_output=True,
        check=False,
    )
    return {"available": result.returncode == 0, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}


def path_writable(path: Path) -> bool:
    parent = path if path.exists() and path.is_dir() else path.parent
    parent.mkdir(parents=True, exist_ok=True)
    probe = parent / ".afb_write_probe"
    try:
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def check_backend_install(backend: str, install_root: Path) -> dict[str, Any]:
    backend_id = normalise_backend_id(backend)
    spec = BACKEND_INSTALLERS[backend_id]
    system = platform.system()
    python_version = ".".join(platform.python_version_tuple()[:2])
    torch_probe = torch_status()
    nvidia_probe = nvidia_smi_status()
    git_path = shutil.which("git")
    pip_path = shutil.which("pip") or shutil.which("pip3")
    blocked_reasons: list[str] = []
    warnings: list[str] = []

    if not git_path:
        blocked_reasons.append("git is required")
    if not pip_path:
        blocked_reasons.append("pip is required")
    if python_version not in spec["python_versions"]:
        warnings.append(f"python {python_version} is outside the preferred versions for {backend_id}")
    if not torch_probe.get("cuda_available"):
        blocked_reasons.append("torch cuda is required")
    if not nvidia_probe["available"]:
        warnings.append("nvidia-smi did not return GPU status")
    if not path_writable(install_root):
        blocked_reasons.append(f"install root is not writable: {install_root.as_posix()}")
    if backend_id in {"trellisv2", "partcrafter"} and system != "Linux":
        blocked_reasons.append(f"{backend_id} setup is Linux-oriented; use WSL, Linux or Spark for full native setup")

    return {
        "backend_id": backend_id,
        "status": "ready" if not blocked_reasons else "blocked",
        "mode": "check",
        "can_install": not blocked_reasons,
        "checked_at": utc_now(),
        "install_root": install_root.as_posix(),
        "system": system,
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "preferred_versions": spec["python_versions"],
        },
        "tools": {
            "git": git_path or "",
            "pip": pip_path or "",
            "nvidia_smi": nvidia_probe,
            "torch": torch_probe,
        },
        "repo_url": spec["repo_url"],
        "install_kind": spec["install_kind"],
        "warnings": warnings,
        "blocked_reasons": blocked_reasons,
    }


def install_backend(backend: str, install_root: Path, force: bool = False) -> dict[str, Any]:
    backend_id = normalise_backend_id(backend)
    spec = BACKEND_INSTALLERS[backend_id]
    check = check_backend_install(backend_id, install_root)
    steps: list[dict[str, Any]] = []
    status = "blocked"
    if check["blocked_reasons"] and not force:
        return {
            "backend_id": backend_id,
            "status": status,
            "mode": "install",
            "checked_at": utc_now(),
            "install_root": install_root.as_posix(),
            "check": check,
            "steps": steps,
            "blocked_reasons": check["blocked_reasons"],
        }

    if not install_root.exists():
        clone_command = ["git", "clone"]
        if spec["clone_recursive"]:
            clone_command.append("--recursive")
        clone_command.extend([spec["repo_url"], install_root.as_posix()])
        steps.append(run_step(clone_command, cwd=install_root.parent, timeout=3600))
    else:
        steps.append(
            {
                "command": ["reuse-existing-root", install_root.as_posix()],
                "returncode": 0,
                "stdout_tail": "",
                "stderr_tail": "",
            }
        )

    pinned_commit = os.environ.get(f"AFB_{backend_id.upper()}_COMMIT", spec.get("pinned_commit", ""))
    resolved_commit = ""
    if steps[-1]["returncode"] == 0 and install_root.exists():
        if pinned_commit:
            steps.append(run_step(["git", "checkout", pinned_commit], cwd=install_root, timeout=600))
        head = run_step(["git", "rev-parse", "HEAD"], cwd=install_root, timeout=120)
        resolved_commit = head["stdout_tail"].strip() if head["returncode"] == 0 else ""

    if steps[-1]["returncode"] == 0:
        install_env = dict(os.environ)
        interpreter_dir = str(Path(sys.executable).resolve().parent)
        install_env["PATH"] = interpreter_dir + os.pathsep + install_env.get("PATH", "")
        install_env["PIP_BREAK_SYSTEM_PACKAGES"] = "1"
        install_env["PIP_NO_BUILD_ISOLATION"] = "1"
        steps.append(run_step(spec["install_command"], cwd=install_root, timeout=7200, env=install_env))

    blocked_reasons = []
    if any(step.get("returncode", 1) != 0 for step in steps):
        blocked_reasons.append(f"{backend_id} install command failed")
    else:
        status = "ready"
        # record the interpreter that provisioned this backend so runs resolve
        # it without an environment handle
        (install_root / ".afb-interpreter").write_text(sys.executable + "\n", encoding="utf-8")

    provision = provision_backend(
        backend_id,
        output_path=ROOT / "artifacts" / "reconstruction-backends" / backend_id / "post-install-provision.json",
    )
    if provision["status"] != "ready":
        status = "blocked"
        blocked_reasons.extend(provision.get("blocked_reasons", []))

    return {
        "backend_id": backend_id,
        "status": status,
        "mode": "install",
        "checked_at": utc_now(),
        "install_root": install_root.as_posix(),
        "pinned_commit": pinned_commit,
        "resolved_commit": resolved_commit,
        "check": check,
        "steps": steps,
        "post_install_provision": provision,
        "blocked_reasons": blocked_reasons,
    }


def write_backend_install_report(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    checksum_path = path.with_suffix(".sha256.json")
    checksum_path.write_text(
        json.dumps({"algorithm": "sha256", "path": path.as_posix(), "sha256": sha256_file(path)}, indent=2) + "\n",
        encoding="utf-8",
    )
    payload["report_path"] = path.as_posix()
    payload["checksum_path"] = checksum_path.as_posix()
    return payload
