# Third party notices

This blueprint builds on, references and acknowledges the following work. Nothing listed here is redistributed by this repository unless stated; entries are grounding references, integration points or bundled samples with their licences recorded.

## Acknowledged foundations

- **[OpenUSD and the Alliance for OpenUSD](https://openusd.org/)**. The asset model, layer composition and schema conventions are built on OpenUSD, originally developed by Pixar Animation Studios and stewarded by the Alliance for OpenUSD. OpenUSD uses the [Tomorrow Open Source Technology License 1.0](https://github.com/PixarAnimationStudios/OpenUSD/blob/release/LICENSE.txt). The knowledge corpus summarises OpenUSD concepts for agents; the specification and its evolution belong to the AOUSD community.
- **[NVIDIA content-agents](https://github.com/NVIDIA-Omniverse/content-agents)**. The texture and material workflows follow material-first and agent-driven content-service patterns established by that project. Its code and dependencies retain their upstream licences.
- **[NVIDIA Omniverse, Isaac Sim and SimReady](https://docs.omniverse.nvidia.com/simready/latest/)**. Isaac Sim is a runtime target for verification and SimReady Profiles inform the promotion model. These products and specifications are NVIDIA's and are used through their public documentation or separately installed software under NVIDIA terms.

## Referenced models and services

| Dependency | Role here | Licence and terms |
| --- | --- | --- |
| [TRELLIS.2](https://github.com/microsoft/TRELLIS.2) | image-to-3D reconstruction backend | MIT for the published code and model; separately licensed dependencies remain upstream obligations |
| [Hunyuan3D 2 and 2mv](https://github.com/Tencent-Hunyuan/Hunyuan3D-2/blob/main/LICENSE) | image and multi-view reconstruction backends | Tencent Hunyuan 3D 2.0 Community License, including territorial, acceptable-use, distribution and scale restrictions; not an OSI licence |
| [TripoSG](https://github.com/VAST-AI-Research/TripoSG) | image-to-3D reconstruction backend | MIT; downloaded dependencies and auxiliary weights retain separate terms |
| [PartCrafter](https://github.com/wgsxm/PartCrafter) | part-aware reconstruction backend | MIT; downloaded dependencies, datasets and auxiliary weights retain separate terms |
| [DUSt3R](https://github.com/naver/dust3r/blob/main/LICENSE) | multi-view geometry backend | CC BY-NC-SA 4.0; non-commercial and share-alike restrictions apply |
| Segment Anything family | segmentation priors | upstream model licences; checkpoints gated |
| [NVIDIA NIM endpoints](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-api-trial-terms-of-service/) | LLM and VLM review lanes | NVIDIA API terms selected by the operator |
| [NVIDIA vMaterials 2](https://developer.nvidia.com/vmaterials) | MDL material catalogue referenced by the exemplar index | NVIDIA vMaterials EULA; obtained separately |
| [USD Search API](https://docs.omniverse.nvidia.com/) | estate search integration point | NVIDIA product terms; deployed by the operator |

Backends are cloned and installed by the operator through the install surfaces; their code and weights never ship in this repository.

## Bundled and downloadable content

| Content | Where | Licence |
| --- | --- | --- |
| Sample jerrycan photo (render of the Poly Haven `metal_jerrycan` model) | `examples/sources/photos/`, `docs/assets/walkthrough-input.png` | CC0 |
| Poly Haven textures and models | fetched on demand into `library/downloads/` | CC0 |
| ambientCG PBR sets | fetched on demand into `library/downloads/` | CC0 |
| Google Scanned Objects, Smithsonian open access, Objaverse-XL | linked in `library/asset-packs.json`, never bundled | CC-BY 4.0, CC0 and mixed licences respectively; rights checks required per item |

## Python dependencies

| Direct dependency | Role | Upstream licence |
| --- | --- | --- |
| [`jsonschema`](https://github.com/python-jsonschema/jsonschema) | runtime JSON Schema validation | MIT |
| [`pydantic`](https://github.com/pydantic/pydantic) | runtime typed contracts | MIT |
| [`Pillow`](https://github.com/python-pillow/Pillow/blob/main/LICENSE) | image inspection and report generation | MIT-CMU |
| [`NumPy`](https://github.com/numpy/numpy/blob/main/LICENSE.txt) | numerical runtime | BSD-3-Clause, with bundled-component notices upstream |
| [`trimesh`](https://github.com/mikedh/trimesh/blob/main/LICENSE.md) | mesh inspection and conversion | MIT |
| [`Matplotlib`](https://github.com/matplotlib/matplotlib/blob/main/LICENSE) | report visualisation | PSF-based Matplotlib licence, with bundled-data notices upstream |
| [`MkDocs`](https://github.com/mkdocs/mkdocs/blob/master/LICENSE) | optional documentation build | BSD-2-Clause |
| [`pytest`](https://github.com/pytest-dev/pytest/blob/main/LICENSE) | optional development checks | MIT |
| [`Ruff`](https://github.com/astral-sh/ruff/blob/main/LICENSE) | optional linting | MIT |

Resolved environments also contain transitive dependencies. A release software bill of materials is the authoritative inventory for a particular artefact; this file is the maintained human-readable notice for direct dependencies and integration points.
