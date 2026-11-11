# 云同步

把会话历史和项目记忆放到自己的云盘上，换台机器接着用。`pocketagent sync` 不是把整个
`~/.pocketagent` 目录丢给网盘同步，而是只挑跨机器有意义的那部分，逐个文件和远端对齐。

## 为什么不用网盘直接同步整个目录

- `shadow/` 是每个工作区一份的影子 git 仓库，检查点里的 commit 只在本机有意义；`traces/`、
  `tool_outputs/`、`attachments/` 体积大且和本机路径绑死。整个目录同步既慢又容易把另一台机器的
  状态覆盖掉。
- 网盘客户端（iCloud、OneDrive）遇到同一个文件两边都改，会生成一堆 `副本` 文件，还可能只同步到一半；
  这里由程序自己判断谁改了、怎么合并，冲突另存到一个固定目录。
- 同步状态（哪个文件上次同步到哪个版本）存在本机，不跟着云走——每台机器各记各的。

## 同步什么

默认只同步两类：

| 数据 | 路径 | 说明 |
|---|---|---|
| 会话 | `sessions/*.json` | 完整消息历史，续跑时用 `pocketagent -r <id>` |
| 项目记忆 | `projects/**/memory/*.md` | 每个工作区一份的跨会话笔记 |

`config.json`、`automations.json`、`mcp.json`、`hooks/` 要显式打开（`--include`），因为它们
带密钥、带绝对路径，或者会在另一台机器上自己跑起来：

| 打开 | 风险 |
|---|---|
| `config` | 模型 API Key 会原样传到你自己的云盘；`web_token` 会去掉（每台机器各自生成） |
| `automations` | 定时任务会在两台机器上各跑一遍，可能重复触发 |
| `mcp` | `mcp.json` 里可能有第三方服务的密钥 |
| `hooks` | 钩子脚本会在另一台机器上被执行 |

不传的东西：`shadow/`、`traces/`、`tool_outputs/`、`attachments/`、`usage.jsonl`、`sync/` 自己，
以及临时任务（`ephemeral`）的会话——它的工作区是 `~/.pocketagent/scratch` 下的随机目录，
换台机器没有意义。单文件超过 5MB 的会跳过并在输出里提示（`--max-file-mb` 可调）。

## 选一个后端

| 方案 | 免费额度 | 国内速度 | 适合 |
|---|---|---|---|
| **坚果云 WebDAV**（推荐） | 每月 1GB 上传 / 3GB 下载，空间随上传量增长 | 直连，快 | 会话 + 记忆，通常十几 MB |
| **已在同步的目录**（iCloud 云盘 / OneDrive / 坚果云客户端 / NAS 挂载点） | 看各家 | 看各家 | macOS 上最省事，不用配 API |
| 七牛云 Kodo 等对象存储 | 标准存储 10GB 永久免费（要实名） | 快 | 需要绑域名、走 CDN，配置比 WebDAV 麻烦，这一版还没做后端 |
| 阿里云 OSS / 腾讯云 COS | 只有新用户试用期 | 快 | 长期用要付费，几毛钱一个月，但不如前两个省事 |

坚果云免费版的额度对这套数据够用：80MB 的 `~/.pocketagent` 里，真正要同步的通常不到 10MB，
之后每次只传改过的会话文件（一个几百 KB）。注意它是**按上传流量**算的，第一次全量传完就只剩增量。

## 配置坚果云

1. 在坚果云里建一个目录，比如 `pocketagent`。
2. 网页端右上角头像 → 账户信息 → 安全选项 → **第三方应用管理** → 添加应用，拿到一个应用密码。
   这个密码不是登录密码，WebDAV 只认它。
3. 配置并检查连通性：

```bash
pocketagent sync init \
  --url https://dav.jianguoyun.com/dav/pocketagent \
  --username you@example.com
# 会提示输入应用密码；也可以先 export POCKETAGENT_SYNC_PASSWORD=...，就不落盘
```

密码默认只从环境变量 `POCKETAGENT_SYNC_PASSWORD` 读；要写进 `~/.pocketagent/sync.json` 也可以，
文件权限是 0600。`sync init` 最后会连一次远端，告诉你两边各有几个文件。

