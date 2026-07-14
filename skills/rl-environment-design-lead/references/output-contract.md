# Output contract

`ToolResult` returns `success`, `data`, `error`, `warnings`, `artefacts`, `proposals` and `validation_status`.

| Field | Contents |
| --- | --- |
| `data` | Design status, gate results, blocked reasons, environment contract and command contracts. Runtime probe results appear only after signed evidence has been imported. |
| `artefacts` | `reports/rl-environment-design-report.json`, `reports/environment-card.md` and `reports/rl-task-fitness-protocol.json`, each with a checksum. The workflow writes `manifests/rl-environment-manifest.json` and the stage report. |
| `proposals` | The environment contract while its review status remains unresolved. The lane does not emit editing curricula, training plans or policy-evaluation records. |

Canonical runtime evidence appears only after the report signature has passed under its probe or fidelity role key and the import receipt has passed under the independent import role key.

The manifest fields are documented in environment-manifest-shape.md.
