# Mandatory early mesh verification

Status: accepted

Date: 2026-07-13

## Decision

Add a mandatory mesh-verification stage immediately after reconstruction and before every downstream geometry consumer. Reconstruction produces candidate geometry. Only mesh verification can promote that candidate to canonical geometry, and approval is bound to the candidate checksum and the active quality-policy checksum.

The stage combines deterministic topology and integrity checks with fixed full-surface renders, source-guided vision review and a blind dominant-object identity check. Deterministic failures cannot be overridden by the vision reviewer. Rejected candidates may be repaired or reconstructed with changed, rights-cleared conditioning, subject to bounded attempt policies. Missing evidence, unavailable review or exhausted attempts leaves the stage blocked.

## Rationale

Geometry defects become more expensive once segmentation, materials, physics and packaging have consumed the mesh. An early promotion boundary prevents malformed topology, missing parts, incorrect identity and poor reconstruction conditioning from becoming downstream assumptions. Tool-assisted measurements make structural decisions reproducible while vision review covers visible defects and source mismatch that topology alone cannot detect.

## Alternatives considered

- Keep the existing generic VLM sign-off after reconstruction. This does not establish deterministic topology gates or an exclusive canonical-geometry promotion rule.
- Use deterministic mesh checks without vision review. This catches structural defects but cannot reliably identify missing parts, wrong proportions or reconstruction of the surrounding scene.
- Defer mesh quality review to SimReady verification. This allows defective geometry to reach segmentation, material inference and physics before rejection.
- Require operator-only review. This provides judgement but does not produce a repeatable, automatable or checksum-bound decision path.

## Compatibility effect

This is a breaking stage-contract change. Stage contracts move from version 2.0 to 3.0 because reconstruction no longer publishes canonical geometry and downstream geometry stages now depend on mesh verification. The new mesh-verification record schema starts at version 1.0. Additions to the VLM review schema, tool surface, capability registry and fix library are compatible extensions.

Existing integrations that consume canonical geometry directly from reconstruction will stop at the new gate until they provide a checksum-matched approval record. Existing source evidence and reconstruction manifests remain valid inputs and do not need to be rewritten.

## Migration path

1. Treat reconstruction output as candidate geometry rather than canonical geometry.
2. Invoke the mesh-verification stage after each reconstruction or structure-changing repair.
3. Consume only the canonical geometry path and checksum recorded by an approved mesh-verification record.
4. Preserve each rejected attempt and distinguish mesh rejections, inference failures and reviewer unavailability in execution logs.
5. Re-run mesh verification for existing projects before allowing downstream geometry stages to continue.

There is no compatibility bypass. Projects without a current checksum-bound approval remain blocked until verification succeeds.
