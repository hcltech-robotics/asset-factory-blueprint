# Support matrix

This matrix separates declared interfaces from combinations that have release evidence. A target is supported only when a tagged release names it as release-verified and links its verification record.

## Status terms

- **Release-verified** means the tagged source, schemas and reference capsule passed the stated checks on that target.
- **CI-checked** means a narrow continuous-integration job exercises the named surface. It is not full runtime qualification.
- **Declared** means the interface is intended to work and is covered by compatibility metadata, but the release has no complete runtime record.
- **Provisional** means integration points exist but no compatibility promise is made.

## Control plane

| Target | Status | Scope |
| --- | --- | --- |
| Python 3.11 on Ubuntu and Windows | CI-checked | Locked install, lint, compile, typed workflow and record-graph validation; strict docs and archives on Ubuntu |
| Python 3.12 on Ubuntu and Windows | Declared | Covered by the lock resolution but not a current CI matrix entry |
| Python 3.13 on Ubuntu and Windows | CI-checked | Locked install, lint, compile, typed workflow and record-graph validation |
| Linux x86-64 container | CI-checked | Frozen image build, CLI import and allowlisted stdio health contract; HTTP qualification remains release-specific |
| Linux aarch64 or GB10 | Provisional | Unified-memory and backend-specific qualification required |
| Mandatory mesh verification | CI-checked | Stage routing, checksum-bound promotion, deterministic topology checks and bounded attempt accounting; live renderer and VLM qualification remain release-specific |

## Asset runtimes

| Target | Status | Evidence required for release verification |
| --- | --- | --- |
| OpenUSD and `UsdValidation` | Declared | Exact OpenUSD build, registered validator inventory, composition checks and fixture results |
| NVIDIA Asset Validator | Provisional | Checker version, Profile, Requirement findings and JSON report |
| NVIDIA Isaac Sim | Provisional | Exact Isaac Sim, driver and GPU versions plus load and behavioural reports |
| PhysX-backed rigid props | Provisional | Applied schema inspection, drop, settle, contact and reset checks |
| Articulated assets | Provisional | Joint schema, axis, limit, drive and repeated runtime sweep checks |

## External systems

Reconstruction backends, hosted providers, NIM endpoints, content libraries, W&B, OSMO, Brev and Slurm are integrations rather than bundled dependencies. Their exact revision, model or service identifier, licence and result must appear in a run capsule before a release can claim them as verified.

When adding a row, link the release evidence rather than relying on a successful local run.

## Deployment lane maturity

| Lane | Maturity | Evidence boundary |
| --- | --- | --- |
| Local CLI and stdio | CI-checked | Typed workflow, record graph, schema validation and governed tool routing |
| Docker Compose HTTP service | Experimental | Authenticated single replica, durable local ledger and bounded jobs; image qualification is release-specific |
| Kubernetes HTTP service | Concept | One-replica manifest and network policy; persistent volume, TLS ingress and platform qualification remain deployment-owned |
| Slurm batch | Concept | Finite command template only |
| OSMO batch | Concept | Finite workflow sketch only |
| Brev host | Concept | Environment and command sketch only |

No deployment lane is `supported` until a tagged release carries its environment, image digest, security configuration and result record.
