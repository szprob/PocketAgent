# 手机上的 PocketAgent（Android，单 APK）

一个 APK 装完就能用：Python、git、ripgrep、bash 和 PocketAgent 后端都在这台手机里，
不依赖 Termux，也不连别的电脑。

```
PocketAgent.apk
├── Alpine rootfs（aarch64，assets 里）
├── PRoot + loader（Termux 的 aarch64 构建）
├── PocketAgent 源码（构建时从仓库打进 assets）
└── WebView 界面
        │ 首次启动：proot 进 rootfs，apk 装 python/git/ripgrep，pip 装 aiohttp/openai
        ▼
   http://127.0.0.1:8765  ← 内置后端，只监听本机，带 token
```

后端只监听 127.0.0.1，但手机上所有 App 共用这个地址，所以还要带 token：App 第一次启动随机生成一个，
存在自己的私有目录里，后端用它启动，WebView 用带 `?token=` 的地址打开。别的 App、浏览器里的网页拿不到 token，
调不了接口。

## 装和用

1. 直接安装 `app-debug.apk`（arm64 手机，Android 8+；不要走应用商店，targetSdk 是 28）。
2. 第一次打开会自动初始化：下载约 100MB（Alpine 软件包 + Python 依赖），约 2–5 分钟，
   界面上能实时看到日志。初始化完成后每次启动都是秒开。
3. 第一次进界面去左下角 ⚙ 里填模型和 API Key；配置和会话都在 rootfs 的 `/root/.pocketagent`。

首次初始化需要网络；失败时日志停在对应步骤，点「重试」会继续。想从头再来就清除应用数据。

## 后台运行、通知、停止

- 后端由前台服务守着：被系统杀掉（Android 12 起系统会清理 App 的子进程）会自动重启，界面回到前台时
  也会检查一次，不在了就切回启动页重新拉起。连续 3 次刚启动就退出才算失败，停下来等你看日志点「重试」。
- 切到别的 App 以后，任务**等你确认 / 回答 / 这一轮结束**会发通知，点通知直接进那个任务。
- 有任务在跑、或者有开着的自动化时才握着 wake-lock（锁屏后继续跑）；都没有就放掉，不白耗电。
- 常驻通知上有「停止」：关掉后端和服务。之后打开 App 点「启动后端」再起。

## 手机文件

- 默认工作区是 rootfs 里的 `/root/workspace`，不授权也能用。
- 想要 agent 直接改手机上的仓库/文档：在首次弹窗里允许「所有文件访问」，rootfs 里的
  `/sdcard` 就是手机存储；也可以在界面里把 `/sdcard/xxx` 加进工作区。
- 不建议把 git 仓库放在 `/sdcard`（FUSE 上权限和符号链接行为不稳定），代码放 `/root/workspace`
  或 `/sdcard` 下的普通目录，产物可以放 `/sdcard`。

## 为什么 targetSdk 是 28

Android 10 起应用数据目录默认 noexec，而 PRoot 必须在这里 exec rootfs 里的程序。
targetSdk ≤ 28 是 Termux 等同类应用的标准做法。代价是应用商店不给上架，只能侧载；
功能上不受影响。

## 和桌面端的差别

| 能力 | 手机上的情况 |
|---|---|
| 文件 / grep / bash / git | 正常（rootfs 里装了 bash、git、ripgrep） |
| 模型调用 | 正常，openai SDK 和 aiohttp 都在 rootfs 里 |
| 沙箱 | 没有 bwrap，靠审批模式 |
| screenshot / computer | 不注册（只支持 macOS） |
| 后台长任务 | 前台服务 + wake-lock，锁屏后还能跑；后端被杀会自动重启，但正在跑的那一轮会断 |
| 下载 / 导出 | 存到系统「下载/PocketAgent」目录（轨迹「导出」、后端给出的文件） |
| 输入 | 回车是换行，发送点按钮（接了键盘可以 Ctrl+Enter） |
| 返回键 | 先关抽屉 / 弹窗，再按页面历史后退，退到头把 App 放到后台 |
| 时区 / DNS | 跟手机走：时区取系统时区，DNS 取当前网络的（换网络、连 VPN 会跟着更新，内网域名能解析） |
| 系统要求 | arm64（x86 模拟器不行），Android 8+，首次约 300MB 磁盘 |

