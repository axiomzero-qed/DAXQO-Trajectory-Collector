package io.daxqo.app;

import android.content.ContentValues;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.os.Build;
import android.os.SystemClock;
import android.view.accessibility.AccessibilityEvent;
import org.json.JSONArray;
import org.json.JSONObject;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.ArrayDeque;
import java.util.HashMap;
import java.util.Iterator;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

/** Optional, bounded, fail-open observer. Never changes phone authorization. */
final class TrajectoryRecorder {
    static final String VERSION = "daxqo.trajectory/0.1";
    private static final Pattern PRIVATE = Pattern.compile("(?i)(password|passwd|secret|token|api.?key|authorization|capability|验证码|密码|银行卡|信用卡|付款码|收款码|确认支付|确认付款|立即支付|私人聊天|verification code|one.?time.?code|card number|cvv|chat history)");
    private static final Pattern VALUE = Pattern.compile("(?i)([A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}|(?<![0-9])[0-9]{6,}(?![0-9])|(?:sk-|Bearer )[A-Za-z0-9_-]+)");
    private final DeviceAccessibilityService service;
    private final SharedPreferences prefs;
    private final SQLiteDatabase db;
    private final ThreadPoolExecutor writer = new ThreadPoolExecutor(1, 1, 0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(64));
    private final ArrayDeque<JSONObject> events = new ArrayDeque<>();
    private final Map<String, ArrayDeque<String>> histories = new HashMap<>();
    private final Map<String, String> previous = new HashMap<>();
    private volatile JSONObject config;
    private final String deviceId, streamId;
    private volatile long dropped, errors, written;
    private volatile String lastError = "";
    private volatile long eventTotal;

    TrajectoryRecorder(DeviceAccessibilityService service) throws Exception {
        this.service = service;
        prefs = service.getSharedPreferences("trajectory_private", 0);
        deviceId = prefs.getString("device_id", UUID.randomUUID().toString());
        streamId = prefs.getString("stream_id", UUID.randomUUID().toString());
        prefs.edit().putString("device_id", deviceId).putString("stream_id", streamId).commit();
        config = new JSONObject(prefs.getString("config", "{}"));
        db = service.openOrCreateDatabase("trajectory-v01.sqlite3", 0, null);
        db.enableWriteAheadLogging();
        db.execSQL("CREATE TABLE IF NOT EXISTS records(seq INTEGER PRIMARY KEY AUTOINCREMENT,record_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL,bytes INTEGER NOT NULL)");
        dropped = prefs.getLong("dropped", 0);
        errors = prefs.getLong("errors", 0);
    }

