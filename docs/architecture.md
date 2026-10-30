# 架构

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
一步可以调多个工具；加工具、接 MCP 都不用改图。

## 分层

```
pocketflow/              上游核心代码原样复制（100 行），不改，见 VENDORED.md
pocketagent/
  cli/                   命令行前端包：默认 REPL 与可选的全屏 TUI
    console.py           原命令行入口、事件渲染、终端审批和 REPL
    controller.py        REPL / TUI 共用的会话、斜杠命令动作和模型切换
    tui.py               Textual 界面、事件渲染、审批 / 提问弹窗
    __init__.py          兼容 pocketagent.cli:main 和原 Console* 导入；不提前加载 Textual
  config.py              配置文件（多个模型、默认值、工作区、沙箱、技能）和运行时 Settings
  core/                  agent 主循环
    agent.py             连线成图 + Agent（run_turn / interject / compact）+ create_agent 组装（工具、沙箱、技能、检查点）
    nodes.py             上面那 5 个节点 + 后台压缩；AgentNode 基类负责发节点进出事件
    context.py           AgentContext：节点间共享的状态（PocketFlow 的 shared store），含 token 锚点
    prompts.py           系统提示词，会拼进 ~/.pocketagent/ 和工作目录下的 AGENTS.md / CLAUDE.md、沙箱说明、技能清单、项目记忆
    compaction.py        压缩：摘要请求、用摘要重建历史
    tokens.py            上下文 token 估算
    events.py, trace.py  事件总线；trace 是写 JSONL 的订阅者
    session.py           任务（会话）持久化：全量 JSON + 一轮进行中的消息日志（按节点边界追加）
    subagents.py         子 agent：同一张图、自己的上下文和工具集，进度转成主 agent 的事件
    tool_outputs.py      工具输出落盘：超过上限的完整输出写到 ~/.pocketagent/tool_outputs
    scratch.py           临时任务的工作目录（~/.pocketagent/scratch，不指定工作区时用）
  llm/                   模型接口：openai_compat（流式、工具调用拼接、重试）、anthropic（Messages 接口转换）、scripted（测试用假模型）
  tools/                 工具基类 + 注册表；files / search / shell / background / web / image / computer / skill / ask / todo / subagent
    _background_supervisor.py  独立监督进程：等待后台命令并落盘最终状态
    ripgrep.py           ripgrep 的调用和 --json 解析；rg 用不了时 search.py 回退 Python 实现
  safety/                权限和兜底
    permissions.py       权限模式、审批接口；按工具类别和这次调用碰到的地方（工作目录外、沙箱外、密钥）决定
    hooks.py             钩子：在几个固定时机跑用户脚本，可以拦住工具调用、给模型补上下文
    reviewer.py          AI 自动审批：让模型判断一次工具调用能不能放行
    sandbox.py           bash 沙箱：macOS Seatbelt 规则、Linux bubblewrap 参数
    checkpoints.py       影子 git 检查点：拍快照、列变更、diff、撤销、整体恢复；嵌套仓库各一个影子仓库
    processes.py         跨平台子进程：找 bash（Windows 用 Git Bash）、结束整棵进程树（POSIX 杀进程组，Windows 用 taskkill）
  knowledge/             技能和记忆
    skills.py            技能：找技能、校验、<available_skills> 清单、<skill_content> 正文、/技能名、压缩时保留
    memory.py            项目记忆：目录位置、系统提示里的说明和索引、列出 / 删除
    frontmatter.py       解析 Markdown 开头 --- 包着的元数据（技能、记忆共用）
  interaction/           和用户打交道
    questions.py         ask_user 的问题和回答：校验、整理成给模型看的结果
    modes.py             工作模式：Agent / Ask / 计划 / 设计，切换建议、提交方案等批准
    attachments.py       附件存储；消息里只放引用，发给模型前展开（图片缩小 + 编码缓存）
    commands.py          斜杠命令：命令行和 web 共用一套
  account/
    usage.py             token 用量记录和统计
    billing.py           查各家接口的账户余额
  server/                HTTP + WebSocket 后端，web、桌面端等所有前端共用（协议写在 __init__.py）
    app.py               组装和启动：token 鉴权、错误转 JSON、路由、web 前端的静态页面、定时任务调度
    routes/              按资源分的接口：sessions / workspace / settings / commands / automations / socket
    session.py           一个任务一个 Agent（WebSession）：插话 / 排队、审批和提问广播给所有连接
    manager.py           所有任务、工作区、配置、技能、记忆（SessionManager），自动化调度
    worktrees.py         从 HEAD 创建任务 worktree、把隔离任务 / 子 agent 的改动提交到分支，失败时回滚新建目录和分支
    automations.py       自动化：跑什么（工作区 / 提示词 / 模型 / 权限）+ 运行记录 + 存储
    triggers/
      model.py           什么时候跑：四种事件源、四种条件、Occurrence、校验和老格式归一
      engine.py          唯一入口：匹配 → 去重 → 环路频次 → 并发 → 条件 → 开任务
    files.py             文件树、文件预览、选目录（只能看登记过的工作区）
    history.py           把存下来的消息转成前端渲染用的条目
  web/                   浏览器前端（ES 模块，不用构建、不依赖 CDN），由 server 一起服务
    static/              index.html / style.css / js/
      js/                main.js 入口；core/ 不碰 DOM 的接口和状态，ui/ 通用界面件，
                         task/ 任务页，sidebar/ 左栏，settings/ 设置，automations/ 自动化
desktop/                 Electron 客户端：前端独立实现，只调 server 的 REST + WebSocket
tests/                   unittest；mock/ 是快速离线用例，live/ 调真实免费 API，vendor_pocketflow/ 是上游测试
```

## 设计约定

- **流式和 trace 走事件总线**。PocketFlow 的 `exec` 只能一次性返回，没有框架级流式，所以 provider 边收边发事件，
  界面和 trace 都只是订阅者。CLI 和 web 就是两套「订阅者 + 审批实现」（`ConsoleRenderer`/`ConsoleApprover` 和
  `WebSession`/`WebApprover`），agent 本身不知道界面的存在；以后做 TUI、IDE 插件照这个来。
- **消息一律用 OpenAI chat 格式**，new-api、vLLM、DeepSeek、Qwen、OpenRouter 网关都直接吃。
  消息里下划线开头的字段（界面要用的原话、附件清单、工具结果是否出错）只存在会话里，发给模型前去掉。
- **中断不弄坏历史**。一轮被中断或出错时，给没拿到结果的 tool_call 补上 tool 消息，不然下次请求会 400。
  按顺序执行时已经跑完的结果照实写进历史。
- **权限看工具类别和这次碰到哪里**。工具用 `access()` 报出要碰的路径、要不要跳出沙箱，`PermissionGate.check` 据此决定；
  AI 审批不放行、审查出错都转给用户，没人在场时就是拒绝。
