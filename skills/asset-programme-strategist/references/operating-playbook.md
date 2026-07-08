# Operating playbook

## Choose the mode

Use pre-workflow authoring for a natural-language brief. Use runtime intake for an existing run-request JSON. Both modes use the same tool order:

1. `asset_programme_intake`
2. `asset_factory_start`

The second tool is conditional on the first returning `data.ready: true` with an empty `missing_inputs` list.

## Pre-workflow authoring

1. Translate the user's brief into a partial run-request object, leaving unknown consequential values absent.
2. Call `asset_programme_intake` with `{draft: <partial run request>}` and read `data.draft`, `data.routed_stages`, `data.missing_inputs` and `data.pending_evidence`.
3. If the result is blocked, ask the questions carried by `data.missing_inputs`. Keep field names unchanged.
4. Merge the answers into the draft and call `asset_programme_intake` again with `{draft}`. Do not call it repeatedly without new information.
5. When intake returns ready, present `data.run_request` and the chosen optional start settings for operator approval through the host, then call `asset_factory_start`.
6. Report the authored request and the created project and run paths.

The intake exchange should be finite. Typical focused questions are:

- `sources`: "Which photo, mesh or USD file should the factory use?"
- `constraints.source_units`: "What units is this mesh authored in?"
- `requested_outputs`: "Do you want textures, physics work, a SimReady USD package or an RL environment?"
- `constraints.simready_profile.profile_id`: "Which exact SimReady Profile ID is the asset targeting?"
- `constraints.simready_profile.profile_version`: "Which exact version of that Profile is required?"
- `sources.converted_asset`: "Please provide a converted USD or supported mesh export for this native CAD source."

Ask the two Profile questions only when the outputs include SimReady, OpenUSD, a USD package or RL. Texture-only and physics-only work proceeds without them. Physics-only intent is encoded as `requested_outputs: ["physics"]`; absent signed physics evidence is retained in `pending_evidence` so the factory starts a fail-closed proposal.

## Runtime intake

1. Read the existing JSON and pass the object as `{draft: <run-request>}` to `asset_programme_intake`.
2. Preserve valid user-authored decisions and extensions.
3. Surface schema, route, source and conditional Profile issues through `data.missing_inputs`.
4. Ask for missing decisions or return blocked.
5. Pass `data.run_request` to `asset_factory_start` as `run_request`.

## Source handling

Photos, rendered drawings, supported meshes and USD files may proceed through their declared routes. Native `.step`, `.stp`, `.iges`, `.igs`, `.dwg` and `.dxf` files stop at intake because the current authoring runtime cannot convert them. Require the user to identify a converted USD or supported mesh file before start-up. Put that export in `sources`; retain the original CAD file under `constraints.governance_evidence` with `kind: cad_source`.

For raw meshes, ask for units when they cannot be established from the source. Do not fabricate scale. For physics work, place absent accepted evidence in `pending_evidence`; never create measured or manufacturer values from the brief. Absent rights evidence is also pending unless known terms prohibit the requested use.

## Start-up

Use dry-run start-up unless live provider-backed work was explicitly requested. Optional start fields are `project_root`, `project_name`, `dry_run` and `max_fix_attempts`. Never start when `data.ready` is false or `missing_inputs` is non-empty.

`asset_factory_start` is a reviewed mutation. Obtain short-lived, parameter-bound operator approval through the host for the exact request and execution settings. Do not place `approval_token` in the tool parameters, mint or inspect the server secret or reuse approval for changed parameters. If approval is absent, denied or expired, stop and report that exact blocker. Return later workflow evidence blockers as blockers, not as reasons to rewrite the confirmed request silently.

When no tool surface is connected but repository shell access is available, persist the draft under `artifacts/run-requests/`, call `afb agent intake --draft <path>` and use `afb agent start --request <path>` only after intake is ready. This CLI path applies the same service contracts and enters the same loop.
