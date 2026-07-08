from __future__ import annotations

import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from asset_factory_blueprint.execution import atomic_write_json
from asset_factory_blueprint.utils.checksums import sha256_file


_TAXONOMY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_LEGACY_API_PREFIX = "SemanticsAPI:"
_CURRENT_API_PREFIX = "SemanticsLabelsAPI:"


def _applied_schema_names(prim: Any) -> list[str]:
    names = [str(item) for item in prim.GetAppliedSchemas()]
    authored = prim.GetMetadata("apiSchemas")
    if authored is not None and hasattr(authored, "GetAppliedItems"):
        names.extend(str(item) for item in authored.GetAppliedItems())
    return list(dict.fromkeys(names))


def _write_migration_report(target: Path, report: dict[str, Any]) -> dict[str, Any]:
    atomic_write_json(target, report)
    return {**report, "report_path": target.as_posix()}


def migrate_legacy_semantics(
    source: str | Path,
    output: str | Path,
    *,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Atomically flatten a stage only when every legacy semantic opinion is migratable."""

    source_path = Path(source).resolve(strict=True)
    output_path = Path(output).resolve(strict=False)
    target_report = Path(report_path).resolve(strict=False) if report_path else output_path.with_suffix(
        output_path.suffix + ".migration.json"
    )
    if not source_path.is_file():
        raise ValueError("semantic migration source must be a USD file")
    if output_path == source_path:
        raise ValueError("semantic migration output must differ from the source")
    if output_path.is_symlink():
        raise ValueError("semantic migration output must not be a symbolic link")
    if target_report in {source_path, output_path}:
        raise ValueError("semantic migration report path must differ from source and output")

    source_sha256 = sha256_file(source_path)
    try:
        from pxr import Sdf, Usd, UsdSemantics, Vt
    except Exception as exc:
        return _write_migration_report(
            target_report,
            {
                "format_version": "1.0",
                "status": "blocked",
                "source": source_path.name,
                "source_sha256": source_sha256,
                "output": output_path.name,
                "output_written": False,
                "source_mutated": False,
                "migrated_prim_count": 0,
                "already_current_prim_count": 0,
                "migrations": [],
                "warnings": [],
                "blocked_reasons": [f"OpenUSD with UsdSemantics is required: {exc}"],
            },
        )

    source_stage = Usd.Stage.Open(str(source_path))
    if source_stage is None:
        raise ValueError("semantic migration source could not be opened as a composed USD stage")

    preflight: list[dict[str, Any]] = []
    blocked_reasons: list[str] = []
    current_prim_count = 0
    for prim in source_stage.TraverseAll():
        applied = _applied_schema_names(prim)
        current_taxonomies = {
            name.removeprefix(_CURRENT_API_PREFIX)
            for name in applied
            if name.startswith(_CURRENT_API_PREFIX)
        }
        if current_taxonomies:
            current_prim_count += 1
        for instance in (
            name.removeprefix(_LEGACY_API_PREFIX)
            for name in applied
            if name.startswith(_LEGACY_API_PREFIX)
        ):
            prim_path = str(prim.GetPath())
            data_name = f"semantic:{instance}:params:semanticData"
            type_name = f"semantic:{instance}:params:semanticType"
            data_attr = prim.GetAttribute(data_name)
            type_attr = prim.GetAttribute(type_name)
            data = str(data_attr.Get() or "") if data_attr else ""
            taxonomy = str(type_attr.Get() or instance) if type_attr else instance
            legacy_properties = sorted(
                str(prop.GetName())
                for prop in prim.GetProperties()
                if str(prop.GetName()).startswith(f"semantic:{instance}:")
            )
            unexpected = sorted(set(legacy_properties) - {data_name, type_name})
            if not _TAXONOMY_PATTERN.fullmatch(instance):
                blocked_reasons.append(f"{prim_path}: invalid legacy semantic instance {instance!r}")
            if not _TAXONOMY_PATTERN.fullmatch(taxonomy):
                blocked_reasons.append(f"{prim_path}: invalid legacy taxonomy {taxonomy!r}")
            if not data:
                blocked_reasons.append(f"{prim_path}: legacy semantic value is empty for {instance}")
            if unexpected:
                blocked_reasons.append(
                    f"{prim_path}: unsupported legacy semantic properties for {instance}: {', '.join(unexpected)}"
                )
            current_labels: list[str] = []
            if taxonomy in current_taxonomies:
                current_api = UsdSemantics.LabelsAPI(prim, taxonomy)
                labels_attr = current_api.GetLabelsAttr()
                current_labels = [str(item) for item in (labels_attr.Get() or [])] if labels_attr else []
                if current_labels and data and data not in current_labels:
                    blocked_reasons.append(
                        f"{prim_path}: current {taxonomy!r} labels conflict with legacy value {data!r}"
                    )
            preflight.append(
                {
                    "prim_path": prim_path,
                    "instance": instance,
                    "taxonomy": taxonomy,
                    "data": data,
                    "legacy_properties": legacy_properties,
                    "current_labels": current_labels,
                }
            )

    grouped_values: dict[tuple[str, str], set[str]] = defaultdict(set)
    for item in preflight:
        grouped_values[(item["prim_path"], item["taxonomy"])].add(item["data"])
    for (prim_path, taxonomy), values in grouped_values.items():
        if len(values) > 1:
            blocked_reasons.append(
                f"{prim_path}: legacy semantic opinions conflict for taxonomy {taxonomy!r}: {sorted(values)!r}"
            )

    blocked_reasons = list(dict.fromkeys(blocked_reasons))
    if blocked_reasons or not preflight and not current_prim_count:
        if not blocked_reasons:
            blocked_reasons.append("no legacy or current semantic labels were found")
        return _write_migration_report(
            target_report,
            {
                "format_version": "1.0",
                "status": "blocked",
                "source": source_path.name,
                "source_sha256": source_sha256,
                "output": output_path.name,
                "output_written": False,
                "source_mutated": sha256_file(source_path) != source_sha256,
                "migrated_prim_count": 0,
                "already_current_prim_count": current_prim_count,
                "migrations": [],
                "taxonomy_policy": {
                    "human_readable": "class and label token arrays",
                    "knowledge_graph": "Wikidata Q-code token arrays when source evidence supplies a Q-code",
                    "fabricated_identifiers_allowed": False,
                },
                "warnings": [],
                "blocked_reasons": blocked_reasons,
            },
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=output_path.suffix or ".usd",
        dir=output_path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    migrations: list[dict[str, Any]] = []
    try:
        flattened = source_stage.Flatten()
        if not flattened.Export(str(temporary)):
            raise RuntimeError("flattened semantic migration output could not be written")
        stage = Usd.Stage.Open(str(temporary))
        if stage is None:
            raise RuntimeError("temporary semantic migration output could not be reopened")

        by_prim: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in preflight:
            by_prim[item["prim_path"]].append(item)
        for prim_path, items in by_prim.items():
            prim = stage.GetPrimAtPath(prim_path)
            if not prim:
                raise RuntimeError(f"flattened semantic migration output is missing {prim_path}")
            values_by_taxonomy: dict[str, set[str]] = defaultdict(set)
            properties_to_remove: set[str] = set()
            for item in items:
                values_by_taxonomy[item["taxonomy"]].add(item["data"])
                values_by_taxonomy[item["taxonomy"]].update(item["current_labels"])
                properties_to_remove.update(item["legacy_properties"])
            for taxonomy, values in values_by_taxonomy.items():
                labels = UsdSemantics.LabelsAPI.Apply(prim, taxonomy)
                labels.CreateLabelsAttr().Set(Vt.TokenArray(sorted(values)))
            retained_apis = [
                name
                for name in _applied_schema_names(prim)
                if not name.startswith(_LEGACY_API_PREFIX)
            ]
            prim.SetMetadata("apiSchemas", Sdf.TokenListOp.CreateExplicit(retained_apis))
            for property_name in properties_to_remove:
                prim.RemoveProperty(property_name)
            migrations.append(
                {
                    "prim_path": prim_path,
                    "taxonomies": sorted(values_by_taxonomy),
                }
            )
        stage.GetRootLayer().Save()

        verification_stage = Usd.Stage.Open(str(temporary))
        if verification_stage is None:
            raise RuntimeError("semantic migration output failed verification reopen")
        for prim in verification_stage.TraverseAll():
            if any(name.startswith(_LEGACY_API_PREFIX) for name in _applied_schema_names(prim)):
                raise RuntimeError(f"legacy semantic API remains on {prim.GetPath()}")
        for (prim_path, taxonomy), values in grouped_values.items():
            prim = verification_stage.GetPrimAtPath(prim_path)
            labels = UsdSemantics.LabelsAPI(prim, taxonomy).GetLabelsAttr()
            actual = {str(item) for item in (labels.Get() or [])} if labels else set()
            if not values.issubset(actual):
                raise RuntimeError(f"migrated semantic labels failed verification on {prim_path}")
        if sha256_file(source_path) != source_sha256:
            raise RuntimeError("semantic migration source changed during migration")
        os.replace(temporary, output_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    report = {
        "format_version": "1.0",
        "status": "pass",
        "source": source_path.name,
        "source_sha256": source_sha256,
        "output": output_path.name,
        "output_sha256": sha256_file(output_path),
        "output_written": True,
        "source_mutated": sha256_file(source_path) != source_sha256,
        "migrated_prim_count": len(migrations),
        "already_current_prim_count": current_prim_count,
        "migrations": migrations,
        "taxonomy_policy": {
            "human_readable": "class and label token arrays",
            "knowledge_graph": "Wikidata Q-code token arrays when source evidence supplies a Q-code",
            "fabricated_identifiers_allowed": False,
        },
        "warnings": [],
        "blocked_reasons": [],
    }
    return _write_migration_report(target_report, report)
