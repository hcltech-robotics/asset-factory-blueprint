# Release process

The canonical release artefact is a tagged source archive. Editable installation from that checkout preserves the repository-relative schemas, policies, skills, examples and scripts used by the runtime. `uv.lock` is the resolved Python dependency contract for Python 3.11 through 3.13.

## Version and evidence

1. Choose the semantic version and update `pyproject.toml`, `src/asset_factory_blueprint/__init__.py`, `CITATION.cff`, `codemeta.json`, `CHANGELOG.md`, the `org.opencontainers.image.version` label in `deploy/Dockerfile`, the image tag in `deploy/kubernetes/asset-factory.yaml` and the BibTeX blocks in `README.md` and `docs/index.md` together. `afb release evidence` checks the first four for alignment; check the rest by hand.
2. Confirm every public schema has the intended schema major version and resolvable identifier.
3. Run `uv lock --check`, then install with `uv sync --frozen --all-extras`.
4. Run the verification suite against the clean release checkout: `uv run --frozen pytest -q tests`, `uv run --frozen python scripts/ci/stdio_tool_server_smoke.py`, `uv run --frozen python scripts/generate_diagrams.py --check` and the example workflow runs and record-graph validations listed in `.github/workflows/ci.yml`. Record the verified commit, its CI run and the exact commands in the release notes.
5. Build the documentation strictly and check generated diagrams.
6. Build the source archive and wheel with `uv run --frozen python -m build --sdist --wheel`, install the extracted source archive into a clean environment and run the documented dry run.
7. Run `afb release evidence --output-dir artifacts/release-evidence`. This writes a CycloneDX 1.6 SBOM from `uv.lock`, an exact versioned schema catalogue, configuration digests, aligned citation-metadata digests, the container-recipe and declared default base-image digests and release checksums. Repository cleanliness is recorded as `clean`, `dirty` or `unknown`; an unavailable Git command never produces a clean claim.
8. Create and validate the positive and negative capsules described in `docs/reference-run-capsule.md`.
9. Check that release artefacts contain no credentials, signed URLs, absolute workstation paths, ignored project workspaces or unlicensed source material.

## Publication

Create a signed `vMAJOR.MINOR.PATCH` tag from `main`. Publish the source archive, wheel, `uv.lock`, schema bundle, bill of materials, checksums and both reference capsules with the release. After the release is archived, add the assigned DOI to `CITATION.cff`, `codemeta.json` and the release notes in a follow-up patch release if necessary.

The release notes identify:

- software and schema versions
- verified commit and its CI run
- supported runtime matrix
- migrations and deprecations
- known blocked profiles or runtimes
- model, dataset and content licences relevant to the reference capsule

Do not describe a Profile, runtime or deployment target as supported unless its evidence appears in the release matrix.
