# Quickstart

## 1. Clone and install

```bash
git clone git@github.com:hcltech-robotics/asset-factory-blueprint.git
cd asset-factory-blueprint
uv sync --frozen --all-extras
uv run afb --help
uv run afb info
uv run afb capabilities
```

`afb capabilities` reports the active reconstruction, segmentation, texturing and review options, plus licence and token gates. Add `--install` to plan missing components.

## 2. Configure provider credentials

Export keys in the shell that runs `afb`. The runtime reads them through environment handles and does not write them to files or manifests.

```bash
export NVIDIA_API_KEY=<your key>        # VLM sign-off reviewer and reasoning lanes (NVIDIA NIM)
export OPENAI_API_KEY=<your key>        # optional: image generation lane for texturing
```

These keys cover the default authoring lanes. Release evidence producers use separate trust pins and secrets in step 6. The reviewer model defaults to `nvidia/llama-3.1-nemotron-nano-vl-8b-v1`; override with `AFB_VISION_MODEL` if needed. Other lanes, including local OpenAI-compatible servers and hosted FLUX, are configured in `configs/provider-policy.json` and the [environment reference](environment-reference.md).

## 3. Describe the job to an agent

Put source files under `artifacts/sources/`, or configure `AFB_SERVICE_SOURCE_ROOTS` for another authorised location. Give your agent the [asset programme strategist](https://github.com/hcltech-robotics/asset-factory-blueprint/blob/main/skills/asset-programme-strategist/SKILL.md), then describe the source and intended result in ordinary language:

> I have a photo at `artifacts/sources/jerrycan.png`. I need a textured SimReady prop for an Isaac Sim manipulation task. Ask for anything required before you start and do not choose a Profile for me.

The skill turns the conversation into a run-request draft and calls `asset_programme_intake`. That approval-free tool validates the draft, shows the routed stages and returns a finite list of questions for any start-blocking input. Once the request is complete, the reviewed `asset_factory_start` tool persists `run-request.json`, creates the project and enters the [whole-run agent loop](platform/agentic-operation.md).

A coding agent with shell access writes its draft under `artifacts/run-requests/` and uses the equivalent CLI boundary:

```bash
afb agent intake --draft artifacts/run-requests/<asset-id>.json
afb agent start --request artifacts/run-requests/<asset-id>.json --project-root projects
```

The first command writes nothing. Exit code 2 means its JSON result contains questions that must be answered before start-up. Run the second command only when intake reports `ready: true`; it repeats intake validation and persists the confirmed request as `projects/<asset-id>/run-request.json` before running the loop.

SimReady, OpenUSD-package and RL requests require both an exact `constraints.simready_profile.profile_id` and pinned `profile_version`. The agent must ask and stop if either is absent. Obtain the exact pair from the programme's approved requirements and validator configuration; the factory does not choose one. If no authority has selected a Profile, keep the request blocked or narrow it to texture or physics work. Texture-only and physics-only requests do not require a Profile. Missing source-rights or signed physics evidence remains explicit pending evidence, so a proposal run can begin while promotion stays blocked.

Photos route through reconstruction, while meshes enter conditioning and need trustworthy units. Multiple photos or a video use the multi-view backends. Native STEP, IGES, DWG and DXF conversion is not implemented, so intake asks for a supported USD or mesh export before start-up. A release scope that depends on physical behaviour also needs measured, manufacturer-specified or measured-density evidence sealed with `afb physics-evidence seal` and placed under `constraints.physics_evidence`; the [physics and articulation page](pipeline/05-physics-articulation.md) defines that record.

## 4. Run an existing request

```bash
afb agent run --request examples/run-requests/jerrycan_from_photo.json --project-root projects --live
afb progress --project projects/metal_jerrycan
```

Use this direct [agentic entry point](platform/agentic-operation.md) when a validated run-request JSON already exists. `asset_factory_start` invokes the same loop automatically after guided intake. The loop runs every routed stage, asks the VLM reviewer to sign off against its rubric, applies bounded fixes when a stage does not pass review and writes the project record under `projects/<id>/`: stage manifests, review records, `progress.json` and the operator contact sheet. Without `--live` the same command is a dry run that writes the workspace and skips provider calls.

The bundled jerrycan request deliberately omits its Profile, so direct CLI execution demonstrates fail-closed planning rather than a release-ready configuration. Guided intake stops earlier and asks for the exact Profile before starting. Copy the request and pin the exact Profile before running the release gates below.

On a machine with no reconstruction backend, the loop records the stages it can judge and the prerequisites still missing; see the [observed runthrough](runthrough.md).

## 5. Install a reconstruction backend

Each backend uses a separate Python environment, isolating its CUDA dependencies from the blueprint install. One-time setup on a GPU machine:

```bash
python3.11 -m venv .cache/afb/venvs/backend
.cache/afb/venvs/backend/Scripts/pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
.cache/afb/venvs/backend/Scripts/pip install -e .
.cache/afb/venvs/backend/Scripts/afb reconstruction install --backend hunyuan3d --output artifacts/install-report.json
```

The installer clones the backend at a pinned commit and records which interpreter provisioned it, so later runs resolve the right environment automatically. Then, per asset:

```bash
afb reconstruction create-backend --backend hunyuan3d --input-manifest projects/<id>/manifests/source-asset-manifest.json --asset-id <id> --project-id <id> --output artifacts/<id>-run.json
afb external-models run --manifest artifacts/<id>-run.json
```

The run reads its inputs from the project's source manifest, generates the mesh on the GPU and writes the mesh, preview renders and run manifest into the workspace. Rerun `afb agent run ... --live` to review the generated geometry, record its run lineage and reduce the release blocker to an operator acceptance.

## 6. Gates and release

Configure three independent managed secrets: `AFB_PHYSICS_EVIDENCE_SECRET`, `AFB_VALIDATION_ATTESTATION_SECRET` and `AFB_ISAAC_ATTESTATION_SECRET`. Each must contain at least 32 UTF-8 bytes and none may reuse a provider key, bearer token or another attestation secret. Set `AFB_ASSET_VALIDATOR_EXECUTABLE` to the trusted native NVIDIA validator, `AFB_ASSET_VALIDATOR_EXECUTABLE_SHA256` to its administrator-approved lowercase 64-hex digest and `AFB_ISAAC_PRODUCER_SHA256` to the administrator-approved lowercase 64-hex digest of `scripts/simready/isaac_load_check.py`. The producer and every later consumer of each report need the same relevant secret and digest pin.

```bash
<isaac-sim>/python.bat scripts/simready/isaac_load_check.py \
  --usd projects/<id>/packaged/<id>/<id>.usda \
  --profile-id <profile-id> \
  --profile-version <profile-version> \
  --output projects/<id>/reports/isaac-load-check.json
afb isaac-load apply --project projects/<id> --report projects/<id>/reports/isaac-load-check.json
afb simready validate-profile \
  --usd projects/<id>/packaged/<id>/<id>.usda \
  --profile-id <profile-id> \
  --profile-version <profile-version> \
  --raw-output projects/<id>/reports/simready-profile-validation.raw.json \
  --output projects/<id>/reports/simready-profile-validation.json
afb fitness template --project projects/<id> --output projects/<id>/reports/task-fitness-template.json
# Execute the approved protocol and materialise its measurements, then apply the completed report.
afb fitness apply --project projects/<id> --report <completed-task-fitness-report.json>
afb project validate --project projects/<id>
afb readiness --project projects/<id> --output artifacts/<id>-readiness.md
```

Replace `<profile-id>` and `<profile-version>` with the exact `simready_profile.profile_id` and `simready_profile.profile_version` values in the asset manifest. The runtime report must not infer or substitute a Profile version. Its fixed protocol identity, pinned producer digest, portable USD label, package inventory and schema-versioned, protocol-domain-separated HMAC attestation are verified before import and independently rechecked by the canonical consumer.

Release requires a blocker-free exact-Profile result from the configured NVIDIA validator, the Isaac load and behavioural report, a task-protocol fitness report with materialised evidence, a complete package and record graph, current rights and retention and a content-bound operator decision. Review cannot waive a failed technical gate or substitute for missing physical evidence. The [SimReady verification page](pipeline/07-simready-verification.md) defines the evidence contracts and the [governance page](platform/governance.md) shows the decision path.

## Optional: configure library backings

```bash
afb library backings
afb library index
afb library search --query "rusty metal"
```

Backings resolve through environment handles (`AFB_MATERIALS_ROOT`, `AFB_TEXTURES_ROOT`, `AFB_USD_ASSETS_ROOT`, `AFB_OMNIVERSE_CONTENT_ROOT`, `AFB_USD_SEARCH_URL`). Indexing is optional; the curated exemplar, property and knowledge seeds work out of the box.

## Optional: launchable tool server

```bash
scripts/afb-agent-launchable
```

The launcher starts the tool server on stdio so an agent runtime can call the governed tool surface directly. The protocol is newline-delimited JSON with `health`, `catalogue` and synchronous `invoke` operations. Reviewed mutations use the same exact-parameter approval tokens and replay ledger as HTTP. Set `AFB_TOOL_SERVER_ALLOWED_TOOLS` before launch to expose only the tools the parent agent needs.

For guided start-up, expose `asset_programme_intake,asset_factory_start` and give the parent agent the asset programme strategist skill. Intake remains approval-free; the host must approve the exact start parameters before the second tool can create the project.

## Review outputs

Generated artefacts live under `projects/` and `artifacts/`, intentionally ignored by Git. Start with `progress.json` and `reports/contact-sheet.md` in the project workspace, then `run-plan.json`, `manifests/` and `reports/`. The docs site builds with `pip install -e ".[docs]"` and `make site`; the verification suite lives in the sibling asset-factory-verification repository.
