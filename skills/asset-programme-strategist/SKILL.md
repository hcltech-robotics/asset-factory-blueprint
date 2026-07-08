---
name: asset-programme-strategist
description: This skill should be used when a user says "I have a photo", "I have a mesh", "I have CAD drawings", "I need physics" or "I just need textures", asks an agent to turn an asset goal into a run-request JSON or supplies an existing run request for intake and start-up.
version: 0.2.0
license: MIT
tools:
  - asset_programme_intake
  - asset_factory_start
metadata:
  tags:
    - asset-factory
    - intake
    - run-request
    - elicitation
  domain: intake
  languages:
    - python
---

# Asset-programme-strategist

## Goal

Turn an asset brief into an explicit, schema-valid run request, then start the governed factory from that request. Ask only the questions needed to choose a valid route. Never fill a consequential gap with an invented value.

The skill also preserves the runtime intake path for an existing run-request JSON. In both modes, `asset_programme_intake` runs first and `asset_factory_start` runs only after intake returns `data.ready: true`.

## Modes

### Pre-workflow authoring

Use this mode when the user describes an asset goal in natural language, names source files or answers with phrases such as:

- "I have a photo."
- "I have a mesh."
- "I have CAD drawings."
- "I need physics."
- "I just need some textures for this thing."

Elicit the minimum missing facts, author the run-request JSON and validate it before starting the factory.

### Runtime intake

Use this mode when the user already has a run-request JSON. Read the JSON object and pass it as `draft` to `asset_programme_intake` for validation and route checks. Do not rewrite valid decisions merely to restyle the document. If intake returns missing inputs, ask for them or stop blocked before calling `asset_factory_start`.

## Required decisions

Collect or confirm:

1. The asset objective in the user's terms.
2. At least one usable source reference.
3. The requested deliverables.
4. Source-specific facts that affect routing, including declared units for a raw mesh when they are not encoded reliably.
5. Review, simulator and physical-behaviour constraints only when they affect the requested result.
6. Exact SimReady Profile identity only when the conditional Profile rule applies.

An identifier and request version may be derived mechanically when this does not change the user's intent. Do not derive source rights, measurements, Profile identity, physical values or release decisions.

## Conditional Profile rule

Require both of these exact fields when any requested deliverable is SimReady, an OpenUSD or USD package or an RL or Isaac Lab environment:

- `constraints.simready_profile.profile_id`
- `constraints.simready_profile.profile_version`

Never invent, default, infer or substitute either value. Do not emit sentinel values such as `unresolved`, `<profile-id>` or a guessed vendor Profile. If either value is absent, ask a focused question. If it remains unanswered, return `validation_status: blocked` with the exact field names in `data.missing_inputs` and do not call `asset_factory_start`.

A texture-only, material-only, nonvisual-material-only or physics-only request does not require a SimReady Profile. Canonical physics-only intent uses `requested_outputs: ["physics"]`. A mixed request that also asks for SimReady, a USD package or RL does require a Profile.

Physics promotion requires signed `constraints.physics_evidence`. Record absent rights records or accepted physics evidence in `data.pending_evidence`; never infer mass, inertia, centre of mass or a signature. These evidence gaps may enter the factory as a fail-closed proposal and do not become start-blocking `missing_inputs`. This evidence rule is independent of the conditional Profile rule.

## Source rules

- Photos and rendered drawings are image sources and may route through reconstruction.
- Raw meshes are accepted, but units and scale must be explicit when the file does not establish them reliably.
- Existing USD sources may enter the governed USD path directly.
- Native STEP, STP, IGES, IGS, DWG and DXF sources are not convertible by the current authoring runtime. Return blocked with a named missing input for a converted USD or supported mesh source. Do not claim that native CAD conversion will occur.
- Put the converted USD or supported mesh in `sources`. Record the original CAD file under `constraints.governance_evidence` with a stable evidence ID and `kind: cad_source` when the user wants it retained.
- Keep original sources immutable and retain the user's source paths in the request.

## Elicitation rules

- Ask short, concrete questions about one decision or a tightly related set of decisions.
- Explain why a requested field matters when the user may not know it.
- Do not ask for a Profile on a texture-only or physics-only path.
- Do not turn absent release or physics evidence into an intake interview when the factory can record it as pending evidence.
- Do not turn uncertainty into a plausible-looking value.
- Preserve answers verbatim where they carry domain meaning, then encode them in the appropriate structured field.
- If several facts are missing, return a finite `missing_inputs` list rather than beginning an open-ended interview.

