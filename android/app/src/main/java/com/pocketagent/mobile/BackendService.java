package com.pocketagent.mobile;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.graphics.drawable.Icon;
import android.net.ConnectivityManager;
import android.net.LinkProperties;
import android.net.Network;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.Log;

import org.apache.commons.compress.utils.IOUtils;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * 前台服务：首次启动时跑 rootfs 初始化，之后把 PocketAgent 的 web 后端放在内置 Linux 里一直跑着。
 *
 * - 后端进程由服务持有；意外退出（比如被系统当成子进程杀掉）就自动重启，连续几次一起来就退才算失败。
 * - 每隔几秒看一眼任务列表：界面不在前台时，任务等确认或者跑完了发一条通知；
 *   有任务在跑、或者有开着的自动化时才握着 wake-lock，空闲时放掉省电。
 * - 通知上有「停止」：关掉后端和服务，回到 App 里点「启动后端」再起。
 */
public class BackendService extends Service {
    static final String ACTION_STOP = "com.pocketagent.mobile.STOP_BACKEND";
    static final String EXTRA_TASK_ID = "task_id";
    /** 任务提醒通知的 tag，界面回到前台时按它清掉。 */
    static final String ALERT_TAG = "task";

    private static final String TAG = "PocketAgent";
    private static final String STATUS_CHANNEL_ID = "pocketagent-backend";
    private static final String ALERT_CHANNEL_ID = "pocketagent-alerts";
    private static final int STATUS_NOTIFICATION_ID = 1;
    private static final String STATUS_GROUP = "pocketagent-status";
    private static final String ALERT_GROUP_PREFIX = "pocketagent-task-";

    /** 起来不到这么久就退，算一次「起不来」。 */
    private static final long QUICK_EXIT_MILLISECONDS = 30_000;
    private static final int MAX_QUICK_EXITS = 3;
    private static final long RESTART_DELAY_MILLISECONDS = 1500;
    private static final long MONITOR_INTERVAL_SECONDS = 5;
    /** 自动化列表变得慢，隔这么多轮看一次（约一分钟）。 */
    private static final int AUTOMATION_CHECK_EVERY = 12;

    private LinuxEnvironment linux;
    private volatile Process serverProcess;
    private Thread worker;
    private volatile boolean stopRequested;
    private PowerManager.WakeLock wakeLock;
    private ScheduledExecutorService monitor;
    private ConnectivityManager.NetworkCallback networkCallback;

    // 下面几个只在 monitor 线程里读写
    private final Map<String, TaskSnapshot> seenTasks = new HashMap<>();
    private boolean baselineTaken;
    private boolean hasEnabledAutomations;
    private int monitorTicks;

    /** 上一轮看到的任务状态，用来判断「刚开始等确认」「刚跑完」。 */
    private static final class TaskSnapshot {
        final boolean running;
        final String waiting;

        TaskSnapshot(boolean running, String waiting) {
            this.running = running;
            this.waiting = waiting;
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        linux = new LinuxEnvironment(this);
        createChannels();
        watchNetwork();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopByUser();
            return START_NOT_STICKY;
        }
        startForeground(STATUS_NOTIFICATION_ID, buildStatusNotification("正在准备内置 Linux 环境…"));
        // 启动和安装阶段一直握着；后端起来之后由 monitor 按有没有活干来握 / 放
        setWakeLockHeld(true);
        if (worker == null || !worker.isAlive()) {
            stopRequested = false;
            BackendStatus.set(BackendStatus.Phase.STARTING, "");
            worker = new Thread(this::runBackend, "pocketagent-backend");
            worker.start();
        }
        startMonitor();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stopRequested = true;
        stopMonitor();
        unwatchNetwork();
        stopProcessesInBackground();
        setWakeLockHeld(false);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ===================== 后端进程 =====================

