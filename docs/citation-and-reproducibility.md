# Citation and reproducibility

Asset Factory Blueprint publishes citation metadata in `CITATION.cff` and `codemeta.json`. Supporting standards and technical references are listed in `references.bib`.

## What to cite

Cite the tagged software release used to produce the asset. A reproducible result also records:

- the source commit and whether the checkout was clean
- the `uv.lock` digest and supported Python resolution marker
- the release schema catalogue with each JSON Schema identity, version and file digest
- the verification repository commit
- the reference-run capsule identifier and digest
- OpenUSD, validator, simulator, driver and GPU versions
- every external backend code revision, model or weight revision and random seed

A repository branch name is not a reproducible software identifier. Prefer a signed tag and archived DOI. If no DOI exists, cite the tag URL and commit SHA.

## Schema identities

The release schema catalogue records every schema `$id`, major schema version, JSON Schema draft, title and file SHA-256, together with an exact count. Manifests are associated with those schemas by the stage-contract catalogue and are validated against the archived files. Public `$id` values must resolve without repository credentials. The documentation site publishes a convenience copy under `/schemas/v1/`, but that mirror is not a second schema identity: the canonical identity remains the schema's versioned `$id`. A release archives the exact schema directory beside the source archive; consumers must not silently substitute a newer schema fetched from a mutable branch.

Schema changes follow the compatibility rules in `GOVERNANCE.md`. A breaking meaning or required-field change receives a new schema major identity and a migration note.

## Release metadata

Before tagging a release, keep the version aligned across `pyproject.toml`, `src/asset_factory_blueprint/__init__.py`, `CITATION.cff`, `codemeta.json` and `CHANGELOG.md`. Add the DOI only after an archive has assigned it. Never publish an invented DOI.

## Reproducing a run

Start from the tagged source archive rather than an arbitrary checkout. Verify the archive, `uv.lock` and capsule checksums, run `uv sync --frozen` and follow the capsule's reproduction commands. Compare regenerated manifest and artefact digests with the capsule, allowing only fields explicitly marked as run-instance values.

The source archive is the canonical distribution because the runtime intentionally consumes repository-relative schemas, policies, skills, scripts and examples. A Python wheel alone is not a complete factory distribution.
