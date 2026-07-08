from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from asset_factory_blueprint.reconstruction_backends import provision_backend  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="provision_reconstruction_backend")
    parser.add_argument("--backend", required=True)
    parser.add_argument("--registry", default="configs/reconstruction-backends.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--require-ready", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = provision_backend(args.backend, registry_path=args.registry, output_path=args.output)
    print(json.dumps(report, indent=2, sort_keys=False))
    if args.require_ready and report["status"] != "ready":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
