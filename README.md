# PocketAgent

基于 [PocketFlow](https://github.com/The-Pocket/PocketFlow) 的编程 agent，目标是做成 Codex / Claude Code / WorkBuddy / Qoder 这一类：
在本地仓库里读代码、改文件、跑命令，用户在旁边审批。

已经能用的：主循环、7 个内置工具、三档权限（含 AI 自动审批）、流式事件、trace、会话续跑、上下文压缩、
多模型配置、附件（图片 / 文件）、插话和排队、文件变更跟踪和撤销、token 用量统计、定时自动化，
以及命令行和 web 两套界面。新功能往上面加就行。

## 快速开始

核心只依赖 `openai`（用它的 SDK 调 OpenAI 兼容接口），web 界面另外要 `aiohttp`，Python ≥ 3.11。

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

- **左侧栏**：新任务、自动化；工作区列表（点开看这个工作区的任务，悬停可以在这个工作区开新任务或移除）；
  最近任务（全部历史，带所在工作区和时间，运行中的有蓝点）；最下面是账户名和 ⚙ 设置。
- **中间**：新任务页选工作区、模型、权限后直接说话；任务页是对话。输入框底下一排：📎 附件（也可以粘贴截图、拖文件进来）、
  权限（每次询问 / AI 自动审批 / 完全访问）、模型、发送 / 停止；再下面是任务所在的工作区和上下文占用。
  标题旁的 ··· 可以重命名、压缩上下文、删除任务。
- **右侧栏**（右上角按钮开关）：任务概要（最新进展、本任务 token 用量、上下文）、文件变更（点开看 diff，可以撤销这个文件的改动）、
  产出（这个任务新建的文件）。

### 权限

| 选项 | 只读工具 | 改文件 / 执行命令 |
|---|---|---|
| 每次询问 | 直接放行 | 每次弹审批卡片（改文件显示 diff） |
| AI 自动审批 | 直接放行 | 先让当前模型判断是否安全、是否在任务范围内；判定安全就放行，否则照常问你 |
| 完全访问 | 直接放行 | 全部放行 |

审批卡片上可以选「本任务总是允许这个工具」。

### agent 干活时又发了一句话

在 ⚙ 设置 → 通用里选：

- **插话**（默认）：马上放进对话，模型下一次调用就能看到，适合纠正方向、补充要求。模型刚说完最后一句时插的话，会接着作为新的一轮执行。
- **排队**：等这一轮做完再作为新的一轮执行，适合接着布置下一件事。排队的消息显示在输入框上方，可以删掉。

点停止时，还没用上的插话和排队消息会退回输入框。

### 附件

图片、文本文件、其他文件都能发。文件存在 `~/.pocketagent/attachments/<任务>/`，不进工作区：

- 图片：模型配置里勾了「能看图」就直接发给模型；不能看图的模型只告诉它文件路径。
- 文本文件：内容直接拼进消息（超过 5 万字只附前面，完整的让它用 read_file 读）。
- 其他文件：告诉模型保存的路径，需要时它用工具去看。

### 用量

⚙ 设置 → 用量：本机记录的每次模型调用（`~/.pocketagent/usage.jsonl`），今天 / 近 7 天 / 近 30 天 / 全部，
近 30 天每日柱状图，按模型的明细。接口没报 usage 时按字数估算并标出来。
「查询余额」会去各家接口查账户余额（DeepSeek 官方、OpenRouter、OpenAI 兼容网关支持；智谱没有公开接口）。

### 自动化

按计划（每天几点、可选周几，或者每隔 N 分钟）在某个工作区里开一个任务，把一段提示词交给 agent。
只在 web 服务开着时运行，错过的计划下次启动后补跑一次。没人在场审批，所以权限只能选 AI 自动审批或完全访问；
AI 审批不放行的操作会等你 10 分钟，没人处理就拒绝。

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
     "extra_body": {}, "supports_images": true}
  ],
  "default_model": "a1b2c3d4",
  "approval_mode": "ask",
  "busy_input_mode": "interject",
  "max_steps": 50,
  "workspaces": ["/Users/me/projects/foo"],
  "user_name": "me",
  "web_token": "..."
}
```

- 思考档位、上下文长度、温度、额外参数、能不能看图都跟着模型走，换模型就一起换；上下文长度决定什么时候自动压缩
- 每个任务记住自己用的模型和权限，续跑时接着用
- 优先级：命令行参数 > 配置文件 > 环境变量 > 默认值。环境变量（`POCKETAGENT_MODEL` / `POCKETAGENT_BASE_URL` /
  `POCKETAGENT_API_KEY` 等，完整列表见 `pocketagent/config.py`）只在一个模型都没配时才用
- `-m` 可以写配置里模型的名称、id 或模型名；都对不上就用默认模型的接口、只换模型名
- 任务记录、trace、附件、变更备份、用量、自动化都在 `~/.pocketagent/`（`POCKETAGENT_HOME` 可以改到别处）

## 主循环

用户说一句话就是一轮（turn）。一轮之内，PocketFlow 的图这样走：

```
guard ──think──▶ think ──tool_calls──▶ run_tools ──next──▶ guard
  │                │  └──continue（模型说完了但用户刚插了话）──▶ guard
  ├──compact──▶ compact ──▶ think          │
  │                │                       │
  └──stop──▶ finish ◀──done────────────────┘ stop（用户拒绝）
