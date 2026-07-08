# Texture defaults

Texture defaults convert declared material classes and property ranges into reviewable PBR map plans supporting [texturing](04-texturing.md). They never author numeric physics values.

<p align="center">
  <img src="../assets/texture-physics-consistency-lane.svg" alt="texture physics consistency lane" width="920">
</p>

## Default map constraints

Default textures provide visual variants when no custom texture model is available. Their constraints prevent unsupported physical-property claims. A worn metal map may suggest corrosion or roughness; the physics layer still requires an evidence-backed property record.

## Inputs

- `configs/texture-defaults.json`
- `schemas/texture-default-policy.schema.json`
- `material-inference-manifest.json` (materials and physical property proposals)

## Map policy

- Required maps: `base_color`, `roughness`, `normal` and `ao`.
- Metallic maps are used only for metals and metal-coated surfaces.
- Height or displacement is allowed only when scale and renderer support it.
- Base colour is sRGB. Scalar maps are linear.
- Default resolution is 2K. Smoke runs use 1K. Hero assets require an explicit override.

## Physical consistency

Each texture set records `physical_consistency` with the visible cue, linked evidence and property claim. Contradictions return review states rather than numeric physics.

Blocked examples:

- stainless steel with heavy red rust and no corrosion evidence
- foam appearance with rigid metal mass
- high-gloss rubber with a high-friction claim but no property evidence

## Commands

```bash
afb texture defaults list
afb texture defaults explain --material rubber --profile outdoor_grip
afb texture prompt --material-manifest material-inference-manifest.json --property-manifest material-inference-manifest.json --output texture-prompt.json
afb texture defaults validate --texture-manifest texturing-manifest.json --property-manifest material-inference-manifest.json
```
