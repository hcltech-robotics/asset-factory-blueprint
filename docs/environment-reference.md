# Environment reference

Environment handles carry machine-specific paths, credentials, trust anchors and deployment overrides. This page lists the handles the blueprint reads, what consumes them, their defaults and when each one is required. Run requests, policy files and CLI arguments remain the durable configuration surfaces.

## Source appliance

| Handle | Consumer | Default | Required when |
| --- | --- | --- | --- |
| `AFB_APPLIANCE_ROOT` | `config.py` and all repository-resource loaders | inferred extracted checkout | a non-editable entry point cannot locate the tagged source appliance containing schemas, policies, skills, examples and scripts |

`afb info` reports whether the source appliance is complete and lists any missing markers. A wheel alone is not the canonical factory distribution.

## Provider credentials and lanes

| Handle | Consumer | Default | Required when |
| --- | --- | --- | --- |
| `NVIDIA_API_KEY` | `providers.py` via `configs/provider-policy.json` | unset | using the `nvidia_nim` lane live |
| `OPENAI_API_KEY` | `providers.py` via provider policy | unset | using the `openai` lane live |
| `AFB_LLM_API_KEY` | `providers.py`, `configs/agent-workflow.json` | unset | using the generic `openai_compatible` lane |
| `AFB_LLM_BASE_URL` | `configs/agent-workflow.json`, provider policy | lane default | pointing the generic lane at a custom endpoint |
| `AFB_LLM_MODEL` | `configs/agent-workflow.json` | lane default | overriding the planner model |
| `AFB_LLM_PROVIDER` | `configs/agent-workflow.json` | `nvidia_nim` | selecting a different default lane |
| `AFB_LOCAL_BASE_URL` | provider policy `local` lane | `http://127.0.0.1:11434/v1` | running against a local OpenAI-compatible server |
| `AFB_LOCAL_API_KEY` | provider policy `local` lane | unset | the local server enforces a key |
| `AFB_LOCAL_MODEL` | provider policy `local` lane | `llama3.2:1b` | overriding the local model |
| `AFB_LOCAL_IMAGE_BASE_URL` | provider policy `local_flux` lane | unset | running a local image generation server |
| `AFB_LOCAL_IMAGE_API_KEY` | provider policy `local_flux` lane | unset | that server enforces a key |
| `AFB_LOCAL_IMAGE_MODEL` | provider policy `local_flux` lane | lane default | overriding the local image model |
| `HF_TOKEN` | provider policy `hf_flux_schnell` lane, gated model downloads | unset | pulling gated Hugging Face weights or spaces |
| `AFB_HF_FLUX_MODEL`, `AFB_HF_FLUX_SPACE` | provider policy `hf_flux_schnell` lane | lane defaults | overriding the hosted FLUX route |
| `AFB_NVIDIA_BASE_URL`, `AFB_NVIDIA_MODEL` | provider policy `nvidia_nim` lane | `https://integrate.api.nvidia.com/v1`, lane default | overriding the NIM endpoint or model |
| `AFB_OPENAI_BASE_URL`, `AFB_OPENAI_MODEL` | provider policy `openai` lane | lane defaults | overriding the OpenAI endpoint or model |
| `AFB_PROVIDER_POLICY_PATH` | `services/live_textures.py` | `configs/provider-policy.json` | loading an alternative policy file |

## Role model overrides

| Handle | Consumer | Default | Required when |
| --- | --- | --- | --- |
| `AFB_PLANNER_MODEL` | `orchestrator.py` | role default from provider policy | overriding the planner role |
| `AFB_VISION_MODEL` | `orchestrator.py`, `services/vlm_review.py` | `configs/vlm-review-policy.json` default | overriding the VLM sign-off reviewer |
| `AFB_TEXTURE_MODEL` | `orchestrator.py`, texture services | role default | overriding texture reasoning |
| `AFB_IMAGE_GENERATION_MODEL` | provider policy image lanes | lane default | overriding texture image generation |
| `AFB_PHYSICS_MODEL` | `orchestrator.py` | role default | overriding physics reasoning |
| `AFB_NONVISUAL_MODEL` | `orchestrator.py` | role default | overriding nonvisual material reasoning |
| `AFB_VALIDATOR_MODEL` | `orchestrator.py` | role default | overriding validation reasoning |