    private void runBackend() {
        try {
            linux.prepare();
            if (!linux.isInstalled()) {
                updateStatusNotification("首次初始化：安装 Python / git / ripgrep…");
                if (!linux.bootstrap(null)) {
                    fail("初始化失败。日志停在下面这一步，检查网络后点「重试」。");
                    return;
                }
            } else if (linux.needsDependencyRefresh()) {
                updateStatusNotification("补装 Python 依赖…");
                linux.refreshDependencies(null);
            }
            keepServerRunning();
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
        } catch (Exception error) {
            Log.e(TAG, "backend failed", error);
            linux.logLine("==> 后端启动失败：" + error);
            fail("后端启动失败：" + error.getMessage());
        } finally {
            stopSelf();
        }
    }

    /** 起后端并守着它：退出就重启；连续几次一起来就退，说明不是偶然被杀，停下来让用户看日志。 */
    private void keepServerRunning() throws Exception {
        int quickExits = 0;
        while (!stopRequested) {
            BackendStatus.set(BackendStatus.Phase.RUNNING, "");
            updateStatusNotification("后端运行中 · 127.0.0.1:" + LinuxEnvironment.PORT);
            long startedAt = SystemClock.elapsedRealtime();
            Process process = linux.startServer(null);
            serverProcess = process;
            if (stopRequested) {
                // 「停止」正好卡在起进程这一下：那边没看到这个进程，这里自己收掉
                linux.stopServer(process);
                return;
            }
            int exitCode = process.waitFor();
            serverProcess = null;
            if (stopRequested) {
                return;
            }
            long ranMilliseconds = SystemClock.elapsedRealtime() - startedAt;
            Log.i(TAG, "backend exited with " + exitCode + " after " + ranMilliseconds + " ms");
            linux.logLine("==> 后端退出，退出码 " + exitCode + "，运行了 " + ranMilliseconds / 1000 + " 秒");
            quickExits = ranMilliseconds < QUICK_EXIT_MILLISECONDS ? quickExits + 1 : 0;
            if (quickExits >= MAX_QUICK_EXITS) {
                fail("后端连续 " + MAX_QUICK_EXITS + " 次刚启动就退出。看下面的日志，改好后点「重试」。");
                return;
            }
            updateStatusNotification("后端退出了，正在重启…");
            Thread.sleep(RESTART_DELAY_MILLISECONDS);
        }
    }

    private void fail(String message) {
        if (stopRequested) {
            // 用户点「停止」掐掉安装时也会走到这里，那不算失败
            return;
        }
        BackendStatus.set(BackendStatus.Phase.FAILED, message);
    }

    private void stopByUser() {
        stopRequested = true;
        BackendStatus.set(BackendStatus.Phase.STOPPED, "后端已停止。点「启动后端」再起。");
        linux.logLine("==> 用户在通知上点了停止");
        stopProcessesInBackground();
        stopForeground(true);
        stopSelf();
    }

    /** 停掉后端和正在跑的安装命令。要等它们退出（几秒），放到后台线程，不卡主线程。 */
    private void stopProcessesInBackground() {
        Process process = serverProcess;
        Thread thread = new Thread(() -> {
            linux.cancelSetup();
            linux.stopServer(process);
        }, "pocketagent-stop");
        thread.start();
    }

    // ===================== 任务提醒和 wake-lock =====================

    private void startMonitor() {
        if (monitor != null) {
            return;
        }
        monitor = Executors.newSingleThreadScheduledExecutor();
        monitor.scheduleWithFixedDelay(
                this::monitorTick, MONITOR_INTERVAL_SECONDS, MONITOR_INTERVAL_SECONDS, TimeUnit.SECONDS);
    }

    private void stopMonitor() {
        if (monitor != null) {
            monitor.shutdownNow();
            monitor = null;
        }
    }

