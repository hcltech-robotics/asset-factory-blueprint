---
name: mesh-verification-agent
description: Perform mandatory vision-guided and tool-assisted verification of candidate geometry before canonical promotion.
version: 0.1.0
license: MIT
tools:
  - governance_mesh_verify
metadata:
  tags:
    - asset-factory
    - geometry
    - verification
  domain: geometry-validation
  languages:
    - python
---
# Mesh-verification agent

## Purpose

Protect every downstream stage from defective geometry by verifying the exact candidate checksum before it can become canonical geometry.

## Required inputs

- source asset manifest and source images where available
- reconstruction manifest
- candidate geometry
- resolved vision reviewer
- mesh-verification policy and rubric

## When to use

Use this skill for every routed geometry source after reconstruction or source conditioning.
Use it for generated GLB or glTF assets.
Use it for imported mesh assets.
Use it for USD assets containing mesh prims.
Use it again whenever candidate geometry bytes change.
Use it before segmentation, materials, physics or SimReady work consumes geometry.

## When not to use

Do not use it to approve source rights.
Do not use it to infer hidden physical properties.
Do not use it to replace SimReady runtime verification.
Do not bypass it because the source geometry was supplied rather than generated.
Do not accept an operator decision as a substitute for the agent invocation.

## Prerequisites

Project workspace path.
Source asset manifest.
Reconstruction manifest.
Candidate geometry path and checksum.
Resolved vision reviewer.
Writable manifest and report directories.
Registered mesh diagnostic tools.
Explicit dry-run status.

## Preflight

Confirm that the candidate is inside the project workspace.
Confirm that the candidate suffix is supported.
Confirm that source evidence remains immutable.
Confirm that the reconstruction manifest names candidate geometry rather than canonical geometry.
Confirm that the reviewer role resolves to a provider and model.
Confirm that raw provider keys are absent from manifests and logs.
Confirm that retry budgets are available.
Confirm that prior approval, when present, matches the current candidate checksum.

## Operating workflow

1. Resolve and checksum the candidate geometry.
2. Run deterministic format and topology diagnostics.
3. Produce fixed-camera beauty and wireframe contact sheets.
4. Present the diagnostic renders, source evidence and tool measurements to the vision reviewer.
5. Record `approve`, `revise_local`, `regenerate` or `blocked`.
6. Re-run diagnostics and review after any local repair or reconstruction resubmission.
7. Promote only when the approval record names the current candidate checksum.

## Diagnostic policy

The verifier loads the complete scene and concatenates renderable meshes for measurement.
The verifier records vertex and face counts.
The verifier records connected component count.
The verifier records finite-coordinate and valid-index checks.
The verifier records degenerate and duplicate face counts.
The verifier records watertightness and winding consistency.
The verifier records bounds, extents, area and Euler number.
The verifier runs glTF Validator or usdchecker when the corresponding executable is available.
The verifier does not mutate the candidate while measuring it.

## Render policy

Use the fixed eight-view camera policy.
Use the fixed seed recorded in the render bundle.
Generate beauty, wireframe and normal contact sheets.
Keep each contact sheet checksum in the verification record.
Put diagnostic renders before source images in the reviewer request.
Limit source images to the configured evidence budget.
Regenerate the bundle after candidate bytes change.

## Vision policy

Ask the reviewer to compare silhouette, proportions, components and visible surface quality.
Supply tool measurements as structured stage context.
Use only controlled defect tags.
Require a structured verdict and action.
Do not allow a visual approval to override a hard tool failure.
Treat malformed output as blocked.
Treat provider failure as blocked.
Record provider, model, role and rubric checksum.

## Decision policy

`approve` promotes the exact candidate checksum.
`revise_local` applies a registered structure-preserving mesh fix.
`regenerate` executes a new reconstruction inference attempt.
`blocked` stops downstream work and names the blocking evidence.
Minor findings may remain recorded with approval.
Blocker or major findings prevent approval.

## Retry policy

Count the initial reconstruction as inference attempt one.
Count every reviewer invocation.
Count every non-approve decision as a mesh rejection.
Count every executed backend retry as an inference resubmission.
Count every applied mesh-conditioning operation as a local fix.
Stop when the configured review or inference budget is exhausted.
Never re-review identical evidence after a fix reports no artefact change.
Append every attempt to the durable history.

## Mandatory behaviour

The stage never returns `skipped`. Missing candidate geometry, missing diagnostic renders, an unavailable reviewer or malformed reviewer output blocks the stage. An operator decision cannot substitute for the agent invocation. Tool-reported hard failures cannot be overridden by the vision model.

## Output contract

Write `manifests/mesh-verification-record.json`, diagnostic JSON, deterministic render bundles and append-only review history. Record inference attempts, reviews, mesh rejections, local fixes and inference resubmissions. Never record provider secrets.

ToolResult.success is true only for an approved candidate with a valid record.
ToolResult.data contains the complete verification record.
ToolResult.error contains schema or execution failures.
ToolResult.warnings contains advisory mesh and reviewer findings.
ToolResult.artefacts lists the manifest, diagnostics and render bundle.
ToolResult.proposals contains the verification record.
ToolResult.validation_status is validated only after checksum-bound approval.

## Evidence contract

Record the candidate geometry checksum.
Record the diagnostics checksum.
Record each render checksum.
Record source evidence checksums through the review record.
Record rubric and provider trace checksums.
Record the canonical geometry path and checksum only after approval.
Set `raw_secrets_recorded` to false.

## Stop conditions

Candidate geometry is missing.
Candidate geometry leaves the project boundary.
The mesh cannot be loaded.
The mesh is empty.
Vertex coordinates are non-finite.
Face indices are invalid.
The format validator fails.
The render bundle cannot be generated.
The reviewer is unavailable.
The reviewer response is malformed.
Repair or resubmission attempts are exhausted.
The approval checksum is stale.

## Progress reporting

Expose the current decision.
Expose the candidate checksum.
Expose review attempt count.
Expose mesh rejection count.
Expose inference resubmission count.
Expose promotion state.
Expose blocker reasons.
Include diagnostic images in the operator contact sheet.

## Failure modes

Imported USD has no renderable mesh prims.
The GLB contains no mesh geometry.
The provider accepts fewer images than the configured evidence bundle.
A local fix reports success without changing candidate bytes.
A reconstruction resubmission does not land a new candidate.
The old approval record refers to a different checksum.
The verification record fails JSON Schema validation.

## Security

Confine candidates and outputs to authorised workspace roots.
Never place provider tokens in prompts, traces or records.
Do not mutate original source assets.
Use governed external-model manifests for inference resubmission.
Preserve append-only attempt evidence.

## Handoff

An approved record publishes `canonical-geometry` bound to the candidate checksum. Every downstream geometry consumer depends on that deliverable.

The handoff names the canonical geometry path.
The handoff names the approved checksum.
The handoff includes the verification record path.
The handoff includes unresolved minor findings.
The handoff blocks when promotion is absent.

## Eval coverage

Route tests prove that every geometry route includes mesh verification.
Diagnostic tests prove that a candidate produces tool measurements and fixed views.
Promotion tests prove that approval binds the exact checksum.
Invalidation tests prove that changed bytes make old approval stale.
Benchmark tests prove that reviewer configuration is mandatory.
Trace tests prove that rejections and inference resubmissions are counted.

## References

See `references/operating-playbook.md`.
See `references/output-contract.md`.
See `docs/pipeline/01a-mesh-verification.md`.

## Final state

A successful invocation leaves an approved, schema-valid mesh-verification record and publishes canonical geometry. Any incomplete invocation leaves downstream geometry deliverables unavailable.
