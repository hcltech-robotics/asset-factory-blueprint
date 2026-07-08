# Geometry tools

`asset_image_segmentation_prior` splits a source image into appearance or SAM-derived region masks before reconstruction. It writes semantic masks, an overlay, a PartCrafter conditioning image and a manifest with the suggested part count.

Use this before PartCrafter when the image has material or appearance regions that should influence the generated part structure. Because PartCrafter does not expose a native mask input, the prior biases PartCrafter through the conditioning image and `num_parts`, then downstream validation must score generated parts against the masks.

`asset_mesh_condition` heals reconstructed mesh files and applies deterministic shape operations only to meshes selected by material, segment or prim metadata.

Use it after reconstruction and material or appearance assignment. Healing can run across all listed meshes. Dents, bumps and smoothing must carry an explicit selector such as `material_family`, `material_name`, `segment_id` or `prim_path` so geometry edits remain material-aware.

The tool writes a report, manifest, checksums and conditioned mesh outputs. Treat results as proposal geometry until downstream visual and USD validation passes.
