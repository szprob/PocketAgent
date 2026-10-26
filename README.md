# PocketAgent

基于 [PocketFlow](https://github.com/The-Pocket/PocketFlow) 的编程 agent，目标是做成 Codex / Claude Code / WorkBuddy / Qoder 这一类：
在本地仓库里读代码、改文件、跑命令，用户在旁边审批。

已经能用的：主循环、内置工具（文件、搜索、bash、联网搜索、读网页、技能、向用户提问）、三档权限（含 AI 自动审批）、
bash 沙箱、工作目录外的访问审批、影子 git 检查点（看变更、撤销、整体恢复）、技能（Agent Skills 标准）、项目记忆、
流式事件、trace、会话续跑、后台自动压缩上下文、多模型配置（思考档位 / 上下文长度随模型走）、
附件（图片 / 文件）、插话和排队、token 用量和缓存命中统计、定时自动化，以及命令行和 web 两套界面。

## 快速开始

核心依赖 `openai`（用它的 SDK 调 OpenAI 兼容接口）和 `httpx`；web 界面另外要 `aiohttp`、`pillow`。Python ≥ 3.11。

```bash
# 可编辑安装：改源码立即生效，不用重装（只有改了 pyproject 的依赖或入口才要重跑）
uv pip install -e '.[web]'          # 或 pip install -e '.[web]'

pocketagent --web                   # 起 web 界面并自动打开浏览器；第一次在左下角 ⚙ 设置里添加模型和 API Key
pocketagent                         # 命令行交互，在当前目录工作
pocketagent -C ~/projects/foo --mode ai_auto
pocketagent -c                      # 继续这个目录最近的会话
pocketagent -m "DS 4.1 Flash" -p "跑一下测试，把失败的修好" --mode full_auto   # 指定模型、单次执行后退出
```

不安装也能用：在仓库根目录 `python -m pocketagent ...`。

命令行交互里的命令：`/help`、`/new`、`/compact`、`/mode [ask|ai_auto|full_auto|read_only|auto_edit]`、`/exit`。
Ctrl-C 只中断当前这一轮，会话保留，可以接着说。

## web 界面

```bash
pocketagent --web -C ~/projects/foo     # 这个目录会加进工作区列表并排第一；8765 被占用就往后找空闲端口
pocketagent --web --no-browser          # 不自动打开浏览器（比如在远端机器上起）
pocketagent --web --host 0.0.0.0        # 让别的机器访问（比如开发机上起、经网关转发）
```

布局参考 Qoder，三栏：

- **左侧栏**：新任务、自动化；工作区（点开是这个工作区的文件树，点文件预览，预览里可以「@ 引用」到输入框；
  任务改了文件会自动刷新）；最近任务（全部历史，运行中的有蓝点，悬停出现 × 可以删除）；两段都能点标题折叠；
  最下面是账户名和 ⚙ 设置。
- **中间**：新任务页选工作区、模型、权限后直接说话；任务页是对话。输入框底下一排：📎 附件（也可以粘贴截图、拖文件进来）、
  权限、模型（选模型的面板里直接改思考档位和上下文长度，存进配置；设置里可以让某个模型不出现在这里）、发送 / 停止；
  再下面是上下文占用和压缩阈值。
- **右侧栏**（右上角按钮开关）：任务概要（最新进展、token 用量、缓存命中、费用、上下文）、变更（从任务开始到现在
  工作区里所有变了的文件，不管是 edit_file 还是 bash 改的；点开看 diff，可以撤销单个文件）、产出（新建的文件）、
  检查点（每一轮开始前拍一张，可以把整个工作区恢复到那时）。

### 权限

| 选项 | 只读工具 | 改文件 / 执行命令 |
|---|---|---|
| 每次询问 | 直接放行 | 每次弹审批卡片（改文件显示 diff） |
| AI 自动审批 | 直接放行 | 先让当前模型判断是否安全、是否在任务范围内；判定安全就放行，否则照常问你 |
| 完全访问 | 直接放行 | 全部放行 |

模式之外，有三种情况要多问一句，审批卡片上会写明原因：

| 情况 | 每次询问 | AI 自动审批 | 完全访问 |
|---|---|---|---|
| 读工作目录以外的路径 | 问你 | 交给 AI 判断 | 放行 |
| 改工作目录以外的路径 | 问你 | 问你 | 放行 |
| bash 要在沙箱外执行 | 问你 | 问你（不交给 AI） | 放行 |
| 碰到密钥目录（`~/.ssh`、`~/.aws`、`~/.pocketagent` 等） | 问你 | 问你 | 也问你 |

「本任务总是允许」记的是具体的东西：工具名、工作目录外的那个目录、那条沙箱外的命令；碰到密钥目录时不给这个选项。
附件目录和技能目录不算工作目录外，读它们不用问。

### 沙箱

bash 默认关在沙箱里（macOS 用系统自带的 `sandbox-exec`，Linux 用 bubblewrap，Linux 这条还没在真机上测过）：

- 能读大部分文件，读不到 `~/.ssh`、`~/.aws`、`~/.gnupg`、`~/.netrc`、`~/.config/gh` 和 PocketAgent 自己的目录（有 API Key）
- 只能写工作目录、临时目录和 `~/.cache`、`~/Library/Caches`；工作目录里的 `.git/hooks`、`.git/config` 也不能写
- 默认不能联网（⚙ 设置 → 通用里可以打开）

被拦下时工具结果会提示模型；它确实需要时（装依赖、`git push`、写别处）会带 `outside_sandbox=true` 重新执行，
这一次要你确认，界面上的工具卡片标着「沙箱外」。机器上没有可用的沙箱时命令直接执行，设置里会说明。

### 检查点

影子 git 仓库放在 `~/.pocketagent/shadow/<工作区路径的哈希>/`，`GIT_DIR` 指向它、`GIT_WORK_TREE` 指向工作区，
不碰工作区自己的 `.git`，不读你的全局 git 配置，`.gitignore` 照常生效，`node_modules`、`.venv` 之类默认排除。
任务开始时和每一轮开始前给整个工作区拍一张，所以 bash 改的、新建的、删的都看得到；
撤销和恢复之前都会先再拍一张，撤销本身也能撤销。工作区大到拍一张超过 30 秒，这个任务的检查点就关掉，不卡对话。

- 工作区是「一个目录下放好几个仓库」时，git 只会把嵌套的仓库记成一个指针、看不到里面的改动，
  所以每个嵌套仓库单独一个影子仓库，各自的 `.gitignore` 照常生效；变更列表里的路径还是相对工作区的
- 每一轮的快照和第一次调用模型同时进行，工具动工作区之前一定拍完；新任务的 index 从同一工作区上一个任务复制，
  只重新读改过的文件

### 技能

按 [Agent Skills](https://agentskills.io) 开放标准，和 Claude Code、Codex 等用的是同一种格式：一个目录一个技能，
目录里的 `SKILL.md` 开头写 `name` 和 `description`，正文写怎么做，旁边可以放 `scripts/`、`references/`、`assets/`。

- 系统提示里放 `<available_skills>` 清单（名字、描述、SKILL.md 的位置），模型用得上时调 `activate_skill`
  拿到正文（`<skill_content>` 包着，附带技能目录和文件清单），也可以直接 read_file；同一个技能不重复加载
- 消息开头写 `/技能名` 直接启用（命令行里 `/skills` 列出全部）
- 压缩上下文时，加载过的技能正文原样保留，不进摘要
- 不合规范（名字不是小写短横线、和目录名不一致、描述超过 1024 字）只提示、照样加载；没有描述的跳过

找技能的地方，项目级优先于用户级；指向同一个真实目录的（软链）只算一次：

- 工作目录下的 `.pocketagent/skills/`、`.agents/skills/`、`.claude/skills/`
- `~/.pocketagent/skills/`、`~/.agents/skills/`、`~/.claude/skills/`
- ⚙ 设置 → 技能里额外加的目录

已经在别的 agent 里攒了一套技能的话，把 `~/.pocketagent/skills` 软链过去就行。
⚙ 设置 → 技能能看到找到了哪些、点开看正文、不合规范的提示，可以单个关掉。

### 项目记忆

每个工作区一份跨会话的笔记，放在 `~/.pocketagent/projects/<工作区路径>/memory/`，不进仓库。格式和 Claude Code 的 auto memory 一样：
`MEMORY.md` 是索引（一行一条），每条记忆一个文件，开头写 `name` / `description` / `type`（user / feedback / project / reference）。

- 开任务时把 `MEMORY.md` 放进系统提示，某条用到时 agent 再去读
- 你说「记住……」它就记；它觉得以后用得上也会自己记，记了会说一声。读写记忆目录不用确认（只读模式除外）
- ⚙ 设置 → 记忆：按工作区看每条记忆、点开看内容、删除（索引里那行一起去掉），也可以整个关掉

### 联网

- `web_search`：智谱 Web Search API（国内直连），密钥不单独配就用配置里智谱模型的密钥
- `web_fetch`：打开网页转成正文，只认 http / https

### 上下文

- 记账：每次模型调用后以接口报的 `prompt_tokens` 为锚点，之后新加的消息按字符估算（中文每字 0.7、其他 0.27 token，
  拿 DeepSeek / 智谱 / Grok 的分词器量过）；图片按 1600 算
- 超过阈值（默认上下文长度的 80%）就在后台总结前面的历史，不打断对话，做完在下一次调用模型前换上；
  快满了（95%）才停下来等。接口报上下文超长时不管开没开自动压缩，都会压缩后重试
- 重试只针对 429 / 5xx / 连接错误，指数退避加抖动，有 `Retry-After` 就按它等；400 / 401 不重试
- 回复超出输出上限（`finish_reason=length`）时，截断的工具调用不执行，让模型拆小了重来；纯文字就让它接着写

### 向你提问

需求有歧义、有几种合理做法要你拍板时，agent 会用 `ask_user` 一次问 1–4 个问题，每个问题 2–4 个选项（推荐的放第一个），
可以单选或多选；每个问题最后都有「其他」，可以自己写。全部选好点提交，也可以跳过让它自己判断（它会在最后说明做了哪些假设）。
问题卡片刷新页面后还在；命令行里按编号回答，0 是自己写，直接回车跳过。没人能回答的时候（单次执行、自动化等太久），
它会自己判断着往下做。

### agent 干活时又发了一句话

在 ⚙ 设置 → 通用里选：

- **插话**（默认）：马上放进对话，模型下一次调用就能看到，适合纠正方向、补充要求。模型刚说完最后一句时插的话，会接着作为新的一轮执行。
- **排队**：等这一轮做完再作为新的一轮执行，适合接着布置下一件事。排队的消息显示在输入框上方，可以删掉。

点停止时，还没用上的插话和排队消息会退回输入框。

### 附件

图片、文本文件、其他文件都能发。文件存在 `~/.pocketagent/attachments/<任务>/`，不进工作区：

- 图片：模型配置里勾了「能看图」就直接发给模型（长边超过 2048 的上传时另存一份缩小的，编码结果有缓存，不会每次调用都重读）；
  不能看图的模型只告诉它文件路径。
- 文本文件：内容直接拼进消息（超过 5 万字只附前面，完整的让它用 read_file 读）。
- 其他文件：告诉模型保存的路径，需要时它用工具去看。

### 用量

⚙ 设置 → 用量：本机记录的每次模型调用（`~/.pocketagent/usage.jsonl`），今天 / 近 7 天 / 近 30 天 / 全部，
近 30 天每日柱状图，按模型的明细（含缓存命中率、接口报的费用）。接口没报 usage 时按字数估算并标出来。
「查询余额」会去各家接口查账户余额（DeepSeek 官方、OpenRouter、OpenAI 兼容网关支持；智谱没有公开接口）。

### 自动化

按计划（每天几点、可选周几，或者每隔 N 分钟）在某个工作区里开一个任务，把一段提示词交给 agent。
只在 web 服务开着时运行，错过的计划下次启动后补跑一次。没人在场审批，所以权限只能选 AI 自动审批或完全访问；
需要人确认的操作会等你 10 分钟，没人处理就拒绝。

### 访问控制

这个服务能在机器上执行任意命令，所以默认带 token：和 Jupyter 一样，第一次用带 `?token=` 的地址打开，之后靠 cookie。
token 第一次启动时生成并存进配置文件，重启服务后浏览器不用重新登录；`--token` 可以指定。默认只监听 127.0.0.1，
`--no-token` 只允许和本机监听一起用。页面里的地址都是相对路径，挂在网关子路径下也能用。

## 配置

配置存在 `~/.pocketagent/config.json`（文件权限 600），在 web 的 ⚙ 设置里改最方便，也可以手动编辑：

```json
{
  "models": [
    {"id": "a1b2c3d4", "name": "DS 4.1 Flash", "model": "deepseek-v4.1-flash",
     "base_url": "https://your-gateway.example.com/v1", "api_key": "sk-...",
     "reasoning_effort": "low", "context_window_tokens": 128000, "temperature": null,
     "extra_body": {}, "supports_images": true, "visible": true}
  ],
  "default_model": "a1b2c3d4",
  "approval_mode": "ask",
  "busy_input_mode": "interject",
  "max_steps": 50,
  "auto_compact": true,
  "compact_threshold_percent": 80,
  "web_search_enabled": true,
  "web_search_engine": "search_std",
  "web_fetch_enabled": true,
  "sandbox_enabled": true,
  "sandbox_network": false,
  "skills_enabled": true,
  "skill_dirs": [],
  "disabled_skills": [],
  "memory_enabled": true,
  "workspaces": ["/Users/me/projects/foo"],
  "user_name": "me",
  "web_token": "..."
}
```

- 思考档位、上下文长度、温度、额外参数、能不能看图都跟着模型走，换模型就一起换。思考档位按接口实测过能用的值来选
  （DeepSeek：关 / 低 / 高 / 最高；智谱：低 / 高 / 最高；OpenRouter：关 / 低 / 中 / 高），OpenRouter 用它自己的
  `reasoning.effort` 写法，其他用顶层 `reasoning_effort`
- `visible: false` 的模型不出现在会话的模型列表里（已经在用它的任务不受影响）
- 每个任务记住自己用的模型和权限，续跑时接着用
- 优先级：命令行参数 > 配置文件 > 环境变量 > 默认值。环境变量（`POCKETAGENT_MODEL` / `POCKETAGENT_BASE_URL` /
  `POCKETAGENT_API_KEY` 等，完整列表见 `pocketagent/config.py`）只在一个模型都没配时才用
- `-m` 可以写配置里模型的名称、id 或模型名；都对不上就用默认模型的接口、只换模型名
- 任务记录、trace、附件、影子仓库、用量、自动化都在 `~/.pocketagent/`（`POCKETAGENT_HOME` 可以改到别处）

## 主循环

用户说一句话就是一轮（turn）。一轮之内，PocketFlow 的图这样走：

```
guard ──think──▶ think ──tool_calls──▶ run_tools ──next──▶ guard
  │                │  ├──continue（用户刚插了话 / 回复被截断）──▶ guard
  │                │  └──compact（接口说上下文超长）──▶ compact
  ├──compact（快满了且没有后台压缩可等）──▶ compact ──▶ think
  │                │                       │
  └──stop──▶ finish ◀──done────────────────┘ stop（用户拒绝）
```

| 节点 | 做什么 |
|---|---|
| `GuardNode` | 调模型前：放进用户插的话；换上做完的后台压缩；步数到上限就停；超过阈值启动后台压缩，快满了就等它 |
| `ThinkNode` | 流式调一次模型（发之前展开附件）；有工具调用就去 run_tools，没有就结束这一轮；处理截断和上下文超长。重试在 provider 里做 |
| `RunToolsNode` | 执行这一步的全部工具调用：全是只读的就并发，否则按顺序；按权限问用户或交给 AI 审批；被拒绝就停下等用户 |
| `CompactNode` | 当场让模型把历史总结成摘要，替换掉原来的消息（平时走后台压缩，不经过这个节点） |
| `FinishNode` | 终点（PocketFlow 遇到没连线的 action 会告警，所以单独设一个） |

和上游 cookbook 里 coding agent 的区别：上游给每个工具建一个节点、action 名就是工具名，模型输出 YAML 再解析，
一步只能调一个工具。这里图只画循环的阶段，工具走原生 function calling，由 `RunToolsNode` 按注册表动态分发，
一步可以调多个工具；加工具、以后接 MCP 都不用改图。

## 分层

```
pocketflow/            上游核心代码原样复制（100 行），不改，见 VENDORED.md
pocketagent/
  agent.py             连线成图 + Agent（run_turn / interject / compact）+ create_agent 组装（工具、沙箱、技能、检查点）
  nodes.py             上面那 5 个节点 + 后台压缩；AgentNode 基类负责发节点进出事件
  context.py           AgentContext：节点间共享的状态（PocketFlow 的 shared store），含 token 锚点
  config.py            配置文件（多个模型、默认值、工作区、沙箱、技能）和运行时 Settings
  llm/                 模型接口：openai_compat（流式、工具调用拼接、重试）、scripted（测试用假模型）
  tools/               工具基类 + 注册表；files / search / shell / web / skill / ask
  permissions.py       权限模式、审批接口；按工具类别和这次调用碰到的地方（工作目录外、沙箱外、密钥）决定
  sandbox.py           bash 沙箱：macOS Seatbelt 规则、Linux bubblewrap 参数
  checkpoints.py       影子 git 检查点：拍快照、列变更、diff、撤销、整体恢复；嵌套仓库各一个影子仓库
  skills.py            技能：找技能、校验、<available_skills> 清单、<skill_content> 正文、/技能名、压缩时保留
  memory.py            项目记忆：目录位置、系统提示里的说明和索引、列出 / 删除
  questions.py         ask_user 的问题和回答：校验、整理成给模型看的结果
  frontmatter.py       解析 Markdown 开头 --- 包着的元数据（技能、记忆共用）
  reviewer.py          AI 自动审批：让模型判断一次工具调用能不能放行
  attachments.py       附件存储；消息里只放引用，发给模型前展开（图片缩小 + 编码缓存）
  tokens.py            上下文 token 估算
  usage.py             token 用量记录和统计
  billing.py           查各家接口的账户余额
  automations.py       自动化的定义、计划计算、存储
  events.py, trace.py  事件总线；trace 是写 JSONL 的订阅者
  session.py           任务（会话）持久化
  compaction.py        压缩：摘要请求、用摘要重建历史
  prompts.py           系统提示词，会拼进 ~/.pocketagent/ 和工作目录下的 AGENTS.md / CLAUDE.md、沙箱说明、技能清单、项目记忆
  cli.py               命令行界面：渲染事件 + 终端审批 + REPL
  web/
    server.py          aiohttp 服务：页面 + REST + 每个任务一条 WebSocket（协议写在文件开头）
    runtime.py         每个任务一个 Agent，插话 / 排队、审批广播；任务、工作区、配置、技能、记忆、自动化调度
    files.py           文件树、文件预览、选目录（只能看登记过的工作区）
    history.py         把存下来的消息转成前端渲染用的条目
    static/            前端：index.html / style.css / js/（ES 模块，不用构建、不依赖 CDN）
tests/                 unittest；vendor_pocketflow/ 是上游测试
```

几条设计约定：

- **流式和 trace 走事件总线**。PocketFlow 的 `exec` 只能一次性返回，没有框架级流式，所以 provider 边收边发事件，
  界面和 trace 都只是订阅者。CLI 和 web 就是两套「订阅者 + 审批实现」（`ConsoleRenderer`/`ConsoleApprover` 和
  `WebSession`/`WebApprover`），agent 本身不知道界面的存在；以后做 TUI、IDE 插件照这个来。
- **消息一律用 OpenAI chat 格式**，new-api、vLLM、DeepSeek、Qwen、OpenRouter 网关都直接吃。
  消息里下划线开头的字段（界面要用的原话、附件清单、工具结果是否出错）只存在会话里，发给模型前去掉。
- **中断不弄坏历史**。一轮被中断或出错时，给没拿到结果的 tool_call 补上 tool 消息，不然下次请求会 400。
  按顺序执行时已经跑完的结果照实写进历史。
- **权限看工具类别和这次碰到哪里**。工具用 `access()` 报出要碰的路径、要不要跳出沙箱，`PermissionGate.check` 据此决定；
  AI 审批不放行、审查出错都转给用户，没人在场时就是拒绝。

## 怎么扩展

- **加工具**：在 `pocketagent/tools/` 继承 `Tool`，填 `name / description / kind / parameters`，实现 `run`，
  在 `default_tool_registry()` 里注册。要在审批时显示 diff 就覆盖 `preview`；按路径干活的覆盖 `access`，
  工作目录外、密钥目录的审批就自动生效。改了文件不用做什么，检查点拍整个工作区。
- **加技能**：不用写代码，放一个带 `SKILL.md` 的目录进技能目录。
- **换模型协议**：实现 `LLMProvider.complete(messages, tools, listener) -> LLMResponse`，传给 `create_agent(provider=...)`。
- **换界面**：订阅 `agent.context.events`，实现 `Approver.request_approval` 和 `Approver.ask_questions`。
- **改流程**：在 `nodes.py` 加节点、在 `agent.py` 的 `build_agent_flow` 里连线。子流程可以直接用 PocketFlow 的 Flow 嵌套。

## 路线图

对照 Codex / Claude Code / WorkBuddy / Qoder，还缺这些，大致按优先级排：

1. **MCP 客户端**：把 MCP server 的工具包装成 `Tool` 注册进来
2. **子 agent**：一个工具起一个独立上下文的 `Agent`，只把结论交回来
3. **Anthropic 等其他协议的 provider**（现在只有 OpenAI 兼容接口）
4. **todo / plan 工具**
5. **hooks**（工具调用前后跑用户脚本）
6. **沙箱联网白名单**：现在只有开 / 关，可以像 Claude Code 那样走代理按域名放行；Linux bubblewrap 要在真机上验证
7. **trace 回放和评测**：拿 JSONL trace 重放、对比不同模型或提示词
8. **界面**：TUI、IDE 插件；web 里搜索任务
9. grep 换成 ripgrep

## 测试

```bash
make test                                 # = python -m unittest discover -s tests -t .
make test PYTHON=~/env/s11/bin/python
```

测试不连网络，模型用 `ScriptedProvider` 按剧本返回。web 测试用 aiohttp 自带的测试客户端走真实的 HTTP 和 WebSocket，
没装 aiohttp 时自动跳过；沙箱测试只在有 `sandbox-exec` 的 macOS 上跑。
