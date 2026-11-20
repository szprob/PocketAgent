package com.pocketagent.mobile;

import android.content.Context;
import android.content.pm.PackageInfo;
import android.net.ConnectivityManager;
import android.net.LinkProperties;
import android.net.Network;
import android.util.Base64;

import org.apache.commons.compress.archivers.tar.TarArchiveEntry;
import org.apache.commons.compress.archivers.tar.TarArchiveInputStream;
import org.apache.commons.compress.utils.IOUtils;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.TimeZone;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import java.util.zip.GZIPInputStream;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/**
 * 手机内置的 Linux 环境：把一个 Alpine rootfs、PRoot 和 PocketAgent 源码解压到应用数据目录，
 * 第一次启动时在 rootfs 里装 Python / git / ripgrep 和 Python 依赖，之后每次直接起 web 后端。
 *
 * 后端直接从解压出来的源码（/opt/PocketAgent）运行，不往 site-packages 里装 PocketAgent 自己：
 * APK 升级后新代码马上生效，不用联网、不用重跑 pip。只有 pyproject.toml 变了才补装一次依赖。
 *
 * 为什么 targetSdk 是 28：Android 10 起应用数据目录默认 noexec，PRoot 要在里面 exec rootfs 里的程序，
 * 只有 targetSdk <= 28 才允许（Termux 也是这么做的）。
 */
public class LinuxEnvironment {
    public static final int PORT = 8765;

    private static final String ROOTFS_TARBALL = "alpine-minirootfs.bin";
    private static final String SOURCE_ZIP = "pocketagent-src.zip";
    private static final String APK_MIRROR = "http://mirrors.tuna.tsinghua.edu.cn/alpine/v3.20";
    /** 镜像取不到索引时退到官方源（见 BOOTSTRAP_SCRIPT）。 */
    private static final String APK_OFFICIAL = "https://dl-cdn.alpinelinux.org/alpine/v3.20";
    private static final String PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple";
    private static final String SOURCE_DIR = "/opt/PocketAgent";
    private static final String PATH_VALUE = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";
    /** 读不到系统 DNS 时才用。 */
    private static final List<String> FALLBACK_DNS = Arrays.asList("223.5.5.5", "1.1.1.1");
    /** musl 的 resolv.conf 最多认 3 个 nameserver。 */
    private static final int MAX_NAMESERVERS = 3;
    /** proot 里主进程的 pid 写在这两个文件里（rootfs 里的相对路径），停进程时用。 */
    private static final String SERVER_PID_FILE = "tmp/pocketagent-server.pid";
    private static final String SETUP_PID_FILE = "tmp/pocketagent-setup.pid";
    private static final int SIGNAL_TERMINATE = 15;
    private static final long TERMINATE_WAIT_SECONDS = 3;
    private static final int TOKEN_BYTES = 24;
    private static final Object TOKEN_LOCK = new Object();
    private static volatile String cachedToken;

    /**
     * 从 pyproject.toml 里读出依赖（基础依赖 + web 那一组）再装，依赖清单只在 pyproject.toml 里写一份。
     * PIP_OPTIONS 由调用处换成这一次要加的 pip 参数。
     */
    private static final String INSTALL_DEPENDENCIES = """
            python3 - > /tmp/pocketagent-requirements.txt <<'PYTHON'
            import tomllib

            with open("/opt/PocketAgent/pyproject.toml", "rb") as handle:
                project = tomllib.load(handle)["project"]
            requirements = list(project["dependencies"]) + list(project["optional-dependencies"]["web"])
            for requirement in requirements:
                print(requirement)
            PYTHON
            python3 -m pip install --break-system-packages --disable-pip-version-check PIP_OPTIONS -i PYPI_MIRROR -r /tmp/pocketagent-requirements.txt \\
              || python3 -m pip install --break-system-packages --disable-pip-version-check PIP_OPTIONS -r /tmp/pocketagent-requirements.txt
            """;

