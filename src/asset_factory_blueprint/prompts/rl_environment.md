# RL environment

Design the repository's supported Isaac Lab 2.3.1 PhysX pick environment for an existing project workspace. The task object is a bottom-origin rigid body on a zero-height ground plane. The robot is fixed-base, has at least six revolute or continuous arm joints and uses declared prismatic gripper joints.

## Inputs

- the promoted SimReady package, composed USD checksum and package fingerprint;
- accepted asset-local grasp frames, mass and friction records;
- Isaac runtime evidence with the PhysX backend, `physics_dt` and settle criteria;
- a project-local URDF with joint types, limits, effort and velocity ceilings;
- explicit end-effector, gripper aperture, open and closed joint positions, action scale and actuator gains; and
- the exact pick success, observation, reward, reset, safety, evaluation and probe declarations.

## Contract

Verify every input against the materialised project files. Bind the result to the run-request digest, USD, package inventory, runtime report, URDF, backend, timestep and Isaac Lab version.

Emit only the implemented scene, state-based observations, ordered arm joint-position action, binary gripper action, exact `reach`, `lift` and `action_rate` rewards, declared terminations and resets, and evidence-bounded mass and friction randomisation. The curriculum tier is `none`.

Runtime probe and collision-fidelity reports remain unaccepted until the importer verifies their role-specific signatures, current project bindings, regenerated source and pinned producer bundles. Canonical reports also require receipts signed with the independent import key.

Reject other behaviours, articulated task objects, floating-base robots, fewer than six arm joints, non-prismatic gripper joints, camera observations, sensor contracts, variants, curricula, other backends, other reward forms and unsupported randomisation axes. Do not infer, approximate or replace a missing physical, geometric, runtime or task record.

Return structured JSON matching the requested schema and nothing else.
