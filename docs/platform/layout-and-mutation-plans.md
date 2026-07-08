# Layout and mutation plans

Batch layout and mutation files are validated before any write. Layout sweeps and controlled mutations start from assets that have passed simready verification. The validating tools are `scene_layout_validate` and `governance_mutation_validate`.

## Explicit permutations

Layout and mutation plans declare asset placements and controlled changes while preserving provenance, unit policy and layer ownership.

## Layout

Each placement names an asset root, target group and transforms or a parametric pattern. Patterns declare unit policy, axis convention, origin, count and spacing. Relative paths resolve from the layout file, project root, then approved library root.

`validate_only=true` checks schema, paths, unit policy, namespace, bounds and placement count. Validation returns all entry errors with stable indices.

## Mutation

Each operation declares target layer, target prim, operation, inputs, expected outputs, gates, rollback note and dry-run support. A plan is valid only when targets exist or are created earlier in the same plan.

## Policy effect

Plans generate domain-randomised environments, layout sweeps and asset state changes through declared operations with gates and rollback notes.

## Commands

```bash
afb layout validate asset-layout-manifest.json --project projects/<slug> --validate-only
afb mutation validate mutation-plan.json --project projects/<slug> --validate-only
```
