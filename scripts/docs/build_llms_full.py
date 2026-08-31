"""Concatenate the documentation into a single llms-full.txt file.

Reads the navigation from mkdocs.yml, strips YAML front matter from each
page and emits every page in navigation order with its canonical URL, so
language models can ingest the complete documentation in one request.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"


class _PermissiveLoader(yaml.SafeLoader):
    """Safe loader that tolerates the python/name tags used by mkdocs."""


def _unknown(loader: yaml.Loader, suffix: str, node: yaml.Node) -> None:
    return None


_PermissiveLoader.add_multi_constructor("tag:yaml.org,2002:python/name:", _unknown)
_PermissiveLoader.add_multi_constructor("!", _unknown)


def _walk_nav(nav: list, pages: list[tuple[str, str]]) -> None:
    for item in nav:
        if isinstance(item, str):
            pages.append((Path(item).stem.replace("-", " ").capitalize(), item))
        elif isinstance(item, dict):
            for label, value in item.items():
                if isinstance(value, str):
                    pages.append((label, value))
                elif isinstance(value, list):
                    _walk_nav(value, pages)


def _strip_front_matter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            return text[end + 5 :].lstrip("\n")
    return text


def build(output: Path) -> None:
    config = yaml.load(MKDOCS_YML.read_text(encoding="utf-8"), Loader=_PermissiveLoader)
    site_url = config["site_url"].rstrip("/") + "/"
    docs_dir = REPO_ROOT / config.get("docs_dir", "docs")
    extra = config.get("extra", {})
    pages: list[tuple[str, str]] = []
    _walk_nav(config.get("nav", []), pages)

    parts = [f"# {config['site_name']} — full documentation\n"]
    description = extra.get("software_description") or config.get("site_description")
    if description:
        parts.append(f"> {description}\n")
    parts.append(
        "This file concatenates every page of "
        f"{site_url} in navigation order. A shorter index is at {site_url}llms.txt.\n"
    )
    for label, rel_path in pages:
        source = docs_dir / rel_path
        if not source.exists():
            continue
        url = site_url + rel_path[: -len(".md")] + ".html"
        body = _strip_front_matter(source.read_text(encoding="utf-8")).strip()
        parts.append(f"\n---\n\n<!-- {label} | {url} -->\n\n{body}\n")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(parts), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "site" / "llms-full.txt",
        help="Path of the generated llms-full.txt file.",
    )
    args = parser.parse_args()
    build(args.output)


if __name__ == "__main__":
    main()
