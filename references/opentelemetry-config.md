# OpenTelemetry / OTLP (not exported by Hermes-A365)

Snapshot date: 2026-10-29

As of v0.9.6.1 ([#189](https://github.com/Satscryption/Hermes-A365/issues/189)),
**Hermes-A365 does not export OTLP spans**. There is no
``HERMES_OTLP_ENDPOINT`` configuration key, no OpenTelemetry exporter
dependency, and no admin-centre telemetry promise from this skill.

Microsoft Agent 365 may still provide platform-side observability for
registered agents via A365's own instrumentation; that path is outside this
repo's runtime.

Historical span vocabulary from the v0.1 design draft lives in
``docs/historical/SPEC-v0.1-draft.md`` §6.8. Hermes core observability uses
the separate ``hermes_otel`` gateway plugin when operators configure it.
