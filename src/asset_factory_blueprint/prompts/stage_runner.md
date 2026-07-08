# Direct partial invocation

Use `asset_stage_run` to run exactly one pipeline stage against a project workspace, without walking the full loop. This is the tool for an agent that only owns a slice of the pipeline: re-texture an asset, re-review a mesh, re-run segmentation after new masks landed.

## Contract

- `stage_id` (required): one of the reviewable content stages, for example `segmentation`, `material-inference`, `texturing`, `physics-articulation`, `nonvisual-materials`, `reconstruction` or `simready-verification`.
- `project` (recommended): path to an existing project workspace such as `projects/metal_jerrycan`.
- `request` (alternative): a run request path; when the project does not exist yet, the workspace is bootstrapped from it first.
- `dry_run` (default true): dry runs record the planned review without provider calls; pass false for a live VLM sign-off and fix attempts.
- `max_fix_attempts`: bound on fix-library remediation rounds for this stage.
- `refresh_artefacts` (default true): rebuild the workspace artefacts first so the review judges current inputs.

## Behaviour

The tool refreshes the workspace, runs the stage's VLM review against its rubric, applies bounded fixes from the fix library when the reviewer bounces the work and refreshes `progress.json`, the contact sheet and checksums. The result carries `final_state` (`approved`, `review_required`, `blocked`, `escalated_to_review` or `fix_attempts_exhausted`), the full iteration record and the stage report path.

## Rules

- The stage must be routed for the project; the tool refuses stages the run plan does not carry and lists what is available.
- A stage approved here is exactly as approved as one reviewed by the full loop: same rubric, same record schema, same gating.
- Numeric physical values stay proposals regardless of the invocation path; partial invocation never relaxes review requirements.
