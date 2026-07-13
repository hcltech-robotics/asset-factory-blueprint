# Output contract

Return ToolResult with success, data, error, warnings, artefacts, proposals and validation_status.

The data payload validates against `mesh-verification-record.schema.json`. It records the candidate checksum, diagnostic metrics, render checksums, reviewer identity, structured decision, findings, attempt counters and promotion binding.

Approval publishes canonical geometry only for the recorded candidate checksum. Changed candidate bytes invalidate the record and require a new invocation.
