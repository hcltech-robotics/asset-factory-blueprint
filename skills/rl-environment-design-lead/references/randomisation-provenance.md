# Randomisation provenance

The supported axes are mass and friction.

| Provenance | Meaning |
| --- | --- |
| `evidence` | interval read from an accepted upstream record |
| `policy_default` | interval explicitly declared in the run request |

Every evidence interval records its manifest, field path and checksum. A requested interval that narrows accepted evidence is `review_required`; a non-overlapping interval blocks the lane.

Posterior inference, trained-policy feasibility sweeps and variants have no attested importer in this release and are rejected.
