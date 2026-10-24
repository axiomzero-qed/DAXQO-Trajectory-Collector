# DAXQO Android trajectory fixture

A small, offline Android app for validating screen observation and action trajectories. It uses only platform widgets and requests no permissions or network access.

## Screens and interactions

- **商品列表** shows 30 clearly fictional rows (`商品 01`, `¥10` through `商品 30`, `¥300`) in a native, scrollable list.
- Tap any row to open **商品详情**, then tap **选择规格** to open the second-level **选择规格** page. Android Back returns one level at a time.
- **显示弹窗** opens a simulated dialog with a **关闭** button.
- The **隐私测试** page is separate from the product flow and contains a numeric password-style field labeled **验证码（模拟敏感字段）**, initialized with the fake value `654321`, for redaction checks only. Open it with the **隐私测试** button on the list; Android Back or **返回商品列表** returns to the list.
- The app also writes best-effort frame statistics to its private `files/fixture-frame-metrics.json` file every 16 callbacks and when paused. `android_frame_metrics` reports `total_frames`, mean `total_duration_ms`, `deadline_missed_frames` (measured durations over the refresh budget), and `drop_count` (Android-reported lost callbacks, not physical frame drops). The nested `fallback` uses `Choreographer` and reports `source: choreographer_gap_estimate`, `available`, `callback_samples`, `mean_callback_interval_ms`, and `missed_vsync_estimate`. This fallback estimates UI scheduling intervals only; it is not GPU render time or a physical dropped-frame count. Confirming physical frame drops requires a supported system trace. Both metrics include the display `refresh_rate_hz`; the outer object also includes `capture_elapsed_ms`. The app collects no UI text or images and requests no permissions. Counters and the file reset when the Activity starts.

## Build

Run `build-fixture.ps1 -ToolsRoot /path/to/android-tools` from this folder. The explicit tools root contains JDK 17 (`temurin-17/`), Gradle 8.10.2 and Android SDK. `DAXQO_ANDROID_TOOLS` can also select that root. The builder copies the debug APK to `dist/fixture.apk`.

Application ID: `io.daxqo.trajectoryfixture`.
