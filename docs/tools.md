# 内置工具

工具在 `pocketagent/tools/` 里，基类 + 注册表；新增工具见 README「怎么扩展」。
这里只写几个有独立机制的：文件读写、grep / glob、edit_file 等常规工具不展开。

## 工作模式

- `suggest_plan_mode`：Agent 模式下遇到值得先规划的任务，问用户要不要切到计划模式；同意后只读调研，拒绝就不再反复问
- `submit_plan`：计划 / 设计模式下把方案交给用户批准；批准后自动切回 Agent 模式开始执行或实现，用户也可以要求继续完善
- Ask / 计划 / 设计模式下权限门禁只放行只读工具，`bash`、写文件、`type=general` 的子 agent 都会被拒绝（见[权限与安全](safety.md)）

## 后台命令

dev server、watch 这类不会自己退出的命令，bash 带上 `run_in_background=true` 就由独立监督进程接管，
立刻返回任务 id，不占着这一轮。stdout / stderr 合并写到 `~/.pocketagent/background_tasks/<任务 id>/output.log`，
状态和退出码在旁边的 `meta.json`；工具调用结束、对话中断、web 服务或桌面端重启都不会把命令带走。

- `bash_output` 带 id 看上次读取之后的新输出（`wait_seconds` 可以等一会儿新输出），不带 id 列出当前工作区的后台命令；
  跑完的也能取最后的输出
- `kill_shell` 按 id 结束，先正常终止整棵进程树，宽限期后再强制结束
- 后台命令不受 `timeout_seconds` 限制；同一个工作区的其它会话也能接管这些命令，不同工作区（包括不同 worktree）互相隔离
- 删任务或服务退出都不会隐式停止命令或删日志；子 agent 共用主任务的管理器

## 联网

- `web_search`：默认抓必应的结果页，免费、不用密钥；设置里可以换成 DeepSeek 搜索（0.005 元/次）
  或智谱 Web Search（0.01 元/次起），密钥不单独配就用对应厂商模型的密钥
- `web_fetch`：打开网页转成正文，只认 http / https

## 图片

- `generate_image`：文生图，走 OpenAI 兼容的 `/images/generations`。默认智谱 CogView
  （`cogview-3-flash` 免费，密钥不单独配就用智谱模型的）；也可以在设置里换成其他 OpenAI 兼容接口
  （接口地址空着就用第一个 OpenAI 兼容模型的网关地址）。图片存到工作区 `images/` 下，回复里用相对路径
  `![说明](路径)` 引用就会直接显示，一轮结束的「本轮产出的图片」也会自动带上
- `search_images`：搜现成的图片，抓必应图片结果页，免费、不用密钥；只给标题、原图链接、缩略图和来源，不下载
- `download_image`：把搜到的图下载到工作区再引用。单独一个工具，因为下载是写文件（要按写权限审批），
  而默认沙箱里不联网（除非设置里配了联网白名单），用 bash 去抓每次都得走「沙箱外」审批
- 工具结果是文本，模型看不到生成出来的画面；生成的图是写文件，按审批模式决定要不要确认

## 本机屏幕（仅 macOS）

能操作整台电脑、又不在沙箱里，所以默认关：⚙ 设置 → 工具 →「操作电脑」勾上（配置文件里的 `computer_use_enabled`）才会给模型这两个工具。

- `screenshot`：截主屏幕，截图直接进工具结果，模型看得到；走系统 `screencapture`，需要
  「屏幕录制」权限。同时记下「截图宽 / 屏幕逻辑宽」的缩放比，供 `computer` 换算坐标
- `computer`：键盘鼠标操作（单击 / 双击 / 输入文字 / 按键，坐标用截图像素坐标自动换算），
  走 AppleScript（System Events），零第三方依赖，需要「辅助功能」权限。没截图过就先 `screenshot`
  ——工具会拦住没有换算比的点击。右键和拖拽不支持；`screenshot` 是只读工具，
  `computer` 按执行类审批

## 搜索

