from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from asset_factory_blueprint.reconstruction_backends import run_adapter_manifest  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_reconstruction_backend")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = run_adapter_manifest(args.manifest, dry_run=args.dry_run)
    print(json.dumps(payload, indent=2, sort_keys=False))
    return 0 if payload["status"] != "blocked" or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
