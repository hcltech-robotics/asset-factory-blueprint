# Environment manifest shape

RL data lives under `extensions.rl` in `manifests/rl-environment-manifest.json`.

```text
extensions.rl
  schema_version
  lineage
  runtime
    physics_backend, isaac_lab_version, sim_dt, decimation
    episode_length_s, render_interval, num_envs, seeds
    validated_usd_sha256, validated_package_dependency_fingerprint
  task
    behaviour
    embodiment
      source, project_path, sha256, format, robot_name
      fixed_base, link_names, actuated_joints
      end_effector_body, gripper_joints, maximum_gripper_aperture_m
      gripper_open_positions, gripper_closed_positions
      action_scale, actuator, status
    success
    grasp_frames
    reconciliation
  scene
    composed_root, object_asset_type, object_initial_position
    support_height_m, env_spacing
  observations
  actions
  rewards
  terminations
  resets
  randomisation
  safety
  evaluation_protocol
  task_fitness_protocol
  command_contracts
  contract_sha256
  status, gates, blocked_reasons, review_reasons
```

The semantic contract checksum excludes review state, evidence gates and rendered documentation. It includes the runtime, task, scene, observation, action, reward, reset, randomisation, safety and protocol bindings.

A validated embodiment has a project-local URDF checksum and complete joint records. Its open and closed position maps name every declared prismatic gripper joint exactly once, stay within the URDF limits and differ. A renderable pick contract has accepted grasp frames, exact supported actions and rewards, a rigid object, zero-height support and no unsupported sensors, variants or curriculum.
