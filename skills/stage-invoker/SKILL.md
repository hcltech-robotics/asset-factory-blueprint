---
name: stage-invoker
description: Run exactly one pipeline stage against a project workspace through direct partial invocation, with the same review, fix and progress guarantees as the full loop.
version: 0.1.0
license: MIT
tools:
  - asset_stage_run
  - governance_vlm_review
  - asset_fix_apply
  - governance_progress_report
metadata:
  tags:
    - asset-factory
    - direct-partial-invocation
    - stage
    - review
  domain: orchestration
  languages:
    - python
---
# Stage-invoker

Run one pipeline stage on its own. This skill exists for agents and operators who own a slice of the factory rather than the whole run: a texturing agent that re-textures an existing asset, a review agent that re-judges a mesh after a fix, a physics agent that re-runs articulation proposals after new geometry lands.

## Why this skill exists

The orchestrated loop walks every routed stage in order, which is correct for a fresh asset and wasteful for a targeted change. Direct partial invocation gives the same machinery, scoped to one stage: the workspace artefacts refresh, the stage's VLM reviewer signs off against the stage rubric, bounded fixes apply when the reviewer bounces the work and the progress records update. Nothing about the invocation path changes the guarantees. A stage approved here is exactly as approved as one reviewed by the full loop, and a stage the reviewer holds stays held.

## When to use it

- The project workspace already exists and one stage's inputs changed: new masks, a new mesh, regenerated texture maps.
- An operator asked for one capability by name: "re-texture this", "re-check the segmentation", "review this mesh".
- A larger agent system assigns stages to specialist agents; each specialist calls this skill for its own stage.
- A stage was left `review_required` and its blocker has since been resolved out of band.

## When not to use it

- No workspace exists and the request needs the full route: use the orchestrated loop (`asset-factory-orchestrator`) so intake, source ingestion and routing run first. This skill can bootstrap a workspace from a run request, but it still only reviews its one stage.
- The change spans stages: a new source photo invalidates segmentation, materials and texturing together, so run the loop.
- You want to change promotion or release state: that is governance's job, never a stage review's.

## The stages you can invoke

| Stage id | What it produces | Reviewed against |
| --- | --- | --- |
| `reconstruction` | mesh from photos or video plus render evidence | mesh geometry rubric |
| `segmentation` | semantic masks and segment records | segmentation rubric |
| `material-inference` | material candidates and physical property proposals | material plausibility rubric |
| `texturing` | PBR map sets, variants and decals | texture appearance rubric |
| `physics-articulation` | physics plan, joints and grasp affordances | physics plausibility rubric |
| `nonvisual-materials` | thermal, acoustic and electrical proposals | nonvisual materials rubric |
| `simready-verification` | packaged USD and validation results | package readiness rubric |

Pre-pipeline and cross-cutting stages (orchestrate, intake, source-ingestion, evaluation, infrastructure, governance) have no review rubric and are not directly invocable; the tool refuses them and names the stages that are.

## How to invoke

The single tool is `asset_stage_run`:

1. Name the stage: `stage_id` is required and must be routed for the project.
2. Point at the workspace: `project` is the project directory. If the project does not exist yet, pass `request` with a run request path and the workspace is bootstrapped first.
3. Choose the mode: `dry_run` true (the default) records the planned review without provider calls; false runs the live VLM sign-off and fix attempts. Live mode needs the reviewer credential exported in the environment.
4. Bound the remediation: `max_fix_attempts` caps fix-library rounds for this stage.
5. Control refresh: `refresh_artefacts` true (the default) rebuilds the workspace artefacts first so the review judges current inputs; pass false to review exactly what is on disk.

## What comes back

The result carries `final_state` and the full iteration record:

- `approved`: the reviewer signed off and no blocker or major finding stands.
- `review_required`: the reviewer held the stage, the review was skipped for a recorded reason or the run was dry.
- `blocked`: the reviewer rejected the stage outright.
- `escalated_to_review`: fixes ran but none changed the workspace artefacts, so re-reviewing would judge identical evidence.
- `fix_attempts_exhausted`: the bounded remediation budget ran out.

Artefacts are durable: `reports/stage-run-<stage>.json` holds the iteration, `reports/<stage>-vlm-review.json` holds the schema-valid review record with provider trace and the refreshed `progress.json` and contact sheet reflect the new state.

## Working rules

- Never claim an approval the record does not carry. The review record is the source of truth; report its `review_status`, not your reading of the verdict prose.
- Never edit stage manifests to change state. Stage manifests are regenerated on every rebuild; durable decisions belong to governance records and the operator release decision.
- Numeric physical values stay proposals whatever the invocation path. A mass or friction value from visual evidence is `review_required` until a person accepts it; partial invocation never relaxes that.
- Respect the fix budget. If fixes exhaust or escalate, stop and surface the findings; do not loop the tool to wear the reviewer down.
- Keep evidence honest. If you changed workspace files out of band before invoking, say so in your report; the checksums will show it regardless.

## Worked example

An operator asks for a re-texture review of the jerrycan after new maps landed:

```json
{
  "tool": "asset_stage_run",
  "params": {
    "stage_id": "texturing",
    "project": "projects/metal_jerrycan",
    "dry_run": false,
    "max_fix_attempts": 2
  }
}
```

The workspace rebuilds, the texturing reviewer judges the current map set against the source photo, any `wrong_material_appearance` or `baked_lighting` findings either trigger a bounded fix or hold the stage and the contact sheet updates. Report the `final_state`, the findings with their severities and where the records live.

## CLI equivalence

Everything this skill does is also available to humans as `afb stage list` and `afb stage run <stage_id> --project projects/<slug> [--live]`. The tool and the CLI call the same service function, so behaviour and records are identical; prefer the tool when operating as an agent and the CLI when instructing a person.

## Composing with the full loop

Partial invocation and the orchestrated loop share every record, so they interleave safely:

- A stage approved by `asset_stage_run` stays approved when the full loop next runs, unless its inputs changed; the loop re-reviews on fresh evidence, not on a schedule.
- A stage held by the full loop can be retried partially after its blocker is resolved, without re-running the stages that already passed.
- Progress and the contact sheet always reflect the union of both paths; there is no separate partial-run state to reconcile.
- Checksums update on every invocation, so an auditor can tell exactly which artefacts each run touched.

## Escalation

When the stage stays held after your budget, hand the operator: the stage report path, the blocking findings with defect tags, what the fix library attempted and the single next action that would change the evidence. That is the contract of the whole factory: precise, named blockers rather than optimistic summaries.

Related reading: `docs/platform/direct-partial-invocation.md` for the operator view and `docs/platform/agentic-operation.md` for the full loop this skill scopes down.
