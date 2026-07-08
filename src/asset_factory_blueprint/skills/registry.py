from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asset_factory_blueprint.config import load_json
from asset_factory_blueprint.skills.base import SkillConfigError


@dataclass(frozen=True)
class SkillRecord:
    name: str
    category: str
    package: str
    provider_roles: list[str]
    inputs: list[str]
    outputs: list[str]
    enabled: bool


def load_builtin_registry(path: str | Path = "configs/skill-registry.json") -> list[SkillRecord]:
    payload = load_json(path)
    records = []
    for item in payload.get("skills", []):
        records.append(
            SkillRecord(
                name=item["name"],
                category=item["category"],
                package=item["package"],
                provider_roles=list(item.get("provider_roles", [])),
                inputs=list(item.get("inputs", [])),
                outputs=list(item.get("outputs", [])),
                enabled=bool(item.get("enabled", True)),
            )
        )
    return records


def discover_extension_skills() -> list[Any]:
    discovered = []
    for entry_point in importlib.metadata.entry_points(group="asset_factory_blueprint.skills"):
        loaded = entry_point.load()
        instance = loaded() if callable(loaded) else loaded
        if getattr(instance, "name", None) != entry_point.name:
            raise SkillConfigError(
                f"entry point {entry_point.name} does not match Skill.name",
                "rename the entry point or the skill",
            )
        discovered.append(instance)
    return discovered
