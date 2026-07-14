# Brev launchable

This launchable runs the Asset Factory Blueprint as a durable, authenticated HTTP control plane. It exposes the guided factory entry points, keeps project workspaces, audit records, approval ledgers and caches on the Brev instance disk, and serves the API only through a Brev Secure Link.

The service is deliberately not a notebook or a finite readiness task. A parent agent calls `asset_programme_intake` to turn an ordinary-language brief into a complete run request, then submits `asset_factory_start` with a one-use, parameter-bound approval to create the project and run the governed agent loop. The public API retains the same bounded execution, durable state and review boundary as the local deployment.

## Launchable contract

| Item | Value |
| --- | --- |
| Repository | `https://github.com/hcltech-robotics/asset-factory-blueprint.git` |
| Revision | A reviewed immutable tag or commit containing `deploy/brev/` |
| Environment | Basic VM with Python and Docker support |
| Setup command | `bash deploy/brev/launch.sh` |
| Jupyter experience | Disabled |
| Secure Link | `afb` on port `8181` |
| Direct TCP or UDP rules | None |
| Service path | `/v1/tools`, `/v1/jobs` and `/healthz` |
| Service authentication | HTTP bearer token plus one-use approval capability for mutations |

The Secure Link supplies the public HTTPS route. Do not publish raw port `8181`. The service still requires its bearer token because the Secure Link controls reachability while the token identifies an API caller.

## Create the launchable in Brev

1. In Brev, create a Launchable from the repository above and select the reviewed revision.
2. Choose the Basic VM environment with Python and Docker support. Do not enable a Jupyter experience.
3. Paste `bash deploy/brev/launch.sh` as the setup command.
4. Add the secrets in the next section. The values stay in Brev's secret store and are injected only into the launch command and service container.
5. Create one Secure Link named `afb` for port `8181`. Leave direct TCP and UDP ports closed.
6. Create the Launchable. Its setup command builds the pinned container, prepares the writable run roots, starts one hardened HTTP service as the instance's non-root user and waits for `/healthz` before Brev marks setup complete.

## Secrets and configuration

Set these values in the Brev Launchable secret configuration. Generate independent random values for the two service secrets. They must each contain at least 32 bytes.

| Name | Required | Purpose |
| --- | --- | --- |
| `AFB_TOOL_SERVER_TOKEN` | Yes | Bearer token for the HTTP API |
| `AFB_TOOL_SERVER_APPROVAL_SECRET` | Yes | Issues one-use approvals for reviewed mutations. It must differ from the bearer token. |
| `NVIDIA_API_KEY` | For live default authoring | NVIDIA NIM reasoning and visual-review lanes |
| `OPENAI_API_KEY` | When using OpenAI image generation | Optional texturing lane |
| `AFB_PHYSICS_EVIDENCE_SECRET` | When producing signed physics evidence | Physics-evidence attestation |
| `AFB_VALIDATION_ATTESTATION_SECRET` | When using the native validator | Validator attestation |
| `AFB_ISAAC_ATTESTATION_SECRET` | When importing Isaac load evidence | Isaac-evidence attestation |

The launchable starts with the minimal guided allowlist:

```text
asset_programme_intake,asset_factory_start
```

Set `AFB_TOOL_SERVER_ALLOWED_TOOLS` only when the parent agent needs a reviewed expansion of that surface. The service rejects unknown tool names. `asset_programme_intake` is read-only. `asset_factory_start` writes a project only when the request is complete and includes a matching, unused approval token.

The following optional non-secret variables are accepted by the launch files:

| Name | Default | Purpose |
| --- | --- | --- |
| `AFB_BREV_PORT` | `8181` | Brev Secure Link target port |
| `AFB_BREV_BIND_ADDRESS` | `0.0.0.0` | VM interface for the Secure Link target |

`launch.sh` derives `AFB_BREV_UID` and `AFB_BREV_GID` from the non-root Brev setup user. They keep the persisted host directories writable without changing ownership of prior project or evidence files.

## Operating the launchable

The service uses these persistent repository roots on the Brev instance:

| Host path | Container path | Contents |
| --- | --- | --- |
| `projects/` | `/workspace/projects` | Factory project workspaces and generated records |
| `artifacts/` | `/workspace/artifacts` | Job ledger, approval ledger, audit log and run artefacts |
| `library/downloads/` | `/workspace/library/downloads` | Explicit library downloads |
| `.cache/afb/` | `/workspace/.cache/afb` | Reusable runtime caches |

On redeploy, rerun the setup command. It rebuilds the container from the selected revision, preserves those roots and recreates only the service container. One service replica is intentional because the local durable ledger is not a distributed scheduler.

To verify a deployed Secure Link from the Brev instance or another trusted shell, set the bearer token in the shell and run:

```bash
export AFB_TOOL_SERVER_TOKEN='<bearer token from Brev secrets>'
bash deploy/brev/smoke.sh 'https://<afb-secure-link-host>'
```

The smoke test checks public health and the authenticated catalogue. It neither starts a run nor prints the token.

## Parent-agent hand-off

Give the parent agent the [`asset-programme-strategist`](../../skills/asset-programme-strategist/SKILL.md) skill and the Secure Link URL. Its first call is `asset_programme_intake`; it must ask any returned questions and keep the request blocked until intake says it is ready. An operator then issues an approval for the exact `asset_factory_start` parameters through the trusted approval workflow. The parent agent submits that approval together with the start request, then reads the durable job and project records from the service.

The endpoint contract, approval issue command and durable job semantics are defined in the [deployment guide](../../docs/platform/deployment.md).