用 iCloud 目录（不需要账号配置）：

```bash
pocketagent sync init --backend local \
  --url "$HOME/Library/Mobile Documents/com~apple~CloudDocs/PocketAgent"
```

## 日常使用

```bash
pocketagent sync                  # 双向：该传的传、该拉的拉、两边都改的合并
pocketagent sync status           # 只看差了什么，不写任何文件
pocketagent sync --dry-run        # 演练，打印要做的事
pocketagent sync --push           # 只上传；远端也改过的先跳过，不覆盖
pocketagent sync --pull           # 只拉取；本地也改过的先跳过
pocketagent sync conflicts        # 看冲突时另存的远端版本，--clear 删掉
```

退出码：0 正常，1 有错误（连不上、鉴权失败、远端像被清空了），配置缺失也是 1。

新机器上的顺序：装好 PocketAgent → `pocketagent sync init`（同一个地址、同一个账号）→
`pocketagent sync` 把会话拉下来 → `pocketagent -r <会话 id>` 续跑。会话里记的是绝对路径，
另一台机器上工作区路径不一样时用 `pocketagent -C <路径> -r <id>` 指过去。

### 自动同步

web / 桌面端（`pocketagent --web`）在后台每 10 分钟同步一次。间隔改 `sync.json` 里的
`auto_interval_minutes`（0 = 关掉，只手动跑），改完不用重启服务；命令行 REPL 不自动同步。

```json
{"auto_interval_minutes": 10}
```

想交给系统定时器也行，但有些公司管理的 Mac 会禁用 launchd / cron——表现为
`Bootstrap failed: 5: Input/output error` 或 `operation not permitted: crontab`，
这种机器只能用程序自带的后台循环。

```bash
# macOS launchd：~/Library/LaunchAgents/com.example.pocketagent-sync.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.pocketagent-sync.plist
```

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.example.pocketagent-sync</string>
  <key>ProgramArguments</key>
  <array><string>/Users/me/env/s11/bin/pocketagent</string><string>sync</string><string>-q</string></array>
  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><true/>
</dict></plist>
```

## 冲突和删除怎么处理

判定只看三样：本地内容哈希、远端 ETag、上次同步记下的底稿哈希。只有两边都偏离底稿才需要合并。

| 情况 | 处理 |
|---|---|
| 一边改了、另一边没动 | 直接照改的那边覆盖另一边 |
| 同一个会话两边都往下走了 | 消息按身份求并集；骨架取更新时间晚的那份，另一份独有的消息接在后面。统计字段取两边最大值 |
| 项目记忆两边都改了 | 保留本地版本，远端版本另存到 `~/.pocketagent/sync/conflicts/`，不往 Markdown 里写冲突标记 |
| `config` / `automations` / `mcp` 两边都改了 | 按顶层键三方合并；同一个键两边都改时本地优先，远端版本另存 |
| 本地删了、远端没动 | 删远端 |
| 本地删了、远端又改过 | 按不丢数据，把远端拉回来 |
| 远端删了、本地又改过 | 按不丢数据，把本地重新传上去 |
| 远端一次少了超过一半以前同步过的文件 | 直接报错停下，不删本地——多半是地址或账号指错了 |

会话不做冲突副本：消息已经求过并集，没有需要人做决定的部分。真正的分叉（两台机器从同一个
会话各聊各的）合并后顺序是「更新晚的那份在前，另一份独有的消息接在后面」，不是严格的时间顺序。

## 限制

- 检查点（影子 git）不同步：它依赖工作区当下的 git 状态，跨机器恢复没有意义。会话同步到另一台
  机器后，撤销按钮对那次任务不可用，新任务照常拍检查点。
- 没有端到端加密：会话正文（含代码片段、命令输出）以明文存在你自己的云盘里。对密钥敏感就别
  打开 `--include config`，或者把远端指到一个自己完全控制的目录。
- 不做实时同步：没有文件监听，靠定时任务或手动跑。
- 同一个会话在两台机器上**同时**跑，合并只保证消息不丢，不保证顺序和工具调用配对完全正确；
  真要在两边并行，用不同的会话。
