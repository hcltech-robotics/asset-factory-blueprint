from __future__ import annotations

from typing import Any

from asset_factory_blueprint.dispatcher import list_tools
from asset_factory_blueprint.skills.base import ToolResult
from asset_factory_blueprint import services


def route_tool(name: str, params: dict[str, Any]) -> ToolResult:
    tool_map = {tool.name: tool for tool in list_tools()}
    if name not in tool_map:
        return ToolResult(success=False, error=f"unknown tool: {name}", validation_status="blocked")
    service_name = tool_map[name].owning_service
    service = getattr(services, service_name, None)
    if service is None:
        return ToolResult(success=False, error=f"missing service: {service_name}", validation_status="blocked")
    return service(params)