    boolean enabled() { return config.optBoolean("enabled", false); }
    // BEGIN SHARED PRIVATE MESSAGE POLICY
    private static final String[] PRIVATE_APP_PREFIXES = {
        "com.whatsapp", "org.telegram.messenger", "org.thunderdog.challegram",
        "org.thoughtcrime.securesms", "com.facebook.orca", "com.instagram.android",
        "com.snapchat.android", "com.viber.voip", "jp.naver.line.android",
        "com.tencent.wework", "com.alibaba.android.rimet", "com.ss.android.lark",
        "com.larksuite.suite", "com.microsoft.teams", "com.slack", "com.discord",
        "com.google.android.apps.messaging", "com.android.mms",
        "com.samsung.android.messaging", "com.miui.mms", "com.android.messaging",
        "com.google.android.apps.dynamite", "com.android.systemui"
    };
    private static final String[] LEGACY_PRIVATE_APP_TERMS = {
        "wechat", "weixin", "com.tencent.mm", "qq", "mail", "email", "gmail",
        "outlook", "pay", "wallet", "bank", "alipay", "payment", "password", ".gm",
        "authenticator", "contacts", "dialer"
    };
    private static final Pattern PRIVATE_MESSAGE_RESOURCE = Pattern.compile(
        "^(?:(?:tv|text|view|item|bubble|chat|conversation|sms|im|incoming|outgoing|received|sent)_)*"
        + "(?:message_(?:text|body|content)|(?:chat|sms)_(?:text|body|content)|chat_message)"
        + "(?:_view|_text_view|_label)?$");
    private static boolean privateApp(String name) {
        if (name == null || name.trim().isEmpty()) return true;
        String p = name.trim().toLowerCase(Locale.ROOT);
        for (String prefix : PRIVATE_APP_PREFIXES) if (p.equals(prefix) || p.startsWith(prefix + ".")) return true;
        for (String term : LEGACY_PRIVATE_APP_TERMS) if (p.contains(term)) return true;
        return false;
    }
    private static boolean hasMessageBodySemantic(String value) {
        if (value == null) return false;
        String name = value.trim();
        name = name.substring(name.lastIndexOf('/') + 1);
        name = name.replaceAll("([A-Z]+)([A-Z][a-z])", "$1_$2");
        name = name.replaceAll("([a-z0-9])([A-Z])", "$1_$2");
        name = name.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9]+", "_").replaceAll("^_+|_+$", "");
        return PRIVATE_MESSAGE_RESOURCE.matcher(name).matches();
    }
    // END SHARED PRIVATE MESSAGE POLICY
    private boolean allowed(String name) {
        if (privateApp(name)) return false;
        JSONArray packages = config.optJSONArray("allowed_packages");
        if (packages != null) for (int i = 0; i < packages.length(); i++) if (name.equals(packages.optString(i))) return true;
        return false;
    }
    static String text(String source) {
        if (source == null) return "";
        if (source.matches("[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")) return source;
        if (PRIVATE.matcher(source).find()) return "[REDACTED]";
        String safe = VALUE.matcher(source).replaceAll("[REDACTED]");
        return safe.substring(0, Math.min(1000, safe.length()));
    }
    static Object sanitize(Object input) throws Exception {
        if (input instanceof JSONObject) {
            JSONObject out = new JSONObject();
            Iterator<String> keys = ((JSONObject) input).keys();
            while (keys.hasNext()) {
                String key = keys.next();
                if (PRIVATE.matcher(key).find() || hasMessageBodySemantic(key) || key.equals("image") || key.equals("data") || key.equals("text")) out.put(key, "[REDACTED]");
                else out.put(key, sanitize(((JSONObject) input).opt(key)));
            }
            return out;
        }
        if (input instanceof JSONArray) {
            JSONArray out = new JSONArray();
            JSONArray arr = (JSONArray) input;
            for (int i = 0; i < Math.min(arr.length(), 400); i++) out.put(sanitize(arr.opt(i)));
            return out;
        }
        if (input instanceof String) return text((String) input);
        return input;
    }
    static String hash(String value) throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
        StringBuilder out = new StringBuilder();
        for (byte b : digest) out.append(String.format(Locale.ROOT, "%02x", b & 255));
        return out.toString();
    }
    private JSONObject task(JSONObject input) throws Exception {
        JSONObject out = input == null ? new JSONObject() : (JSONObject) sanitize(input);
        if (!out.has("id")) out.put("id", config.optString("session_id", streamId));
        for (String key : new String[]{"user_task", "subgoal", "stage", "source"}) if (!out.has(key)) out.put(key, "");
        return out;
    }

    /** Input is an independent tree; it must never replace the Agent's node snapshot. */
    JSONObject state(JSONObject tree) throws Exception {
        String pkg = tree.optString("packageName");
        boolean include = allowed(pkg);
        boolean sensitive = false;
        JSONArray input = tree.optJSONArray("nodes"), nodes = new JSONArray();
        if (input != null && include) for (int i = 0; i < input.length(); i++) {
            JSONObject node = input.getJSONObject(i);
            if (node.optBoolean("password") || PRIVATE.matcher(node.optString("text") + " " + node.optString("description") + " " + node.optString("viewId")).find()) sensitive = true;
            for (String key : new String[]{"viewId", "resourceId", "resource_id", "view_id", "id", "resourceName", "resource_name"})
                if (hasMessageBodySemantic(node.optString(key))) sensitive = true;
            JSONObject safe = new JSONObject(node.toString());
            safe.remove("selectionStart"); safe.remove("selectionEnd");
            safe.put("text", node.optBoolean("editable") || node.optBoolean("password") ? "[REDACTED]" : text(node.optString("text")));
            safe.put("description", node.optBoolean("editable") || node.optBoolean("password") ? "[REDACTED]" : text(node.optString("description")));
            nodes.put(safe);
        }
        if (sensitive) nodes = new JSONArray();
        boolean train = include && !sensitive && nodes.length() > 0 && !tree.optBoolean("truncated");
        JSONObject out = new JSONObject().put("package_name", pkg).put("width", tree.optInt("width")).put("height", tree.optInt("height"))
                .put("nodes", nodes).put("redacted", true).put("training_allowed", train).put("truncated", tree.optBoolean("truncated"))
                .put("screen_state", DeviceState.locked(service) ? "locked" : "unlocked")
                .put("exclusion_reason", !include ? "package_not_allowed" : sensitive ? "sensitive_screen" : train ? "" : "empty_or_truncated");
        // snapshotId/time are deliberately absent from the stable state fingerprint.
        out.put("fingerprint", hash(pkg + "|" + tree.optInt("width") + "|" + tree.optInt("height") + "|" + nodes));
        out.put("captured_at", Instant.now().toString());
        return out;
    }

    void event(AccessibilityEvent event) {
        if (!enabled() || event == null) return;
        eventTotal++;
        String pkg = event.getPackageName() == null ? "" : event.getPackageName().toString();
        if (!allowed(pkg)) return;
        try {
            JSONObject item = new JSONObject().put("type", AccessibilityEvent.eventTypeToString(event.getEventType()))
                    .put("timestamp", Instant.now().toString()).put("package_name", pkg).put("event_time_ms", event.getEventTime());
            synchronized (events) {
                if (events.size() >= 64) events.removeFirst();
                events.addLast(item);
            }
        } catch (Exception ignored) { errors++; }
    }

    static final class Scope {
        JSONObject request, before;
        String startedAt;
        long start, treeMs, observerBeforeMs, eventStart;
    }
    Scope begin(JSONObject request) {
        if (!enabled() || "status".equals(request.optString("action"))) return null;
        try {
            Scope s = new Scope(); s.start = SystemClock.elapsedRealtime(); s.startedAt = Instant.now().toString();
            s.eventStart = eventTotal; s.request = new JSONObject(request.toString());
            if (!"tree".equals(request.optString("action"))) {
                long queryStart = SystemClock.elapsedRealtime();
                JSONObject raw = service.trajectoryTree();
                s.treeMs = SystemClock.elapsedRealtime() - queryStart;
                s.before = state(raw);
            }
            s.observerBeforeMs = SystemClock.elapsedRealtime() - s.start;
            return s;
        } catch (Exception ignored) { errors++; lastError = "before_capture_failed"; return null; }
    }
    void finish(Scope scope, JSONObject response, long actionMs, int settleMs) {
        if (scope == null) return;
        try {
            long capture = SystemClock.elapsedRealtime();
            boolean reuseTree = "tree".equals(scope.request.optString("action")) && response.optBoolean("ok");
            JSONObject rawAfter = reuseTree ? response.getJSONObject("result") : service.trajectoryTree();
            long afterQueryMs = reuseTree ? 0 : SystemClock.elapsedRealtime() - capture;
            JSONObject after = state(rawAfter);
            JSONObject before = scope.before == null ? after : scope.before;
            JSONObject context = scope.request.optJSONObject("trajectory");
            if (context == null) context = new JSONObject();
            JSONObject task = context.optJSONObject("task");
            if (task == null) task = config.optJSONObject("task");
            if (task == null) task = new JSONObject().put("id", config.optString("session_id", streamId)).put("source", "unattributed");
            task = task(task);
            if (!task.has("id")) task.put("id", config.optString("session_id", streamId));
            String actionId = scope.request.optString("id"), taskId = task.optString("id");
            JSONObject args = scope.request.optJSONObject("args");
            if (args == null) args = new JSONObject();
            String type = scope.request.optString("action");
            JSONObject target = new JSONObject();
            String nodeId = args.optString("nodeId");
            JSONArray nodes = before.optJSONArray("nodes");
            if (nodes != null) for (int i = 0; i < nodes.length(); i++) if (nodeId.equals(nodes.getJSONObject(i).optString("id"))) { target = nodes.getJSONObject(i); break; }
            JSONObject decision = context.optJSONObject("decision");
            if (decision == null) decision = new JSONObject().put("source", "unattributed").put("candidate_actions", new JSONArray()).put("confidence", JSONObject.NULL);
            JSONObject result = new JSONObject().put("status", response.optBoolean("ok") ? "succeeded" : "failed")
                    .put("verification", new JSONObject().put("status", "unverified"));
            JSONObject error = response.optJSONObject("error");
            if (error != null) {
                String code = error.optString("code"); result.put("error_code", code);
                if (code.equals("timeout") || code.equals("outcome_unknown")) result.put("status", "unknown");
            }
            JSONObject nativeResult = response.optJSONObject("result");
            if (nativeResult != null) {
                JSONObject actual = new JSONObject();
                for (String key : new String[]{"performed", "launched", "method", "x", "y", "nodeId", "snapshotId", "packageName"}) if (nativeResult.has(key)) actual.put(key, nativeResult.opt(key));
                result.put("actual", actual);
            }
            JSONArray recent = new JSONArray();
            synchronized (events) { for (JSONObject e : events) if (e.optLong("event_time_ms") >= scope.start) recent.put(e); }
            String from = before.getString("fingerprint"), to = after.getString("fingerprint");
            ArrayDeque<String> history = histories.get(taskId);
            if (history == null && histories.size() >= 128) { String oldest = histories.keySet().iterator().next(); histories.remove(oldest); previous.remove(oldest); }
            if (history == null) { history = new ArrayDeque<>(); histories.put(taskId, history); }
            if (history.isEmpty()) history.addLast(from);
            if (!from.equals(to)) { if (history.size() >= 64) history.removeFirst(); history.addLast(to); }
            JSONArray nav = new JSONArray(); for (String p : history) nav.put(p);
            boolean train = before.optBoolean("training_allowed") && after.optBoolean("training_allowed");
            JSONObject safeDecision = (JSONObject) sanitize(decision);
            JSONObject safeArgs = (JSONObject) sanitize(args);
            boolean sensitiveRecord = privateApp(before.optString("package_name")) || privateApp(after.optString("package_name"))
                    || "sensitive_screen".equals(before.optString("exclusion_reason")) || "sensitive_screen".equals(after.optString("exclusion_reason"));
            if (sensitiveRecord) {
                target = new JSONObject(); safeArgs = new JSONObject();
                JSONObject metadata = new JSONObject();
                for (String key : new String[]{"source", "model", "tool_call_id", "tool", "confidence"})
                    if (safeDecision.has(key)) metadata.put(key, safeDecision.opt(key));
                safeDecision = metadata;
                safeDecision.put("candidate_actions", new JSONArray()).put("selected_action", new JSONObject());
                task.put("user_task", "[REDACTED]").put("subgoal", "[REDACTED]");
            }
            JSONObject record = base("action", actionId, task).put("started_at", scope.startedAt).put("before", before).put("after", after)
                    .put("action", new JSONObject().put("type", type).put("target", target).put("parameters", safeArgs))
                    .put("decision", safeDecision).put("result", result)
                    .put("navigation", new JSONObject().put("from", from).put("to", to).put("changed", !from.equals(to))
                            .put("previous_action_id", previous.containsKey(taskId) ? previous.get(taskId) : JSONObject.NULL).put("history", nav))
                    .put("events", recent).put("privacy", new JSONObject().put("redacted", true).put("excluded", !train)
                            .put("reasons", new JSONArray().put(before.optString("exclusion_reason")).put(after.optString("exclusion_reason"))))
                    .put("metrics", new JSONObject().put("action_ms", actionMs).put("tree_ms", scope.treeMs + afterQueryMs)
                            .put("settle_ms", settleMs).put("observer_ms", scope.observerBeforeMs + SystemClock.elapsedRealtime() - capture)
                            .put("total_ms", SystemClock.elapsedRealtime() - scope.start).put("event_count", eventTotal - scope.eventStart)
                            .put("java_heap_bytes", Runtime.getRuntime().totalMemory() - Runtime.getRuntime().freeMemory()).put("queue_size", writer.getQueue().size()));
            // Opt-in screenshots only from an explicit Agent screenshot, never a timer.
            // Any editable node blocks pixels, including fields without password flags.
            boolean editable = false;
            for (int i = 0; nodes != null && i < nodes.length(); i++) if (nodes.getJSONObject(i).optBoolean("editable")) editable = true;
            if (type.equals("screenshot") && config.optBoolean("screenshots", false) && train && !editable && nativeResult != null && nativeResult.has("image")) record.put("screenshot", nativeResult.getJSONObject("image"));
            previous.put(taskId, actionId);
            record.getJSONObject("metrics").put("observer_ms", scope.observerBeforeMs + SystemClock.elapsedRealtime() - capture);
            enqueue(record);
        } catch (Exception ignored) { errors++; lastError = "after_capture_failed"; }
    }
    private JSONObject base(String kind, String actionId, JSONObject task) throws Exception {
        return new JSONObject().put("schema_version", VERSION).put("record_id", UUID.randomUUID().toString()).put("record_type", kind)
                .put("device", new JSONObject().put("id", deviceId).put("model", Build.MODEL).put("manufacturer", Build.MANUFACTURER)
                        .put("os_version", Build.VERSION.RELEASE).put("sdk", Build.VERSION.SDK_INT).put("platform", service.getString(R.string.platform_id)))
                .put("session_id", config.optString("session_id", streamId)).put("task", task).put("action_id", actionId)
                .put("started_at", Instant.now().toString()).put("finished_at", Instant.now().toString());
    }
    private void enqueue(JSONObject record) {
        final String payload = record.toString();
        try { writer.execute(() -> {
            try {
                long limit = config.optLong("max_bytes", 64L * 1024 * 1024);
                int bytes = payload.getBytes(StandardCharsets.UTF_8).length;
                synchronized (db) {
                    long used = scalar("SELECT COALESCE(SUM(bytes),0) FROM records");
                    if (bytes > 2 * 1024 * 1024 || used + bytes > limit || scalar("SELECT COUNT(*) FROM records") >= 10000) { dropped++; lastError = "buffer_full"; return; }
                    ContentValues v = new ContentValues(); v.put("record_id", record.optString("record_id")); v.put("payload", payload); v.put("bytes", bytes);
                    db.insertOrThrow("records", null, v); written++;
                }
            } catch (Exception ignored) { errors++; lastError = "storage_error"; }
            finally { prefs.edit().putLong("dropped", dropped).putLong("errors", errors).apply(); }
        }); } catch (java.util.concurrent.RejectedExecutionException ignored) { dropped++; lastError = "queue_full"; }
    }
    private long scalar(String sql) {
        try (Cursor c = db.rawQuery(sql, null)) { return c.moveToFirst() ? c.getLong(0) : 0; }
    }
    JSONObject status() throws Exception {
        synchronized (db) {
            return new JSONObject().put("schema_version", VERSION).put("device_id", deviceId).put("stream_id", streamId)
                    .put("enabled", enabled()).put("screenshots", config.optBoolean("screenshots", false))
                    .put("session_id", config.optString("session_id", streamId)).put("task", config.optJSONObject("task"))
                    .put("allowed_packages", config.optJSONArray("allowed_packages")).put("pending", scalar("SELECT COUNT(*) FROM records"))
                    .put("buffer_bytes", scalar("SELECT COALESCE(SUM(bytes),0) FROM records")).put("dropped", dropped).put("errors", errors)
                    .put("written_this_process", written).put("queue_size", writer.getQueue().size()).put("last_error", lastError)
                    .put("last_sequence", scalar("SELECT COALESCE(MAX(seq),0) FROM records"));
        }
    }
    JSONObject configure(JSONObject body) throws Exception {
        JSONObject next = new JSONObject(config.toString());
        for (String key : new String[]{"enabled", "screenshots"}) if (body.has(key)) {
            if (!(body.opt(key) instanceof Boolean)) throw new IllegalArgumentException("boolean required");
            next.put(key, body.getBoolean(key));
        }
        if (body.has("allowed_packages")) {
            JSONArray input = body.getJSONArray("allowed_packages");
            if (input.length() > 64) throw new IllegalArgumentException("too many packages");
            for (int i = 0; i < input.length(); i++) if (!input.getString(i).matches("[A-Za-z0-9_]+(\\.[A-Za-z0-9_]+)+") || privateApp(input.getString(i))) throw new IllegalArgumentException("private/invalid package");
            next.put("allowed_packages", input);
        }
        if (body.has("task")) { next.put("task", task(body.getJSONObject("task"))); next.put("session_id", UUID.randomUUID().toString()); }
        if (body.has("max_bytes")) next.put("max_bytes", Math.max(1024L, Math.min(256L * 1024 * 1024, body.getLong("max_bytes"))));
        if (!next.has("session_id")) next.put("session_id", UUID.randomUUID().toString());
        if (!prefs.edit().putString("config", next.toString()).commit()) throw new IllegalStateException("config persistence failed");
        config = next;
        return status();
    }
    JSONObject batch(long after, int limit) throws Exception {
        JSONArray rows = new JSONArray(), sequences = new JSONArray(); long end = after;
        synchronized (db) {
            try (Cursor c = db.rawQuery("SELECT seq,payload FROM records WHERE seq>? ORDER BY seq LIMIT ?", new String[]{Long.toString(Math.max(0, after)), Integer.toString(Math.max(1, Math.min(20, limit)))})) {
                int bytes = 0;
                while (c.moveToNext()) { String payload = c.getString(1); bytes += payload.length(); if (bytes > 3 * 1024 * 1024 && rows.length() > 0) break; end = c.getLong(0); rows.put(new JSONObject(payload)); sequences.put(end); }
            }
        }
        return new JSONObject().put("stream_id", streamId).put("device_id", deviceId).put("after", after).put("through", end).put("records", rows).put("sequences", sequences);
    }
    JSONObject ack(JSONObject body) throws Exception {
        if (!streamId.equals(body.optString("stream_id"))) throw new IllegalArgumentException("stream mismatch");
        long through = body.getLong("through");
        synchronized (db) { db.delete("records", "seq<=?", new String[]{Long.toString(through)}); }
        return new JSONObject().put("acknowledged", through);
    }
    JSONObject feedback(JSONObject body) throws Exception {
        if (!enabled()) return new JSONObject().put("recorded", false);
        String actionId = body.optString("action_id");
        if (!actionId.matches("[A-Za-z0-9_.:-]{1,128}")) throw new IllegalArgumentException("invalid action id");
        JSONObject task = body.optJSONObject("task");
        if (task == null) task = config.optJSONObject("task");
        if (task == null) task = new JSONObject().put("id", config.optString("session_id", streamId));
        JSONObject record = base("agent_feedback", actionId, task(task));
            for (String key : new String[]{"decision", "verification"}) if (body.has(key)) record.put(key, sanitize(body.getJSONObject(key)));
        record.put("privacy", new JSONObject().put("redacted", true).put("excluded", false).put("reasons", new JSONArray()));
        enqueue(record);
        return new JSONObject().put("recorded", true).put("action_id", actionId);
    }
    void close() { writer.shutdown(); /* SQLite is owned by the process; queued writes can drain. */ }
}
