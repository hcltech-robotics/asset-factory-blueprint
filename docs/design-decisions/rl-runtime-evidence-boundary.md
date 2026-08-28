---
description: "Review the accepted boundary for Isaac Lab runtime evidence, including supported pick-task semantics, checksum bindings and rejected behaviours."
---

# RL runtime evidence boundary

Status: accepted

Date: 2026-08-27

## Decision

The executable RL lane is an Isaac Lab 2.3.1 PhysX pick-task implementation for a rigid bottom-origin object and a fixed-base, full-pose arm with a prismatic gripper. It renders only the semantics implemented in the generated package. Unsupported behaviours, articulated task objects, floating bases, camera observations, sensor contracts, variants, physics backends, reward forms and randomisation axes fail validation before artefact creation.

Runtime evidence is valid only for the contract that produced it. Probe and collision-fidelity reports bind the environment contract checksum, run-request checksum, composed-USD checksum, robot-URDF checksum, package inventory fingerprint, backend, physics timestep, decimation, producer inventory and generated-code checksums. The importer regenerates the expected Python sources from the current manifest, verifies those bindings and the report signature, then writes a signed receipt. Canonical evidence has no effect without that receipt.

The lane provides environment design, rendering, runtime probes and collision-fidelity measurement. It does not provide policy training or policy evaluation.

## Rationale

The mismatch is silent. A manifest can look complete, pass structural validation and reach downstream automation even though the simulator has never executed the declared behaviour because its renderer omitted the corresponding observation, action, reward or randomisation term. Rejecting unsupported semantics keeps generated code and manifest equivalent.

Runtime results depend on code, dependencies, solver and timing. A report bound only to an asset or filename can survive changes to task semantics, installed packages, generated modules, the selected backend or the number of simulation steps between actions, while still presenting the same outward status. The complete binding makes that reuse detectable.

## Alternatives considered

- Emit unimplemented reward and randomisation terms as comments or exceptions in generated files. This creates an artefact that cannot satisfy its manifest.
- Replace unavailable observations or contact measurements with zero values. This converts missing evidence into a passing numerical input.
- Accept a caller-declared backend without reading the live simulator. This attests the request rather than the runtime.
- Bind evidence only to the environment-manifest file checksum. This misses package and producer changes, and makes status-only manifest rewrites invalidate otherwise identical task semantics.
- Include training and evaluation commands before their runners exist. This presents an interface without an implementation.

## Compatibility effect

The RL stage contract moves to version 4.0. The stage consumes the promoted SimReady package, accepted physics and material records, runtime evidence and robot embodiment. It produces the RL environment manifest, task-fitness protocol, environment card and, after their independent runs and imports, attested probe and collision-fidelity evidence with import receipts.

Reports produced against an earlier contract, package inventory, timing configuration or generated environment are rejected. Requests outside the supported pick-task boundary remain blocked without generated environment files.

There is no compatibility bypass.

## Extension rule

A new behaviour, sensor, backend, reward or randomisation axis enters the boundary only when its renderer, runtime measurement, schema, probe coverage and evidence binding are implemented together. Documentation records the capability after those surfaces agree.
