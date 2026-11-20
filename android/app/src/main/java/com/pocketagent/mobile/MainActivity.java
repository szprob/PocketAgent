package com.pocketagent.mobile;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.NotificationManager;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.ContentValues;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.provider.Settings;
import android.service.notification.StatusBarNotification;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.MimeTypeMap;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import org.apache.commons.compress.utils.IOUtils;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ConnectException;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * PocketAgent 的手机入口。后端不在 Termux，也不在别的机器上：
 * 应用启动 BackendService，在自带的 Alpine rootfs（PRoot）里跑 pocketagent --web，
 * 这里只负责探活、显示初始化日志，然后把 127.0.0.1:8765 的 web 界面放进 WebView。
 */
public class MainActivity extends Activity {
    private static final String BACKEND_ORIGIN = "http://127.0.0.1:" + LinuxEnvironment.PORT + "/";
    private static final int FILE_CHOOSER_REQUEST = 1001;
    private static final int STORAGE_REQUEST = 1002;
    private static final int POLL_ATTEMPTS = 600;
    private static final long POLL_INTERVAL_MS = 2000;
    private static final String DOWNLOAD_FOLDER = "PocketAgent";
    /** 网页里注册的返回键处理（main.js 的 __pocketAgentBack）：关掉了抽屉或弹窗就返回 true。 */
    private static final String BACK_SCRIPT =
            "(function(){try{return !!(window.__pocketAgentBack&&window.__pocketAgentBack());}"
                    + "catch(error){return false;}})()";

    private WebView webView;
    private View setupPanel;
    private TextView statusView;
    private TextView logView;
    private ProgressBar progressBar;
    private Button startButton;
    private Button retryButton;

    /** 探活专用；下载另用一个，免得大文件把探活堵住。 */
    private final ExecutorService probeExecutor = Executors.newSingleThreadExecutor();
    private final ExecutorService downloadExecutor = Executors.newSingleThreadExecutor();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    /** 网页调 PocketAgentAndroid 时要带的口令，见 AndroidBridge。 */
    private final String bridgeKey = randomKey();
    private LinuxEnvironment linux;
    private ValueCallback<Uri[]> fileCallback;
    private boolean pageLoaded;
    private boolean polling;
    /** 从通知点进来要打开的任务；界面还没加载好时先记着。 */
    private String pendingTaskId;

    private enum ProbeResult {
        UP,
        /** 端口上没人听：后端肯定不在了。 */
        REFUSED,
        /** 超时或别的错：后端可能只是忙。 */
        UNKNOWN,
    }

    /** 往输出流里写要保存的内容。 */
    private interface ContentWriter {
        void write(OutputStream output) throws IOException;
    }

    /**
     * 给网页用的原生接口，挂在 window.PocketAgentAndroid 上。
     *
     * addJavascriptInterface 挂上去的对象页面里所有 frame 都能调，包括右侧栏「预览」里打开的任意网页。
     * 所以每次调用要带口令：口令只在主页面加载完后注入主 frame，别的源的 iframe 读不到。
     */
    private final class AndroidBridge {
        /** 把一段文本存成文件（WebView 里 blob 下载存不下来，「导出」走这里）。 */
        @JavascriptInterface
        public boolean saveTextFile(String key, String fileName, String text) {
            if (!bridgeKey.equals(key) || text == null) {
                return false;
            }
            byte[] content = text.getBytes(StandardCharsets.UTF_8);
            saveInBackground(fileName, output -> output.write(content));
            return true;
        }
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        linux = new LinuxEnvironment(this);
        pendingTaskId = getIntent().getStringExtra(BackendService.EXTRA_TASK_ID);
        bindViews();
        configureWebView();
        requestStoragePermission();
    }

    @Override
    protected void onStart() {
        super.onStart();
        BackendStatus.setInterfaceVisible(true);
        clearTaskAlerts();
    }

