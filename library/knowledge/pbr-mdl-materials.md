# PBR, MDL and material authoring

How surface appearance is described here, and how to keep generated textures physically honest.

## PBR metallic-roughness model

- Base colour: albedo for dielectrics, reflectance tint for metals; sRGB encoded; must not contain baked lighting, shadows or ambient occlusion.
- Metallic: binary in most real materials (0 for dielectrics, 1 for metals); intermediate values only at coated or corroded transitions. Linear encoded.
- Roughness: microfacet roughness; linear encoded. Polished metal sits low, rubber and concrete sit high.
- Normal: tangent-space normal map, linear; this factory follows OpenGL-style green channel unless the source pack declares otherwise, and the import step records the convention.
- Ambient occlusion: a separate linear map; never premultiplied into base colour.
- Height or displacement: only where the renderer and scale policy support it.

The factory's map policy: base_color, roughness, normal and ao are required; metallic only for metals and metal-coated surfaces. sRGB for base colour, linear for scalar maps.

## MDL and Omniverse materials

- MDL is NVIDIA's material definition language; Omniverse renders MDL natively and ships base materials (OmniPBR for opaque PBR surfaces, OmniGlass for transmission) plus the measured vMaterials catalogue.
- `UsdShadeMaterial` prims hold the shader network; an MDL shader prim carries `info:mdl:sourceAsset` and `info:mdl:sourceAsset:subIdentifier`. A portable stack also carries a `UsdPreviewSurface` network so packages remain readable outside Omniverse.
- Texture maps connect through `UsdShadeShader` texture readers with `inputs:file` asset paths; those paths must resolve inside the package.

## Working with the library

- Material candidates come from the constrained material library and the exemplar index; each exemplar entry maps a catalogue material (vMaterials or Omniverse base materials) to a material class and default PBR ranges.
- Generated maps are checked against the exemplar's expected ranges: a stainless steel proposal with roughness 0.95 or a rubber proposal with metallic 1.0 is a consistency defect.
- Texture packs from indexed sources (ambientCG, Poly Haven) are public domain sets whose channel naming the import step normalises to the factory policy; imported sets keep their pack id and licence in the manifest.
- The NVIDIA content-agents texture workflows follow the same pattern this factory uses: material intent comes first, maps are generated against it and consistency is checked before binding.

## Grounding rule

Never invent a material name, MDL module path or texture channel. Bind to materials that exist in `mtl.usda`, reference exemplar ids from the library and record pack ids and licences for every imported map.
