# Manifest contracts

Manifest contracts are the file-backed interface between stages. They let a project be resumed, reviewed, validated and replayed without relying on process memory.

<p align="center">
  <img src="assets/record-graph.svg" alt="Run identity and immutable attempts connect stage manifests and evidence to cross-record validation, governance and release." width="920">
</p>

## Manifest authority

Stages can produce geometry, materials, textures, physics, articulation and environment artefacts. The schema catalogue records what each stage received, produced and used as evidence, plus whether the result may move forward.

## How to read a manifest

Agent skill: `evaluation-validation-lead`. The orchestrator usually hands manifests to validation as each stage completes.

Read a manifest from top to bottom before looking at generated files. The manifest should name the stage, inputs, outputs, evidence, validation state and review state. If a file exists but is missing from the manifest, treat the file as unmanaged.

For a quick check:

```bash
afb manifest validate <schema-name> projects/<slug>/manifests/<manifest-name>.json
```

## Authoring flow

1. Pick the schema for the stage output.
2. Generate a skeleton when starting a new manifest.
3. Fill required fields from project evidence and service output.
4. Validate against JSON Schema before downstream stages consume it.
5. Record errors as report artefacts rather than console-only failures.

## Stage manifests

Each project writes stage manifests as `manifests/<schema-name>.json`. The current catalogue holds 28 schemas: `asset-programme-intake-manifest` and `source-asset-manifest` for the pre-pipeline steps, then `reconstruction-manifest`, `segmentation-manifest`, `material-inference-manifest` (which carries `physical_property_proposals`), `texturing-manifest` (which carries a `decals` array), `physics-articulation-manifest` (the merged physics and articulation record with `affordances.grasp_points`), `nonvisual-material-manifest` and `simready-asset-manifest` for the seven stages. Cross-cutting records cover `run-request`, immutable `stage-attempt` history, `evaluation-manifest`, `governance-record`, `operator-release-decision`, `provenance-record`, `external-model-run-manifest`, `isaac-runtime-evidence`, `rl-environment-manifest`, `asset-layout-manifest`, `mutation-plan`, `layer-ownership-manifest`, `texture-default-policy`, `skill-context`, `vlm-review-record`, `library-index`, `task-fitness-protocol`, `task-fitness-evidence` and `reference-run-capsule`.

## Contract rules

- Required IDs are stable across reruns.
- Paths resolve under the project, approved cache or approved library root.
- Units are explicit where physical values appear.
- Evidence IDs point to durable records.
- Proposal, review-required, validated and blocked states are distinct.
- Secrets never appear in manifests.

## Commands

```bash
afb schema list
afb schema skeleton source-asset-manifest --output artifacts/source-asset-manifest.json
afb manifest validate source-asset-manifest artifacts/source-asset-manifest.json
```

## Release dependency

A release record is only meaningful when its upstream manifests are valid. The governance record can then cite validated stage records instead of restating every technical detail.
