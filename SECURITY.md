# Security Policy

Hermes-A365 connects an internet-facing Bot Framework endpoint to Hermes and
handles Microsoft Entra identities, delegated token exchange, and local client
secrets. Please report suspected vulnerabilities privately so they can be
investigated before public disclosure.

## Supported versions

Before v1.0, security fixes are provided for the latest released version only.
The `main` branch contains unreleased work and is not a supported deployment.

| Version | Supported |
| --- | --- |
| Latest release | Yes |
| Earlier releases | No |
| Unreleased `main` | No |

This policy will be reviewed at v1.0 as part of the public-surface and upgrade
policy freeze.

## Reporting a vulnerability

Use GitHub's
[private vulnerability reporting](https://github.com/Satscryption/Hermes-A365/security/advisories/new)
for vulnerabilities that are not already public. This is the preferred route
because it provides a private discussion, remediation, and advisory workspace.

If private reporting is unavailable, email
[sadiq.jaffer@satscryption.io](mailto:sadiq.jaffer@satscryption.io) with the
subject `Hermes-A365 security report`. Do not include credentials, access
tokens, tenant data, or other live secrets. Please include:

- the affected version or commit;
- the affected component and deployment path;
- reproduction steps or a minimal proof of concept;
- the security impact and any known prerequisites; and
- suggested mitigations, if available.

Do not open a public issue for an undisclosed vulnerability.

## What to expect

- We aim to acknowledge a report within three business days.
- We aim to provide an initial triage decision within seven business days.
- For accepted reports, we will share progress at least every 14 days while a
  fix or mitigation is in development.
- Disclosure timing will be coordinated with the reporter after a fix is
  available. Complex issues or upstream dependencies may require more time.

Please make a good-faith effort to avoid privacy violations, data destruction,
service disruption, and access beyond what is needed to demonstrate the issue.
We will credit reporters who request attribution.