## Library backings

| Handle | Consumer | Default | Required when |
| --- | --- | --- | --- |
| `AFB_MATERIALS_ROOT` | `configs/library-registry.json` | unset | indexing an operator materials folder |
| `AFB_TEXTURES_ROOT` | library registry | unset | indexing an operator textures folder |
| `AFB_USD_ASSETS_ROOT` | library registry | unset | indexing an operator USD asset folder |
| `AFB_OMNIVERSE_CONTENT_ROOT` | library registry | unset | indexing a local Omniverse content mirror |
| `AFB_VMATERIALS_ROOT` | library registry | unset | indexing a local vMaterials install |
| `AFB_USD_SEARCH_URL` | `services/library.py`, capability registry | unset | querying a USD Search endpoint |
| `AFB_USD_SEARCH_API_KEY` | `services/library.py` | unset | the endpoint requires auth |
| `AFB_USD_SEARCH_AUTH_MODE` | `services/library.py` | `bearer` | the endpoint expects `api-key` header auth |
| `AFB_USD_SEARCH_METHOD` | `services/library.py` | POST with GET fallback | forcing one HTTP method |

## Reconstruction backends

| Handle | Consumer | Default | Required when |
| --- | --- | --- | --- |
| `AFB_BACKEND_INSTALL_ROOT` | `reconstruction_installers.py` | `.cache/afb/backends/` under the repository | relocating backend checkouts |
| `AFB_TRELLISV2_ROOT`, `AFB_HUNYUAN3D_ROOT`, `AFB_TRIPOSG_ROOT`, `AFB_PARTCRAFTER_ROOT`, `AFB_DUST3R_ROOT` | `configs/reconstruction-backends.json` | install root default | pointing at an existing checkout elsewhere |
| `AFB_<BACKEND>_COMMIT` (for example `AFB_TRELLISV2_COMMIT`) | `reconstruction_installers.py` | registry `pinned_commit` | pinning a backend to a specific upstream commit |
| `AFB_RECONSTRUCTION_BACKEND` | `reconstruction_backends.py` | manifest value | overriding the backend for an adapter run |
| `AFB_RECONSTRUCTION_BACKEND_ROOT` | `reconstruction_backends.py` | resolved install root | overriding the checkout used by an adapter run |
| `AFB_RECONSTRUCTION_REGISTRY` | `reconstruction_backends.py` | `configs/reconstruction-backends.json` | loading an alternative registry |
| `AFB_RECONSTRUCTION_PYTHON` | `reconstruction_backends.py` | the interpreter recorded at install time, else the invoking interpreter | overriding the auto-discovered backend interpreter |
| `AFB_RECONSTRUCTION_INPUT_ASSET` | `reconstruction_backends.py` | manifest value | supplying the single input image out of band |
| `AFB_RECONSTRUCTION_INPUT_ASSETS` | `reconstruction_backends.py` | manifest value | supplying multi-view or video inputs, `os.pathsep` separated |
| `AFB_HF_CACHE` | `services/segmentation.py` | `.cache/afb/hf` under the repository | relocating the Hugging Face cache; also honours `HF_HOME` |
| `AFB_SAM3_CHECKPOINT` | capability registry | unset | using the gated SAM segmentation option |

## Runtime targets and infrastructure