    private static final String BOOTSTRAP_SCRIPT = """
            set -e
            export PATH=PATH_VALUE
            export PIP_NO_INPUT=1
            echo "==> apk update"
            # 镜像偶尔会拒绝一次请求（模拟器上遇到过 main 索引取不到）：先原地再试一次，还不行换官方源
            apk update || {
              echo "==> 镜像没取到索引，等几秒再试"
              sleep 3
              apk update || {
                echo "==> 换 Alpine 官方源"
                sed -i "s#PRIMARY_APK_SOURCE#FALLBACK_APK_SOURCE#" /etc/apk/repositories
                apk update
              }
            }
            echo "==> 安装 python3 / git / ripgrep / bash"
            apk add --no-cache python3 py3-pip git ripgrep bash curl ca-certificates tzdata
            echo "==> 安装 Python 依赖（aiohttp / openai / httpx）"
            INSTALL_DEPENDENCIES
            python3 -c "import aiohttp, openai, httpx; print('python deps ok')"
            mkdir -p /root/workspace
            touch /opt/.pocketagent-installed
            echo "INSTALL_OK"
            """;

    /** 依赖清单变了（pyproject.toml 变了）之后补装。没网时很快失败，不拖住启动。 */
    private static final String REFRESH_SCRIPT = """
            set -e
            export PATH=PATH_VALUE
            export PIP_NO_INPUT=1
            INSTALL_DEPENDENCIES
            echo "REFRESH_OK"
            """;

    private static final String SERVER_SCRIPT =
            "cd /root/workspace && exec python3 -m pocketagent --web --no-browser --port "
                    + PORT + " -C /root/workspace";

    private final Context context;
    private final File baseDir;
    private final File rootfsDir;
    private final File prootDir;
    private final File logFile;
    private final File installMarker;
    private final File sourceStamp;
    private final File dependencyMarker;
    private final File tokenFile;
    /** 正在跑的安装 / 补装命令，用户点「停止」时要能掐掉。 */
    private volatile Process setupProcess;

    public LinuxEnvironment(Context context) {
        this.context = context.getApplicationContext();
        this.baseDir = new File(this.context.getFilesDir(), "linux");
        this.rootfsDir = new File(baseDir, "rootfs");
        this.prootDir = new File(baseDir, "proot");
        this.logFile = new File(baseDir, "backend.log");
        this.installMarker = new File(baseDir, ".installed");
        this.sourceStamp = new File(baseDir, ".source-stamp");
        this.dependencyMarker = new File(rootfsDir, "opt/.pocketagent-dependencies");
        this.tokenFile = new File(baseDir, ".web-token");
    }

    public File logFile() {
        return logFile;
    }

    public boolean isInstalled() {
        return installMarker.exists();
    }

    /**
     * 后端的访问 token：第一次用时随机生成，存在应用私有目录里，界面和服务用同一个。
     * 手机上所有 App 共用 127.0.0.1，不带 token 的话任何 App、任何网页都能让 agent 执行命令。
     */
    public String webToken() {
        synchronized (TOKEN_LOCK) {
            if (cachedToken != null) {
                return cachedToken;
            }
            String token = readText(tokenFile);
            if (token.isEmpty()) {
                byte[] random = new byte[TOKEN_BYTES];
                new SecureRandom().nextBytes(random);
                token = Base64.encodeToString(random, Base64.URL_SAFE | Base64.NO_PADDING | Base64.NO_WRAP);
                try {
                    writeText(tokenFile, token);
                } catch (IOException error) {
                    // 存不下来就只在这次进程里用（记在 cachedToken 里，界面和服务拿到的是同一个）
                    appendLog("==> 存不了访问 token：" + error);
                }
            }
            cachedToken = token;
            return token;
        }
    }

    /** 解压 PRoot / rootfs / 源码。可以重复调用：rootfs 只在缺失时解压，源码只在 APK 换过之后重新解压。 */
    public void prepare() throws IOException {
        ensureDirectory(baseDir);
        ensureDirectory(prootDir);
        ensureDirectory(new File(baseDir, "tmp"));

        // APK 没换过就不重拷：上一个 proot 可能还没退干净，覆盖正在运行的可执行文件会报 Text file busy
        boolean overwrite = !packageUnchanged();
        copyAsset("proot/proot", new File(prootDir, "proot"), true, overwrite);
        // loader 会被 proot exec 起来，必须有可执行位
        copyAsset("proot/loader", new File(prootDir, "loader"), true, overwrite);
        copyAsset("proot/loader32", new File(prootDir, "loader32"), true, overwrite);
        copyAsset("proot/libtalloc.so.2", new File(prootDir, "libtalloc.so.2"), false, overwrite);
        copyAsset("proot/libandroid-shmem.so", new File(prootDir, "libandroid-shmem.so"), false, overwrite);

        if (!new File(rootfsDir, ".extracted").exists()) {
            appendLog("==> 解压 Alpine rootfs");
            extractRootfs();
            try {
                new File(rootfsDir, ".extracted").createNewFile();
            } catch (IOException ignored) {
                // 下次启动再解压一次即可，不影响安装
            }
        }
        refreshSources();
        writeApkRepositories();
        writeResolvConf();
        ensureDirectory(new File(rootfsDir, "root/workspace"));
        ensureDirectory(new File(rootfsDir, "opt"));
    }

