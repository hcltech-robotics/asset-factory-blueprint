# Troubleshooting

Each row maps an observable symptom to its likely cause and corrective action.

## Installation and CLI

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `afb: command not found` | package not installed in the active environment | `pip install -e .` inside the virtual environment, then re-run |
| `afb` fails on import with a missing module | optional dependency not installed | run `uv sync --frozen --all-extras`, or install the named `mesh`, `vision` or `validation` extra from the lockfile |
| `asset_image_segmentation_prior` returns blocked with a dependency note | `numpy` or `opencv-python-headless` missing | install both; the tool degrades to blocked rather than crashing |
| contact sheet PNG missing while the markdown sheet exists | `pillow` missing or image encode failed | install `pillow` and re-run `afb progress --project projects/<slug>` |

## Workflow and agent loop

| Symptom | Likely cause | Action |
| --- | --- | --- |
| every stage ends `review_required` after `afb agent run` | dry run is the default; no provider was called | expected; add `--live` once provider credentials are exported |
| a stage review is skipped with "no stage-output images exist yet" | the stage has produced no renderable outputs, so there is nothing to judge | run the producing step live (for reconstruction, `afb reconstruction create-backend` then the external model run) and re-run the loop |
| review verdict is approve but the stage stays `review_required` | the verdict carried blocker or major defects; approval with blockers is held | read the review record in `reports/`, clear the defect, re-run |
| fixes ran but the loop escalated with "no fix changed the workspace artefacts" | the applicable recipes could not touch the failing artefact | read `fix-report` entries for the `not_applicable` reasons; supply the missing input or fix manually |
| `missing-evidence.json` lists items after a dry run | the run request references evidence that is not in the workspace | add the listed sources or trim the request; blocked stages will not progress until cleared |

## Providers

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `provider check` reports a lane missing an environment variable | credential not exported in this shell | export the handle the message names (`NVIDIA_API_KEY`, `OPENAI_API_KEY`, `AFB_LLM_API_KEY`); see the environment reference |
| live review fails with HTTP 401 or 403 | wrong or expired key for the lane | rotate the key; confirm the lane's `api_key_env` in `configs/provider-policy.json` |
| live review fails with a model-not-found error | model override does not exist on the lane | unset `AFB_VISION_MODEL` or set it to a model the endpoint serves; defaults live in `configs/vlm-review-policy.json` |
| Hugging Face download fails with 401 or 403 | gated repository and no token | export `HF_TOKEN` after accepting the model licence upstream |
| a review fails with a bare HTTP 500 while other stages review fine | too many or too large evidence images for the hosted vision endpoint | the reviewer sends at most five images and re-encodes oversized evidence as capped JPEG derivatives; if a custom policy raises those limits, lower them again |
| model downloads stall at zero throughput with connections established | a machine-level `HF_HUB_CACHE` or `HF_HOME` points at a network share, or the Xet transport is stalling | override `HF_HUB_CACHE` to a local path for the run and set `HF_HUB_DISABLE_XET=1` |

## Libraries

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `afb library search` finds nothing from operator folders | backing declared but never indexed | export the backing root handle, then `afb library index` |
| USD Search returns `unrecognised_response` | endpoint URL or auth mode mismatch | check `AFB_USD_SEARCH_URL`; switch `AFB_USD_SEARCH_AUTH_MODE` between `bearer` and `api-key`; force `AFB_USD_SEARCH_METHOD` if the deployment only serves one verb |
| `library fetch` ends blocked with `unresolved_item_ids` | requested item ids not present in the remote source listing | check the id spelling against the source site or drop to a query fetch |
| a download is refused | non-https URL or the 512 MB per-file cap | both limits are deliberate; fetch the file manually into `library/downloads/` if you accept it |

## Reconstruction backends

