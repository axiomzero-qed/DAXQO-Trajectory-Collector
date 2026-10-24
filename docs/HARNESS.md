# Harness integration

## Public interfaces

Python: `trajectory.Dataset`, `trajectory.Recorder`, `trajectory.snapshot`.
CLI: `daxqo-collect` / `python -m trajectory`.
MCP: `daxqo-collect-mcp` / `python -m trajectory.mcp_server`.

All ingestion uses the same validation, privacy filtering and durable storage. Offline API/CLI/MCP use needs no DAXQO harness, Motor, ADB or model provider.

## MCP

Use [examples/mcp-host.json](../examples/mcp-host.json) with the host's standard stdio MCP configuration. Replace the executable with the absolute Python from the installed environment, choose Dataset/export paths and assign a stable device UUID.

Supported stdio revisions: 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25. Compatibility is tested using the independent official MCP Python client. No HTTP listener is exposed.

`trajectory_record` accepts task/action IDs, UTC start/finish times, an action and before/after observations. States contain `package_name`, `nodes`, `width`, `height`; the server adds schema metadata and fingerprints.

Set `--session-id` to a stable UUID for retries across process restarts. Keep IDs, timestamps, contents and source attribution identical. Conflicting record contents are rejected. Tool callers cannot mark themselves as independent human/fixture verification.

`trajectory_export` writes beneath `--export-dir` only. Its `name` is one directory name; no arbitrary path, deletion or device-control operation is offered.

## Python API

Reuse one Dataset for a group of actions and one Recorder per source/session. Record after the executor returns and the next state is observed. Preserve failures/uncertainty with `status="failed"` / `"unknown"`. Omit unprovided candidate lists/confidence and never supply hidden reasoning.

Advanced callers can use `Dataset.ingest(records)` directly. Recording backends can implement `trajectory.protocols.RecordingClient` for `sync_device(client, dataset)`; they must honor stream/sequence/ACK semantics.

## Ownership

This repository owns the trajectory schema, privacy policy, Dataset, Recorder and native observer. DAXQO/Motor consume a versioned release or explicit source checkout. Local datasets/cursors are operator-owned state, never distributed with source.

Protocol reference: https://modelcontextprotocol.io/docs/develop/connect-local-servers