    public boolean bootstrap(Consumer<String> listener) throws IOException, InterruptedException {
        appendLog("==> 首次初始化：安装 Python / git / ripgrep（约 100MB，几分钟）");
        int exitCode = runProot(fillScript(BOOTSTRAP_SCRIPT, ""), listener);
        if (exitCode != 0) {
            appendLog("==> 初始化失败，退出码 " + exitCode);
            return false;
        }
        ensureDirectory(installMarker.getParentFile());
        if (!installMarker.exists() && !installMarker.createNewFile()) {
            throw new IOException("写不了安装标记：" + installMarker);
        }
        writeDependencyMarker();
        appendLog("==> 初始化完成");
        return true;
    }

    /**
     * 代码直接从源码运行，APK 升级后不用重装；只有依赖清单（pyproject.toml）变了才要补装。
     * 老版本装的环境没有这个标记，也会走一次，依赖都在的话 pip 不联网、几秒就过。
     */
    public boolean needsDependencyRefresh() {
        if (!isInstalled()) {
            return false;
        }
        String fingerprint = dependencyFingerprint();
        return !fingerprint.isEmpty() && !fingerprint.equals(readText(dependencyMarker));
    }

    /** 补装依赖。失败不挡启动：没网时多半是依赖其实没变，先用着，下次启动再试。 */
    public boolean refreshDependencies(Consumer<String> listener) {
        appendLog("==> 依赖清单变了，补装 Python 依赖");
        try {
            int exitCode = runProot(fillScript(REFRESH_SCRIPT, "--retries 1 --timeout 10"), listener);
            if (exitCode == 0) {
                writeDependencyMarker();
                return true;
            }
            appendLog("==> 补装失败（退出码 " + exitCode + "），先用已经装好的依赖");
        } catch (Exception error) {
            appendLog("==> 补装失败：" + error);
        }
        return false;
    }

    /** 掐掉正在跑的安装 / 补装命令（用户点了「停止」）。会等几秒，别在主线程上调。 */
    public void cancelSetup() {
        terminate(setupProcess, SETUP_PID_FILE);
    }

    private String fillScript(String script, String pipOptions) {
        return script
                .replace("INSTALL_DEPENDENCIES", INSTALL_DEPENDENCIES)
                .replace("PIP_OPTIONS", pipOptions)
                .replace("PYPI_MIRROR", PYPI_MIRROR)
                .replace("PRIMARY_APK_SOURCE", APK_MIRROR)
                .replace("FALLBACK_APK_SOURCE", APK_OFFICIAL)
                .replace("PATH_VALUE", PATH_VALUE);
    }

    /** pyproject.toml 内容的 SHA-256；读不到返回空串（当作不用补装）。 */
    private String dependencyFingerprint() {
        File pyproject = new File(rootfsDir, SOURCE_DIR.substring(1) + "/pyproject.toml");
        try (InputStream input = new FileInputStream(pyproject)) {
            byte[] hash = MessageDigest.getInstance("SHA-256").digest(IOUtils.toByteArray(input));
            StringBuilder text = new StringBuilder();
            for (byte value : hash) {
                text.append(String.format(Locale.ROOT, "%02x", value));
            }
            return text.toString();
        } catch (Exception error) {
            return "";
        }
    }

    private void writeDependencyMarker() {
        try {
            writeText(dependencyMarker, dependencyFingerprint());
        } catch (IOException error) {
            appendLog("==> 写依赖标记失败：" + error);
        }
    }

