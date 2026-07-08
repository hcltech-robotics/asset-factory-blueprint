from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from asset_factory_blueprint.providers import generate_image
from asset_factory_blueprint.utils.checksums import sha256_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe a live image texture provider and write evidence artefacts.")
    parser.add_argument("--provider", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--image-name", default="texture-probe.png")
    parser.add_argument("--report", required=True)
    parser.add_argument("--checksums", required=True)
    parser.add_argument("--asset-id", default="asset")
    parser.add_argument("--variant-id", default="probe")
    parser.add_argument("--segment-id", default="")
    parser.add_argument("--map-kind", default="base_color")
    parser.add_argument("--model", default="")
    parser.add_argument("--size", default="512x512")
    parser.add_argument("--quality", default="medium")
    parser.add_argument("--output-format", default="png")
    return parser


def sanitise_error(value: str) -> str:
    redacted = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-redacted", value)
    redacted = re.sub(r"nvapi-[A-Za-z0-9_-]+", "nvapi-redacted", redacted)
    redacted = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer redacted", redacted, flags=re.IGNORECASE)
    return redacted.replace("\n", " ")[:1000]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    image_path = output_dir / args.image_name
    report_path = Path(args.report)
    checksums_path = Path(args.checksums)
    status = "blocked"
    blocked_reasons: list[str] = []
    provider_trace: list[dict[str, Any]] = []
    files: list[Path] = []
    try:
        generated = generate_image(
            args.provider,
            args.prompt,
            model=args.model or None,
            size=args.size,
            output_format=args.output_format,
            quality=args.quality,
        )
        image_path.write_bytes(generated.image_bytes)
        status = "generated"
        provider_trace.append(
            {
                "provider": generated.provider,
                "model": generated.model,
                "role": "texture_generator_probe",
                "map_kind": args.map_kind,
                "variant_id": args.variant_id,
                "segment_id": args.segment_id,
                "base_url_host": generated.base_url_host,
                "request_payload_redacted": generated.request_payload_redacted,
                "response_usage": generated.response_usage,
                "output_path": image_path.as_posix(),
                "output_sha256": sha256_file(image_path),
            }
        )
        files.append(image_path)
    except Exception as exc:
        blocked_reasons.append(sanitise_error(str(exc)))
    report = {
        "status": status,
        "asset_id": args.asset_id,
        "provider": args.provider,
        "output_dir": output_dir.as_posix(),
        "provider_trace": provider_trace,
        "blocked_reasons": blocked_reasons,
        "files": [path.as_posix() for path in files],
    }
    write_json(report_path, report)
    checksum_files = [report_path, *files]
    write_json(
        checksums_path,
        {
            "files": [
                {
                    "path": path.as_posix(),
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
                for path in checksum_files
                if path.exists()
            ]
        },
    )
    print(
        json.dumps(
            {
                "status": status,
                "report": report_path.as_posix(),
                "image": image_path.as_posix() if image_path.exists() else "",
                "blocked_reasons": blocked_reasons,
            },
            indent=2,
            sort_keys=False,
        )
    )
    return 0 if status == "generated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