| Handle | Consumer | Default | Required when |
| --- | --- | --- | --- |
| `AFB_ISAAC_SIM_ROOT` | `cli.py`, capability registry | runtime config value | running the Isaac Sim load gate |
| `AFB_ISAAC_LAB_ROOT` | `cli.py` | runtime config value | RL environment work against Isaac Lab |
| `AFB_ISAAC_ATTESTATION_SECRET` | Isaac runtime producer, importer, SimReady consumer and positive-capsule validator | unset | producing or consuming Isaac runtime evidence; use the same independent managed secret of at least 32 UTF-8 bytes on both machines |
| `AFB_ISAAC_PRODUCER_SHA256` | Isaac runtime producer, importer, canonical SimReady consumer and positive-capsule validator | unset | producing or consuming Isaac runtime evidence; set to the administrator-approved lowercase 64-hex SHA-256 of `scripts/simready/isaac_load_check.py` |
| `AFB_ASSET_VALIDATOR_EXECUTABLE` | `services/official_validator.py` | unset | running official NVIDIA Profile validation; set to the trusted native validator executable |
| `AFB_ASSET_VALIDATOR_EXECUTABLE_SHA256` | official validator bridge, consumer and positive-capsule validator | unset | pinning the administrator-approved validator executable; required for a passing official result |
| `AFB_VALIDATION_ATTESTATION_SECRET` | official validator producer, consumer and positive-capsule validator | unset | HMAC-attesting official validator evidence; must be an independent managed secret of at least 32 UTF-8 bytes |
| `AFB_PHYSICS_EVIDENCE_SECRET` | physics evidence sealer, authoring, SimReady verification and positive-capsule validator | unset | HMAC-attesting accepted mass-property evidence; must be an independent managed secret of at least 32 UTF-8 bytes |
| `AFB_ASSET_VALIDATOR_TIMEOUT_SECONDS` | official validator bridge | `600` | changing the hard validator runtime limit |
| `AFB_ASSET_VALIDATOR_MAX_OUTPUT_BYTES` | official validator bridge | `1048576` | changing the combined stdout and stderr cap |
| `AFB_ASSET_VALIDATOR_MAX_REPORT_BYTES` | official validator bridge | `16777216` | changing the vendor JSON evidence cap |
| `AFB_ENV` | `cli.py`, deploy manifests | `local` | labelling the execution environment |
| `AFB_TRUSTED_TOOL_SERVER_NETWORK` | `cli.py` tool server | disabled | binding the HTTP tool server beyond loopback |
| `AFB_TOOL_SERVER_TOKEN` | HTTP tool service | unset | requiring bearer authentication; mandatory for non-loopback binding |
| `AFB_TOOL_SERVER_APPROVAL_SECRET` | HTTP and stdio tool service | unset | enabling reviewed mutations; mandatory for non-loopback binding and at least 32 bytes |
| `AFB_TOOL_SERVER_ALLOWED_TOOLS` | HTTP and stdio tool service | all catalogue tools | restricting the exposed catalogue to a comma-separated allowlist |
| `AFB_TOOL_SERVER_JOB_STORE` | HTTP and stdio tool service | unset | preserving HTTP job records and consumed approval tokens across process restarts |
| `AFB_STORAGE_ROOT`, `AFB_ARTIFACT_ROOT`, `AFB_CACHE_ROOT` | `deploy/.env.example` consumers | repository-relative | relocating work roots in deployments |
| `AFB_S3_BUCKET`, `AFB_S3_REGION`, `AFB_S3_PROFILE` | deploy env | unset | syncing artefacts to object storage |
| `AFB_OMNIVERSE_URL`, `AFB_OMNIVERSE_USER` | deploy env | unset | publishing to a Nucleus server |
| `AFB_RENDER_ENDPOINT`, `AFB_OPTIMIZER_ENDPOINT` | deploy env | unset | wiring external render or optimisation services |
| `AFB_DOCKER_GPU_REQUIRED` | deploy env | `false` | compose lanes must schedule a GPU |
| `AFB_EXTERNAL_MODELS_CONFIG` | deploy env | `configs/external-models.json` | loading an alternative external model config |
| `AFB_EXTERNAL_MODEL_CACHE` | deploy env | repository-relative | relocating external model caches |
| `AFB_EXTERNAL_MODEL_ALLOW_NETWORK` | deploy env | disabled | permitting network access for external model runs |
| `AFB_REQUIRE_REVIEW_FOR_UNCERTAIN_PROPERTIES` | deploy env | enabled | never relax this in production |

## Security, confinement and resource limits