## 构建 APK

需要 JDK 17+、Android SDK（platform 34 + build-tools 34.0.0）、Gradle 8.9。
`make apk` 会优先用环境变量 `ANDROID_HOME` / `JAVA_HOME`；没设的话自动找 `~/tools/android` 下的工具链，
并在第一次构建时生成被 gitignore 的 `android/local.properties`（写 `sdk.dir`）。

```bash
make apk          # 等价于 cd android && ./gradlew :app:assembleDebug
```

产物：`android/app/build/outputs/apk/debug/app-debug.apk`。

构建时会把仓库里的 `pocketagent/`、`pocketflow/`、`pyproject.toml` 打成 `pocketagent-src.zip`
放进 assets（`packagePocketAgentSources` 任务）；改了 Python 代码要重新打包 APK。

手机上后端直接从解压出来的源码（`/opt/PocketAgent`）运行，不往 site-packages 里装：覆盖安装新 APK 后
新代码马上生效，不用改版本号、不用联网。只有 `pyproject.toml` 变了才会补装一次依赖（没网就先用已有的，
下次启动再试）。

### 签名

默认用本机的 debug 签名（`~/.android/debug.keystore`）。**换一台电脑打的包签名不同，覆盖安装不了**，
只能卸载重装，rootfs、配置、会话都会丢。两种办法：

- 把原来电脑的 `~/.android/debug.keystore` 拷到新电脑同一位置（最省事，手机上不用动）；
- 或者用一个固定的 keystore：生成后在 `android/keystore.properties`（已 gitignore）里写

  ```properties
  storeFile=/绝对路径/pocketagent.jks
  storePassword=...
  keyAlias=pocketagent
  keyPassword=...
  ```

  有这个文件时 debug 和 release 包都用它签。注意从 debug 签名换到固定签名的那一次也要卸载重装，
  先用云同步把配置和会话备份好。

## 打包进 APK 的东西

- `assets/alpine-minirootfs.bin`：Alpine 3.20.3 aarch64 minirootfs（gzip tar），https://alpinelinux.org/
- `assets/proot/`：Termux 的 PRoot 5.1.107.102 + loader + libtalloc + libandroid-shmem
- 这些二进制直接放在仓库里（约 4.2MB），不用联网就能打包。每个文件的下载地址、SHA-256、源码地址和
  许可证都写在 `assets/proot/NOTICE.txt`，可以自己核对。2026-11-20 核对过：rootfs 的校验值和 Alpine 官方
  公布的一致，PRoot 那五个文件和 Termux 官方包里的逐字节一致。
  升级其中任何一个时记得同步改 NOTICE。
- `assets/pocketagent-src.zip`：构建时生成，不提交

## 故障排查

- **一直停在某一步 / 初始化失败**：日志就是初始化命令的原始输出；常见是网络。Alpine 软件包和 PyPI 都先走
  清华 TUNA，取不到会自动再试、再退到官方源；还是不行就换个网络点「重试」（失败后几秒内按钮就能点）。
- **装 Python 依赖失败**：看日志里是哪个 wheel；pydantic-core / jiter / aiohttp 都有
  musllinux aarch64 轮子，正常不需要编译。
- **后端没起来**：日志最后一行通常是 Python 异常；启动页的日志可以长按选中复制。
- **后台跑着跑着断了**：日志里有「==> 后端退出」而你没点停止，多半是系统清理子进程（Android 12+ 的
  phantom process killer）。后端会自己重启，但那一轮任务断了。想根治：开发者选项里关掉「子进程限制」，
  或 `adb shell device_config put activity_manager max_phantom_processes 2147483647`；
  再把 PocketAgent 的电池策略设成「无限制」。
- **内网域名解析不了**：rootfs 的 DNS 取的是手机当前网络的 DNS，先确认手机浏览器能打开那个域名。
- **`/sdcard` 不能读写**：系统设置 → 应用 → PocketAgent → 所有文件访问，允许后重开 App。
- **想彻底重置**：系统设置里清除 PocketAgent 的应用数据，下次启动会重新解压和安装。