    private void monitorTick() {
        try {
            JSONObject payload = fetchJson("/api/sessions");
            if (payload == null) {
                // 后端还没起来或正在重启：握着锁，别让安装 / 重启半路睡过去
                setWakeLockHeld(true);
                return;
            }
            boolean anyRunning = inspectTasks(payload.optJSONArray("sessions"));
            if (monitorTicks % AUTOMATION_CHECK_EVERY == 0) {
                hasEnabledAutomations = fetchHasEnabledAutomations();
            }
            monitorTicks++;
            // 自动化要按点触发，机器睡着了就错过；所以有开着的自动化时也握着
            setWakeLockHeld(anyRunning || hasEnabledAutomations);
        } catch (Exception error) {
            Log.w(TAG, "monitor tick failed", error);
        }
    }

    /** 对比上一轮的状态，该提醒的发通知；返回现在有没有任务在跑。 */
    private boolean inspectTasks(JSONArray tasks) {
        boolean anyRunning = false;
        Map<String, TaskSnapshot> current = new HashMap<>();
        int count = tasks == null ? 0 : tasks.length();
        for (int index = 0; index < count; index++) {
            JSONObject task = tasks.optJSONObject(index);
            if (task == null) {
                continue;
            }
            String id = task.optString("id", "");
            boolean running = task.optBoolean("running", false);
            String waiting = task.isNull("waiting") ? "" : task.optString("waiting", "");
            current.put(id, new TaskSnapshot(running, waiting));
            // 卡在等用户那一步时后台其实没在干活，不算「在跑」
            anyRunning = anyRunning || (running && waiting.isEmpty());
            String alert = alertFor(seenTasks.get(id), running, waiting);
            if (alert != null && !BackendStatus.isInterfaceVisible()) {
                notifyTask(id, task.optString("title", "任务"), alert);
            }
        }
        seenTasks.clear();
        seenTasks.putAll(current);
        baselineTaken = true;
        return anyRunning;
    }

    /** 这个任务这一轮要不要提醒、提醒什么；不用提醒返回 null。 */
    private String alertFor(TaskSnapshot before, boolean running, String waiting) {
        if (!baselineTaken) {
            // 第一轮只记状态：服务刚起来时已经在等、已经跑完的，不补发
            return null;
        }
        String waitingBefore = before == null ? "" : before.waiting;
        if (!waiting.isEmpty() && !waiting.equals(waitingBefore)) {
            return waitingText(waiting);
        }
        boolean wasRunning = before != null && before.running;
        if (wasRunning && !running && waiting.isEmpty()) {
            return "这一轮结束了";
        }
        return null;
    }

    private String waitingText(String waiting) {
        switch (waiting) {
            case "approval":
                return "有操作等你确认";
            case "question":
                return "有问题等你回答";
            case "plan":
                return "计划等你确认";
            default:
                return "在等你";
        }
    }

    private boolean fetchHasEnabledAutomations() {
        JSONObject payload = fetchJson("/api/automations");
        JSONArray automations = payload == null ? null : payload.optJSONArray("automations");
        int count = automations == null ? 0 : automations.length();
        for (int index = 0; index < count; index++) {
            JSONObject automation = automations.optJSONObject(index);
            if (automation != null && automation.optBoolean("enabled", false)) {
                return true;
            }
        }
        return false;
    }

    /** GET 后端的一个接口；后端没起来、出错都返回 null。 */
    private JSONObject fetchJson(String path) {
        HttpURLConnection connection = null;
        try {
            URL url = new URL("http://127.0.0.1:" + LinuxEnvironment.PORT + path + "?token=" + linux.webToken());
            connection = (HttpURLConnection) url.openConnection();
            connection.setConnectTimeout(2000);
            connection.setReadTimeout(4000);
            if (connection.getResponseCode() != 200) {
                return null;
            }
            try (InputStream input = connection.getInputStream()) {
                return new JSONObject(new String(IOUtils.toByteArray(input), StandardCharsets.UTF_8));
            }
        } catch (Exception error) {
            return null;
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private synchronized void setWakeLockHeld(boolean held) {
        if (held) {
            if (wakeLock == null) {
                PowerManager manager = (PowerManager) getSystemService(POWER_SERVICE);
                wakeLock = manager.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "PocketAgent:backend");
                wakeLock.setReferenceCounted(false);
            }
            if (!wakeLock.isHeld()) {
                wakeLock.acquire();
            }
        } else if (wakeLock != null && wakeLock.isHeld()) {
            wakeLock.release();
        }
    }

    // ===================== 网络变化：更新 rootfs 里的 DNS =====================

    private void watchNetwork() {
        ConnectivityManager manager = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
        if (manager == null) {
            return;
        }
        // 默认网络换了（Wi-Fi ↔ 流量、连上 VPN）或者它的 DNS 变了，都会走到这里。
        // 用回调带来的 properties，不在回调里再去问 ConnectivityManager（那样可能拿到切换前的旧网络）。
        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onLinkPropertiesChanged(Network network, LinkProperties properties) {
                linux.writeResolvConf(properties.getDnsServers());
            }
        };
        try {
            manager.registerDefaultNetworkCallback(networkCallback);
        } catch (RuntimeException error) {
            Log.w(TAG, "cannot watch network", error);
            networkCallback = null;
        }
    }

