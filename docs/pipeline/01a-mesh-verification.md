# Mandatory mesh verification

Mesh verification is the early promotion boundary between reconstruction and every downstream geometry consumer. It reduces the risk that missing parts, malformed topology, wrong proportions or reconstruction artefacts become embedded in segmentation, materials, physics or SimReady packages.

Reconstruction writes `candidate-geometry`. It does not write `canonical-geometry`. The `mesh-verification-agent` is the only stage that can promote the candidate and its approval is bound to the candidate SHA-256 checksum.

## Verification sequence

1. Resolve the candidate inside the project workspace and record its checksum.
2. Run format loading and deterministic mesh diagnostics with the registered geometry tools.
3. Measure exact edge incidence, connected components, Euler characteristic, genus where defined, boundary loops, watertightness, winding consistency, non-manifold edges, interior faces, duplicate faces, degenerate faces and triangle quality.
4. Generate fixed eight-view full-surface beauty, wireframe and normal contact sheets.
5. Build a labelled source-to-candidate sheet with the source photo above the eight candidate views.
6. Give the comparison sheet and authoritative deterministic check statuses to the configured vision reviewer.
7. Before promotion, run a blind identity check on the candidate views without revealing the intended label and compare the dominant object against governed aliases.
8. Record `approve`, `revise_local`, `regenerate` or `blocked`.
9. Re-run diagnostics and vision review after every repair or inference resubmission.
10. Publish `canonical-geometry` only when the approval and promotion records match the current candidate checksum.

The verifier uses the NVIDIA vision endpoint by default. The provider, model, rubric checksum, diagnostic tool versions, camera policy and every evidence checksum are recorded without storing API keys.

## Failure semantics

Missing candidate geometry, an unreadable mesh, invalid face indices, non-finite coordinates, missing diagnostic renders, an unavailable reviewer or malformed reviewer output blocks the stage. The mandatory verifier never returns `skipped`. Tool-reported hard failures cannot be overridden by the vision model.

The quality policy is a deterministic gate, not advice to the reviewer. The `simulation_closed_surface` profile requires watertight, consistently wound geometry with a defined genus, no boundary, non-manifold, orientation-conflict, degenerate, duplicate or interior faces and no more than 64 connected components. The `appearance_mesh` profile keeps exact topology measurements but permits open and multi-component reconstruction output within its declared thresholds. Each check records its expected value, actual value, status, severity and recommended action. The policy checksum is bound to the approval, so changing the policy invalidates an older approval.

The vision reviewer receives the status of every check with the renders and source evidence. It does not reinterpret passing raw counts. It can choose a structure-preserving repair or regeneration, but it cannot approve a candidate while a deterministic quality failure remains. Genus is reported only for closed, consistently oriented surfaces. Open or non-orientable geometry records genus as undefined and fails closed when the selected profile requires it.

The blind identity check prevents the intended label from biasing the approval. It names the dominant enclosing object from the candidate renders alone. Promotion requires that name to match a governed alias for the intended asset. A cabinet, box, room or other surrounding structure therefore cannot pass merely because the intended object appears inside it.

## Repair and regeneration

`revise_local` selects a registered structure-preserving mesh repair such as healing holes, pruning floating fragments or repairing normals. `regenerate` executes a new reconstruction backend run with changed conditioning. The remediation sequence can remove the background from the original, select another rights-cleared source view, remove the background from that alternate or apply another registered source repair. A send-back never reuses the rejected mesh as a new candidate.

The retry budget is bounded by both inference and review caps. Inference failures consume an inference attempt but are not mesh rejections. Reviewer transport or response-format failures are recorded as review unavailability and are not mesh rejections. Exhausted attempts leave the stage blocked with its full evidence history.

## Artefacts

- `manifests/mesh-verification-record.json`
- `reports/mesh-verification/diagnostics.json`
- `reports/mesh-verification/beauty-contact-sheet.png`
- `reports/mesh-verification/wireframe-contact-sheet.png`
- `reports/mesh-verification/normal-contact-sheet.png`
- `reports/mesh-verification/source-candidate-comparison.png`
- `reports/mesh-verification-blind-identity.json`
- `reports/mesh-verification-attempt-<nn>/`
- `reports/mesh-verification-history.jsonl`
- `reports/fix-attempts.json`

## Direct invocation

```bash
afb stage run mesh-verification --project projects/<slug> --live --max-fix-attempts 2
```

Downstream stages remain blocked until this invocation produces an approved checksum-bound record.

The [mandatory early mesh verification design decision](../design-decisions/mandatory-mesh-verification.md) records the alternatives, compatibility effect and migration path for this promotion boundary.
