# Kubernetes

The manifest runs one authenticated HTTP tool-service replica behind a ClusterIP Service. Jobs and consumed approvals are persisted in the artefact volume. Multiple replicas are deliberately unsupported because the file-backed ledger is not a distributed scheduler.

Create `asset-factory-tool-server` with independent `bearer-token` and `approval-secret` keys before applying the manifest. Replace the image and artefact `emptyDir` with a persistent volume claim for pod-replacement durability. Review the tool allowlist in the ConfigMap. Only pods carrying `asset-factory-client=true` pass the included ingress policy.

The pod runs without a service-account token, as a non-root identity, with a read-only root filesystem and dropped capabilities. Provider and storage credentials belong in separate externally managed Secrets.
