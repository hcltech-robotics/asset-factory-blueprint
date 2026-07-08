from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from asset_factory_blueprint.config import load_json


def list_profiles(config_path: str = "configs/texture-defaults.json") -> list[dict[str, Any]]:
    return load_json(config_path)["material_profiles"]


def explain(material: str, profile: str | None = None) -> dict[str, Any]:
    for item in list_profiles():
        if item["material_class"] == material and (profile is None or item["profile_id"] == profile):
            return item
    raise KeyError(f"no texture profile for material {material}")


def build_prompt(material_manifest: str | Path, property_manifest: str | Path, output: str | Path) -> dict[str, Any]:
    material_payload = json.loads(Path(material_manifest).read_text(encoding="utf-8"))
    property_payload = json.loads(Path(property_manifest).read_text(encoding="utf-8"))
    material = material_payload.get("material_class") or material_payload.get("selected_material") or "rubber"
    profile = explain(str(material), None)
    prompt = {
        "material_class": material,
        "profile_id": profile["profile_id"],
        "prompt": profile["pbr_defaults"]["base_color_prompt"],
        "negative_prompt": ", ".join(profile["forbidden_cues"]),
        "maps": profile["map_policy"]["maps"],
        "physical_consistency": {
            "property_manifest": str(property_manifest),
            "numeric_physics_authored": False,
            "notes": property_payload.get("notes", "nonvisual evidence controls numeric physics"),
        },
        "seed": 1729,
    }
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(prompt, indent=2) + "\n", encoding="utf-8")
    return prompt
