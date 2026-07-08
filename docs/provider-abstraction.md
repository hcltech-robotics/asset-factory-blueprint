# Provider abstraction

Providers are selected by role and capability, not by hard-coded vendor logic.

## Provider authority

Models may generate proposals, classify evidence, draft texture prompts or summarise stage records. Provider selection remains replaceable and repository gates retain validation authority.

## Lanes

- `nvidia_nim`
- `openai`
- `openai_compatible`
- `local`
- `local_flux`
- `hf_flux_schnell`

## Roles

Role defaults in `configs/provider-policy.json` cover planner, vision_reasoner, material_reasoner, texture_prompt_writer, nonvisual_material_reasoner, physics_reasoner, validator_judge, embeddings, image_generation, texture_generator and external_model_runner.

## Logged fields

- provider name
- kind
- model env name
- base URL host
- prompt checksum
- role
- request ID when available

Raw API keys, bearer tokens and signed URLs are not logged.

## Proposal policy

Provider outputs are written as proposal artefacts. Promotion requires deterministic validation or reviewer approval.

## Request flow

1. Resolve provider role from `configs/provider-policy.json`.
2. Build the provider request from a prompt and stage context.
3. Store the redacted request and response metadata.
4. Write provider output as a proposal artefact.
5. Let the stage validator decide whether it can be promoted.

## Commands

```bash
afb provider check --policy configs/provider-policy.json
afb provider prompt --policy configs/provider-policy.json --provider openai --prompt "Summarise the stage contract" --output artifacts/provider-proposal.json
```
