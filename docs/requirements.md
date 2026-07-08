# Requirements

Requirements range from contract-only operation to full generative runs.

## Software

| Layer | Requirement |
| --- | --- |
| Core runtime | Python 3.11 through 3.13 with the exact resolution recorded in `uv.lock` |
| Documentation | `mkdocs` from the `docs` extra |
| USD and material verification | `usd-core` and `MaterialX` from the `validation` extra |
| Mesh conditioning | `trimesh` plus `networkx`, `scipy` and `fast-simplification` from the `mesh` extra |
| Segmentation priors | `numpy` and `opencv-python-headless` from the `vision` extra; `torch` plus checkpoints for the separately gated SAM option |
| Isaac load gate | a local Isaac Sim installation, pointed at by `AFB_ISAAC_SIM_ROOT` |
| Backend installs | git, pip and for Linux-oriented backends a WSL or Linux host |

`uv sync --frozen --all-extras` installs the locked validation, mesh, vision, documentation and development toolchain. The dry-run pipeline, the agent loop in dry mode, the library and all contract checks run on CPU only. `afb capabilities` reports the options this machine can serve and plans installs for the gaps.

## GPU expectations per reconstruction backend

| Backend | Model reference | Minimum VRAM |
| --- | --- | --- |
| trellisv2 | microsoft/TRELLIS.2-4B | 24 GB |
| hunyuan3d | tencent/Hunyuan3D-2 | 16 GB |
| hunyuan3d-mv | tencent/Hunyuan3D-2mv | 16 GB |
| dust3r and video-multiview | naver/DUSt3R ViT-L 512 dpt | 12 GB |
| triposg | VAST-AI/TripoSG | 8 GB |
| partcrafter | wgsxm/PartCrafter | 8 GB |

Values mirror `configs/reconstruction-backends.json`, which is the machine-readable source of truth.

## Model dependency table

| Role | Lane | Default model | Configured by |
| --- | --- | --- | --- |
| Planner and stage reasoning | nvidia_nim | nvidia/llama-3.1-nemotron-nano-8b-v1 | `AFB_LLM_MODEL`, `NVIDIA_API_KEY` |
| VLM sign-off reviewer | nvidia_nim | nvidia/llama-3.1-nemotron-nano-vl-8b-v1 | `AFB_VISION_MODEL` |
| Vision reasoning | openai | gpt-5.5 | `AFB_VISION_MODEL`, `OPENAI_API_KEY` |
| Texture image generation | openai, local_flux or hf_flux_schnell | FLUX.1 family on the local and hosted lanes | `AFB_TEXTURE_MODEL`, `AFB_IMAGE_GENERATION_MODEL` |
| Local OpenAI-compatible lane | local, openai_compatible | llama3.2:1b | `AFB_LOCAL_MODEL`, `AFB_LOCAL_BASE_URL` |

Provider lanes, key envs and defaults live in `configs/provider-policy.json`; the reviewer defaults live in `configs/vlm-review-policy.json`. Model weights for reconstruction backends carry their own licences, catalogued in `THIRD_PARTY_NOTICES.md` at the repository root.

## Disk and network

- Backend checkouts and weights land under `.cache/afb/backends/` (override with `AFB_BACKEND_INSTALL_ROOT`); budget 20 to 60 GB per generative backend with weights.
- Library downloads land under `library/downloads/`, https only, capped at 512 MB per file.
- Project workspaces grow with render and texture evidence; keep `projects/` on fast local storage.
