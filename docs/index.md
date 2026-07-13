# Asset Factory Blueprint

This blueprint turns source evidence into governed OpenUSD asset candidates and controlled simulation variants. It records the visual detail, physical behaviour, material state and articulation needed for robotics policy training, subject to named conformance and runtime gates.

## Read first

- [Quickstart](quickstart.md)
- [Walkthrough: one photo to a governed workspace](walkthrough.md)
- [Observed runthrough](runthrough.md)
- [Blueprint](blueprint.md)
- [Reference architecture](reference-architecture.md)
- [Source map](source-map.md)

## Pipeline stages

- [Intake and source ingestion](pipeline/00-intake-and-sources.md)
- [Reconstruction](pipeline/01-reconstruction.md)
- [Mandatory mesh verification](pipeline/01a-mesh-verification.md)
- [Segmentation and semantic inference](pipeline/02-segmentation.md)
- [Material and physical inference](pipeline/03-material-inference.md)
- [Texturing](pipeline/04-texturing.md)
- [Physics and articulation](pipeline/05-physics-articulation.md)
- [Nonvisual materials](pipeline/06-nonvisual-materials.md)
- [SimReady verification](pipeline/07-simready-verification.md)
- [Texture defaults](pipeline/texture-defaults.md)

## Implementation guidance

- [Orchestrator](platform/orchestrator.md)
- [Agentic operation](platform/agentic-operation.md)
- [Libraries](platform/libraries.md)
- [Governance](platform/governance.md)
- [Infrastructure](platform/infrastructure.md)
- [Deployment](platform/deployment.md)
- [External model runners](platform/external-model-runners.md)
- [Layer ownership and variants](platform/layer-ownership-and-variants.md)
- [Layout and mutation plans](platform/layout-and-mutation-plans.md)
- [RL environment](extensions/rl-environment.md)
- [Runtime architecture](runtime-architecture.md)
- [Agent system](agent-system.md)
- [Toolchain](toolchain.md)
- [Manifest contracts](manifest-contracts.md)
- [Skill SDK](skill-sdk.md)
- [Project workspaces](project-workspaces.md)
- [Provider abstraction](provider-abstraction.md)
- [Repository structure](repository-structure.md)
- [Support matrix](support-matrix.md)
- [Citation and reproducibility](citation-and-reproducibility.md)
- [Reference-run capsule](reference-run-capsule.md)

## Generated visualisations

Core architecture:

- [Architecture diagram](assets/architecture.svg)
- [Eight-stage pipeline diagram](assets/asset-factory-pipeline.svg)
- [Image to USD storyboard](assets/image-to-usd-storyboard.svg)
- [Runtime layer contract](assets/runtime-layer-contract.svg)
- [Agent workflow diagram](assets/agent-workflow.svg)
- [Agentic loop diagram](assets/agentic-loop.svg)
- [Execution lanes diagram](assets/execution-lanes.svg)
- [Library grounding diagram](assets/library-grounding.svg)

Stage flows:

- [Source ingestion lineage](assets/source-ingestion-lineage.svg)
- [Segmentation lane](assets/segmentation-lane.svg)
- [Material and physical inference lane](assets/material-inference-lane.svg)
- [Texturing lane](assets/texturing-lane.svg)
- [Texture and physics consistency lane](assets/texture-physics-consistency-lane.svg)
- [Physics and articulation lane](assets/physics-articulation-lane.svg)
- [Nonvisual materials lane](assets/nonvisual-materials-lane.svg)
- [SimReady verification gates](assets/simready-verification-gates.svg)
- [RL environment loop](assets/rl-environment-loop.svg)

Operations and governance:

- [Orchestrator routing](assets/orchestrator-routing.svg)
- [Direct partial invocation](assets/partial-invocation-convergence.svg)
- [Tool service authorisation](assets/tool-service-authorisation.svg)
- [Record graph](assets/record-graph.svg)
- [USD layer ownership](assets/usd-layer-ownership.svg)
- [Governance release decision](assets/governance-release-decision.svg)
- [Reference capsule trust chain](assets/capsule-trust-chain.svg)
