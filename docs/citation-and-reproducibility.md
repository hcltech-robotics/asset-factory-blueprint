---
description: "Learn how to cite Asset Factory releases and record source commits, schemas, dependencies, runtime versions and reference-run evidence."
---

# Citation and reproducibility

Asset Factory Blueprint records machine-readable project details in `.zenodo.json`, `CITATION.cff` and `codemeta.json` [@cff_specification_2021; @codemeta_project_2026]. `references.bib` lists the archived software release, supporting standards and technical sources.

## DOI and current release

Zenodo assigns two related identifiers:

- [`10.5281/zenodo.22201829`](https://doi.org/10.5281/zenodo.22201829) is the concept DOI for the software series and resolves to its latest archived version.
- [`10.5281/zenodo.22201830`](https://doi.org/10.5281/zenodo.22201830) identifies the immutable v1.1.0 release published on 31 August 2026 [@von_csefalvay_asset_factory_2026].

Use the concept DOI for a rolling project reference. Cite the version DOI for experiments, assets, release evidence and any claim that must remain tied to exact source. This follows Zenodo's distinction between a concept DOI and the DOI minted for each version [@zenodo_doi_versioning_2026; @zenodo_citation_guidance_2026]. Zenodo is the archival publisher in the release citation; HCLTech Robotics Intelligence CoE remains the software and website publisher.

## Citing in the documentation

`references.bib` is the single bibliography for the repository and documentation. Body pages cite entries with pandoc syntax such as `@voncsefalvay2026`, separating multiple keys with semicolons. The build renders author-year citations as footnotes using the Cite Them Right Harvard style vendored under `docs/assets/citation-style/`.

Pages with citations end with a `References` heading and the bibliography command on its own line, as in `docs/extensions/rl-environment.md`. The [References](references.md) page renders the complete bibliography at build time.

Unknown keys fail the strict build. Keys use `surname_keyword_year` for papers and books, and `organisation_topic_year` for online sources. Braces preserve the capitalisation of proper nouns, acronyms and venue names. The `pandoc` executable must be on `PATH`; the documentation workflows install it before building the site.

## What to cite

Cite the tagged software release used to produce the asset. A reproducible result also records:

- the source commit and whether the checkout was clean
- the `uv.lock` digest and supported Python resolution marker
- the release schema catalogue with each JSON Schema identity, version and file digest
- the verified commit and its CI run
- the reference-run capsule identifier and digest
- OpenUSD, validator, simulator, driver and GPU versions
- every external backend code revision, model or weight revision and random seed

A repository branch name is not a reproducible software identifier. Prefer a signed tag and archived version DOI. If no DOI exists, cite the tag URL and commit SHA.

## Schema identities

The release schema catalogue records every schema `$id`, major schema version, JSON Schema draft [@json_schema_2022], title and file SHA-256, together with an exact count. Manifests are associated with those schemas by the stage-contract catalogue and are validated against the archived files. Public `$id` values must resolve without repository credentials. The documentation site publishes a convenience copy under `/schemas/v1/`, but that mirror is not a second schema identity: the canonical identity remains the schema's versioned `$id`. A release archives the exact schema directory beside the source archive; consumers must not silently substitute a newer schema fetched from a mutable branch.

Schema changes follow the compatibility rules in `GOVERNANCE.md`. A breaking meaning or required-field change receives a new schema major identity and a migration note.

## Release metadata

Before tagging a release, keep the version aligned across `pyproject.toml`, `src/asset_factory_blueprint/__init__.py`, `CITATION.cff`, `codemeta.json` and `CHANGELOG.md`. `.zenodo.json` is the authoritative metadata source for Zenodo's GitHub integration and deliberately omits a fixed version, release date and version DOI; Zenodo derives those fields from the published GitHub Release and the archive it creates. When both `.zenodo.json` and `CITATION.cff` exist, Zenodo gives `.zenodo.json` precedence [@zenodo_describe_software_2026].

The repository must remain enabled in the Zenodo GitHub settings. Publishing a GitHub Release then causes Zenodo to ingest the tagged source and mint the next version DOI automatically [@zenodo_enable_repository_2026]. A tag or draft release alone does not cross that publication boundary. The `Zenodo release verification` workflow polls the public versions API after each published release, verifies the tag, concept DOI, minted version DOI, creators, ORCIDs, affiliations, licence and archived source, then adds the DOI to the GitHub release notes. It does not create a second deposit and requires no Zenodo token.

Before Zenodo mints the identifier, version-specific DOI and record fields must be consistently absent; the documentation then cites the stable concept DOI. After assignment, every exact-DOI surface must be populated consistently. The verifier extracts the immutable tag, rejects stale or partially populated DOI metadata, and checks any DOI already present in that tag against the record Zenodo actually minted.

The external repository toggle cannot be enforced from Git. A failed verification therefore means the release owner should check the Zenodo GitHub integration and ingestion status before changing metadata or republishing anything. Never invent a DOI, rewrite a signed tag or run an independent depositor against this DOI series.

## Reproducing a run

Start from the tagged source archive rather than an arbitrary checkout. Verify the archive, `uv.lock` and capsule checksums, run `uv sync --frozen` and follow the capsule's reproduction commands. Compare regenerated manifest and artefact digests with the capsule, allowing only fields explicitly marked as run-instance values.

The source archive is the canonical distribution because the runtime intentionally consumes repository-relative schemas, policies, skills, scripts and examples. A Python wheel alone is not a complete factory distribution.

## References

\bibliography
