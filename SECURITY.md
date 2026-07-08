# Security policy

## Supported versions

Security fixes are applied to the default branch and, when one exists, the most recent tagged release. Older releases are not routinely patched.

| Version | Supported |
| --- | --- |
| `main` | Yes |
| Most recent tagged release | Yes |
| Older releases | No |

## Report a vulnerability

Do not report suspected vulnerabilities in a public issue, discussion or pull request. Use GitHub's [private vulnerability reporting form](https://github.com/hcltech-robotics/asset-factory-blueprint/security/advisories/new).

If private reporting is unavailable, contact the repository owner privately through the contact method on their GitHub profile. Do not include vulnerability details in a public message.

Include:

- the affected version or commit
- the affected component and environment
- prerequisites and the smallest reproducible sequence
- observed and expected behaviour
- the security impact and who could be affected
- a proof of concept or relevant logs with secrets and private assets removed
- any known mitigation or workaround
- your preferred name for acknowledgement, or a request to remain anonymous

Do not send live credentials, access tokens, signed URLs, proprietary source assets or personal data. If sensitive evidence is necessary, describe it first and wait for a safe transfer method.

## What happens next

We aim to acknowledge a report within three business days and provide an initial assessment within ten business days. Confirmed reports receive progress updates at least every 14 days until remediation or closure.

We will coordinate the fix, advisory and disclosure timing with the reporter. Please allow a reasonable remediation period before public disclosure. We will credit the reporter when requested and when doing so is safe.

## Scope

This policy covers code, configuration, documentation, release artefacts and deployment templates maintained in this repository. Report vulnerabilities in third-party runtimes, hosted providers, model weights or reconstruction backends to their maintainers unless this repository's integration introduces the vulnerability.

Reports about exposed credentials or sensitive data committed to this repository are in scope. Revoke or rotate any credential you control before reporting it.

## Safe harbour

Good-faith research conducted under this policy is authorised when it:

- avoids privacy violations, data destruction and service disruption
- uses only the access needed to demonstrate the issue
- does not retain, alter or disclose data belonging to others
- reports the issue promptly and keeps it confidential during remediation
- complies with applicable law

We will not pursue action against researchers who follow these conditions. This safe-harbour statement does not authorise testing of third-party systems or data outside this repository's control.
