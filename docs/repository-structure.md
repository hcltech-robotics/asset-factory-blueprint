# Repository structure

The repository separates documentation, schemas, runtime code, skills, configs, scripts, examples and generated project workspaces.

## Layout

| Path | Role |
| --- | --- |
| `README.md` | Entry point and command map. |
| `docs/` | Concept docs at the root, stage docs under `pipeline/`, cross-cutting docs under `platform/`, downstream docs under `extensions/` and generated diagrams under `assets/`. |
| `schemas/` | 28 versioned JSON Schema contracts for durable files. |
| `configs/` | Provider, workflow, validation, skill and backend policy. |
| `src/asset_factory_blueprint/` | Runtime package. |
| `library/` | Curated grounding indexes (material exemplars, physical property dictionary, asset pack links, agent knowledge corpus); `library/local/` and `library/downloads/` are gitignored work roots for operator indexes and cached downloads. |
| `skills/` | Skill packages, each with SKILL.md, skill-card.md, references and an agent config. |
| `scripts/` | Stage scripts under `reconstruction/`, `segmentation/`, `texturing/` and `simready/`, plus `generate_diagrams.py` and the `afb` launchers. |
| `deploy/` | Deployment manifests for compose, cluster and batch lanes. |
| `examples/` | Sample run requests and manifests. |
| `projects/` | Durable project workspaces (gitignored work root). |
| `artifacts/` | Local command output (gitignored work root). |
| `Makefile` | Install and diagram targets. |
| `pyproject.toml` | Package definition and the `afb` entry point. |

## Boundary rules

- Source assets are copied into project workspaces and are not mutated in place.
- Generated or normalised artefacts belong under `projects/<slug>`.
- Public tools call service functions.
- Heavy runtime imports stay in services or utilities.
- Config files define policy. Runtime code enforces policy.

## Verification

Tests, benchmarks, repository contract checks and continuous integration live in the sibling asset-factory-verification repository. Point it at this checkout with `AFB_REPO_ROOT` and run its pytest suite and `repo_checks/validate_repository.py`.
