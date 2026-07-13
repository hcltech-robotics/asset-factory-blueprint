# Project workspaces

Every run writes a persistent project folder. The folder is the unit of replay, review and packaging.

## Workspace boundary

A project workspace groups every stage output so the asset can be rebuilt, audited and varied with traceable provider responses, source copies, reports and checksums.

The workspace is also the boundary between immutable source material and generated work. Source files are copied in. Normalised, generated and authored artefacts are written beside the records that justify them.

## How to inspect a workspace

Agent skill: `asset-factory-orchestrator`. The orchestrator normally creates and updates the workspace as it runs each stage end to end.

Treat the workspace as an audit packet. Start with `project.json`, then read `run-plan.json` and `missing-evidence.json`. Those three files state what the factory was asked to do, which route it chose and what still blocks release.

Then inspect `manifests/` and `reports/`. Each generated file should trace back to a manifest entry, evidence record and checksum.

<p align="center">
  <img src="assets/record-graph.svg" alt="Run identity and immutable attempts connect stage manifests and evidence to cross-record validation, governance and release." width="920">
</p>

## Required files

- `project.json`
- `run-request.json`
- `run-plan.json`
- `provider-assignment.json`
- `validation-plan.json`
- `missing-evidence.json`
- `wandb-run-plan.json`
- `manifests/` with per-stage records written as `manifests/<schema-name>.json`, including `reconstruction-manifest.json` for candidate geometry and `mesh-verification-record.json` for canonical promotion before downstream stage manifests
- `evidence/checksums.json`, the exact SHA-256 inventory of every regular project file except the inventory itself and the ephemeral workspace lease; both exclusions and their fixed reasons are recorded in the file
- `reports/`
- `source-assets/`

## Source handling

Source assets are copied into the project before normalisation or authoring. Source files are not mutated in place. The source manifest records original checksum, project-copy checksum, rights status and unit policy.

## Workspace lifecycle

1. Create or open the workspace.
2. Copy source assets and write source manifests.
3. Write run-plan, provider assignment and validation plan.
4. Let each stage write manifests, reports, evidence and checksums.
5. Package validated outputs and preserve blocked reports when gates fail.

## Commands

```bash
afb project new "Warehouse Pick Cell" --project-root projects
afb project open warehouse_pick_cell
afb workflow run --request examples/run-requests/warehouse_pick_cell.json --dry-run
afb readiness --project projects/<slug> --output projects/<slug>/reports/readiness.md
```
