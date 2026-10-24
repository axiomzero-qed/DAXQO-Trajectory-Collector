package io.daxqo.trajectoryfixture;

import android.app.Activity;
import android.app.AlertDialog;
import android.graphics.Color;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Bundle;
import android.os.SystemClock;
import android.view.Choreographer;
import android.view.Display;
import android.view.FrameMetrics;
import android.view.Gravity;
import android.view.View;
import android.view.Window;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import org.json.JSONException;
import org.json.JSONObject;
import java.io.FileOutputStream;

public final class MainActivity extends Activity {
    private static final int PAGE_LIST = 0;
    private static final int PAGE_DETAIL = 1;
    private static final int PAGE_VARIANTS = 2;
    private static final int PAGE_PRIVACY = 3;
    private int page = PAGE_LIST;
    private int selectedProduct = 1;
    private LinearLayout root;
    private final Object frameMetricsLock = new Object();
    private HandlerThread frameMetricsThread;
    private Handler frameMetricsHandler;
    private Window.OnFrameMetricsAvailableListener frameMetricsListener;
    private Choreographer.FrameCallback choreographerCallback;
    private boolean fallbackCallbackRunning;
    private long frameMetricsStartedAt;
    private long totalFrames;
    private long durationNanos;
    private long deadlineMissedFrames;
    private long callbackDropCount;
    private float refreshRateHz;
    private long fallbackCallbackCount;
    private long fallbackIntervalSamples;
    private long fallbackIntervalNanos;
    private long fallbackMissedVsyncEstimate;
    private long lastFallbackFrameTimeNanos;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        resetAndStartFrameMetrics();
        if (state != null) {
            page = state.getInt("page", PAGE_LIST);
            selectedProduct = state.getInt("product", 1);
        }
        render();
    }

    private void resetAndStartFrameMetrics() {
        synchronized (frameMetricsLock) {
            totalFrames = 0;
            durationNanos = 0;
            deadlineMissedFrames = 0;
            callbackDropCount = 0;
            frameMetricsStartedAt = SystemClock.elapsedRealtime();
            refreshRateHz = readRefreshRate();
        }
        deleteFile("fixture-frame-metrics.json");
        frameMetricsThread = new HandlerThread("fixture-frame-metrics");
        frameMetricsThread.start();
        frameMetricsHandler = new Handler(frameMetricsThread.getLooper());
        frameMetricsListener = (window, metrics, droppedReports) -> {
            long frameDuration = metrics.getMetric(FrameMetrics.TOTAL_DURATION);
            synchronized (frameMetricsLock) {
                refreshRateHz = readRefreshRate();
                totalFrames++;
                durationNanos += Math.max(0L, frameDuration);
                callbackDropCount += Math.max(0, droppedReports);
                double budgetNanos = refreshRateHz > 0f ? 1_000_000_000d / refreshRateHz : 0d;
                if (budgetNanos > 0d && frameDuration > budgetNanos) deadlineMissedFrames++;
                if (totalFrames % 16 == 0) writeFrameMetricsSnapshotLocked();
            }
        };
        choreographerCallback = this::onChoreographerFrame;
        getWindow().addOnFrameMetricsAvailableListener(frameMetricsListener, frameMetricsHandler);
    }

    private void onChoreographerFrame(long frameTimeNanos) {
        if (!fallbackCallbackRunning) return;
        boolean writeSnapshot = false;
        synchronized (frameMetricsLock) {
            fallbackCallbackCount++;
            refreshRateHz = readRefreshRate();
            if (lastFallbackFrameTimeNanos != 0L && frameTimeNanos > lastFallbackFrameTimeNanos) {
                long interval = frameTimeNanos - lastFallbackFrameTimeNanos;
                fallbackIntervalSamples++;
                fallbackIntervalNanos += interval;
                double budgetNanos = refreshRateHz > 0f ? 1_000_000_000d / refreshRateHz : 0d;
                if (budgetNanos > 0d && interval > budgetNanos) {
                    fallbackMissedVsyncEstimate += Math.max(0L, Math.round(interval / budgetNanos) - 1L);
                }
            }
            lastFallbackFrameTimeNanos = frameTimeNanos;
            writeSnapshot = fallbackCallbackCount % 16 == 0;
        }
        if (writeSnapshot && frameMetricsHandler != null) frameMetricsHandler.post(this::writeFrameMetricsSnapshot);
        if (fallbackCallbackRunning) Choreographer.getInstance().postFrameCallback(choreographerCallback);
    }

    private void startFallbackCallbacks() {
        fallbackCallbackRunning = true;
        lastFallbackFrameTimeNanos = 0L;
        Choreographer.getInstance().postFrameCallback(choreographerCallback);
    }

    private void stopFallbackCallbacks() {
        fallbackCallbackRunning = false;
        Choreographer.getInstance().removeFrameCallback(choreographerCallback);
    }

    private float readRefreshRate() {
        Display display = getWindowManager().getDefaultDisplay();
        return display == null ? 0f : display.getRefreshRate();
    }

    private void writeFrameMetricsSnapshot() {
        synchronized (frameMetricsLock) {
            writeFrameMetricsSnapshotLocked();
        }
    }

    private void writeFrameMetricsSnapshotLocked() {
        JSONObject snapshot = new JSONObject();
        try {
            snapshot.put("source", "android_frame_metrics");
            snapshot.put("total_frames", totalFrames);
            snapshot.put("total_duration_ms", totalFrames == 0 ? 0d : durationNanos / 1_000_000d / totalFrames);
            snapshot.put("deadline_missed_frames", deadlineMissedFrames);
            snapshot.put("drop_count", callbackDropCount);
            snapshot.put("refresh_rate_hz", refreshRateHz);
            snapshot.put("capture_elapsed_ms", Math.max(0L, SystemClock.elapsedRealtime() - frameMetricsStartedAt));
            JSONObject fallback = new JSONObject();
            fallback.put("source", "choreographer_gap_estimate");
            fallback.put("available", fallbackIntervalSamples > 0);
            fallback.put("callback_samples", fallbackIntervalSamples);
            fallback.put("mean_callback_interval_ms", fallbackIntervalSamples == 0 ? JSONObject.NULL
                    : fallbackIntervalNanos / 1_000_000d / fallbackIntervalSamples);
            fallback.put("missed_vsync_estimate", fallbackMissedVsyncEstimate);
            snapshot.put("fallback", fallback);
            try (FileOutputStream output = openFileOutput("fixture-frame-metrics.json", MODE_PRIVATE)) {
                output.write(snapshot.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
                output.flush();
            } catch (Exception ignored) {
                // Metrics are best-effort and must not affect the fixture UI.
            }
        } catch (JSONException ignored) {
            // All values above are JSON primitives; retain UI behavior if serialization fails.
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        startFallbackCallbacks();
    }

    @Override
    protected void onPause() {
        stopFallbackCallbacks();
        if (frameMetricsHandler != null) frameMetricsHandler.post(this::writeFrameMetricsSnapshot);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        stopFallbackCallbacks();
        if (frameMetricsListener != null) getWindow().removeOnFrameMetricsAvailableListener(frameMetricsListener);
        if (frameMetricsThread != null) frameMetricsThread.quitSafely();
        super.onDestroy();
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        out.putInt("page", page);
        out.putInt("product", selectedProduct);
        super.onSaveInstanceState(out);
    }

    @Override
    public void onBackPressed() {
        if (page == PAGE_VARIANTS) page = PAGE_DETAIL;
        else if (page == PAGE_DETAIL) page = PAGE_LIST;
        else if (page == PAGE_PRIVACY) page = PAGE_LIST;
        else { super.onBackPressed(); return; }
        render();
    }

    private void render() {
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.rgb(247, 248, 250));
        root.setPadding(dp(18), dp(16), dp(18), dp(12));
        setContentView(root);

        String heading = page == PAGE_LIST ? "商品列表" : page == PAGE_DETAIL ? "商品详情"
                : page == PAGE_VARIANTS ? "选择规格" : "隐私测试";
        TextView title = new TextView(this);
        title.setText("DAXQO 轨迹验收\n" + heading);
        title.setTextSize(22);
        title.setTextColor(Color.rgb(28, 43, 62));
        title.setPadding(0, 0, 0, dp(10));
        root.addView(title, new LinearLayout.LayoutParams(-1, -2));

        if (page == PAGE_LIST) renderList();
        else if (page == PAGE_DETAIL) renderDetail();
        else if (page == PAGE_VARIANTS) renderVariants();
        else renderPrivacy();
    }

    private void renderList() {
        Button popup = new Button(this);
        popup.setText("显示弹窗");
        popup.setOnClickListener(v -> new AlertDialog.Builder(this)
                .setTitle("提示")
                .setMessage("这是用于轨迹验收的模拟弹窗。")
                .setPositiveButton("关闭", (dialog, which) -> dialog.dismiss())
                .show());
        root.addView(popup, new LinearLayout.LayoutParams(-1, -2));

        Button privacy = new Button(this);
        privacy.setText("隐私测试");
        privacy.setOnClickListener(v -> { page = PAGE_PRIVACY; render(); });
        root.addView(privacy, new LinearLayout.LayoutParams(-1, -2));

        TextView listLabel = new TextView(this);
        listLabel.setText("全部商品（向下滚动查看更多）");
        listLabel.setTextSize(17);
        listLabel.setTextColor(Color.rgb(44, 62, 80));
        listLabel.setPadding(0, dp(12), 0, dp(4));
        root.addView(listLabel, new LinearLayout.LayoutParams(-1, -2));

        ScrollView scroll = new ScrollView(this);
        scroll.setContentDescription("商品列表，可滚动");
        LinearLayout rows = new LinearLayout(this);
        rows.setOrientation(LinearLayout.VERTICAL);
        for (int i = 1; i <= 30; i++) {
            final int number = i;
            TextView row = new TextView(this);
            row.setText(String.format(java.util.Locale.ROOT, "商品 %02d    ¥%d", i, i * 10));
            row.setTextSize(18);
            row.setTextColor(Color.rgb(35, 49, 66));
            row.setGravity(Gravity.CENTER_VERTICAL);
            row.setPadding(dp(14), 0, dp(10), 0);
            row.setBackgroundColor(i % 2 == 0 ? Color.WHITE : Color.rgb(239, 243, 247));
            row.setContentDescription(String.format(java.util.Locale.ROOT, "商品 %02d，价格 ¥%d，查看详情", i, i * 10));
            row.setClickable(true);
            row.setFocusable(true);
            row.setOnClickListener(v -> { selectedProduct = number; page = PAGE_DETAIL; render(); });
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1, dp(58));
            lp.bottomMargin = dp(2);
            rows.addView(row, lp);
        }
        scroll.addView(rows);
        root.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1f));
    }

    private void renderDetail() {
        addInfo(String.format(java.util.Locale.ROOT, "商品 %02d", selectedProduct));
        addInfo(String.format(java.util.Locale.ROOT, "价格：¥%d", selectedProduct * 10));
        addInfo("说明：这是用于自动化验收的虚构商品。");
        Button variants = new Button(this);
        variants.setText("选择规格");
        variants.setOnClickListener(v -> { page = PAGE_VARIANTS; render(); });
        root.addView(variants, new LinearLayout.LayoutParams(-1, -2));
        Button back = new Button(this);
        back.setText("返回商品列表");
        back.setOnClickListener(v -> { page = PAGE_LIST; render(); });
        root.addView(back, new LinearLayout.LayoutParams(-1, -2));
    }

    private void renderVariants() {
        addInfo(String.format(java.util.Locale.ROOT, "商品 %02d · 规格", selectedProduct));
        for (String option : new String[]{"标准款", "大号", "加大号"}) {
            Button choice = new Button(this);
            choice.setText(option);
            root.addView(choice, new LinearLayout.LayoutParams(-1, -2));
        }
        Button back = new Button(this);
        back.setText("返回商品详情");
        back.setOnClickListener(v -> { page = PAGE_DETAIL; render(); });
        root.addView(back, new LinearLayout.LayoutParams(-1, -2));
    }

    private void renderPrivacy() {
        addInfo("验证码（模拟敏感字段）");
        EditText code = new EditText(this);
        code.setSingleLine(true);
        code.setHint("验证码");
        code.setInputType(2 | 0x00000080); // numberPassword
        code.setText("654321");
        code.setContentDescription("验证码");
        root.addView(code, new LinearLayout.LayoutParams(-1, dp(54)));
        Button back = new Button(this);
        back.setText("返回商品列表");
        back.setOnClickListener(v -> { page = PAGE_LIST; render(); });
        root.addView(back, new LinearLayout.LayoutParams(-1, -2));
    }

    private void addInfo(String value) {
        TextView text = new TextView(this);
        text.setText(value);
        text.setTextSize(19);
        text.setTextColor(Color.rgb(35, 49, 66));
        text.setPadding(dp(4), dp(12), dp(4), dp(12));
        root.addView(text, new LinearLayout.LayoutParams(-1, -2));
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
