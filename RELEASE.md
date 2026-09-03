# Release process

The canonical release artefact is a tagged source archive. Editable installation from that checkout preserves the repository-relative schemas, policies, skills, examples and scripts used by the runtime. `uv.lock` is the resolved Python dependency contract for Python 3.11 through 3.13.

## Version and evidence

1. Choose the semantic version and update `pyproject.toml`, `src/asset_factory_blueprint/__init__.py`, `CITATION.cff`, `codemeta.json`, `CHANGELOG.md`, `mkdocs.yml`, the `org.opencontainers.image.version` label in `deploy/Dockerfile`, the image tag in `deploy/kubernetes/asset-factory.yaml` and the BibTeX blocks in `README.md` and `docs/index.md` together. Keep the stable concept DOI `10.5281/zenodo.22201829`. Before the new version DOI exists, remove the previous release's exact DOI from the `DOI` project URL, the version identifier and preferred citation in `CITATION.cff`, and the `identifier`, `citation`, exact-record `sameAs` value and `downloadUrl` in `codemeta.json`; leave `version_doi`, `archive_url` and `archive_download_url` empty in `mkdocs.yml`, which makes the site fall back to the concept DOI. Do not guess the next version DOI or add a fixed version, date or DOI to `.zenodo.json`. `afb release evidence` accepts only a fully aligned assigned-DOI state or a fully aligned pre-mint state and checks the machine-readable release identity, author order, ORCID iDs and affiliations; check the remaining version surfaces by hand.
2. Confirm every public schema has the intended schema major version and resolvable identifier.
3. Run `uv lock --check`, then install with `uv sync --frozen --all-extras`.
4. Run the verification suite against the clean release checkout: `uv run --frozen pytest -q tests`, `uv run --frozen python scripts/ci/stdio_tool_server_smoke.py`, `uv run --frozen python scripts/generate_diagrams.py --check` and the example workflow runs and record-graph validations listed in `.github/workflows/ci.yml`. Record the verified commit, its CI run and the exact commands in the release notes.
5. Build the documentation strictly and check generated diagrams.
6. Build the source archive and wheel with `uv run --frozen python -m build --sdist --wheel`, install the extracted source archive into a clean environment and run the documented dry run.
7. Run `afb release evidence --output-dir artifacts/release-evidence`. This writes a CycloneDX 1.6 SBOM from `uv.lock`, an exact versioned schema catalogue, configuration digests, aligned citation-metadata digests, the container-recipe and declared default base-image digests and release checksums. Repository cleanliness is recorded as `clean`, `dirty` or `unknown`; an unavailable Git command never produces a clean claim.
8. Create and validate the positive and negative capsules described in `docs/reference-run-capsule.md`.
9. Check that release artefacts contain no credentials, signed URLs, absolute workstation paths, ignored project workspaces or unlicensed source material.

## Publication

Create a signed `vMAJOR.MINOR.PATCH` tag from `main`. Publish a GitHub Release for that tag with the source archive, wheel, `uv.lock`, schema bundle, bill of materials, checksums and both reference capsules. A tag or draft release is not sufficient.

The `hcltech-robotics/asset-factory-blueprint` repository must remain enabled in the authenticated [Zenodo GitHub settings](https://help.zenodo.org/docs/github/enable-repository/). A published GitHub Release is automatically ingested into the existing DOI series: Zenodo assigns a new immutable version DOI while retaining the concept DOI `10.5281/zenodo.22201829`. `.zenodo.json` is the authoritative ingest metadata and intentionally leaves version, date and DOI assignment to Zenodo.

The `Zenodo release verification` workflow starts when the GitHub Release is published. It extracts and validates the exact tagged source, polls the public Zenodo versions API, checks the tag, concept DOI, newly minted version DOI, creators, ORCIDs, affiliations, licence, documentation relation and source archive against repository metadata, and appends the DOI to the GitHub release notes. This read-only Zenodo check uses no deposit token and must pass before the release is announced through downstream channels. It does not archive separately because a second deposition path could split or duplicate the DOI lineage. A failed or delayed prerelease check can be rerun manually with its tag; the scheduled check also revalidates the latest stable release every Monday.

The GitHub setting is external state and cannot be guaranteed by repository code. If verification times out, inspect the Zenodo GitHub integration and failed-ingestion status; do not republish, rewrite the signed tag or invent a DOI. After Zenodo assigns the identifier, update version-specific citation surfaces on moving `main`; rolling project links should continue to use the concept DOI. The published tag remains immutable and correctly carries the pre-mint citation state, while Zenodo supplies its immutable version citation.

The release notes identify:

- software and schema versions
- verified commit and its CI run
- supported runtime matrix
- migrations and deprecations
- known blocked profiles or runtimes
- model, dataset and content licences relevant to the reference capsule

Do not describe a Profile, runtime or deployment target as supported unless its evidence appears in the release matrix.
