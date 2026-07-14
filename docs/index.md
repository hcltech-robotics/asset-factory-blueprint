# Asset Factory Blueprint

Robotic policies learn from what the simulator shows them. Clean-looking but physically wrong scenes teach brittle cues. Traceable geometry, scale, mass, friction, joints and material state make failures easier to find before they reach training. **The Asset Factory Blueprint creates the repeatable, governed USD pipelines that automatically build assets from your photos, meshes, USD files and other source evidence that will be _useful_, not just good-looking.**

![asset factory pipeline](assets/asset-factory-pipeline.svg)

The key idea is _repeatability_. A simulation asset should be rebuildable from its sources, with its geometry, materials, textures, physical properties, articulation and variants tied to evidence.

The Asset Factory Blueprint is a coordinator that works with your tools, patches into your workflow where you want it to pick up, and integrates with your governance and Profiles. Asset Factories power high-performance, high-throughput environment generation for reinforcement learning, simulation and verification.

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

## About the Asset Factory Blueprint

The Asset Factory Blueprint was developed at HCLTech's Robotics Intelligence Lab in early 2026. We are a team of engineers, developers and roboticists who have to navigate a world of increasing complexity and provide training environments that reflect reality so that our robots can learn the right policies, faster. Asset factories make this possible at scale and in an economically efficient manner.

### Principal investigators

* Chris von Csefalvay, HCLTech
* Tamas Foldi, HCLTech, Head of Lab

### Citation

```bibtex
@software{voncsefalvay_asset_factory_blueprint_2026,
  author  = {von Csefalvay, Chris},
  title   = {{Asset Factory Blueprint}},
  year    = {2026},
  version = {1.0.0},
  url     = {https://github.com/hcltech-robotics/asset-factory-blueprint}
}
```


### License

The Asset Factory Blueprint and all its code are released under the MIT license. 
