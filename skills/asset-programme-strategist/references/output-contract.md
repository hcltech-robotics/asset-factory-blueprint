# Output contract

Return `ToolResult` with `success`, `data`, `error`, `warnings`, `artefacts`, `proposals` and `validation_status`.

## Blocked intake

Set `success` to true because the intake diagnostic completed and set `validation_status` to `blocked`. The data block contains:

- `ready`: false
- `draft`: the normalised partial run request
- `missing_inputs`: a non-empty list
- `pending_evidence`: evidence requirements that do not block start-up once the missing inputs are resolved

Each `missing_inputs` item contains:

- `field`: stable JSON field or source requirement
- `code`: stable machine-readable reason code
- `reason`: why the factory cannot start safely
- `question`: the focused question to ask

For conditional Profile blocks, use the exact fields `constraints.simready_profile.profile_id` and `constraints.simready_profile.profile_version`. For unsupported native CAD, name `sources.converted_asset`. Do not include Profile fields for texture-only or physics-only work. Reserve `missing_inputs` for start-blocking questions; put absent rights records and signed physics evidence in `pending_evidence`.

## Ready intake

Set `success` to true and use `validation_status: validated`. The data block contains:

- `ready`: true
- `missing_inputs`: an empty list
- `run_request`: the schema-valid request object
- `routed_stages`: the resolved stage identifiers
- `pending_evidence`: explicit evidence gaps for the fail-closed run

Pass this exact `run_request` object to `asset_factory_start`. The start tool persists it as the project `run-request.json`.

## Started factory

After the host obtains operator approval for the exact parameters, call the reviewed `asset_factory_start` mutation with `run_request` and optional `project_root`, `project_name`, `dry_run` and `max_fix_attempts`. The approval token stays in the invocation envelope, never the tool parameters. After start-up, retain the ready intake fields and add the returned:

- `status`: the start result status
- `project_id`
- `project_dir`
- `run_id`
- `progress`
- `contact_sheet`
- `agent_report`

Carry forward warnings and later evidence blockers without changing the confirmed request. A successful start means the governed run was created, not that validation or release gates passed.