    @Override
    protected void onStop() {
        BackendStatus.setInterfaceVisible(false);
        super.onStop();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (polling) {
            return;
        }
        if (webView.getVisibility() == View.VISIBLE) {
            // 界面开着的时候后端也可能在后台被系统杀掉，回到前台时看一眼
            verifyBackendStillUp();
        } else {
            checkBackend();
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        String taskId = intent.getStringExtra(BackendService.EXTRA_TASK_ID);
        if (taskId != null) {
            openTask(taskId);
        }
    }

    @Override
    protected void onDestroy() {
        polling = false;
        probeExecutor.shutdownNow();
        downloadExecutor.shutdownNow();
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
        }
        super.onDestroy();
    }

    private void bindViews() {
        webView = findViewById(R.id.web_view);
        setupPanel = findViewById(R.id.setup_panel);
        statusView = findViewById(R.id.status);
        logView = findViewById(R.id.boot_log);
        progressBar = findViewById(R.id.boot_progress);
        startButton = findViewById(R.id.start_button);
        retryButton = findViewById(R.id.retry_button);

        startButton.setOnClickListener(view -> startBackend());
        retryButton.setOnClickListener(view -> {
            // 清掉「失败 / 已停止」，不然检查完不会自动再起
            BackendStatus.set(BackendStatus.Phase.IDLE, "");
            checkBackend();
        });
    }

    private void configureWebView() {
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setTextZoom(100);
        webView.addJavascriptInterface(new AndroidBridge(), "PocketAgentAndroid");

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return handleUrl(request.getUrl());
            }

