# Changelog

## Unreleased

- Added the evidence-bound PhysX pick-task lane for Isaac Lab 2.3.1. It renders the fixed-base, rigid-object contract, runs five runtime probes, measures affordance-weighted collision fidelity and rejects unsupported declarations before artefact creation. Runtime reports bind the task, USD, URDF, package inventory, simulator timing, producer files and regenerated source. Canonical evidence requires a signed importer receipt.
- Added bibliography rendering to the documentation build. `references.bib` is now the single bibliography for the repository and site. `mkdocs-bibtex` renders citations as footnotes using the vendored Cite Them Right Harvard style, a build hook renders the complete References page, and unknown keys fail the strict build. Custom sidebar admonitions support supplementary notes.
- Added a mandatory mesh-verification stage between reconstruction and every downstream geometry consumer. The stage combines deterministic topology and integrity gates, full-surface vision review, blind identity checking, checksum-bound canonical promotion and bounded adaptive reconstruction attempts.
- Added the reproducibility benchmark for five image assets and five USD assets, including per-attempt rejection accounting, exact mesh-invariant comparisons and preserved visual evidence.

## 1.0.0

Initial public shape of the asset factory blueprint.

- Canonical seven-stage pipeline: optional reconstruction from images, multi-view sets and video; segmentation and semantic inference; material and physical inference; texturing with decals; physics, articulation and grasp affordances; optional nonvisual materials; SimReady packaging and USD verification with Isaac Sim as one runtime target.
- Agentic operating layer: per-stage VLM sign-off gates with rubric library and controlled defect vocabulary, fix library with bounded remediation and workspace regeneration, capability steward with primary and fallback chains, agent loop writing machine progress records and operator contact sheets.
- Library backings: operator locations, an Omniverse estate, a USD Search endpoint and public domain remote sources indexed into grounding references; curated exemplar materials, a physical property dictionary and an agent knowledge corpus.
- Documentation site with generated schematic figures, quickstart and a captured end-to-end walkthrough.
- Verification lives in the sibling asset-factory-verification repository: pytest suites, repository contract checks, benchmarks and assessment tooling.
