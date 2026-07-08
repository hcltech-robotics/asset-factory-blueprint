from __future__ import annotations

from asset_factory_blueprint.config import load_json
from asset_factory_blueprint.skills.base import Tool


def list_tools() -> list[Tool]:
    payload = load_json("configs/tool-surface.json")
    tools = []
    for item in payload["tools"]:
        tools.append(
            Tool(
                name=item["name"],
                description=item["description"],
                input_schema=item["input_schema"],
                write_layer=item["write_layer"],
                dry_run_supported=item["dry_run_supported"],
                requires_review=item["requires_review"],
                owning_service=item["owning_service"],
                prompt_file=item["prompt_file"],
            )
        )
    return tools
