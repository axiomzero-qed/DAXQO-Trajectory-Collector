# DAXQO Trajectory Collector

Local GUI action recording, privacy filtering, replay and dataset export for **any harness**.

本地 GUI Agent 轨迹采集工具，可通过 Python API、命令行或 MCP 接入不同 harness。数据处理无需运行 DAXQO 或 Motor；Android 原生观察器另有明确的 bridge 依赖。

- Python 3.10+, standard-library-only runtime, MIT license.
- Durable JSONL + SQLite, conflict detection, idempotent ingestion and leakage-aware exports.
- Sensitive UI/input values and hidden reasoning filtered before persistence.
- No model provider, cloud upload or training service included.

## Install

```sh
python -m pip install "git+https://github.com/axiomzero-qed/DAXQO-Trajectory-Collector.git@v0.1.0"
```

From a source checkout: `python -m pip install .`. No sibling checkout is needed. [Motor](https://github.com/axiomzero-qed/DAXQO-Motor) is optional.

## Use from another harness

```python
from pathlib import Path
from trajectory import Dataset, Recorder, snapshot

with Dataset(Path("./data")) as dataset:
    writer = Recorder(dataset,
        device_id="b683d45a-e298-40b3-90c2-3bcd8f59cb84",
        session_id="7468c2e4-dae4-4d3b-b959-b6e671f6f951",
        source="my_harness")
    receipt = writer.record_action(
        task_id="example-task", action_id="example-action",
        action={"type": "click", "target": {"resource_id": "details"}},
        before=snapshot("demo.app", [{"text": "Details"}]),
        after=snapshot("demo.app", [{"text": "Demo details"}]),
        status="succeeded",
        started_at="2026-10-24T10:00:00Z",
        finished_at="2026-10-24T10:00:01Z")
```

This records **synthetic observations** and performs no device action. In production, supply observations/outcomes from the existing executor and assign your own stable device UUID. Keep the device/session/action IDs, source, timestamps and contents unchanged when retrying; conflicting retries are rejected.

Reported success remains an unverified agent claim. Independent human/fixture verification is required for positive training examples by default.

## CLI and MCP

```sh
daxqo-collect --data ./data stats
daxqo-collect --data ./data import ./actions.jsonl
daxqo-collect --data ./data replay --output ./dist/replay.html
daxqo-collect --data ./data export --output ./exports/scoring --split-by task
python -m trajectory.mcp_server --data ./data --device-id b683d45a-e298-40b3-90c2-3bcd8f59cb84
```

MCP tools: `trajectory_record`, `trajectory_stats`, `trajectory_export`. They record supplied actions; they never execute phone actions. See [harness integration](docs/HARNESS.md), [host config](examples/mcp-host.json) and [schema](schema/trajectory-v0.1.schema.json).

## Optional Android observer

USB collection needs a compatible DAXQO Android bridge, an authorized ADB connection and the owner's existing phone-control permissions. No root is required. The core API/MCP path above needs no ADB, APK or DAXQO harness.

```sh
daxqo-collect --adb /path/to/adb --serial DEVICE_SERIAL devices
daxqo-collect --adb /path/to/adb --serial DEVICE_SERIAL --data ./data start --task "Browse the fixture" --allow-app io.daxqo.trajectoryfixture
daxqo-collect --adb /path/to/adb --serial DEVICE_SERIAL --data ./data watch
```

Coordinate one ACK receiver and one explicit Dataset root per device stream. Replay only inspects records; it never repeats historical actions. See [Android build/integration](docs/INTEGRATION.md). Native observer sources are canonical in this repository.

## Development

```sh
python -m pip install ".[dev]"
python -m unittest discover -s tests
python -m build
python examples/mcp_smoke.py
```

The independent official MCP client smoke test uses synthetic inputs and an isolated working directory. [Historical device results](docs/ACCEPTANCE.md) do not replace acceptance on a new device/harness.

Source releases exclude device data, credentials, generated reports, APKs and website sources. See [dataset behavior](docs/DATASET.md).
