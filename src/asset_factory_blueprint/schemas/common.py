from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


SCHEMA_VERSION_PATTERN = r"^[1-9][0-9]*\.[0-9]+(?:\.[0-9]+)?(?:[-+][0-9A-Za-z.-]+)?$"
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$"
PATH_COMPONENT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
SHA256_PATTERN = r"^(?:sha256:)?[A-Fa-f0-9]{64}$"

ValidationStatus = Literal["proposal", "validated", "review_required", "blocked", "released", "not_validated"]
ReviewStatus = Literal["not_reviewed", "review_required", "approved", "rejected"]
RightsStatus = Literal["unknown", "pending", "cleared", "restricted", "rejected"]
PrivacyStatus = Literal["not_applicable", "unknown", "cleared", "restricted", "rejected"]


class EvidenceRecord(BaseModel):
    """A content-addressed item used to support a manifest claim."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(pattern=IDENTIFIER_PATTERN)
    kind: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    checksum: str = Field(min_length=1)
    media_type: str | None = None
    created_at: str | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)


class ProviderTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    role: str = Field(min_length=1)
    prompt_checksum: str = Field(min_length=1)
    request_id: str | None = None
    model_revision: str | None = None
    weights_checksum: str | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)


class SourceRightsRecord(BaseModel):
    """Machine-readable rights evidence for one source or source collection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rights_id: str = Field(pattern=IDENTIFIER_PATTERN)
    source_id: str = Field(min_length=1)
    rights_status: RightsStatus = "unknown"
    licence_expression: str = "NOASSERTION"
    terms_uri: str | None = None
    creator: str | None = None
    revision: str | None = None
    attribution: str | None = None
    permitted_uses: tuple[str, ...] = ()
    redistribution_allowed: bool = False
    derivatives_allowed: bool = False
    privacy_status: PrivacyStatus = "unknown"
    consent_evidence_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    expires_at: str | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)


class RetentionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: Literal["project", "delete_after_project", "fixed_period", "indefinite", "legal_hold"] = "project"
    expires_at: str | None = None
    deletion_required: bool = False
    evidence_ids: tuple[str, ...] = ()
    extensions: dict[str, Any] = Field(default_factory=dict)


class ReviewerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewer_id: str = ""
    review_status: ReviewStatus = "not_reviewed"
    reviewed_at: str | None = None
    evidence_ids: tuple[str, ...] = ()
    extensions: dict[str, Any] = Field(default_factory=dict)


class StageAttemptIdentity(BaseModel):
    """Immutable identity for a single execution attempt of one stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str = Field(pattern=PATH_COMPONENT_PATTERN)
    run_id: str = Field(pattern=PATH_COMPONENT_PATTERN)
    stage_id: str = Field(pattern=PATH_COMPONENT_PATTERN)
    attempt_number: int = Field(ge=1)
    request_digest: str = Field(pattern=SHA256_PATTERN)


class StageAttempt(BaseModel):
    """Immutable terminal record for one stage execution attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: StageAttemptIdentity
    status: Literal["succeeded", "failed", "cancelled", "timed_out", "blocked"]
    started_at: str
    completed_at: str
    consumed_ids: tuple[str, ...] = ()
    produced_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()
    provenance_id: str | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)


class ConformanceFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_id: str
    status: Literal["pass", "fail", "not_run", "not_applicable"]
    message: str = ""
    evidence_ids: tuple[str, ...] = ()
    extensions: dict[str, Any] = Field(default_factory=dict)


class ConformanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conformance_id: str = Field(pattern=IDENTIFIER_PATTERN)
    attempt_id: str = Field(pattern=IDENTIFIER_PATTERN)
    profile_id: str
    profile_version: str
    status: Literal["pass", "fail", "not_run"]
    findings: tuple[ConformanceFinding, ...] = ()
    checker: str
    checker_version: str
    extensions: dict[str, Any] = Field(default_factory=dict)


class ProvenanceIdentity(BaseModel):
    """Immutable identity and creation metadata for a provenance record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provenance_id: str = Field(pattern=IDENTIFIER_PATTERN)
    schema_version: str = Field(pattern=SCHEMA_VERSION_PATTERN)
    created_at: str
    run_id: str | None = None
    attempt_ids: tuple[str, ...] = ()


class EnvironmentBillOfMaterials(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operating_system: dict[str, str]
    python: dict[str, Any]
    hardware: dict[str, str]
    accelerator: dict[str, str]
    container: dict[str, str]
    simulation: dict[str, str]
    extensions: dict[str, Any] = Field(default_factory=dict)


class ModelBillOfMaterialsEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    provider: str
    kind: str
    model_id: str
    revision: str
    weights_checksum: str
    licence_expression: str
    resolution_status: str
    runtime: str
    extensions: dict[str, Any] = Field(default_factory=dict)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(pattern=PATH_COMPONENT_PATTERN)
    version: str = Field(default="1.0", pattern=SCHEMA_VERSION_PATTERN)
    objective: str = Field(min_length=1)
    sources: list[str]
    requested_outputs: list[str]
    constraints: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_v2_extension_namespace(self) -> Self:
        if self.version.startswith("2.") and self.__pydantic_extra__:
            unknown = ", ".join(sorted(self.__pydantic_extra__))
            raise ValueError(f"v2 run request fields must be declared or placed under extensions: {unknown}")
        return self


class StagePlan(BaseModel):
    id: str = Field(pattern=PATH_COMPONENT_PATTERN)
    name: str
    skill: str
    status: ValidationStatus = "proposal"
    provider_roles: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    consumes: list[str] = Field(default_factory=list)
    produces: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    resources: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(default=1, ge=1)
    execution_mode: Literal["local", "service", "external", "manual"] = "local"
    validation_gates: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)


class ProviderAssignment(BaseModel):
    provider: str
    kind: str
    model_env: str
    model_id: str = ""
    model_resolution_status: str = "blocked_unresolved"
    blocked_reason: str = ""
    base_url_env: str | None = None


class RunPlan(BaseModel):
    id: str = Field(pattern=PATH_COMPONENT_PATTERN)
    run_id: str = Field(pattern=PATH_COMPONENT_PATTERN)
    request_digest: str = ""
    created_at: str = ""
    stage_contract_version: str = "2.0"
    asset_id: str
    request_id: str
    objective: str
    requested_outputs: list[str] = Field(default_factory=list)
    stages: list[StagePlan]
    provider_assignments: dict[str, ProviderAssignment]
    missing_evidence: list[str]
    validation_gates: list[str]
    wandb_plan: dict[str, Any]
    wandb: dict[str, Any]
    provenance: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    manifests: Path
    evidence: Path
    reports: Path
    snapshots: Path
    packaged: Path