    /**
     * APK 换过（装了新包，哪怕版本号没变）才重新解压源码；解压前先删掉旧的，免得新版本里删掉的文件还留着。
     * 标记放在 rootfs 外面：rootfs 被清掉重来时目录不在了，也会重新解压。
     */
    private void refreshSources() throws IOException {
        File destination = new File(rootfsDir, SOURCE_DIR.substring(1));
        if (packageUnchanged() && destination.isDirectory()) {
            return;
        }
        appendLog("==> 解压 PocketAgent 源码");
        deleteRecursively(destination);
        extractSourceZip();
        String stamp = packageStamp();
        if (!stamp.isEmpty()) {
            writeText(sourceStamp, stamp);
        }
    }

    /** 上次解压之后 APK 没换过。 */
    private boolean packageUnchanged() {
        String stamp = packageStamp();
        return !stamp.isEmpty() && stamp.equals(readText(sourceStamp));
    }

    /** 版本号 + 这次安装的时间：同一个版本号重新打包再装，时间也会变。取不到返回空串（每次都解压）。 */
    private String packageStamp() {
        try {
            PackageInfo info = context.getPackageManager().getPackageInfo(context.getPackageName(), 0);
            return info.versionName + ":" + info.lastUpdateTime;
        } catch (Exception error) {
            return "";
        }
    }

    private void deleteRecursively(File target) throws IOException {
        if (!Files.isSymbolicLink(target.toPath()) && target.isDirectory()) {
            File[] children = target.listFiles();
            if (children != null) {
                for (File child : children) {
                    deleteRecursively(child);
                }
            }
        }
        Files.deleteIfExists(target.toPath());
    }

    private String readText(File file) {
        if (!file.exists()) {
            return "";
        }
        try (InputStream input = new FileInputStream(file)) {
            return new String(IOUtils.toByteArray(input), StandardCharsets.UTF_8).trim();
        } catch (IOException error) {
            return "";
        }
    }

    /** 起 web 后端；返回的 Process 输出会一直写进 backend.log。 */
    public Process startServer(Consumer<String> listener) throws IOException {
        List<String> serverEnvironment = Arrays.asList(
                // 直接从解压出来的源码跑；老版本装在 site-packages 里的那份排在后面，不会被用到
                "PYTHONPATH=" + SOURCE_DIR,
                "PYTHONUNBUFFERED=1",
                "POCKETAGENT_WEB_TOKEN=" + webToken());
        ProcessBuilder builder = prootBuilder(SERVER_SCRIPT, serverEnvironment, SERVER_PID_FILE);
        builder.redirectErrorStream(true);
        Process process = builder.start();
        pump(process.getInputStream(), listener);
        return process;
    }

    /** 停掉 startServer 起的后端（连同它在 rootfs 里起的所有子进程）。会等几秒，别在主线程上调。 */
    public void stopServer(Process process) {
        terminate(process, SERVER_PID_FILE);
    }

    /**
     * 停掉一次 proot 运行。
     *
     * 不能只对 proot 发信号：proot 把终止信号留给里面的程序，自己不理会 SIGTERM；
     * 硬杀 proot 的话里面的进程会脱离它继续跑（路径不再被翻译，还占着端口）。
     * 所以信号发给 proot 里的主进程（脚本开头把自己的 pid 写在 pidFile 里，exec 之后 pid 不变）；
     * 主进程一退，--kill-on-exit 让 proot 把剩下的子进程一起带走，然后自己退出。
     */
    private void terminate(Process process, String pidFile) {
        if (process == null || !process.isAlive()) {
            return;
        }
        int mainProcessId = readProcessId(new File(rootfsDir, pidFile));
        if (mainProcessId > 0) {
            android.os.Process.sendSignal(mainProcessId, SIGNAL_TERMINATE);
        }
        try {
            if (process.waitFor(TERMINATE_WAIT_SECONDS, TimeUnit.SECONDS)) {
                return;
            }
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
        }
        // 没按时退（或者还没来得及写 pid 文件）：硬杀
        if (mainProcessId > 0) {
            android.os.Process.sendSignal(mainProcessId, android.os.Process.SIGNAL_KILL);
        }
        process.destroyForcibly();
    }

    private int readProcessId(File file) {
        try {
            return Integer.parseInt(readText(file));
        } catch (NumberFormatException error) {
            return -1;
        }
    }

    public void logLine(String line) {
        appendLog(line);
    }