            @Override
            @SuppressWarnings("deprecation")
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return handleUrl(Uri.parse(url));
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (isBackendUri(Uri.parse(url))) {
                    // evaluateJavascript 只在主 frame 里跑，口令到不了 iframe
                    view.evaluateJavascript(
                            "window.__pocketAgentBridgeKey=" + JSONObject.quote(bridgeKey) + ";", null);
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame() && isBackendUri(request.getUrl())) {
                    pageLoaded = false;
                    showSetup(getString(R.string.status_down));
                }
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(
                    WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileCallback != null) {
                    fileCallback.onReceiveValue(null);
                }
                fileCallback = callback;
                Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("*/*");
                if (params.getMode() == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE) {
                    intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                }
                try {
                    startActivityForResult(
                            Intent.createChooser(intent, getString(R.string.choose_file)),
                            FILE_CHOOSER_REQUEST);
                } catch (ActivityNotFoundException error) {
                    fileCallback = null;
                    return false;
                }
                return true;
            }
        });

        webView.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) ->
                downloadFromPage(url, contentDisposition, mimeType));
    }

    // ===================== 探活和启动 =====================

    /** 启动页用：后端在就进界面；不在就起（上次是失败或被用户停掉的除外，那要用户自己点）。 */
    private void checkBackend() {
        setBusy(true);
        statusView.setText(R.string.status_checking);
        probeExecutor.execute(() -> {
            ProbeResult result = probeBackend();
            mainHandler.post(() -> onBackendChecked(result == ProbeResult.UP));
        });
    }

    private void onBackendChecked(boolean up) {
        if (polling) {
            return;
        }
        setBusy(false);
        if (up) {
            showWeb();
            return;
        }
        BackendStatus.Phase phase = BackendStatus.phase();
        if (phase == BackendStatus.Phase.FAILED || phase == BackendStatus.Phase.STOPPED) {
            showSetup(BackendStatus.message());
            return;
        }
        showSetup(getString(R.string.status_down));
        startBackend();
    }

    /** 界面已经开着时用：只有端口确实没人听才切回启动页。超时不算，后端忙的时候别把页面刷掉。 */
    private void verifyBackendStillUp() {
        probeExecutor.execute(() -> {
            ProbeResult result = probeBackend();
            mainHandler.post(() -> {
                boolean webShowing = webView.getVisibility() == View.VISIBLE;
                if (result == ProbeResult.REFUSED && webShowing && !polling) {
                    onBackendChecked(false);
                }
            });
        });
    }

    private void startBackend() {
        if (polling) {
            return;
        }
        // 先改状态再起服务：轮询可能比服务的 onStartCommand 先跑，别让它读到上一次的「失败」
        BackendStatus.set(BackendStatus.Phase.STARTING, "");
        startForegroundService(new Intent(this, BackendService.class));
        polling = true;
        statusView.setText(R.string.status_starting);
        setBusy(true);
        pollUntilUp(POLL_ATTEMPTS);
    }

    private ProbeResult probeBackend() {
        HttpURLConnection connection = null;
        try {
            URL url = new URL(BACKEND_ORIGIN + "api/info?token=" + linux.webToken());
            connection = (HttpURLConnection) url.openConnection();
            connection.setConnectTimeout(1500);
            connection.setReadTimeout(1500);
            connection.setRequestMethod("GET");
            int code = connection.getResponseCode();
            return code >= 200 && code < 300 ? ProbeResult.UP : ProbeResult.UNKNOWN;
        } catch (ConnectException error) {
            return ProbeResult.REFUSED;
        } catch (Exception error) {
            return ProbeResult.UNKNOWN;
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private void pollUntilUp(int attemptsLeft) {
        if (!polling) {
            return;
        }
        probeExecutor.execute(() -> {
            ProbeResult result = probeBackend();
            mainHandler.post(() -> {
                if (!polling) {
                    return;
                }
                updateLogTail();
                if (result == ProbeResult.UP) {
                    stopPolling();
                    showWeb();
                    return;
                }
                BackendStatus.Phase phase = BackendStatus.phase();
                if (phase == BackendStatus.Phase.FAILED || phase == BackendStatus.Phase.STOPPED) {
                    // 服务已经放弃了（初始化失败、后端反复起不来、用户点了停止），不用等到超时
                    stopPolling();
                    statusView.setText(BackendStatus.message());
                    return;
                }
                if (attemptsLeft <= 1) {
                    stopPolling();
                    statusView.setText(R.string.status_start_timeout);
                    return;
                }
                mainHandler.postDelayed(() -> pollUntilUp(attemptsLeft - 1), POLL_INTERVAL_MS);
            });
        });
    }

    private void stopPolling() {
        polling = false;
        setBusy(false);
    }

    private void updateLogTail() {
        String tail = linux.readLogTail(10);
        if (!tail.equals(logView.getText().toString())) {
            logView.setText(tail);
        }
    }

    private void showWeb() {
        setupPanel.setVisibility(View.GONE);
        webView.setVisibility(View.VISIBLE);
        String taskId = pendingTaskId;
        pendingTaskId = null;
        if (!pageLoaded || taskId != null) {
            pageLoaded = true;
            webView.loadUrl(pageUrl(taskId));
        } else {
            webView.reload();
        }
        suggestEnablingNotifications();
    }

    /**
     * 通知没开就问一次要不要开（进了界面再问，不和启动时的文件权限询问叠在一起）。
     * Android 13 起通知要用户同意；这个 App 的 targetSdk 低，没法自己弹系统的授权框，只能带用户去设置页。
     * 不开的话「等你确认 / 结束了」的提醒和常驻通知上的「停止」都看不到。
     */
    private void suggestEnablingNotifications() {
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager.areNotificationsEnabled()) {
            return;
        }
        SharedPreferences preferences = getSharedPreferences("settings", MODE_PRIVATE);
        if (preferences.getBoolean("notifications_asked", false)) {
            return;
        }
        preferences.edit().putBoolean("notifications_asked", true).apply();
        new AlertDialog.Builder(this)
                .setTitle(R.string.notifications_title)
                .setMessage(R.string.notifications_message)
                .setPositiveButton(R.string.notifications_action, (dialog, which) -> openNotificationSettings())
                .setNegativeButton(android.R.string.cancel, null)
                .show();
    }

    private void openNotificationSettings() {
        Intent intent = new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                .putExtra(Settings.EXTRA_APP_PACKAGE, getPackageName());
        try {
            startActivity(intent);
        } catch (Exception error) {
            Toast.makeText(this, R.string.notifications_unavailable, Toast.LENGTH_LONG).show();
        }
    }

    /** 带 token 的首页地址；服务端看到 ?token= 会种 cookie，之后的请求靠 cookie。 */
    private String pageUrl(String taskId) {
        String url = BACKEND_ORIGIN + "?token=" + linux.webToken();
        if (taskId != null) {
            url += "#/task/" + Uri.encode(taskId);
        }
        return url;
    }

    /** 从通知点进来：界面开着就直接跳到那个任务，没开着就等加载时带上。 */
    private void openTask(String taskId) {
        boolean webReady = webView.getVisibility() == View.VISIBLE && pageLoaded;
        if (!webReady) {
            pendingTaskId = taskId;
            return;
        }
        String hash = "#/task/" + Uri.encode(taskId);
        webView.evaluateJavascript("window.location.hash=" + JSONObject.quote(hash) + ";", null);
    }

    private void showSetup(String message) {
        webView.setVisibility(View.GONE);
        setupPanel.setVisibility(View.VISIBLE);
        statusView.setText(message);
        updateLogTail();
    }

    private void setBusy(boolean value) {
        progressBar.setVisibility(value ? View.VISIBLE : View.GONE);
        startButton.setEnabled(!value);
        retryButton.setEnabled(!value);
    }

    private boolean handleUrl(Uri uri) {
        if (isBackendUri(uri)) {
            return false;
        }
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (ActivityNotFoundException error) {
            Toast.makeText(this, uri.toString(), Toast.LENGTH_LONG).show();
        }
        return true;
    }

    private boolean isBackendUri(Uri uri) {
        String host = uri.getHost();
        return "http".equals(uri.getScheme())
                && ("127.0.0.1".equals(host) || "localhost".equals(host))
                && uri.getPort() == LinuxEnvironment.PORT;
    }

    /** 界面回到前台，任务提醒就不用留着了。 */
    private void clearTaskAlerts() {
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        for (StatusBarNotification notification : manager.getActiveNotifications()) {
            if (BackendService.ALERT_TAG.equals(notification.getTag())) {
                manager.cancel(notification.getTag(), notification.getId());
            }
        }
    }

    // ===================== 下载 =====================

    /** 页面里点了一个浏览器会当成下载的链接。后端自己的文件存到「下载」，外面的交给系统浏览器。 */
    private void downloadFromPage(String url, String contentDisposition, String mimeType) {
        Uri uri = Uri.parse(url);
        if (isBackendUri(uri)) {
            String fileName = URLUtil.guessFileName(url, contentDisposition, mimeType);
            String cookie = CookieManager.getInstance().getCookie(url);
            saveInBackground(fileName, output -> copyFromBackend(url, cookie, output));
            return;
        }
        String scheme = uri.getScheme();
        if ("http".equals(scheme) || "https".equals(scheme)) {
            handleUrl(uri);
            return;
        }
        Toast.makeText(this, R.string.download_unsupported, Toast.LENGTH_LONG).show();
    }

    private void copyFromBackend(String url, String cookie, OutputStream output) throws IOException {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        try {
            connection.setConnectTimeout(5000);
            connection.setReadTimeout(60000);
            if (cookie != null) {
                connection.setRequestProperty("Cookie", cookie);
            }
            int code = connection.getResponseCode();
            if (code != 200) {
                throw new IOException("后端返回 " + code);
            }
            try (InputStream input = connection.getInputStream()) {
                IOUtils.copy(input, output);
            }
        } finally {
            connection.disconnect();
        }
    }

    private void saveInBackground(String fileName, ContentWriter writer) {
        downloadExecutor.execute(() -> {
            String message;
            try {
                message = getString(R.string.download_saved, saveToDownloads(fileName, writer));
            } catch (Exception error) {
                message = getString(R.string.download_failed, String.valueOf(error.getMessage()));
            }
            String text = message;
            mainHandler.post(() -> Toast.makeText(this, text, Toast.LENGTH_LONG).show());
        });
    }

    /** 存到系统「下载/PocketAgent」里，返回给用户看的位置。 */
    private String saveToDownloads(String requestedName, ContentWriter writer) throws IOException {
        String fileName = safeFileName(requestedName);
        String shownPath = Environment.DIRECTORY_DOWNLOADS + "/" + DOWNLOAD_FOLDER + "/" + fileName;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ContentValues values = new ContentValues();
            values.put(MediaStore.Downloads.DISPLAY_NAME, fileName);
            values.put(MediaStore.Downloads.MIME_TYPE, mimeTypeFor(fileName));
            values.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/" + DOWNLOAD_FOLDER);
            Uri target = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values);
            if (target == null) {
                throw new IOException("系统没给保存位置");
            }
            try (OutputStream output = getContentResolver().openOutputStream(target)) {
                if (output == null) {
                    throw new IOException("打不开保存位置");
                }
                writer.write(output);
            }
            return shownPath;
        }
        File directory = new File(
                Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), DOWNLOAD_FOLDER);
        if (!directory.isDirectory() && !directory.mkdirs()) {
            throw new IOException("建不了目录 " + directory);
        }
        try (OutputStream output = new FileOutputStream(new File(directory, fileName))) {
            writer.write(output);
        }
        return shownPath;
    }

    /** 只留文件名本身：去掉目录分隔符和控制字符，空了就给个默认名。 */
    private static String safeFileName(String requestedName) {
        String name = requestedName == null ? "" : requestedName;
        name = name.replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]", "_").trim();
        while (name.startsWith(".")) {
            name = name.substring(1);
        }
        return name.isEmpty() ? "download" : name;
    }

    /** 按扩展名定类型；系统不认识的扩展名用通用二进制，免得系统给文件名再补一个扩展名。 */
    private static String mimeTypeFor(String fileName) {
        int dot = fileName.lastIndexOf('.');
        String extension = dot < 0 ? "" : fileName.substring(dot + 1).toLowerCase(Locale.ROOT);
        String known = MimeTypeMap.getSingleton().getMimeTypeFromExtension(extension);
        return known == null ? "application/octet-stream" : known;
    }

    private static String randomKey() {
        byte[] random = new byte[16];
        new SecureRandom().nextBytes(random);
        StringBuilder text = new StringBuilder();
        for (byte value : random) {
            text.append(String.format(Locale.ROOT, "%02x", value));
        }
        return text.toString();
    }

    // ===================== 存储权限 =====================

    private void requestStoragePermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (Environment.isExternalStorageManager()) {
                return;
            }
            SharedPreferences preferences = getSharedPreferences("settings", MODE_PRIVATE);
            if (preferences.getBoolean("storage_asked", false)) {
                return;
            }
            preferences.edit().putBoolean("storage_asked", true).apply();
            new AlertDialog.Builder(this)
                    .setTitle(R.string.storage_title)
                    .setMessage(R.string.storage_message)
                    .setPositiveButton(R.string.storage_action, (dialog, which) -> openAllFilesSettings())
                    .setNegativeButton(android.R.string.cancel, null)
                    .show();
            return;
        }
        if (checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
                == PackageManager.PERMISSION_GRANTED) {
            return;
        }
        requestPermissions(new String[]{
                android.Manifest.permission.READ_EXTERNAL_STORAGE,
                android.Manifest.permission.WRITE_EXTERNAL_STORAGE,
        }, STORAGE_REQUEST);
    }

    private void openAllFilesSettings() {
        try {
            startActivity(new Intent(
                    Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                    Uri.parse("package:" + getPackageName())));
        } catch (Exception error) {
            try {
                startActivity(new Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION));
            } catch (Exception ignored) {
                Toast.makeText(this, R.string.storage_unavailable, Toast.LENGTH_LONG).show();
            }
        }
    }

    @Override
    @SuppressWarnings("deprecation")
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != FILE_CHOOSER_REQUEST || fileCallback == null) {
            return;
        }
        Uri[] results = null;
        if (resultCode == RESULT_OK && data != null) {
            ClipData clip = data.getClipData();
            if (clip != null) {
                results = new Uri[clip.getItemCount()];
                for (int index = 0; index < clip.getItemCount(); index++) {
                    results[index] = clip.getItemAt(index).getUri();
                }
            } else if (data.getData() != null) {
                results = new Uri[]{data.getData()};
            }
        }
        fileCallback.onReceiveValue(results);
        fileCallback = null;
    }

    /**
     * 返回键先交给网页：开着抽屉、设置、文件预览就关掉它。网页没处理再按浏览历史后退；
     * 退到头了把 App 放到后台，不销毁界面，回来时还是原样。
     */
    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        if (webView.getVisibility() != View.VISIBLE) {
            super.onBackPressed();
            return;
        }
        webView.evaluateJavascript(BACK_SCRIPT, handled -> {
            if ("true".equals(handled)) {
                return;
            }
            if (webView.canGoBack()) {
                webView.goBack();
            } else {
                moveTaskToBack(true);
            }
        });
    }
}
