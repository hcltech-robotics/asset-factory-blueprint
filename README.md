# Asset Factory Blueprint

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.22201829.svg)](https://doi.org/10.5281/zenodo.22201829)

[Asset Factory Blueprint](https://hcltech-robotics.github.io/asset-factory-blueprint/index.html) (AFB) is an MIT-licensed reference implementation from the HCLTech Robotics Intelligence CoE that turns photos, scans, CAD and USD sources into governed, reproducible OpenUSD assets and Isaac Lab reinforcement learning environments for robotics simulation, with every geometry, physics and variant decision verified against SimReady requirements and tied to recorded evidence.

Robotic policies learn from what the simulator shows them. Clean-looking but physically wrong scenes teach brittle cues. Traceable geometry, scale, mass, friction, joints and material state make failures easier to find before they reach training. **The Asset Factory Blueprint creates the repeatable, governed USD pipelines that automatically build assets from your photos, meshes, USD files and other source evidence that will be _useful_, not just good-looking.**

![asset factory pipeline](docs/assets/asset-factory-pipeline.svg)

The key idea is _repeatability_. A simulation asset should be rebuildable from its sources, with its geometry, materials, textures, physical properties, articulation and variants tied to evidence.

The Asset Factory Blueprint is a coordinator that works with your tools, patches into your workflow where you want it to pick up, and integrates with your governance and Profiles. Asset Factories power high-performance, high-throughput environment generation for reinforcement learning, simulation and verification.

## Quick starts

Install the repository. You need Git, [uv](https://docs.astral.sh/uv/), Python 3.11 to 3.13 and repository access. Use HTTPS if your GitHub account is not configured for SSH.

```bash
git clone git@github.com:hcltech-robotics/asset-factory-blueprint.git
# HTTPS alternative: git clone https://github.com/hcltech-robotics/asset-factory-blueprint.git
cd asset-factory-blueprint
uv sync --frozen --all-extras
uv run afb capabilities
```

Put source files under `artifacts/sources/`, which is ignored by Git, or configure `AFB_SERVICE_SOURCE_ROOTS` with an `os.pathsep`-separated list of additional authorised locations. See [security and confinement](https://hcltech-robotics.github.io/asset-factory-blueprint/environment-reference.html#security-confinement-and-resource-limits).

Start with the [asset programme strategist](https://hcltech-robotics.github.io/asset-factory-blueprint/asset-programme-strategist.html). Give an agent in the checkout this instruction:

> Read and follow `skills/asset-programme-strategist/SKILL.md`. Start an asset-factory programme from my brief. Ask every start-blocking question, do not invent evidence or a Profile and use dry run unless I approve live work.

With shell access, the agent writes its draft to `artifacts/run-requests/<asset-id>.json`, then uses:

```bash
uv run afb agent intake --draft artifacts/run-requests/<asset-id>.json
uv run afb agent start --request artifacts/run-requests/<asset-id>.json --project-root projects
```

`agent intake` returns the exact missing questions without writing a project. Exit code 2 means the draft is blocked and must be updated from the user's answers; run `agent start` only when intake reports `ready: true`. Start repeats those checks, persists the confirmed object as `projects/<asset-id>/run-request.json` and enters the [whole-run agent loop](https://hcltech-robotics.github.io/asset-factory-blueprint/platform/agentic-operation.html).

For an external agent host, [`scripts/afb-agent-launchable`](https://hcltech-robotics.github.io/asset-factory-blueprint/platform/deployment.html#external-agent-hosts) exposes the equivalent `asset_programme_intake` and `asset_factory_start` tools over the governed stdio surface. Intake needs no approval. Starting the factory creates a project and may enter live provider-backed work, so it requires the existing parameter-bound, single-use [tool approval](https://hcltech-robotics.github.io/asset-factory-blueprint/platform/deployment.html#http-tool-service).

### I have a photo

Give the agent the image path and the intended use:

> I have a photo at `artifacts/sources/jerrycan.png`. I need a textured SimReady prop for an Isaac Sim manipulation task.

The strategist routes supported images through source ingestion, reconstruction, mandatory mesh verification, segmentation, material inference and the requested downstream stages. A SimReady or RL request must name an exact SimReady Profile ID and pinned version. If either is absent, the agent asks for it and stops before start-up rather than selecting one.

The exact pair comes from the programme's approved SimReady requirements and validator configuration. If that authority has not selected one, follow the [SimReady verification guidance](https://hcltech-robotics.github.io/asset-factory-blueprint/pipeline/07-simready-verification.html), keep the request blocked or narrow the deliverable to texture or physics work. The factory does not choose a release contract on the user's behalf.

### I have a mesh

Name the mesh, its units when the format does not carry them reliably and the result you need:

> I have an OBJ mesh at `artifacts/sources/pump.obj`. It is in millimetres. I need a textured SimReady asset with collision geometry.

PLY, OBJ, STL, GLB and glTF sources enter the mesh-conditioning route. GLB and glTF use their metre convention; other formats need trustworthy unit evidence. The agent keeps the source immutable, records the conversion and blocks SimReady promotion if scale remains unknown.

### I have CAD drawings

Give the agent the original CAD evidence and a supported authoring export:

> I have a USD export at `artifacts/sources/gripper.usd` for authoring. Retain the original STEP file at `artifacts/sources/gripper.step` as governance evidence.

The run request uses the USD or mesh export in `sources` and records the original CAD file under `constraints.governance_evidence`. STEP, STP, IGES, IGS, DWG and DXF can be registered as evidence, but native CAD conversion is not implemented. If no USD or supported mesh export is supplied, intake stops and asks for one. A rendered drawing can enter as an image, but dimensions and scale still need independent evidence.

### I need physics

Ask for the physics deliverable directly:

> I have a GLB at `artifacts/sources/bin.glb`. Add rigid-body physics and colliders for bin-picking. I have a measured mass record but no inertia measurement yet.

The `physics` output routes through `reconstruction` and `material-inference` into the `physics-articulation` stage without requiring a SimReady Profile. Missing measured, manufacturer-specified or measured-density evidence remains visible as pending evidence. The factory may prepare a proposal, but it does not invent mass, inertia, centre of mass or an approval signature.

For an existing project, run only that stage:

```bash
uv run afb stage run physics-articulation --project projects/<asset-id>
```

### I just need some textures for this thing

Keep the requested output narrow:

> I have a photo at `artifacts/sources/toolcase.png`. I need three worn polymer texture variants and no physics or SimReady package.

A `texture` request routes through the source, segmentation and material evidence needed to generate defensible texture proposals, then stops after texturing. It does not ask for a SimReady Profile. For an existing workspace:

```bash
uv run afb stage run texturing --project projects/<asset-id>
```

### I already have a run request

Pass the JSON through `afb agent intake` for interactive blocker handling, or start the [agent loop](https://hcltech-robotics.github.io/asset-factory-blueprint/platform/agentic-operation.html) directly:

```bash
uv run afb agent run --request artifacts/run-requests/<asset-id>.json --project-root projects
```

Direct `agent run` repeats schema and route validation but does not conduct the strategist's interview. It can materialise a blocked planning workspace when release-critical inputs are unresolved. Dry run is the default. Add `--live` to `agent start`, `agent run` or `stage run` only when provider-backed review and bounded fixes are intended. The project records the canonical request, run plan, missing evidence, stage manifests, reports, checksums, `progress.json` and the operator contact sheet. See the [full quickstart](https://hcltech-robotics.github.io/asset-factory-blueprint/quickstart.html) for provider credentials, reconstruction backends and release gates.

## The pipeline

Every run follows the same stage order. Intake and source ingestion register immutable evidence before the selected stages run:

1. Reconstruction (optional): candidate mesh from image(s), multi-view sets, video captures and descriptions through governed external backends, conditioning for supported meshes and USD sources and registration of the converted USD or mesh export supplied for native CAD evidence.
2. Mandatory mesh verification: deterministic geometry diagnostics and fixed-camera renders guide a vision reviewer. Only an approval bound to the candidate checksum publishes canonical geometry.
3. Segmentation: segmentation and semantic inference over approved meshes, producing appearance segments, semantic labels and material regions.
4. Material and physical inference: material classes and bindings from a constrained library, plus review-gated physical property proposals such as mass, density and friction.
5. Texturing: texture prompts, PBR map generation, texture variants and decals tied to material evidence.
6. Physics and articulation: rigid bodies, colliders, mass properties, joints, limits, drives and grasp affordances authored as testable plans.
7. Nonvisual materials (optional): thermal, acoustic and electrical material properties with uncertainty and evidence.
8. SimReady packaging and USD verification: layer stack assembly, package checks and runtime load gates, with Isaac Sim as one runtime target.

Downstream extensions build RL environment contracts and controlled layout or mutation permutations around validated assets.

## Agentic operation

`afb agent run` drives the routed stages. It runs deterministic gates, sends each stage to a vision-language reviewer, applies bounded fixes from the fix library and escalates unresolved findings to an operator. Candidate geometry always passes through the stricter `mesh-verification` gate before downstream stages can consume it.

Each iteration rewrites `progress.json` and the Markdown and PNG contact sheets under `reports/`. These record the stage, gate, verdict, fix state, thumbnails and defect tags. `afb capabilities` shows the active implementation for each capability, its fallbacks and any licence or token gate. It can also plan installs. See [Agentic operation](https://hcltech-robotics.github.io/asset-factory-blueprint/platform/agentic-operation.html).

## Libraries

Library indexes ground materials, textures, assets and physical values in known sources. Point the factory at existing material and texture folders, USD asset folders, the Omniverse content estate or a USD Search endpoint, then index them with `afb library index`.

The repository includes exemplar PBR and MDL materials based on the Omniverse reference catalogues, a physical property dictionary for review-gated proposals, links to asset packs and a knowledge corpus covering USD, asset creation, validation, PhysX binding, PBR and MDL. Free sources such as ambientCG and Poly Haven are queryable and downloadable. `afb library shop --query "rusty metal"` opens the terminal selector for matching items or whole packs. See [Libraries](https://hcltech-robotics.github.io/asset-factory-blueprint/platform/libraries.html).

## Core idea

Generated geometry, textures and inferred properties remain proposals until their evidence and gates pass. Provider output can accelerate authoring, but its place in a manifest does not make it true.

Use the factory when you want to:

- bring CAD, USD, images, scans or robot descriptions into a durable project workspace
- turn source evidence into manifests, reports and checksums
- compare generated geometry, texture, physics and articulation proposals against validation gates
- create controlled variants for domain randomisation
- package a SimReady candidate with clear promotion or blocked status
- build an RL environment contract around a validated asset

## Architecture

The runtime uses a fixed layer contract.

- `schemas/` contains machine-readable JSON Schema contracts.
- `src/asset_factory_blueprint/schemas/` contains Pydantic contracts.
- `src/asset_factory_blueprint/tools/` contains the public tool surface.
- `src/asset_factory_blueprint/services/` owns state mutation and orchestration.
- `src/asset_factory_blueprint/utils/` contains pure helper functions.
- `src/asset_factory_blueprint/prompts/` contains editable tool help.
- `skills/` contains operator-ready skill packages.

Provider routing comes from `configs/provider-policy.json`. Each public tool calls one service function and ships with its operator-facing prompt, keeping the callable surface and implementation in step.

## Command map

- `afb project new` creates a persistent project workspace.
- `afb schema skeleton` writes a valid manifest skeleton.
- `afb manifest validate` validates a manifest against a schema.
- `afb run-plan` builds a stage plan from a run request.
- `afb workflow run --dry-run` writes the project workspace, run plan, stage manifests, reports, evidence and checksums without provider calls.
- `afb agent run` drives the agentic loop: gates, VLM sign-off, fix library and progress artefacts per stage. Dry run is the default; add `--live` for provider-backed reviews and fixes.
- `afb agent intake` validates a partial run request and returns the exact start-blocking questions.
- `afb agent start` revalidates the completed request and enters the whole-run agent loop.
- `afb stage run` reviews and advances one routed stage in an existing project.
- `afb progress` rebuilds the progress record and operator contact sheet for a project.
- `afb capabilities` probes available capabilities, primaries, fallbacks and gates, and plans installs.
- `afb library search` finds grounded references across backings, curated seeds and knowledge.
- `afb library index` scans operator-declared backings into searchable indexes.
- `afb library shop` opens the terminal pack selector for query-responsive downloads.
- `afb texture prompt` writes a texture prompt from material and property manifests.
- `afb texture variation-workflow` writes an image review, texture variety and dent or bump workflow contract.
- `afb reconstruction backends` lists governed image-to-3D backends.
- `afb external-models run --dry-run` validates an external model run manifest.
- `afb provider check` validates provider lanes and probes configured live endpoints when credentials are present.
- `afb provider prompt` sends a real provider request and stores the redacted response as a proposal artefact.
- `afb readiness` rolls stage and gate statuses into a readiness report.
- `afb simready validate-profile` runs the configured NVIDIA validator through the bounded normalisation bridge and retains the raw report.
- `afb isaac-load apply` records an Isaac Sim load check result against a project.
- `afb semantics migrate` converts a USD layer from legacy semantics to the current instance-based API.
- `afb physics-evidence seal` signs accepted measured or specified mass-property evidence before authoring.
- `afb fitness template` and `afb fitness apply` bind a versioned task protocol and materialised fitness evidence to the exact package.
- `afb project validate` verifies the complete record graph and checksum inventory.
- `afb governance decide` previews or writes a content-bound, expiring operator decision.
- `afb capsule create` and `afb capsule validate` build and independently check positive or negative reference-run capsules.
- `afb release evidence` writes the locked dependency SBOM, schema catalogue and release metadata bundle.
- `afb tool-server --transport http` runs the bounded HTTP tool service. `--job-store` preserves terminal job records and marks interrupted jobs failed after restart. Reviewed mutations require a short-lived, parameter-bound, single-use approval token.

## Documentation map

- [Index](https://hcltech-robotics.github.io/asset-factory-blueprint/index.html) gives task-based routes through the documentation.
- [Blueprint](https://hcltech-robotics.github.io/asset-factory-blueprint/blueprint.html) explains the purpose, promotion model and policy-quality link.
- [Theory](https://hcltech-robotics.github.io/asset-factory-blueprint/theory/index.html) sets out the task-relative, evidential and physical basis of the blueprint.
- [Reference architecture](https://hcltech-robotics.github.io/asset-factory-blueprint/reference-architecture.html) explains runtime layers and artefact flow.
- The stage docs under [Pipeline stages](https://hcltech-robotics.github.io/asset-factory-blueprint/index.html#pipeline-stages) cover intake and sources, reconstruction, mandatory mesh verification, segmentation, material and physical inference, texturing, physics and articulation, nonvisual materials and SimReady verification in canonical order.
- The platform docs under [Platform operations](https://hcltech-robotics.github.io/asset-factory-blueprint/index.html#implementation-guidance) cover the orchestrator, governance, infrastructure, deployment, external model runners and layer ownership.
- [RL environment design](https://hcltech-robotics.github.io/asset-factory-blueprint/extensions/rl-environment.html) explains how validated assets become policy-training environments.
- [Support matrix](https://hcltech-robotics.github.io/asset-factory-blueprint/support-matrix.html) distinguishes declared, CI-checked, provisional and release-verified targets.
- [Citation and reproducibility](https://hcltech-robotics.github.io/asset-factory-blueprint/citation-and-reproducibility.html) defines software and schema citation requirements.
- [Reference-run capsule](https://hcltech-robotics.github.io/asset-factory-blueprint/reference-run-capsule.html) defines the evidence package required for a citeable release claim.

## Verification

Tests, the reproducibility benchmark and continuous integration live in this repository: `tests/` holds the pytest suite, `benchmarks/reproducibility/` holds the benchmark and `.github/workflows/ci.yml` runs both alongside the stdio tool-service smoke test, the diagram drift gate, the example workflow runs, the strict documentation build, release evidence and the source-archive exercise. Run `uv run --frozen pytest -q tests` against a checkout to reproduce the core suite locally.

## Citation and project policy

Use `CITATION.cff` for software citation and record the signed release tag, schema digests and verification commit used by a run. The [concept DOI](https://doi.org/10.5281/zenodo.22201829) resolves to the latest archived version; reproducible work should cite the DOI for the exact version used. For v1.1.0, the canonical BibTeX entry is:

```bibtex
@software{von_csefalvay_2026_22201830,
  author    = {von Csefalvay, Chris and Foldi, Tamas},
  title     = {Asset Factory Blueprint},
  month     = aug,
  year      = {2026},
  publisher = {Zenodo},
  version   = {v1.1.0},
  doi       = {10.5281/zenodo.22201830},
  url       = {https://doi.org/10.5281/zenodo.22201830}
}
```

Project decisions and releases follow [Project governance](https://hcltech-robotics.github.io/asset-factory-blueprint/GOVERNANCE.html) and [Release process](https://hcltech-robotics.github.io/asset-factory-blueprint/RELEASE.html). Support, security and participation policies are in [Support](https://hcltech-robotics.github.io/asset-factory-blueprint/SUPPORT.html), [Security policy](https://hcltech-robotics.github.io/asset-factory-blueprint/SECURITY.html) and [Code of conduct](https://hcltech-robotics.github.io/asset-factory-blueprint/CODE_OF_CONDUCT.html).

## License

The repository code is [licensed under the MIT License](https://hcltech-robotics.github.io/asset-factory-blueprint/license.html).

The Asset Factory Blueprint was developed in early 2026 by HCLTech Robotics Intelligence CoE. Its material workflows draw on NVIDIA's `content-agents`, while Omniverse, Isaac Sim and SimReady inform its runtime and promotion model; see [acknowledged foundations](https://hcltech-robotics.github.io/asset-factory-blueprint/THIRD_PARTY_NOTICES.html#acknowledged-foundations) for attribution.
