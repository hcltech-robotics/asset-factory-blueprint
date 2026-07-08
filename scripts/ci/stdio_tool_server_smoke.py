from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    requests = [
        {"operation": "health"},
        {"operation": "catalogue"},
        {"operation": "invoke", "tool": "asset_library_search", "params": {"query": "physics", "limit": 1}},
        {"operation": "invoke", "tool": "asset_programme_intake", "params": {"draft": {}}},
        {
            "operation": "invoke",
            "tool": "asset_library_search",
            "params": {"query": "physics", "unexpected": True},
        },
    ]
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "asset_factory_blueprint.cli",
            "tool-server",
            "--transport",
            "stdio",
            "--allowed-tools",
            "asset_factory_start,asset_library_search,asset_programme_intake",
        ],
        input="".join(json.dumps(item) + "\n" for item in requests),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(f"stdio tool server exited with {process.returncode}: {process.stderr}")
    responses = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
    if len(responses) != len(requests):
        raise RuntimeError(f"stdio tool server returned {len(responses)} responses for {len(requests)} requests")
    if responses[0].get("ok") is not True or responses[0].get("transport") != "stdio":
        raise RuntimeError("stdio health response is invalid")
    tools = responses[1].get("tools") if responses[1].get("ok") is True else None
    expected_tools = ["asset_factory_start", "asset_library_search", "asset_programme_intake"]
    if not isinstance(tools, list) or [item.get("name") for item in tools] != expected_tools:
        raise RuntimeError("stdio catalogue did not enforce the configured allowlist")
    approvals = {item.get("name"): item.get("service_approval") for item in tools}
    if approvals.get("asset_factory_start") != "required" or approvals.get("asset_programme_intake") != "not_required":
        raise RuntimeError("stdio catalogue reported the wrong agentic-start approval boundary")
    if responses[2].get("ok") is not True or not isinstance(responses[2].get("result"), dict):
        raise RuntimeError("stdio read-only invocation failed")
    intake = responses[3].get("result") if responses[3].get("ok") is True else None
    if not isinstance(intake, dict) or intake.get("validation_status") != "blocked":
        raise RuntimeError("stdio programme intake did not return a blocked diagnostic")
    if responses[4].get("ok") is not False or "Additional properties" not in str(responses[4].get("error")):
        raise RuntimeError("stdio schema did not reject an unknown parameter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
