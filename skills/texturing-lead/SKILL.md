---
name: texturing-lead
description: Generate texture prompts, texture artefact plans and decals after material and UV checks.
version: 0.1.0
license: MIT
tools:
  - material_texture_prompt
  - material_texture_defaults_validate
  - material_texture_variation_workflow
metadata:
  tags:
    - asset-factory
    - texturing
  domain: texturing
  languages:
    - python
---
# Texturing-lead

## Purpose
Generate texture prompts, texture artefact plans and decals after material and UV checks.
The skill writes explicit proposal, evidence, report and handoff artefacts.
It uses provider output only as proposal material.
Promotion requires schema checks, evidence checks and review gates.

## When to use
Use this skill when the workflow stage matches the declared domain.
Use it when a project workspace exists or when the orchestrator is creating one.
Use it when the run request can be represented as structured manifests.
Use it when all source paths are inside approved project, cache or library roots.

## When not to use
Do not use it for direct mutation of original source assets.
Do not use it when rights, safety or retention status is unknown.
Do not use it to promote model output without validation.
Do not use it when required provider credentials are absent and the action is not dry-run safe.

## Prerequisites
Project workspace path.
Run identifier.
Manifest directory.
Evidence directory.
Report directory.
Provider resolver.
Validation gate list.

## Required inputs
Run request or stage manifest.
Source asset references where the stage consumes source data.
Prior stage manifests where the stage depends on previous evidence.
Provider role assignment.
Review policy.
W&B policy.

## Preflight
Check that the project directory exists.
Check that manifests are valid JSON.
Check that source paths are approved.
Check that output directories are writable.
Check that provider role assignment exists.
Check that raw keys are not present in checked-in files.
Check that dry-run status is explicit.
Check that blocked dependencies are reported before mutation.
Check whether any pending dependency needs user input or approval. When it does, ask the user directly for the required input or return a blocked ToolResult that names the missing input and approval target.

## Operating workflow
1. Load the project state.
2. Load the run request.
3. Load prior manifests.
4. Build the stage input contract.
5. Resolve provider roles.
6. Create candidate proposal requests.
7. Write proposal artefacts.
8. Run deterministic schema checks.
9. Run domain validation checks.
10. Write evidence records.
11. Write a stage report.
12. Return ToolResult with validation_status.
13. Stop on unapproved destructive action.
14. Hand off to the next declared skill.

## Provider requirements
Provider names come from configs/provider-policy.json.
Model names come from environment variables or policy defaults.
Provider traces record provider name, role, model and prompt checksum.
Provider traces never record bearer tokens.
A provider without required capability blocks the stage.
A failed provider call returns retryability and stage impact.

## Output contract
ToolResult.success states whether the tool completed its contract.
ToolResult.data contains structured output only.
ToolResult.error contains actionable failure text when success is false.
ToolResult.warnings lists non-blocking issues.
ToolResult.artefacts lists file paths and checksums.
ToolResult.proposals lists candidate records that need validation.
ToolResult.validation_status is proposal, validated, review_required, blocked or not_validated.

## Verification gates
Schema validity.
Source lineage.
Units and scale where applicable.
Evidence coverage.
Provider trace coverage.
Layer ownership.
Review requirement.
W&B plan status.
Checksum presence.
Promotion decision.

## Stop conditions
Missing required manifest.
Missing source evidence.
Invalid JSON schema.
Unknown write layer.
Unsafe source path.
Provider role cannot satisfy capability.
Required runtime unavailable.
Human review required before mutation.
Pending user input or approval is required and has not been requested or explained.
Rights or retention status blocks release.

## W&B logging expectations
Record run id.
Record stage id.
Record provider role and model id.
Record artefact checksums.
Record validation status.
Record blocked dependency reasons.
Do not record raw secrets.
Do not record proprietary source text unless retention policy allows it.

## Failure modes
Input manifest is missing required fields.
Provider endpoint is unavailable.
Provider returns malformed proposal data.
Evidence is too weak for promotion.
Layer ownership is ambiguous.
Runtime dependency is absent.
Output path leaves the project boundary.
Review gate is not satisfied.

## Handoff rules
Write handoff summary into the report directory.
Include upstream manifest ids.
Include downstream required manifests.
Include blocked dependencies.
Include reviewer actions.
Include any pending user input or approval request with the exact missing decision.
Include artefact checksums.
Include next skill name.
Do not mutate downstream layers directly.

## Eval coverage
Benchmark and eval payloads for this skill live in the asset-factory-verification repository under skill-checks/texturing-lead.
Run them from that repository against a blueprint checkout.
Eval cases cover the texturing contract path with structured output and explicit validation status.

## References
See references/operating-playbook.md.
See references/output-contract.md.

## Final state
A completed invocation leaves manifest, evidence, report and checksum artefacts in the project workspace.
Release status remains blocked until governance and validation gates pass.
