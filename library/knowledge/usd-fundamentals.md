# USD fundamentals

How OpenUSD works, for agents working in this factory. Never invent prims, schema names or attribute names: every prim you author must follow these rules and every reference you cite must exist in an index or a stage you inspected.

## Stages, layers and prims

- A stage is the composed view of one or more layers. Layers are files (`.usda` text, `.usdc` binary, `.usdz` package) holding prim specs and opinions.
- Prims are the nodes of the scene graph, addressed by paths like `/World/Asset/Geometry`. Prims have a type (Xform, Mesh, Scope, Material, Shader), properties (attributes and relationships) and metadata.
- Attributes carry typed values, optionally time-sampled. Relationships point at other prims or properties, like `material:binding`.

## Composition arcs

Layers combine through composition arcs, strongest first under LIVRPS ordering: local opinions, inherits, variant sets, references, payloads, specializes. Practical rules:

- Sublayers stack layers inside one stage; the factory's asset layer stack (geo, mtl, phy, art, sem, deform, variants) composes this way under the asset root layer.
- References graft another asset's prim tree under a prim; use them for assembly, never copy geometry between files.
- Payloads are deferrable references for heavy geometry.
- Variant sets hold switchable alternatives (material variants, damage states); exactly one variant per set is active at composition time.
- An opinion in a stronger layer wins without deleting the weaker opinion; deactivation (`active = false`) hides a prim without removing its spec.

## Stage metadata that must always be correct

- `defaultPrim` names the prim a reference targets by default; every publishable layer sets it.
- `metersPerUnit` and `upAxis` declare the unit and orientation contract; this factory uses metersPerUnit 1 and Y up unless the source declares otherwise and the manifest records it.
- Kind metadata (`component`, `assembly`, `subcomponent`) marks model boundaries for selection and instancing.

## Schemas

- Typed (IsA) schemas define what a prim is: `UsdGeomMesh`, `UsdGeomXform`, `UsdShadeMaterial`, `UsdLuxRectLight`.
- API schemas attach capabilities to an existing prim: `UsdPhysicsRigidBodyAPI`, `UsdPhysicsCollisionAPI`, `UsdShadeMaterialBindingAPI`, `SemanticsAPI`. API schemas are applied, and the `apiSchemas` metadata lists them.
- Property names are namespaced: `physics:mass`, `material:binding`, `semantics:Semantics:params:semantic_type`. Do not invent namespaces.

## Geometry conventions

- Meshes carry `points`, `faceVertexCounts`, `faceVertexIndices`; normals and UVs are primvars (`primvars:st` for the default UV set, with interpolation vertex or faceVarying).
- Transform stacks are ordered `xformOp` attributes declared in `xformOpOrder`; a published asset root carries an identity transform.
- Purpose (`default`, `render`, `proxy`, `guide`) separates display representations; colliders often live under guide or proxy purpose prims.

## Grounding rule

When authoring, copy exact prim paths, schema names and attribute names from an inspected stage, a schema definition or a library index entry. If a needed name cannot be grounded, stop and record the gap as review required instead of guessing.
