"""Stage the read-only v0.5 source with narrowly anchored trajectory hooks."""
from __future__ import annotations

import shutil
import argparse
import os
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
V05 = None
ADDONS = PROJECT / "integration"
VERSION_CODE = 8
STAGE = PROJECT / "dist" / "native-v05"

EXCLUDED = {".git", ".gradle", "build", "dist", "local.properties", ".tools", "__pycache__", "node_modules"}


def ignore_generated(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in EXCLUDED}


def replace_once(path: Path, old: str, new: str, *, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected one {label} anchor in {path}, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


def patch_file(path: Path, replacements: list[tuple[str, str, str]]) -> None:
    for old, new, label in replacements:
        replace_once(path, old, new, label=label)


def stage_android() -> None:
    app_java = STAGE / "android" / "app" / "src" / "main" / "java" / "io" / "daxqo" / "app"
    service = app_java / "DeviceAccessibilityService.java"
    patch_file(service, [
        ("    private DeviceHttpBridge bridge;\n", "    private DeviceHttpBridge bridge;\n    private TrajectoryRecorder trajectory;\n", "recorder field"),
        ("        current = this;\n        bridge = new DeviceHttpBridge(this);",
         "        current = this;\n        try { trajectory = new TrajectoryRecorder(this); } catch (Exception ignored) { trajectory = null; }\n        bridge = new DeviceHttpBridge(this);",
         "recorder initialization"),
        ("    @Override public void onAccessibilityEvent(AccessibilityEvent event) {\n",
         "    @Override public void onAccessibilityEvent(AccessibilityEvent event) {\n        try { if (trajectory != null) trajectory.event(event); } catch (Exception ignored) { }\n",
         "accessibility event collection"),
        ("        if (bridge != null) bridge.close();\n        screenshots.shutdownNow();",
         "        if (bridge != null) bridge.close();\n        if (trajectory != null) trajectory.close();\n        screenshots.shutdownNow();",
         "recorder shutdown"),
        ("    boolean bridgeListening() { return bridge != null && bridge.isListening(); }\n",
         "    boolean bridgeListening() { return bridge != null && bridge.isListening(); }\n    TrajectoryRecorder trajectory() { return trajectory; }\n",
         "recorder accessor"),
        ("            try { executeMain(id, action, actionArgs, response -> { result[0] = response; finished.countDown(); }); }\n",
         """            final TrajectoryRecorder.Scope observed = trajectory == null ? null : trajectory.begin(request);
            final long actionStarted = android.os.SystemClock.elapsedRealtime();
            try { executeMain(id, action, actionArgs, response -> {
                long actionMs = android.os.SystemClock.elapsedRealtime() - actionStarted;
                int settleMs = observed != null && response.optBoolean("ok") && !DeviceState.isReadOnly(action) ? 120 : 0;
                Runnable complete = () -> {
                    try {
                        if (observed != null) {
                            trajectory.finish(observed, response, actionMs, settleMs);
                            response.put("trajectory_action_id", id);
                        }
                    } catch (Exception ignored) { /* Collection cannot fail an Agent action. */ }
                    finally { result[0] = response; finished.countDown(); }
                };
                if (settleMs > 0) main.postDelayed(complete, settleMs); else complete.run();
            }); }
""", "action trajectory capture"),
        ("    private JSONObject tree() throws Exception {\n",
         """    // Collector reads use an independent snapshot and do not invalidate the Agent's node IDs.
    JSONObject trajectoryTree() throws Exception {
        int[] size = screenSize();
        AccessibilityNodeInfo root = getRootInActiveWindow();
        JSONArray nodes = new JSONArray();
        boolean[] truncated = {false};
        String pkg = root == null ? "" : value(root.getPackageName(), 200);
        if (root != null) { try { appendNode(root, "0", "", nodes, 0, truncated); } finally { root.recycle(); } }
        return new JSONObject().put("packageName", pkg).put("width", size[0]).put("height", size[1]).put("nodes", nodes).put("truncated", truncated[0]);
    }
    private JSONObject tree() throws Exception {
""", "separate collector tree"),
    ])

    bridge = app_java / "DeviceHttpBridge.java"
    patch_file(bridge, [
        ("            if (\"GET\".equals(start[0]) && start[1].startsWith(\"/api/device/apps\")) {\n",
         """            TrajectoryRecorder recorder = service.trajectory();
            if ("GET".equals(start[0]) && start[1].startsWith("/api/trajectory/")) {
                if (recorder == null) { reply(client, 503, error("collector_unavailable", "采集模块不可用，手机控制仍可使用")); return; }
                if ("/api/trajectory/status".equals(start[1])) { reply(client, 200, recorder.status()); return; }
                if (start[1].startsWith("/api/trajectory/batch?")) {
                    android.net.Uri uri = android.net.Uri.parse(start[1]);
                    try { reply(client, 200, recorder.batch(Long.parseLong(uri.getQueryParameter("after")), Integer.parseInt(uri.getQueryParameter("limit")))); }
                    catch (IllegalArgumentException ignored) { reply(client, 400, error("invalid_args", "批次参数无效")); }
                    return;
                }
                reply(client, 404, error("not_found", "采集接口不存在")); return;
            }
            if ("GET".equals(start[0]) && start[1].startsWith("/api/device/apps")) {
""", "trajectory GET routes"),
        ("            if (!\"POST\".equals(start[0]) || (!\"/api/device/execute\".equals(start[1]) && !approvalDecision)) { reply(client, 404, error(\"not_found\", \"接口不存在\")); return; }\n",
         "            boolean trajectoryRoute = start[1].matches(\"/api/trajectory/(config|ack|feedback)\");\n"
         "            if (!\"POST\".equals(start[0]) || (!\"/api/device/execute\".equals(start[1]) && !approvalDecision && !trajectoryRoute)) { reply(client, 404, error(\"not_found\", \"接口不存在\")); return; }\n",
         "trajectory POST route allowlist"),
        ("            if (approvalDecision) {\n                String approvalId = start[1].substring(\"/api/device/approvals/\".length());\n",
         """            if (trajectoryRoute) {
                if (recorder == null) { reply(client, 503, error("collector_unavailable", "采集模块不可用，手机控制仍可使用")); return; }
                try {
                    JSONObject value = start[1].endsWith("/config") ? recorder.configure(request)
                            : start[1].endsWith("/ack") ? recorder.ack(request) : recorder.feedback(request);
                    reply(client, 200, value);
                } catch (Exception ignored) { reply(client, 400, error("invalid_args", "采集参数或存储不可用")); }
                return;
            }
            if (approvalDecision) {
                String approvalId = start[1].substring("/api/device/approvals/".length());
""", "trajectory POST dispatch"),
    ])

    xml = STAGE / "android" / "app" / "src" / "main" / "res" / "xml" / "device_accessibility.xml"
    replace_once(xml,
        "android:accessibilityEventTypes=\"typeWindowStateChanged|typeWindowContentChanged|typeWindowsChanged\"",
        "android:accessibilityEventTypes=\"typeWindowStateChanged|typeWindowContentChanged|typeWindowsChanged|typeViewClicked|typeViewScrolled\"",
        label="click/scroll event types")

    gradle = STAGE / "android" / "app" / "build.gradle"
    patch_file(gradle, [
        ("        versionCode 5\n", f"        versionCode project.hasProperty('trajectoryBuild') ? {VERSION_CODE} : 5\n", "collector version code"),
    ])
    shutil.copy2(ADDONS / "android" / "TrajectoryRecorder.java",
                 app_java / "TrajectoryRecorder.java")
    # OEM guidance comes solely from the selected, versioned bridge checkout.


def stage_backend() -> None:
    backend = STAGE / "backend"
    bridge = backend / "android_mcp_server.py"
    patch_file(bridge, [
        ("    def __init__(self, code: str, message: str, approval: dict | None = None):\n"
         "        super().__init__(message)\n        self.code = code\n        self.approval = _public_value(approval) if isinstance(approval, dict) else None\n",
         "    def __init__(self, code: str, message: str, approval: dict | None = None, action_id: str | None = None):\n"
         "        super().__init__(message)\n        self.code = code\n        self.approval = _public_value(approval) if isinstance(approval, dict) else None\n        self.action_id = action_id\n",
         "BridgeError trajectory action id"),
        ("        route = (method, path) in ((\"GET\", \"/api/device/status\"), (\"GET\", \"/api/device/approvals\"),\n"
         "                                  (\"POST\", \"/api/device/execute\"))\n",
         "        route = (method, path) in ((\"GET\", \"/api/device/status\"), (\"GET\", \"/api/device/approvals\"),\n"
         "                                  (\"POST\", \"/api/device/execute\"), (\"POST\", \"/api/trajectory/feedback\"))\n",
         "feedback bridge allowlist"),
        ("            raise BridgeError(code[:80], message[:500], error.get(\"approval\"))\n",
         "            raise BridgeError(code[:80], message[:500], error.get(\"approval\"), response.get(\"trajectory_action_id\"))\n",
         "failed action id propagation"),
        ("        public = _public_value(result)\n        if action == \"screenshot\":\n",
         "        public = _public_value(result)\n        if response.get(\"trajectory_action_id\") == request_id:\n            public[\"trajectory_action_id\"] = request_id\n        if action == \"screenshot\":\n",
         "successful action id propagation"),
        ("                if exc.approval is not None: error[\"approval\"] = exc.approval\n                result = self._tool_result",
         "                if exc.approval is not None: error[\"approval\"] = exc.approval\n"
         "                if exc.action_id is not None: error[\"trajectory_action_id\"] = exc.action_id\n"
         "                result = self._tool_result",
         "MCP error action id"),
    ])

    server = backend / "server.py"
    replace_once(server,
        "        if method == \"item/completed\" and item.get(\"type\") == \"mcpToolCall\" and item.get(\"server\") == \"daxqo_android\":\n"
        "            self._record_android_tool(target, item)\n",
        "        if method == \"item/completed\" and item.get(\"type\") == \"mcpToolCall\" and item.get(\"server\") == \"daxqo_android\":\n"
        "            self._record_android_tool(target, item)\n"
        "            try:\n"
        "                from android_mcp_server import observe_engine_event\n"
        "                trajectory_task = dict(self.store.row(target))\n"
        "                trajectory_task[\"_trajectory_subgoal\"] = self._task_buffers.get(target, \"\")\n"
        "                observe_engine_event(self.state, trajectory_task, params)\n"
        "            except Exception:\n"
        "                pass  # Optional trajectory collection must not interrupt task execution.\n",
        label="app-server trajectory hook")
    shutil.copy2(ADDONS / "backend" / "trajectory_hooks.py", backend / "trajectory_hooks.py")
    # Keep the installed runtime's existing source allowlist: ship the optional
    # hook inside an already-allowed module, verified by the normal manifest.
    hooks = (ADDONS / "backend" / "trajectory_hooks.py").read_text(encoding="utf-8")
    hooks = hooks.replace("from __future__ import annotations\n", "")
    with bridge.open("a", encoding="utf-8") as output:
        output.write("\n\n# Optional trajectory feedback (embedded for existing update allowlists).\n" + hooks)


def main() -> int:
    global V05, VERSION_CODE, STAGE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=os.environ.get('DAXQO_ANDROID_SOURCE'),
                        help='Explicit read-only compatible DAXQO checkout (or DAXQO_ANDROID_SOURCE)')
    parser.add_argument('--stage', type=Path, default=STAGE)
    parser.add_argument('--version-code', type=int, default=8, help='Must exceed the currently installed APK code')
    args = parser.parse_args()
    if args.source is None:
        parser.error('Pass --source or set DAXQO_ANDROID_SOURCE; no local checkout is assumed')
    STAGE = args.stage.resolve()
    V05, VERSION_CODE = args.source.resolve(), args.version_code
    if not 6 <= VERSION_CODE <= 2_100_000_000: raise ValueError('Invalid collector APK version code')
    if not V05.is_dir():
        raise FileNotFoundError(f"Read-only v0.5 source not found: {V05}")
    if STAGE == V05 or V05 in STAGE.parents or STAGE in V05.parents:
        raise ValueError('Staging and source directories must be separate')
    STAGE.mkdir(parents=True, exist_ok=True)
    for directory in ("android", "backend", "scripts"):
        shutil.copytree(V05 / directory, STAGE / directory, dirs_exist_ok=True, ignore=ignore_generated)
    shutil.copy2(V05 / "VERSION", STAGE / "VERSION")
    stage_android()
    stage_backend()
    print(f"Prepared staged source: {STAGE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
