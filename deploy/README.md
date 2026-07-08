# Deployment

Deployment templates are parameterised and non-secret. Provider keys, the HTTP bearer token and the independent reviewed-mutation approval secret come from environment variables or platform secret stores, never from committed files.

## Runnable service templates

Docker Compose and Kubernetes run the bounded HTTP tool service with a persistent job and consumed-approval ledger. Recorded terminal jobs survive process restart and interrupted jobs are marked failed. Keep one replica because the ledger is not a distributed scheduler.

For Compose, copy `.env.example` to `.env`, set independent strong values for `AFB_TOOL_SERVER_TOKEN` and `AFB_TOOL_SERVER_APPROVAL_SECRET`, review `AFB_TOOL_SERVER_ALLOWED_TOOLS` and start the service from the repository root:

```bash
docker compose -f deploy/docker-compose.asset-factory.yml up --build
```

The published Compose port remains loopback-only. Kubernetes requires an externally created Secret and permits ingress only from pods labelled `asset-factory-client=true`:

```bash
kubectl create secret generic asset-factory-tool-server \
  --from-literal=bearer-token='<generated-bearer-secret>' \
  --from-literal=approval-secret='<generated-approval-secret>'
kubectl apply -f deploy/kubernetes/asset-factory.yaml
```

Replace the Kubernetes image reference and artefact `emptyDir` with a persistent volume claim before production use. The checked-in volume survives container restart in one pod but not pod replacement.

## Batch templates

Slurm, OSMO and Brev examples run finite validation or workflow commands. They do not provide a durable API service. Adapt resource requests and secret injection to the target scheduler, then retain the generated reports with the run record.

## Image provenance

`PYTHON_IMAGE` can override the Docker base. Release evidence records the Dockerfile digest and its declared default base-image digest. When a release build overrides that argument, record the resolved base-image and built-image digests in the release notes beside the software bill of materials. Do not infer reproducibility from a floating image tag alone.

The image installs the application non-editably with `uv sync --frozen --no-dev --extra validation --extra mesh --extra vision` from the checked-in `uv.lock`. `AFB_APPLIANCE_ROOT=/workspace` binds the installed command to the copied schemas, policies, skills and examples rather than to a build workstation.
