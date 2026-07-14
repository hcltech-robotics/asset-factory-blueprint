# Operating playbook

1. Verify the run-request digest, SimReady promotion, composed-USD checksum, package fingerprint and Isaac runtime report before reading task evidence, rejecting the workspace unless the report's asset identity, request digest, backend, physics timestep, package fingerprint and USD checksum match the materialised files.
2. Load accepted physics and material evidence for grasp frames, mass and friction.
3. Resolve and hash the requested URDF while validating the fixed base, arm degrees of freedom, gripper joints, aperture, explicit open and closed joint positions and actuator limits.
4. Build the pick scene and its observation, action, reward, reset, termination and safety terms, rejecting any declaration that the implemented renderer would have to ignore.
5. Write the draft task-fitness protocol, command contracts, environment card and environment manifest.
6. Render the current contract to `envs/`.
7. Run probe and fidelity producers into `reports/incoming/`, using only the attestation key assigned to each report role.
8. Import each report through the producer-pinning evidence command, verify it with its report-role key and sign the receipt with the independent import-role key. The importer then refreshes the manifest.

Stop on a missing or mismatched record. Do not replace missing runtime, geometry, contact or task evidence with a default value.