```

| 节点 | 做什么 |
|---|---|
| `GuardNode` | 调模型前：先把用户插的话放进对话；步数到上限就停；上下文快满了先去压缩 |
| `ThinkNode` | 流式调一次模型（发之前展开附件）；有工具调用就去 run_tools，没有就结束这一轮。网络错误用 PocketFlow 自带的重试 |
| `RunToolsNode` | 执行这一步的全部工具调用：全是只读的就并发，否则按顺序；按权限问用户或交给 AI 审批；被拒绝就停下等用户 |
| `CompactNode` | 让模型把历史总结成摘要，替换掉原来的消息 |
| `FinishNode` | 终点（PocketFlow 遇到没连线的 action 会告警，所以单独设一个） |

和上游 cookbook 里 coding agent 的区别：上游给每个工具建一个节点、action 名就是工具名，模型输出 YAML 再解析，
一步只能调一个工具。这里图只画循环的阶段，工具走原生 function calling，由 `RunToolsNode` 按注册表动态分发，
一步可以调多个工具；加工具、以后接 MCP 都不用改图。

## 分层

```
pocketflow/            上游核心代码原样复制（100 行），不改，见 VENDORED.md
pocketagent/
  agent.py             连线成图 + Agent（run_turn / interject / compact）+ create_agent 组装
  nodes.py             上面那 5 个节点；AgentNode 基类负责发节点进出事件
  context.py           AgentContext：节点间共享的状态（PocketFlow 的 shared store）
  config.py            配置文件（多个模型、默认值、工作区）和运行时 Settings
  llm/                 模型接口：openai_compat（流式 + 工具调用拼接）、scripted（测试用假模型）
  tools/               工具基类 + 注册表；files / search / shell 三组内置工具
  permissions.py       权限模式和审批接口
  reviewer.py          AI 自动审批：让模型判断一次工具调用能不能放行
  attachments.py       附件存储；消息里只放引用，发给模型前展开
  changes.py           记下每个任务改过的文件的原始内容，给出 diff、支持撤销
  usage.py             token 用量记录和统计
  billing.py           查各家接口的账户余额
  automations.py       自动化的定义、计划计算、存储
  events.py, trace.py  事件总线；trace 是写 JSONL 的订阅者
  session.py           任务（会话）持久化
  compaction.py        上下文估算和压缩
  prompts.py           系统提示词，会拼进 ~/.pocketagent/ 和工作目录下的 AGENTS.md / CLAUDE.md
  cli.py               命令行界面：渲染事件 + 终端审批 + REPL
  web/
    server.py          aiohttp 服务：页面 + REST + 每个任务一条 WebSocket（协议写在文件开头）
    runtime.py         每个任务一个 Agent，插话 / 排队、审批广播；任务、工作区、配置、自动化调度
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
- **权限按工具类别**（只读 / 改文件 / 执行）。AI 审批不放行、审查出错都转给用户，没人在场时就是拒绝。

## 怎么扩展

- **加工具**：在 `pocketagent/tools/` 继承 `Tool`，填 `name / description / kind / parameters`，实现 `run`，
  在 `default_tool_registry()` 里注册。要在审批时显示 diff 就覆盖 `preview`；会写文件的先调 `context.before_write(path)`，
  右侧栏的变更才看得到。
- **换模型协议**：实现 `LLMProvider.complete(messages, tools, listener) -> LLMResponse`，传给 `create_agent(provider=...)`。
- **换界面**：订阅 `agent.context.events`，实现 `Approver.request_approval`。
- **改流程**：在 `nodes.py` 加节点、在 `agent.py` 的 `build_agent_flow` 里连线。子流程可以直接用 PocketFlow 的 Flow 嵌套。

## 路线图

对照 Codex / Claude Code / WorkBuddy / Qoder，还缺这些，大致按优先级排：

1. **MCP 客户端**：把 MCP server 的工具包装成 `Tool` 注册进来
2. **Skills**：扫描 `SKILL.md`，把名字和描述放进系统提示，按需加载正文
3. **子 agent**：一个工具起一个独立上下文的 `Agent`，只把结论交回来
4. **沙箱**：bash 现在只靠审批，可以参考 Codex 接 macOS `sandbox-exec` 或 Linux bubblewrap；bash 改的文件也还没进变更跟踪
5. **Anthropic 等其他协议的 provider**（现在只有 OpenAI 兼容接口）
6. **todo / plan 工具、web_fetch**
7. **hooks**（工具调用前后跑用户脚本）
8. **trace 回放和评测**：拿 JSONL trace 重放、对比不同模型或提示词
9. **界面**：TUI、IDE 插件；web 里看文件树、搜索任务
10. grep 换成 ripgrep；更准的 token 计数

## 测试

```bash
make test                                 # = python -m unittest discover -s tests -t .
make test PYTHON=~/env/s11/bin/python
```

测试不连网络，模型用 `ScriptedProvider` 按剧本返回。web 测试用 aiohttp 自带的测试客户端走真实的 HTTP 和 WebSocket，
没装 aiohttp 时自动跳过。
