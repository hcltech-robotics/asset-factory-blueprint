# Toolchain

`afb` and the repository scripts form the factory's operating surface.

## Primary surfaces

- `afb` is the operator and automation CLI. In addition to the core workflow, it exposes graph validation, semantic migration, exact Profile validation, task-fitness evidence, bound governance decisions, durable tool serving, single-use tool approvals, reference capsules and release evidence.
- `scripts/discover_capabilities.py` probes available capabilities and plans on-demand installs.
- `scripts/generate_diagrams.py` owns documentation diagrams.
- `scripts/reconstruction/` holds backend adapters, run, provision and install helpers, PartCrafter USD stage authoring and GLB preview rendering.
- `scripts/segmentation/` holds USD mesh segment conditioning.
- `scripts/texturing/` holds rollout PBR texture creation, PBR semantics calibration, ambientCG import, live texture generation and probing and visual set pass composition.
- `scripts/simready/` holds the bounded official Profile bridge, Isaac load and behavioural check and Isaac visual set render.
- `scripts/ci/` holds process-level service smoke checks that also run from the extracted source appliance and deployment image.
- `scripts/afb` and `scripts/afb-agent-launchable` are the CLI and agent tool-server launchers.

## Release workflow

1. Verify and install the locked source appliance with `uv sync --frozen --all-extras`.
2. Generate or validate the run plan.
3. Run the workflow in dry-run or live mode.
4. Validate manifests, skills, diagrams and the complete record graph.
5. Build the source archive, inspect its appliance contents, extract it into a clean directory and rerun the locked workflow there.

Pytest suites, benchmarks and repository contract validation live in the asset-factory-verification repository and run against a checkout of this blueprint.

## Commands

```bash
uv sync --frozen --all-extras
uv run afb --help
uv run python scripts/generate_diagrams.py --check
uv run afb skill-audit --root . --output artifacts/skill-audit.json
```

## Recorded execution

Commands leave reports and checksums so reviewers can reconstruct execution after the process exits.
