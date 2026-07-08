from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from asset_factory_blueprint.reconstruction_installers import (  # noqa: E402
    check_backend_install,
    default_install_root,
    install_backend,
    normalise_backend_id,
    write_backend_install_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="install_reconstruction_backend")
    parser.add_argument("--backend", required=True)
    parser.add_argument("--mode", choices=["check", "install"], default="check")
    parser.add_argument("--install-root")
    parser.add_argument("--output", required=True)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    backend_id = normalise_backend_id(args.backend)
    install_root = Path(args.install_root) if args.install_root else default_install_root(backend_id)
    if args.mode == "check":
        payload = check_backend_install(backend_id, install_root)
    else:
        payload = install_backend(backend_id, install_root, force=args.force)
    payload = write_backend_install_report(Path(args.output), payload)
    print(json.dumps(payload, indent=2, sort_keys=False))
    return 0 if payload["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
