"""Discover available capabilities and optionally install missing ones on demand.

Probes the capability registry (configs/capability-registry.json): python
modules, environment handles, reconstruction backend installs, provider lanes
and GPU state. Reports which option serves each capability (primary or
fallback), which capabilities are blocked and which gates (licences, tokens)
stand in the way. With --install it plans or performs the installation of a
capability option.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asset_factory_blueprint.services.capability import install_capability, probe_capabilities


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", default="configs/capability-registry.json")
    parser.add_argument("--output", default="", help="write the capability report to this path")
    parser.add_argument("--install", default="", help="capability id to install")
    parser.add_argument("--option", default="", help="specific option id to install for the capability")
    parser.add_argument("--live", action="store_true", help="perform installs instead of planning them")
    args = parser.parse_args(argv)

    if args.install:
        plan = install_capability(args.install, args.option or None, registry_path=args.registry, dry_run=not args.live)
        print(json.dumps(plan, indent=2, sort_keys=False))
        return 0 if plan.get("status") in {"planned", "installed", "manual"} else 1

    report = probe_capabilities(args.registry)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=False))
    return 0 if report["blocked_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