`grep` / `glob` 优先调用 [ripgrep](https://github.com/BurntSushi/ripgrep)：

- 遵守 `.gitignore`，默认跳过 `.git`；大仓库上比纯 Python 遍历快很多
- rg 的 Rust 正则不支持 lookaround（`(?=...)`）这类写法，遇到时自动回退到 Python 正则，结果不变
- 机器上没有 rg 就用原来的 Python 实现。配置里的 `ripgrep_path` 或环境变量 `POCKETAGENT_RIPGREP`
  可以指定可执行文件位置（Homebrew 装在 `/opt/homebrew/bin/rg`，Qoder 插件缓存里也可能有一个）

## MCP 工具

配 MCP 服务器有两种方式：⚙ 设置 → MCP 里增删、启停、看连接状态和工具列表（密钥不回显，
留空表示不改），或者直接改 `~/.pocketagent/mcp.json`。配好后它们的工具就会以内置工具的身份出现
（名字是 `mcp__<服务器>__<工具>`），命令行、web、自动化任务都能用，不需要改代码。
每轮开跑前会重新确认一遍，改了配置下一轮生效。

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}
    },
    "docs": {
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer ${DOCS_KEY}"},
      "timeout_seconds": 60
    }
  }
}
```

- 配 `command` 的是 stdio 传输（起子进程，一行一条 JSON-RPC）；配 `url` 的是 streamable HTTP 传输
  （2025-03-26 之后的标准，回应是 JSON 还是 SSE 都支持）。更早的纯 SSE 接入方式不支持
- `command`、`args`、`env` 的值、`url`、`headers` 的值里的 `${VAR}` 和 `${VAR:-默认值}` 用环境变量展开
- 单个服务器可以 `"enabled": false` 停用；`timeout_seconds` 是一次工具调用的超时，默认 120 秒
- 权限：MCP 工具一律算「执行命令」类。ask 模式每次调用都要确认，AI 自动审批交给 AI 判断，
  常用的工具点一次「本任务总是允许」就不再问
- 服务器崩了：这一次调用报错，并附上它 stderr 的最后几行（能看出是包没装还是密钥没配）；
  不自动重试——请求可能已经在服务器那边执行了，重试会把有副作用的操作做两遍。下一次调用会自动重启进程
- 服务器发来的进度通知显示在工具输出里；这一轮被中断时会给服务器发 `notifications/cancelled`
- 只接 tools；MCP 的 resources / prompts 不接。不支持 OAuth，需要授权的服务把 token 放进 headers
- 只读用户级配置，不读仓库里的 `.mcp.json`：那等于让仓库内容决定跑什么命令
- 连接按 `~/.pocketagent` 共享：web 里多个任务共用同一套服务器进程，命令行换会话也不重启

## 子 agent

agent 可以用 `subagent` 工具派子 agent 去独立干一件事。子 agent 从零开始（看不到主对话），有自己的上下文，
做完只把结论交回来，主 agent 的上下文不会被它读过的大量文件塞满。

- `type=explore`：只读（读文件、搜索、查资料），互不相关的几件事可以在一次回复里同时派几个，并行跑
- `type=general`：能改文件、跑命令，每一步照常按权限审批，审批卡片上标着是哪个子 agent
- 子 agent 不能问用户，也不能再派子 agent；用量、沙箱、「本任务总是允许」和主 agent 共用
- 模型默认和任务的一样，⚙ 设置 → 智能体里可以单独选模型和思考档位（`subagent_model` / `subagent_reasoning`）
- 有时长上限：explore 默认 10 分钟、general 默认 40 分钟（`subagent_explore_minutes` / `subagent_general_minutes`），等你审批的时间不算。到点不会直接丢掉：停下正在跑的命令（连进程树一起杀）、补齐没结果的工具调用，再让它不带工具写一次总结交回来
- 一批里有 general 子 agent 时按顺序跑，不会和别的改动同时进行
- general 子 agent 默认在独立 worktree 里做（`worktree_background`），做完自动提交到 `pocketagent/<任务 id>-sub-*` 分支并把分支交回主 agent；explore 只读，不隔离。worktree 建不出来（不是 git 仓库等）就退回共用工作区，结论里会注明
- 子 agent 每调一个工具，它那张工具卡片下面就实时多一行；点停止时子 agent 一起停
