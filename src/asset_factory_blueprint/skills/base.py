from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class SkillConfigError(RuntimeError):
    def __init__(self, message: str, fix_hint: str | None = None) -> None:
        super().__init__(message)
        self.fix_hint = fix_hint


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    write_layer: str
    dry_run_supported: bool
    requires_review: bool
    owning_service: str
    prompt_file: str


@dataclass
class ToolResult:
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    artefacts: list[str] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    validation_status: str = "not_validated"


@dataclass(frozen=True)
class SkillContext:
    library_dir: Path | None
    cache_dir: Path | None
    project_dir: Path | None
    scene_path: Path | None
    run_id: str
    manifest_dir: Path
    evidence_dir: Path
    report_dir: Path
    provider_resolver: Any
    wandb_context: Any | None
    dry_run: bool


class Skill(Protocol):
    name: str
    category: str
    cache_subdir: str | None

    def get_tools(self) -> list[Tool]:
        ...

    def validate_config(self) -> None:
        ...

    async def execute(self, tool_name: str, params: dict[str, Any], ctx: SkillContext) -> ToolResult:
        ...