| Symptom | Likely cause | Action |
| --- | --- | --- |
| install reports the host as blocked on Windows | the backend is Linux-oriented | run the install through WSL or a Linux host, or pass `--force` and accept the risk |
| install-check fails on CUDA or GPU visibility | no visible GPU or driver mismatch | verify `nvidia-smi`; check the per-backend VRAM floor in the requirements page |
| a backend run ends with `timed_out: true` | upstream model exceeded the step timeout | re-run on a stronger GPU or raise the timeout in the registry entry |
| checkout is at the wrong revision | no commit pin | set `AFB_<BACKEND>_COMMIT` or the registry `pinned_commit`; the install report records the resolved commit |
| backend run blocked with "input asset path is required" | the run manifest cites no project source manifest and no input env handle is set | point `--input-manifest` at the project's `manifests/source-asset-manifest.json`, or set `AFB_RECONSTRUCTION_INPUT_ASSET` |
| backend fails with `ModuleNotFoundError` for torch, diffusers or PIL despite a provisioned venv | the backend was provisioned before interpreter recording existed, so no `.afb-interpreter` marker is present in the checkout | re-run `afb reconstruction install` from the backend venv, or set `AFB_RECONSTRUCTION_PYTHON` to the backend venv's `python.exe` |

## Gates and packaging

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `isaac-load` gate never passes | no Isaac Sim on this machine | set `AFB_ISAAC_SIM_ROOT`, run the load check script, then `afb isaac-load apply --project ... --report ...` |
| package checks report USD errors on a mesh that renders fine | layer, unit or axis metadata mismatch | read the simready report; the manifest contract requires explicit units and axis policy |
| `readiness` rolls up blocked with all stages green | a required gate has no recorded result | run the missing gate and regenerate with `afb readiness` |

## Documentation and figures

| Symptom | Likely cause | Action |
| --- | --- | --- |
| `mkdocs build --strict` fails on a link | target moved or points outside `docs/` | keep cross-repository references as code spans, not links |
| `generate_diagrams.py --check` reports drift | a diagram source changed without regeneration | run `python scripts/generate_diagrams.py` and commit the refreshed SVGs |
| figure PNG rasterisation fails | no Chromium available to Playwright | `playwright install chromium` or point `AFB_CHROMIUM` at a browser binary |

## Tool server

| Symptom | Likely cause | Action |
| --- | --- | --- |
| HTTP tool server refuses a non-loopback host | network exposure is opt-in | set `AFB_TRUSTED_TOOL_SERVER_NETWORK=1` only on a trusted network |
| HTTP tool server still refuses a non-loopback host | authentication is mandatory outside loopback | set a strong `AFB_TOOL_SERVER_TOKEN` through the deployment secret store |
| HTTP tool server reports that an approval secret is required | reviewed mutations must remain fail-closed | set a separate 32-byte or longer `AFB_TOOL_SERVER_APPROVAL_SECRET` through the deployment secret store |
| request returns `413` | body exceeds `--max-request-bytes` | reduce the request or raise the bound deliberately |
| submission returns `503` | all retained jobs are active | wait for a job to finish or increase `--max-retained-jobs` within the host memory budget |
| job disappears after restart | no durable job store was configured | start the service with `--job-store` or `AFB_TOOL_SERVER_JOB_STORE`; the audit log alone is not a job store |
| an active job is failed after restart | execution was interrupted before a terminal result was recorded | inspect its durable record, correct the cause and use `/v1/jobs/<id>/retry`; reviewed mutations need a fresh approval |
| mutation returns `403` | approval is absent, expired, mismatched or already consumed | mint a token for the exact canonical parameters with `afb tool-approval issue`; never reuse a token |
| catalogue omits an expected tool | the deployment allowlist excludes it | add the exact catalogue name to `AFB_TOOL_SERVER_ALLOWED_TOOLS` after reviewing its mutation scope |
| agent runtime sees no tools | server started on the wrong transport | use `--transport stdio` for launchable agents, `--transport http` for networked callers |

When a symptom is not listed, start from the newest file in the project's `reports/` folder; every service failure is written there as a structured report before the CLI exits.
