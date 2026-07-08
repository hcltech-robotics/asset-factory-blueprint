# Asset creation in this factory

How an asset is structured and authored here, and what every stage may touch.

## Layer stack

Each asset composes a fixed layer family under `assets/<asset_id>/`:

- `<asset_id>.usda`: the asset root layer; sets defaultPrim, unit and axis metadata and sublayers the rest.
- `geo.usda`: geometry only; owned by reconstruction and conditioned by segmentation-driven mesh operations.
- `mtl.usda`: material definitions, shader networks and bindings; owned by material inference and texturing.
- `phy.usda`: physics opinions (rigid bodies, colliders, mass, physics materials); owned by physics-articulation.
- `art.usda`: joints, drives, limits and articulation roots; owned by physics-articulation.
- `sem.usda`: semantic labels, affordances and task metadata; fed by segmentation.
- `deform.usda`: dent, bump and displacement request opinions for geometry variants.
- `variants.usda`: variant sets for material, texture, physics and damage permutations.
- `contents.usda`: assembly references when the asset aggregates parts.

Layer authority is enforced: a tool writes only to its owned layer family. Cross-layer edits are a defect.

## Authoring rules

- Source assets are immutable; all authored layers live in the project workspace beside their manifests.
- Every generated file appears in a manifest with a checksum; unmanaged files are treated as absent.
- Prim naming follows the source hierarchy where one exists; generated part prims use the segmentation labels (`/Asset/Geometry/body`, `/Asset/Geometry/handle`), never invented names.
- Bindings use `UsdShadeMaterialBindingAPI` with materials defined in `mtl.usda`; a binding to a material that does not exist in the stack is a blocking defect.
- Variants change opinions inside declared bounds; a variant must never change the asset's lineage, unit policy or semantic identity.

## Packaging

Packaging composes the layer stack, resolves every reference, confirms unit and axis metadata and writes the package under `packaged/<asset_id>/`. A package that references files outside the project or approved library roots fails the self-contained check.

## Where to find exemplars

Before authoring an unfamiliar object class, search the library for exemplars: curated material entries, indexed local assets, the configured USD Search estate and the curated asset packs. Ground proportions, part structure and naming on what the exemplars show, and cite the exemplar item ids in the stage manifest evidence.
