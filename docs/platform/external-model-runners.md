# External model runners

External models run from declared manifests and structured configs. Their outputs are proposals until a downstream validator or reviewer promotes them.

## Runner boundary

External runners execute specialised reconstruction and generation systems. Repository state remains under the factory's manifest and validation contracts. Local, remote and adapter-backed runners declare their inputs, outputs, logs, checksums, status and downstream validation.

## How to run a backend safely

Agent skill: `external-model-runner-lead`. The orchestrator usually prepares the runner manifest from the selected reconstruction route, then leaves this skill to enforce the external-runner contract.

Start with a dry run and a provision check to validate the manifest, paths and output contract before a heavy model starts.

```bash
afb reconstruction create-backend --backend triposg
afb external-models run --manifest external-model-run-manifest.json --dry-run
afb reconstruction provision --backend triposg --output artifacts/reconstruction-backends/triposg/provision-report.json
```

Move to a live run only when the input asset, backend root, GPU expectations and output directory are explicit. Keep failure reports because they record the missing runtime condition.

## Config contract

Each model in `configs/external-models.json` declares:

- `model_id`
- `kind`
- `input_schema`
- `output_schema`
- `gpu_requirements`
- `allowed_paths`
- `timeout_seconds` when needed
- `command` as an argument list or a redacted endpoint

Shell-string command construction is not allowed. User paths must resolve under the project, cache or approved library directories.

## Run manifest

`external-model-run-manifest.json` records input and output manifests, schema IDs, GPU requirements, runtime env, redacted command or endpoint, artefacts, log path, status and W&B run ID.

## Commands

```bash
afb external-models list
afb external-models validate --config configs/external-models.json
afb external-models run --manifest external-model-run-manifest.json --dry-run
afb reconstruction backends
afb reconstruction create-backend --backend trellisv2
afb reconstruction provision --backend trellisv2 --output artifacts/reconstruction-backends/trellisv2/provision-report.json
afb reconstruction provision --backend triposg --output artifacts/reconstruction-backends/triposg/provision-report.json
afb reconstruction provision --backend partcrafter --output artifacts/reconstruction-backends/partcrafter/provision-report.json
afb reconstruction install-check --backend trellisv2 --output artifacts/reconstruction-backends/trellisv2/install-check.json
afb reconstruction install --backend hunyuan3d --output artifacts/reconstruction-backends/hunyuan3d/install-report.json
python scripts/reconstruction/provision_reconstruction_backend.py --backend trellisv2 --output artifacts/reconstruction-backends/trellisv2/provision-report.json
python scripts/reconstruction/install_reconstruction_backend.py --backend trellisv2 --mode check --output artifacts/reconstruction-backends/trellisv2/install-check.json
```

## Reconstruction backend adapter spec

The local reconstruction adapter is the extension point for image-to-3D backends such as Trellis v2, Hunyuan3D, TripoSG and PartCrafter. Backend records live in `configs/reconstruction-backends.json`.

Each backend declares:

- `id` and `aliases` for command routing
- `model_ref` for the local model or cache identity
- `accepted_inputs` and `produces`
- `path_env_vars`, `candidate_roots` and `required_files` for provision checks
- `adapter_script` and `native_command` as argument lists
- `gpu_requirements`, `timeout_seconds` and `allowed_paths`

`afb reconstruction create-backend --backend <id>` writes an `external-model-run-manifest.json` with `model_kind` set to `local-reconstruction-adapter`. `afb external-models run --manifest <manifest> --dry-run` dispatches that manifest through `scripts/reconstruction/run_reconstruction_backend.py`, writes a provision report, writes a run log and writes a `reconstruction-manifest.json`.

Dry runs do not start the backend command and do not mutate the run manifest. A non-dry run requires `AFB_RECONSTRUCTION_INPUT_ASSET` or `input_asset` in the manifest. The adapter then expands the backend `native_command`, runs it with `shell=False`, records stdout and stderr in the run log and treats outputs as proposals until the normal validation and review gates promote them.

The Trellis v2 exemplar uses `scripts/reconstruction/backend_adapters/trellis2_image_to_glb.py`. It imports the local Trellis checkout at runtime, uses `Trellis2ImageTo3DPipeline.from_pretrained("microsoft/TRELLIS.2-4B")` and exports `asset.glb` through `o_voxel`.

The Hunyuan3D exemplar uses `scripts/reconstruction/backend_adapters/hunyuan3d_image_to_glb.py`. It follows the public Hunyuan3D two-stage shape and texture flow and exports `asset.glb`.

The TripoSG adapter uses `scripts/reconstruction/backend_adapters/triposg_image_to_glb.py`. It calls the upstream `scripts.inference_triposg` module with an image input and writes `asset.glb`.

The PartCrafter adapter uses `scripts/reconstruction/backend_adapters/partcrafter_image_to_parts.py`. It calls the upstream part-level object generator, copies generated 3D files into the project output directory and writes `parts-manifest.json`. The reconstruction manifest records the primary GLB plus the part manifest as proposal evidence. Downstream USD authoring can use that manifest to create semantic prims, exploded visual sets and per-part material bindings.

## Local installer spec

`afb reconstruction install-check`, `afb reconstruction install` and `scripts/reconstruction/install_reconstruction_backend.py` are the backend installation surfaces. They write a JSON report and a SHA-256 sidecar for both modes:

- `install-check` or `--mode check`: verifies git, pip, Python version, CUDA availability, GPU visibility and install-root writability
- `install` or `--mode install`: clones or reuses the backend checkout, runs the backend install command and then calls the provision check

Install roots resolve environment-first, with a repository-relative default:

- `AFB_BACKEND_INSTALL_ROOT` sets the base for all backend checkouts; the default is `.cache/afb/backends/` under the repository.
- Per-backend environment handles (`AFB_TRELLISV2_ROOT`, `AFB_HUNYUAN3D_ROOT`, `AFB_TRIPOSG_ROOT`, `AFB_PARTCRAFTER_ROOT`, `AFB_DUST3R_ROOT`) point at existing checkouts anywhere on the machine and take precedence over the defaults.

Trellis v2 uses the upstream `setup.sh` path and is treated as a Linux-native install. On Windows the installer reports the host as blocked unless `--force` is supplied. Hunyuan3D uses an editable pip install from the local checkout and reports pip output in the install report.

PartCrafter uses its upstream `settings/setup.sh` path and is treated as Linux-oriented. Run it through WSL, Linux or Spark for native setup. TripoSG uses the upstream `requirements.txt` install path.
