# Optional Android integration

Dataset/Recorder/CLI/MCP are harness-independent. `integration/android/TrajectoryRecorder.java` adapts the DAXQO Android bridge.

## Explicit source and reproducible staging

Compatible public DAXQO base: commit `3de65f9e9751c67d7118e72e789f898901a50ce6` at https://github.com/axiomzero-qed/DAXQO. It predates this overlay. Changed anchors or double application are rejected.

```sh
git clone https://github.com/axiomzero-qed/DAXQO.git /path/to/daxqo
git -C /path/to/daxqo checkout 3de65f9e9751c67d7118e72e789f898901a50ce6
python scripts/prepare_native.py --source /path/to/daxqo --stage ./dist/native-v05 --version-code 8
```

Choose a versionCode higher than the installed APK. Source/staging paths must be separate; the source stays read-only. OEM behavior comes only from that selected source.

Windows: `scripts/build-native.ps1 -Platform standard -Source /path/to/daxqo -ToolsRoot /path/to/android-tools`. The tools root contains `temurin-17/`, `android-sdk/`, `gradle-8.10.2/`; `DAXQO_ANDROID_TOOLS` can select it explicitly. Use `-Platform xm` for XM.

Other hosts can build the staged project with JDK 17, Gradle 8.10.2 and a configured SDK: `gradle -p dist/native-v05/android -Pplatform=standard -PtrajectoryBuild=true assembleDebug lintDebug`.

This repository owns the canonical native observer and backend hook. No developer home directory, worktree or sibling source is scanned.

## Runtime

The observer extends paired loopback `/api/trajectory/*` routes. Existing owner authorization, executor and idempotency remain intact. Observations do not invalidate the active Agent's node snapshots.

Backend hooks attach public task/model context by native action ID. Generic harnesses may use Recorder/MCP directly or implement RecordingClient without DAXQO's agent server.

Collection starts disabled, uses app allowlists and retains privacy gates. Screenshots require explicit opt-in and an original Agent screenshot request.

## Durable receive

1. GET `/api/trajectory/status`: stable device/stream IDs.
2. GET `/api/trajectory/batch?after=N&limit=20`: ordered batch.
3. Commit validated records and receiver cursor durably.
4. POST `/api/trajectory/ack`: acknowledge only committed sequences.

Use one coordinated receiver/Dataset per device stream. Merge old datasets read-only with `daxqo-collect --data TARGET merge --source SOURCE`.

Motor's Android extension builds on the Collector staging. A Collector-only APK cannot replace a Motor-enabled APK without removing its extensions; use the combined Motor build when needed. Python-only recording does not change APKs.