    public String readLogTail(int maxLines) {
        if (!logFile.exists()) {
            return "";
        }
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(new FileInputStream(logFile), StandardCharsets.UTF_8))) {
            List<String> lines = new ArrayList<>();
            String line;
            while ((line = reader.readLine()) != null) {
                lines.add(withoutToken(line));
                if (lines.size() > maxLines) {
                    lines.remove(0);
                }
            }
            return String.join("\n", lines);
        } catch (IOException error) {
            return "";
        }
    }

    /** pidFile：rootfs 里的相对路径，脚本一开始把主进程的 pid 写进去，terminate 靠它找到要停的进程。 */
    private ProcessBuilder prootBuilder(String script, List<String> extraEnvironment, String pidFile) {
        // 先删掉上一次留下的，免得还没写新的就有人来读，把信号发给不相干的进程
        new File(rootfsDir, pidFile).delete();
        List<String> command = new ArrayList<>();
        command.add(new File(prootDir, "proot").getAbsolutePath());
        // 主进程退出时把它留下的子进程（agent 起的后台命令等）一起杀掉，proot 才会跟着退出；
        // 不加的话 proot 会一直等这些子进程，服务就发现不了后端已经没了
        command.add("--kill-on-exit");
        command.add("--link2symlink");
        command.add("-0");
        command.add("-r");
        command.add(rootfsDir.getAbsolutePath());
        addBind(command, "/dev", "/dev");
        addBind(command, "/proc", "/proc");
        addBind(command, "/sys", "/sys");
        // /sdcard 在 Android 上是指向 /storage/self/primary 的符号链接，proot 直接 bind 会 sanitize 失败；
        // 绑真实路径到 guest 的 /sdcard。
        addBind(command, "/storage/emulated/0", "/sdcard");
        addBind(command, "/storage/emulated/0", "/storage/emulated/0");
        command.add("-w");
        command.add("/root");
        command.add("/usr/bin/env");
        command.add("-i");
        command.add("HOME=/root");
        command.add("PATH=" + PATH_VALUE);
        command.add("TERM=xterm");
        command.add("LANG=C.UTF-8");
        command.add("TZ=" + timeZoneValue());
        command.add("PIP_NO_INPUT=1");
        command.addAll(extraEnvironment);
        command.add("/bin/sh");
        command.add("-lc");
        command.add("echo $$ > /" + pidFile + "\n" + script);

        ProcessBuilder builder = new ProcessBuilder(command);
        builder.directory(baseDir);
        Map<String, String> environment = builder.environment();
        environment.put("PROOT_LOADER", new File(prootDir, "loader").getAbsolutePath());
        environment.put("PROOT_TMP_DIR", new File(baseDir, "tmp").getAbsolutePath());
        environment.put("LD_LIBRARY_PATH", prootDir.getAbsolutePath());
        environment.put("PROOT_NO_SECCOMP", "1");
        return builder;
    }

    private void addBind(List<String> command, String hostPath, String guestPath) {
        if (new File(hostPath).exists()) {
            command.add("-b");
            command.add(hostPath + ":" + guestPath);
        }
    }

    /**
     * rootfs 里的时区跟手机走，不然会话时间和定时自动化都按 UTC 算。
     * 装了 tzdata 就用时区名（带夏令时规则）；老环境没装，退回成固定偏移的 POSIX 写法，musl 不靠 tzdata 也认。
     */
    private String timeZoneValue() {
        TimeZone zone = TimeZone.getDefault();
        File zoneFile = new File(rootfsDir, "usr/share/zoneinfo/" + zone.getID());
        if (zoneFile.isFile()) {
            return zone.getID();
        }
        return posixTimeZone(zone.getOffset(System.currentTimeMillis()));
    }

    /** 东八区 → "<+08>-8"：POSIX 的偏移是「本地时间加多少到 UTC」，符号和平常说的相反。 */
    static String posixTimeZone(int offsetMilliseconds) {
        int totalMinutes = offsetMilliseconds / 60000;
        String nameSign = totalMinutes >= 0 ? "+" : "-";
        String offsetSign = totalMinutes >= 0 ? "-" : "";
        int hours = Math.abs(totalMinutes) / 60;
        int minutes = Math.abs(totalMinutes) % 60;
        if (minutes == 0) {
            return String.format(Locale.ROOT, "<%s%02d>%s%d", nameSign, hours, offsetSign, hours);
        }
        return String.format(Locale.ROOT, "<%s%02d%02d>%s%d:%02d", nameSign, hours, minutes, offsetSign, hours, minutes);
    }

    private int runProot(String script, Consumer<String> listener) throws IOException, InterruptedException {
        ProcessBuilder builder = prootBuilder(script, Collections.emptyList(), SETUP_PID_FILE);
        builder.redirectErrorStream(true);
        Process process = builder.start();
        setupProcess = process;
        try {
            pump(process.getInputStream(), listener);
            return process.waitFor();
        } finally {
            setupProcess = null;
        }
    }

    private void pump(InputStream input, Consumer<String> listener) {
        Thread thread = new Thread(() -> {
            try (BufferedReader reader = new BufferedReader(
                    new InputStreamReader(input, StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    appendLog(line);
                    if (listener != null) {
                        listener.accept(line);
                    }
                }
            } catch (IOException ignored) {
                // 进程结束时流会关掉，这里不用处理
            }
        }, "pocketagent-log");
        thread.setDaemon(true);
        thread.start();
    }

    private synchronized void appendLog(String line) {
        ensureDirectory(logFile.getParentFile());
        try (OutputStream output = new FileOutputStream(logFile, true)) {
            output.write((withoutToken(line) + "\n").getBytes(StandardCharsets.UTF_8));
        } catch (IOException ignored) {
            // 日志写不进去不影响主流程
        }
    }

    /** 后端启动时会打印带 token 的地址。日志显示在启动页上，也可能被复制出去给别人看，token 不留在里面。 */
    private String withoutToken(String line) {
        String token = cachedToken;
        if (token == null || token.isEmpty()) {
            return line;
        }
        return line.replace(token, "<token>");
    }

    private void copyAsset(String assetName, File target, boolean executable, boolean overwrite) throws IOException {
        if (!overwrite && target.isFile() && target.length() > 0) {
            return;
        }
        ensureDirectory(target.getParentFile());
        try (InputStream input = context.getAssets().open(assetName);
             OutputStream output = new FileOutputStream(target)) {
            IOUtils.copy(input, output);
        }
        if (executable) {
            target.setExecutable(true, true);
        }
    }

    private void extractRootfs() throws IOException {
        ensureDirectory(rootfsDir);
        String rootPath = rootfsDir.getCanonicalPath();
        try (InputStream raw = context.getAssets().open(ROOTFS_TARBALL);
             TarArchiveInputStream tar = new TarArchiveInputStream(new GZIPInputStream(raw))) {
            TarArchiveEntry entry;
            while ((entry = tar.getNextTarEntry()) != null) {
                File target = new File(rootfsDir, entry.getName());
                String targetPath = target.getCanonicalPath();
                if (!targetPath.equals(rootPath) && !targetPath.startsWith(rootPath + File.separator)) {
                    continue;
                }
                if (entry.isDirectory()) {
                    ensureDirectory(target);
                    applyMode(target, entry.getMode());
                } else if (entry.isSymbolicLink()) {
                    ensureDirectory(target.getParentFile());
                    try {
                        if (target.exists() || Files.isSymbolicLink(target.toPath())) {
                            Files.deleteIfExists(target.toPath());
                        }
                        Files.createSymbolicLink(target.toPath(), Paths.get(entry.getLinkName()));
                    } catch (IOException error) {
                        // 少数文件系统不允许建符号链接；退化成普通文件，不影响启动
                        copyLinkTarget(entry.getLinkName(), target);
                    }
                } else if (entry.isLink()) {
                    ensureDirectory(target.getParentFile());
                    copyLinkTarget(entry.getLinkName(), target);
                } else {
                    ensureDirectory(target.getParentFile());
                    try (OutputStream output = new FileOutputStream(target)) {
                        IOUtils.copy(tar, output);
                    }
                    applyMode(target, entry.getMode());
                }
            }
        }
    }

    private void copyLinkTarget(String linkName, File target) throws IOException {
        File source = new File(rootfsDir, linkName);
        if (source.isFile()) {
            try (InputStream input = new FileInputStream(source);
                 OutputStream output = new FileOutputStream(target)) {
                IOUtils.copy(input, output);
            }
        }
    }

    private void applyMode(File file, int mode) {
        if (mode == 0) {
            mode = file.isDirectory() ? 0755 : 0644;
        }
        boolean executable = (mode & 0111) != 0;
        file.setReadable(true, true);
        file.setWritable((mode & 0200) != 0, true);
        file.setExecutable(executable, true);
    }

    private void extractSourceZip() throws IOException {
        File destination = new File(rootfsDir, "opt/PocketAgent");
        ensureDirectory(destination);
        try (InputStream raw = context.getAssets().open(SOURCE_ZIP);
             ZipInputStream zip = new ZipInputStream(raw)) {
            ZipEntry entry;
            while ((entry = zip.getNextEntry()) != null) {
                File target = new File(destination, entry.getName());
                String destinationPath = destination.getCanonicalPath();
                String targetPath = target.getCanonicalPath();
                if (!targetPath.equals(destinationPath) && !targetPath.startsWith(destinationPath + File.separator)) {
                    continue;
                }
                if (entry.isDirectory()) {
                    ensureDirectory(target);
                } else {
                    ensureDirectory(target.getParentFile());
                    try (OutputStream output = new FileOutputStream(target)) {
                        IOUtils.copy(zip, output);
                    }
                }
            }
        }
    }

    private void writeApkRepositories() throws IOException {
        writeText(new File(rootfsDir, "etc/apk/repositories"),
                APK_MIRROR + "/main\n" + APK_MIRROR + "/community\n");
    }

    /**
     * 把手机当前网络的 DNS 写进 rootfs。每次启动写一次，网络换了（Wi-Fi ↔ 流量、连上 VPN）服务会再调。
     * musl 每次解析都重读这个文件，改完马上生效。
     *
     * 有系统 DNS 时不掺公共 DNS：musl 同时问列表里所有服务器、谁先答用谁，
     * 内网域名会被公共 DNS 抢先答成「不存在」。读不到系统 DNS 才退回公共的。
     */
    public void writeResolvConf() {
        writeResolvConf(activeNetworkDnsServers());
    }

    public void writeResolvConf(List<InetAddress> systemServers) {
        // rootfs 还没解压出来（服务刚起、网络回调先到）就先不写
        if (!new File(rootfsDir, "etc").isDirectory()) {
            return;
        }
        List<String> servers = usableDnsServers(systemServers);
        if (servers.isEmpty()) {
            servers = FALLBACK_DNS;
        }
        StringBuilder text = new StringBuilder();
        for (String server : servers) {
            text.append("nameserver ").append(server).append('\n');
        }
        try {
            writeText(new File(rootfsDir, "etc/resolv.conf"), text.toString());
        } catch (IOException error) {
            appendLog("==> 写 resolv.conf 失败：" + error);
        }
    }

    private List<InetAddress> activeNetworkDnsServers() {
        try {
            ConnectivityManager manager =
                    (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            Network network = manager == null ? null : manager.getActiveNetwork();
            LinkProperties properties = network == null ? null : manager.getLinkProperties(network);
            if (properties != null) {
                return properties.getDnsServers();
            }
        } catch (RuntimeException error) {
            // 个别系统这里会抛权限异常，当作读不到
        }
        return Collections.emptyList();
    }

    private List<String> usableDnsServers(List<InetAddress> addresses) {
        List<String> servers = new ArrayList<>();
        for (InetAddress address : addresses) {
            // 链路本地地址要带网卡名才能用，rootfs 里用不了
            if (address.isLinkLocalAddress() || address.isLoopbackAddress()) {
                continue;
            }
            servers.add(address.getHostAddress());
            if (servers.size() == MAX_NAMESERVERS) {
                break;
            }
        }
        return servers;
    }

    /** 先写到旁边的临时文件再改名：resolv.conf 这类文件随时有人在读，不能让人读到写了一半的。 */
    private void writeText(File file, String text) throws IOException {
        ensureDirectory(file.getParentFile());
        File temporary = new File(file.getParentFile(), file.getName() + ".tmp");
        try (OutputStream output = new FileOutputStream(temporary)) {
            output.write(text.getBytes(StandardCharsets.UTF_8));
        }
        if (!temporary.renameTo(file)) {
            throw new IOException("改名失败：" + temporary + " → " + file);
        }
    }

    private void ensureDirectory(File directory) {
        if (directory != null && !directory.exists()) {
            directory.mkdirs();
        }
    }
}