Each missing-input record must name the JSON field or source requirement, provide a stable code, explain why it blocks and provide the focused question to ask.

## Operating workflow

1. Determine whether the input is a natural-language brief or an existing run request.
2. Read `references/operating-playbook.md` for the mode-specific sequence.
3. Translate the conversation into a partial run-request object without inventing unknown values, then call `asset_programme_intake` with `{draft: <partial run request>}`.
4. Inspect `data.ready`, the normalised `data.draft` or canonical `data.run_request`, `data.routed_stages`, `data.missing_inputs` and `data.pending_evidence`.
5. If `data.ready` is false, ask every returned `missing_inputs[].question`. Call intake again only when new answers are available, passing the updated object as `draft`.
6. If the user does not provide the blocking decisions, return the blocked diagnostic unchanged in substance and stop.
7. When `data.ready` is true, confirm that `data.run_request` satisfies `schemas/run-request.schema.json`, uses supported deliverable aliases and obeys the source and Profile rules above. Retain `pending_evidence` as explicit fail-closed work.
8. Present the validated request and exact execution settings for operator approval through the host, then call `asset_factory_start` with `{run_request: data.run_request}` plus any explicit `project_root`, `project_name`, `dry_run` or `max_fix_attempts` choice.
9. Use the project's persisted `run-request.json` as the authored request artefact.
10. Report the request path, project path, run identifier, status and any remaining evidence blockers.

## Start rules

- `asset_programme_intake` always precedes `asset_factory_start`.
- Never call the start tool while `data.ready` is false or while `missing_inputs` is non-empty.
- `asset_factory_start` is a reviewed mutation boundary. Obtain operator approval through the host for the exact request and execution settings before invoking it.
- The approval token belongs to the tool-server invocation envelope. Never add it to tool parameters, mint it, read the server secret or reuse it for different parameters.
- If approval is absent, denied or expired, stop and report that exact blocker.
- Use dry-run behaviour unless the user explicitly asks for live provider-backed work.
- Starting the factory does not waive later validation, review, evidence or governance gates.
- A successfully authored request is not a claim that the resulting asset is SimReady or release-ready.

## CLI path

When the agent has repository shell access but no connected tool surface, write the draft under `artifacts/run-requests/<asset-id>.json` and run:

```bash
uv run afb agent intake --draft artifacts/run-requests/<asset-id>.json
uv run afb agent start --request artifacts/run-requests/<asset-id>.json --project-root projects
```

Treat the first command's JSON exactly like the `asset_programme_intake` result. Update the draft only from user answers, then run intake again. The start command applies the same checks and persists the confirmed request in the project before entering the agent loop. Use the tool-server path instead when an external host owns mutation approval.

## Stop conditions

Stop with `validation_status: blocked` when:

- the objective or requested deliverables remain ambiguous
- no usable source is supplied
- the source format has no supported route
- a native STEP, STP, IGES, IGS, DWG or DXF file has no converted USD or supported mesh companion
- mesh units needed for safe authoring remain unknown
- a SimReady, USD-package or RL request lacks an exact Profile ID or version
- known source rights or retention terms prevent the requested use
- the run request fails schema or route validation
- a required user decision, approval, credential or runtime remains unavailable

Do not report Profile fields as missing for texture-only or physics-only work.

## Output contract

Return a `ToolResult` with `success`, `data`, `error`, `warnings`, `artefacts`, `proposals` and `validation_status`. Follow `references/output-contract.md` for the required data fields.

When blocked, `success` remains true because intake completed its diagnostic contract, `validation_status` is `blocked` and `data.missing_inputs` contains stable field names. When ready, `data.run_request` is the validated JSON object and `data.pending_evidence` records non-start-blocking evidence gaps. After start-up, the project contains the persisted request and the result names the created project and run.

## Handoff

The validated request object is handed to `asset_factory_start`, which enters the whole-run agentic path and persists `run-request.json`. Include every `pending_evidence` item in the handoff without converting it into an accepted fact. Do not mutate downstream stage manifests directly.

## Verification

- Frontmatter tools must resolve to `asset_programme_intake` and `asset_factory_start`.
- The request must validate against `schemas/run-request.schema.json`.
- Requested outputs must resolve through the current stage contracts.
- Conditional Profile and native-CAD blocks must be represented in `missing_inputs`.
- The canonical request passed to start must match the proposal the user confirmed.
- The project-persisted request must match the canonical request passed to start.

## References

Read `references/operating-playbook.md` for the invocation sequence and `references/output-contract.md` for ready and blocked result shapes.
