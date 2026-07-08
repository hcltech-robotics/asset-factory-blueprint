from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from asset_factory_blueprint.services.official_validator import (  # noqa: E402
    OmniAssetValidatorConfig,
    normalise_official_profile_report,
    run_official_profile_validation,
    write_official_profile_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the configured NVIDIA Omni Asset Validator against one exact SimReady Profile."
    )
    parser.add_argument("--usd", required=True, help="Composed USD root to validate.")
    parser.add_argument("--profile-id", required=True, help="Exact SimReady Profile ID.")
    parser.add_argument("--profile-version", required=True, help="Exact pinned Profile version.")
    parser.add_argument("--output", required=True, help="Normalised evidence report path.")
    parser.add_argument(
        "--raw-output",
        default="",
        help="Vendor JSON report path. Defaults to <output stem>.raw.json.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument("--max-process-output-bytes", type=int, default=None)
    parser.add_argument("--max-report-bytes", type=int, default=None)
    return parser


def _configured_limits(args: argparse.Namespace) -> OmniAssetValidatorConfig:
    environment = OmniAssetValidatorConfig.from_environment()
    return OmniAssetValidatorConfig(
        executable=environment.executable,
        executable_sha256=environment.executable_sha256,
        attestation_secret=environment.attestation_secret,
        timeout_seconds=args.timeout_seconds if args.timeout_seconds is not None else environment.timeout_seconds,
        max_process_output_bytes=(
            args.max_process_output_bytes
            if args.max_process_output_bytes is not None
            else environment.max_process_output_bytes
        ),
        max_report_bytes=args.max_report_bytes if args.max_report_bytes is not None else environment.max_report_bytes,
    )


def _blocked_report(args: argparse.Namespace, raw_output: Path, reason: str) -> dict[str, object]:
    return normalise_official_profile_report(
        None,
        profile_id=args.profile_id,
        profile_version=args.profile_version,
        validator_version="",
        usd_path=Path(args.usd).name or "asset.usd",
        usd_sha256="",
        composition_fingerprint="",
        raw_report_path=raw_output.name or "validator.raw.json",
        raw_report_sha256="",
        execution={
            "command_contract": [
                Path(os.environ.get("AFB_ASSET_VALIDATOR_EXECUTABLE", "<validator>")).name,
                "--profile",
                f"{args.profile_id}@{args.profile_version}",
                "--no-fix",
                "--no-stamp",
                "--json-output",
                "<raw-report>",
                "<asset>",
            ],
            "version_probe": {},
            "validation": {},
        },
        preflight_problems=[reason],
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output)
    raw_output = Path(args.raw_output) if args.raw_output else output.with_name(f"{output.stem}.raw.json")
    if output.resolve() == Path(args.usd).resolve():
        parser_error = _blocked_report(args, raw_output, "normalised report path must not overwrite the USD root")
        print(json.dumps(parser_error, indent=2, ensure_ascii=True))
        return 1
    if output.exists() and not output.is_file():
        parser_error = _blocked_report(args, raw_output, "normalised report path exists and is not a file")
        print(json.dumps(parser_error, indent=2, ensure_ascii=True))
        return 1
    package_root = Path(args.usd).resolve().parent
    if any(path.resolve() == package_root or package_root in path.resolve().parents for path in (output, raw_output)):
        parser_error = _blocked_report(args, raw_output, "validator reports must be outside the immutable package")
        print(json.dumps(parser_error, indent=2, ensure_ascii=True))
        return 1
    if output.resolve() == raw_output.resolve():
        parser_error = _blocked_report(args, raw_output, "normalised and raw report paths must be different")
        write_official_profile_report(output, parser_error)
        print(json.dumps(parser_error, indent=2, ensure_ascii=True))
        return 1
    try:
        config = _configured_limits(args)
    except ValueError as exc:
        parser_error = _blocked_report(args, raw_output, f"invalid validator environment configuration: {exc}")
        write_official_profile_report(output, parser_error)
        print(json.dumps(parser_error, indent=2, ensure_ascii=True))
        return 1
    report = run_official_profile_validation(
        args.usd,
        profile_id=args.profile_id,
        profile_version=args.profile_version,
        raw_report_path=raw_output,
        config=config,
    )
    write_official_profile_report(output, report)
    print(json.dumps(report, indent=2, sort_keys=False, ensure_ascii=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
