# Governance

Asset Factory Blueprint is maintained as a reference implementation whose contracts must remain reviewable and reproducible. Repository activity is public by default, except for security and conduct reports.

## Roles

- Maintainers merge changes, cut releases and decide whether a proposal changes a public contract.
- Contract reviewers check schema compatibility, provenance and release semantics.
- Runtime reviewers check OpenUSD, physics and simulator behaviour on a declared support target.
- Contributors may propose any change through an issue or pull request.

One person may hold more than one role, but a release-affecting change should receive review from someone other than its author whenever another maintainer is available.

## Decisions

Routine fixes use normal pull-request review. A change requires a design record when it changes a public schema, stage boundary, promotion rule, layer owner, tool protocol or compatibility promise. The record must state the decision, alternatives, compatibility effect and migration path.

Maintainers seek rough consensus. If consensus is not possible, the lead maintainer records the decision and its rationale in the pull request or design record. Decisions may be revisited when new runtime evidence becomes available.

## Compatibility

The project uses semantic versioning for software releases. Public schema identities have their own major version. Additive compatible fields may land within a schema major version. Required-field changes, changed meanings and removed values require a new schema major version and a migration note.

Deprecations remain documented for at least one minor software release before removal. Security fixes may shorten that period.

## Releases

Only maintainers cut releases. A release must satisfy [RELEASE.md](RELEASE.md), use a signed tag and preserve the exact schemas and reference evidence associated with that tag. A DOI or archive identifier is attached after publication rather than invented in advance.

## Conflicts and recusal

Reviewers disclose relevant commercial, research or authorship conflicts and recuse themselves when those conflicts could affect a release decision. Conduct concerns follow [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Vulnerabilities follow [SECURITY.md](SECURITY.md).
