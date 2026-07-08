# Skill SDK

The public SDK is exported from `asset_factory_blueprint.skills`.

## Extension boundary

Stage-specific logic lives behind the dispatcher, provider policy and workspace contract. A skill receives context and returns structured proposals; services decide how those results become durable files.

## Public names

- `Skill`
- `SkillCategory`
- `SkillConfigError`
- `SkillContext`
- `Tool`
- `ToolResult`

## Context

`SkillContext` is the only route to outside state. It carries project, manifest, evidence, report, cache, provider resolver, W&B and dry-run fields.

## Result contract

`ToolResult` contains success state, data, error, warnings, artefacts, proposals and validation status. Provider outputs remain proposals until gates promote them.

## Registry

Built-in skills load from `configs/skill-registry.json`. Extension skills can register through the `asset_factory_blueprint.skills` entry point group and cannot alter the core dispatcher.

## Checks

```bash
afb skills list
afb skills validate-config --config configs/runtime-config.example.json
afb skill-audit --root . --output artifacts/skill-audit.json
```