    private void unwatchNetwork() {
        if (networkCallback == null) {
            return;
        }
        ConnectivityManager manager = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
        try {
            manager.unregisterNetworkCallback(networkCallback);
        } catch (RuntimeException ignored) {
            // 没注册上的话这里会抛
        }
        networkCallback = null;
    }

    // ===================== 通知 =====================

    private void createChannels() {
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        NotificationChannel status = new NotificationChannel(
                STATUS_CHANNEL_ID, "PocketAgent 后端", NotificationManager.IMPORTANCE_LOW);
        status.setDescription("内置 Linux 后端的运行状态");
        manager.createNotificationChannel(status);
        NotificationChannel alerts = new NotificationChannel(
                ALERT_CHANNEL_ID, "任务提醒", NotificationManager.IMPORTANCE_HIGH);
        alerts.setDescription("任务等你确认、或者跑完了");
        manager.createNotificationChannel(alerts);
    }

    private PendingIntent openAppIntent(String taskId) {
        Intent intent = new Intent(this, MainActivity.class);
        // 每个任务一个 PendingIntent（请求码不同），不然后发的会盖掉先发的 extra
        int requestCode = 0;
        if (taskId != null) {
            intent.putExtra(EXTRA_TASK_ID, taskId);
            requestCode = taskId.hashCode();
        }
        return PendingIntent.getActivity(
                this, requestCode, intent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private Notification buildStatusNotification(String text) {
        Intent stopIntent = new Intent(this, BackendService.class).setAction(ACTION_STOP);
        PendingIntent stopPendingIntent = PendingIntent.getService(
                this, 0, stopIntent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Action stopAction = new Notification.Action.Builder(
                Icon.createWithResource(this, R.drawable.ic_stat_pocketagent), "停止", stopPendingIntent).build();
        return new Notification.Builder(this, STATUS_CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_stat_pocketagent)
                .setContentTitle("PocketAgent")
                .setContentText(text)
                .setContentIntent(openAppIntent(null))
                .addAction(stopAction)
                .setOngoing(true)
                .setGroup(STATUS_GROUP)
                .build();
    }

    private void updateStatusNotification(String text) {
        if (stopRequested) {
            return;
        }
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        manager.notify(STATUS_NOTIFICATION_ID, buildStatusNotification(text));
    }

    private void notifyTask(String taskId, String title, String text) {
        Notification notification = new Notification.Builder(this, ALERT_CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_stat_pocketagent)
                .setContentTitle(title)
                .setContentText(text)
                .setContentIntent(openAppIntent(taskId))
                .setAutoCancel(true)
                // 每条提醒自己一组：不指定的话系统会把这个 App 的几条通知自动并成一组，
                // 点那个并出来的组只会打开 App，不会进到具体任务（模拟器上实测）
                .setGroup(ALERT_GROUP_PREFIX + taskId)
                .build();
        NotificationManager manager = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        // 同一个任务用同一个 id：先「等你确认」后「结束了」只留最新一条
        manager.notify(ALERT_TAG, taskId.hashCode(), notification);
    }
}