| Handle | Consumer | Default | Required when |
| --- | --- | --- | --- |
| `AFB_ALLOWED_PROVIDER_HOSTS` | `security.py` | provider-policy allowlist | adding administrator-approved provider hosts; comma separated, with `*.example.com` syntax for subdomains |
| `AFB_ALLOWED_MEDIA_HOSTS` | `security.py` | built-in public-media allowlist | adding administrator-approved media hosts; comma separated |
| `AFB_EXTERNAL_ALLOWED_ROOTS` | `security.py` external runners | `projects`, `artifacts`, `.cache/afb` | extending external-runner file access; `os.pathsep` separated |
| `AFB_EXTERNAL_REGISTRY_ROOTS` | `security.py` external runners | `configs` | loading an external-model registry outside the appliance; `os.pathsep` separated |
| `AFB_SERVICE_WORKSPACE_ROOTS` | service request confinement | `projects`, `artifacts`, `.cache/afb` | extending writable service roots; `os.pathsep` separated |
| `AFB_SERVICE_SOURCE_ROOTS` | service request confinement | `projects`, `artifacts`, `examples` | extending readable service source roots; `os.pathsep` separated |
| `AFB_MAX_SOURCE_FILES` | `workflow.py` source localisation | `1000` in service requests, `100000` locally | lowering the maximum number of localised source files |
| `AFB_MAX_SOURCE_BYTES` | `workflow.py` source localisation | 2 GiB in service requests, 20 GiB locally | lowering the aggregate localised-source byte limit |
| `AFB_AVAILABLE_CPUS`, `AFB_AVAILABLE_GPUS` | `execution.py` stage preflight | unknown | enforcing declared CPU or GPU resource minima before a stage starts |

Host and root extensions are administrator policy, not end-user request parameters. Keep them as narrow as the deployment permits.

## Provenance overrides

| Handle | Recorded field | Default |
| --- | --- | --- |
| `AFB_GPU_VENDOR`, `AFB_GPU_MODEL`, `AFB_GPU_COUNT`, `AFB_MEMORY_BYTES` | hardware BOM | `not_recorded` |
| `CUDA_VERSION`, `NVIDIA_DRIVER_VERSION`, `AFB_CUDA_COMPUTE_CAPABILITY` | accelerator BOM | environment value or `not_recorded` |
| `AFB_CONTAINER_IMAGE`, `AFB_CONTAINER_DIGEST`, `AFB_CONTAINER_RUNTIME` | container BOM | `not_recorded` |
| `AFB_OPENUSD_VERSION`, `AFB_ISAAC_SIM_VERSION`, `AFB_MATERIALX_VERSION` | simulation BOM | detected package version where available |
| `AFB_RENDERER`, `AFB_PHYSICS_BACKEND` | simulation BOM | `not_recorded` |

These handles describe the environment used for a run. Set them from trusted runtime inventory rather than user-authored asset metadata.

## Texturing

| Handle | Consumer | Default | Required when |
| --- | --- | --- | --- |
| `AFB_TEXTURE_PROVIDER` | `services/live_textures.py` | policy route | forcing a specific texture lane |
| `AFB_TEXTURE_QUALITY` | `services/live_textures.py` | lane default | trading quality against cost |
| `AFB_TEXTURE_SIZE` | `services/live_textures.py` | lane default | overriding generated texture resolution |

## Telemetry and documentation

| Handle | Consumer | Default | Required when |
| --- | --- | --- | --- |
| `AFB_WANDB_ENABLED` | `orchestrator.py`, `wandb_logging.py` | disabled | mirroring run telemetry to Weights and Biases |
| `AFB_WANDB_PROJECT`, `AFB_WANDB_ENTITY` | `wandb_logging.py` | unset | targeting a specific W&B project or entity |
| `WANDB_API_KEY` | `wandb_logging.py` | unset | `AFB_WANDB_ENABLED` is on |
| `AFB_CHROMIUM` | `scripts/generate_diagrams.py` | Playwright Chromium | rasterising figures with a specific browser binary |

## Verification sibling

| Handle | Consumer | Default | Required when |
| --- | --- | --- | --- |
| `AFB_REPO_ROOT` | asset-factory-verification `conftest.py` and `repo_checks/` | unset | running the verification suite against a blueprint checkout |

## Conventions

- Handles are read at call time, so exporting them in the shell that runs `afb` is sufficient.
- Reports and manifests record handle names and redacted metadata, never raw secret values.
- Repository-relative defaults keep contract-only and dry-run paths usable in a fresh checkout. Live providers, trusted evidence producers and non-loopback services require their documented credentials, pins or secrets.
