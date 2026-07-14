"""Render the environment manifest to the project's Isaac Lab package."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from asset_factory_blueprint.rl_render import render_to_directory


def main() -> int:
    parser = argparse.ArgumentParser(description="render the RL environment manifest to Isaac Lab configuration")
    parser.add_argument("--project", required=True)
    parser.add_argument("--manifest", default="manifests/rl-environment-manifest.json")
    parser.add_argument("--output", default="envs")
    args = parser.parse_args()
    project = Path(args.project).resolve(strict=True)
    record = render_to_directory(project / args.manifest, project / args.output)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0 if record["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
