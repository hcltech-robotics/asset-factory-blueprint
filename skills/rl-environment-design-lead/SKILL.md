---
name: rl-environment-design-lead
description: Configure the supported Isaac Lab pick task from validated assets and enforce its signed execution boundary.
version: 0.3.0
license: MIT
tools:
  - rl_route
metadata:
  tags:
    - asset-factory
    - rl
    - isaac-lab
    - task-design
  domain: rl
  languages:
    - python
---
# RL environment design lead

## Purpose

Apply the repository's implemented Isaac Lab 2.3.1 PhysX pick lane to a validated asset package. The task object is rigid and bottom-origin. Its fixed-base robot has at least six angular arm joints and declared prismatic gripper joints with explicit open and closed positions and a maximum aperture.

The skill writes design records. It does not train a policy or claim sensor deployment.

## Entry conditions

Start after the SimReady package has a passing runtime record and its physics and material manifests carry accepted mass, friction and asset-local grasp evidence.

The run request must provide:

- behaviour `pick` and a structured lift-and-hold success condition;
- a project-local URDF named in the request sources;
- the end-effector body, fixed-base flag, gripper joints, maximum aperture, open and closed joint-position maps, action scale and actuator gains;
- a zero-height support surface and a bottom-origin object position;
- exact reward forms, reset ranges, safety limits and probe parameters;
- PhysX, Isaac Lab 2.3.1, timing, environment count and unique seeds; and
- at least five unique evaluation seeds.

Reject other behaviours, articulated task objects, floating-base embodiments, fewer than six arm joints, non-prismatic gripper joints, camera observations, sensor contracts, variants, curricula, unsupported reward forms and unsupported randomisation axes.

## Evidence checks

Verify the run-request digest and upstream checksums before design. Resolve the URDF through the source manifest, then parse its links, joint types, limits, effort ceilings and velocity ceilings. A named body or joint that is absent from the URDF blocks the embodiment.

Use accepted grasp points only. Each point needs a stable ID, an asset-local seven-value pose, `wxyz` unit quaternion, unit approach vector, positive gripper width, evidence IDs and accepted status. A width above the embodiment's maximum aperture blocks the lane. Each declared gripper joint needs finite open and closed positions within its URDF limits. The two maps name exactly the declared gripper joints and must differ.

Bind the environment to the composed USD checksum, package inventory fingerprint, runtime report checksum, robot checksum, backend, `sim_dt`, decimation and Isaac Lab version. Do not infer a missing value.

## Environment contract

The policy and critic groups may use only the supported simulator-state and proprioceptive terms. `object_position` and `object_velocity` make the contract state-based; they do not establish a perception or sim-to-real path.

The action manager contains an ordered arm joint-position term and one binary gripper term. The gripper term carries the declared open and closed position maps. Arm reset offsets apply only to revolute and continuous joints. Gripper joints reset with zero offset from their declared open initial positions.

The reward manager contains exactly:

```text
reach       = 1 - tanh(distance_to_grasp_frame / 0.1)
lift        = clip(object_height - support_height, 0, 0.1) / 0.1
action_rate = -sum(square(action_t - action_t_minus_1))
```

Mass and friction are the only supported randomisation axes. Their intervals come from accepted upstream evidence. The only supported curriculum tier is `none`.

## Runtime acceptance

Render to `envs/`. Runtime producers regenerate the expected source from the current manifest and require byte-for-byte equality before execution.

The probe report passes only when all five probes pass:

- smoke: finite zero-action and random-action rollouts;
- oracle: every accepted grasp is reachable and successful in every environment, with no joint-limit violation;
- reset: every declared reset settles without early termination, excessive PhysX penetration or excessive speed;
- repeat: terminal transitions, trajectories and returns repeat within the declared tolerances; and
- gaming: all four canonical patterns run for the complete scripted-oracle horizon and stay within the per-environment oracle-return allowance.

The oracle also requires finite observed reward components and the recorded component-dominance bound.

Collision fidelity uses separate render-purpose visual meshes and proxy-purpose collision meshes in the composed USD. It measures the exact declared sample count in asset-local metres around every accepted grasp.

## Import boundary

Probe and fidelity producers write only under `reports/incoming/`. Probe reports use only `AFB_RL_PROBE_ATTESTATION_SECRET`; fidelity reports use only `AFB_RL_FIDELITY_ATTESTATION_SECRET`. Import verifies the schema, report-role signature, task protocol, materialised project bindings, regenerated environment source and administrator-pinned producer bundle. The importer signs the receipt with the independent `AFB_RL_IMPORT_ATTESTATION_SECRET`, writes the canonical report and receipt, then refreshes the environment manifest. The upstream Isaac load-evidence key is not accepted for any RL report or receipt.

Canonical reports without valid receipts do not satisfy a gate.

## Outputs

The lane writes `manifests/rl-environment-manifest.json`, `reports/rl-environment-design-report.json`, `reports/rl-task-fitness-protocol.json` with scope `rigid_body_manipulation`, and `reports/environment-card.md`. Rendering adds `envs/`. Imports add the canonical probe and fidelity reports with signed receipts.

The environment remains blocked while deterministic inputs or runtime evidence are missing. Once those gates pass, its status is `review_required`; human review remains distinct from runtime acceptance.

## References

- [Environment manifest shape](references/environment-manifest-shape.md)
- [Output contract](references/output-contract.md)
- [RL environment documentation](../../docs/extensions/rl-environment.md)
