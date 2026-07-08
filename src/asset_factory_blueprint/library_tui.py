"""Terminal pack selector for the library fetch surface.

Interactive when a terminal is attached: shows query-responsive items or
whole packs with numbered selectors, then plans or performs downloads.
Non-interactive automation passes --select and --yes instead.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from asset_factory_blueprint.config import load_json
from asset_factory_blueprint.services.library import fetch_from_source, search_remote_sources


PACKS_INDEX = "library/asset-packs.json"
FETCHABLE_SOURCES = ("ambientcg", "polyhaven")


def _parse_selection(raw: str, count: int) -> list[int]:
    value = raw.strip().lower()
    if not value:
        return []
    if value in {"all", "a", "*"}:
        return list(range(count))
    picked: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_raw, end_raw = part.split("-", 1)
            start, end = int(start_raw), int(end_raw)
            picked.update(range(start - 1, end))
        else:
            picked.add(int(part) - 1)
    return sorted(index for index in picked if 0 <= index < count)


def _print_rows(rows: list[dict[str, Any]]) -> None:
    for index, row in enumerate(rows, start=1):
        licence = row.get("licence", "")
        tags = ", ".join(row.get("tags", [])[:6])
        print(f"  [{index:>2}] {row.get('name', row.get('item_id', ''))}  ({row.get('source', '')}, {licence})")
        if tags:
            print(f"       {tags}")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _gather_query_rows(query: str, limit: int, sources: list[str]) -> list[dict[str, Any]]:
    remote = search_remote_sources(query, sources, limit)
    rows: list[dict[str, Any]] = []
    for source in sources:
        result = remote.get(source, {})
        if result.get("status") != "ready":
            print(f"note: {source} is unavailable: {result.get('error', 'no response')}")
            continue
        rows.extend(result.get("hits", []))
    return rows


def _gather_pack_rows() -> list[dict[str, Any]]:
    payload = load_json(PACKS_INDEX)
    return list(payload.get("items", []))


def run_shop(
    query: str = "",
    sources: list[str] | None = None,
    select: str = "",
    live: bool = False,
    limit: int = 12,
    assume_yes: bool = False,
) -> int:
    sources = [item for item in (sources or list(FETCHABLE_SOURCES)) if item in FETCHABLE_SOURCES] or list(FETCHABLE_SOURCES)
    print("Asset factory library shop")
    print(f"mode: {'download' if live else 'plan only (use --live to download)'}")
    print()

    if query:
        print(f'Query "{query}" across: {", ".join(sources)}')
        rows = _gather_query_rows(query, limit, sources)
        if not rows:
            print("No responsive items found.")
            return 1
    else:
        print("Registered packs (query-driven download works for API-backed sources):")
        rows = _gather_pack_rows()

    _print_rows(rows)
    print()

    if select:
        selection = _parse_selection(select, len(rows))
    elif _interactive():
        raw = input('Select items ("all", "1,3-5", blank to cancel): ')
        selection = _parse_selection(raw, len(rows))
    else:
        print('No terminal attached and no --select given; nothing selected. Pass --select "all" or indices.')
        return 1
    if not selection:
        print("Nothing selected.")
        return 0

    chosen = [rows[index] for index in selection]
    if not query:
        print("Selected packs:")
        for row in chosen:
            print(f"  {row.get('name', '')}: {row.get('uri', '')}  ({row.get('licence', '')})")
        fetchable = [row for row in chosen if row.get("item_id", "").startswith(("ambientcg", "polyhaven"))]
        if not fetchable:
            print("These packs are browsed and downloaded at their own locations; API-backed downloads cover ambientCG and Poly Haven.")
            return 0
        print("For API-backed packs, rerun with a query, for example: afb library shop --query \"rusty metal\"")
        return 0

    if _interactive() and not assume_yes:
        confirm = input(f"{'Download' if live else 'Plan'} {len(chosen)} item(s)? [y/N]: ").strip().lower()
        if confirm not in {"y", "yes"}:
            print("Cancelled.")
            return 0

    by_source: dict[str, list[str]] = {}
    for row in chosen:
        by_source.setdefault(row.get("source", ""), []).append(row.get("item_id", ""))
    results = []
    for source, item_ids in by_source.items():
        result = fetch_from_source(source, query=query, item_ids=item_ids, limit=len(item_ids), dry_run=not live)
        results.append(result)
        for download in result.get("downloads", []):
            print(f"  {download.get('item_id', '')}: {download.get('status', '')} -> {download.get('target', '')}")
    print()
    print(json.dumps({"results": results}, indent=2, sort_keys=False))
    return 0
