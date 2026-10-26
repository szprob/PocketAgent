# PocketAgent

基于 [PocketFlow](https://github.com/The-Pocket/PocketFlow) 的编程 agent，目标是做成 Codex / Claude Code / WorkBuddy / Qoder 这一类：
在本地仓库里读代码、改文件、跑命令，用户在旁边审批。

现在是骨架：主循环、7 个内置工具、权限审批、流式事件、trace、会话续跑、上下文压缩、命令行界面和 web 界面都已经能用。
新功能往上面加就行。

## 快速开始

核心只依赖 `openai`（用它的 SDK 调 OpenAI 兼容接口），web 界面另外要 `aiohttp`，Python ≥ 3.11。

```bash
# 可编辑安装：改源码立即生效，不用重装（只有改了 pyproject 的依赖或入口才要重跑）
uv pip install -e '.[web]'          # 或 pip install -e '.[web]'

pocketagent --web                   # 起 web 界面并自动打开浏览器；第一次在右上角 ⚙ 设置里添加模型和 API Key
pocketagent                         # 命令行交互，在当前目录工作
pocketagent -C ~/projects/foo --mode auto_edit
pocketagent -c                      # 继续这个目录最近的会话
pocketagent -m "DS 4.1 Flash" -p "跑一下测试，把失败的修好" --mode full_auto   # 指定模型、单次执行后退出
```

不安装也能用：在仓库根目录 `python -m pocketagent ...`。

交互模式里的命令：`/help`、`/new`、`/compact`、`/mode [read_only|ask|auto_edit|full_auto]`、`/exit`。
Ctrl-C 只中断当前这一轮，会话保留，可以接着说。

### web 界面

```bash
pocketagent --web -C ~/projects/foo     # 自动打开浏览器；8765 被占用就往后找空闲端口，可以同时给几个项目各开一个
pocketagent --web --no-browser          # 不自动打开（比如在远端机器上起）
pocketagent --web --host 0.0.0.0        # 让别的机器访问（比如开发机上起、经网关转发）
```

能做的事：

- 流式显示回复（Markdown 带表格和代码块）、折叠的思考过程，每个工具调用一张卡片，点开看输出
- 审批卡片：改文件的显示 diff；按钮有「允许」「本会话总是允许」「拒绝」
- 停止按钮（或按 Esc）中断当前这一轮；顶栏切模型、切审批模式、手动压缩
- ⚙ 设置：添加 / 修改 / 删除模型（名称、模型名、接口地址、API Key、思考档位、上下文长度、温度、额外请求参数），
  每个模型可以「测试连接」、设为默认；还有新会话的默认审批模式、单轮最多调用模型次数。保存后立刻对所有会话生效
- 侧栏列出这个目录的历史会话，点一下就续上（连同它用的模型）；刷新页面、断线重连都会重放完整历史
- 同一会话可以开多个标签页一起看，任何一个标签页都能点审批
- 自动跟随系统的亮色/暗色，窄屏下侧栏变成抽屉

这个服务能在机器上执行任意命令，所以默认带 token：和 Jupyter 一样，第一次用带 `?token=` 的地址打开，之后靠 cookie。
token 第一次启动时生成并存进配置文件，重启服务后浏览器不用重新登录；`--token` 可以指定。默认只监听 127.0.0.1，
`--no-token` 只允许和本机监听一起用。页面里的地址都是相对路径，挂在网关子路径下也能用。

### 配置

配置存在 `~/.pocketagent/config.json`（文件权限 600），在 web 的 ⚙ 设置里改最方便，也可以手动编辑：

```json
{
  "models": [
    {"id": "a1b2c3d4", "name": "DS 4.1 Flash", "model": "deepseek-v4.1-flash",
     "base_url": "https://your-gateway.example.com/v1", "api_key": "sk-...",
     "reasoning_effort": "low", "context_window_tokens": 128000, "temperature": null, "extra_body": {}}
  ],
  "default_model": "a1b2c3d4",
  "approval_mode": "ask",
  "max_steps": 50,
  "web_token": "..."
}
```

- 思考档位、上下文长度、温度、额外参数都跟着模型走，换模型就一起换；上下文长度决定什么时候自动压缩
- 优先级：命令行参数 > 配置文件 > 环境变量 > 默认值。环境变量（`POCKETAGENT_MODEL` / `POCKETAGENT_BASE_URL` /
  `POCKETAGENT_API_KEY` 等，完整列表见 `pocketagent/config.py`）只在一个模型都没配时才用
- `-m` 可以写配置里模型的名称、id 或模型名；都对不上就用默认模型的接口、只换模型名
- 会话和 trace 也在 `~/.pocketagent/`（`POCKETAGENT_HOME` 可以改到别处）

## 主循环

用户说一句话就是一轮（turn）。一轮之内，PocketFlow 的图这样走：

```
guard ──think──▶ think ──tool_calls──▶ run_tools ──next──▶ guard
  │                │                       │
  ├──compact──▶ compact ──▶ think          │
  │                │                       │
  └──stop──▶ finish ◀──done────────────────┘ stop（用户拒绝）
```

| 节点 | 做什么 |
|---|---|
| `GuardNode` | 调模型前检查：步数到上限就停，上下文快满了先去压缩 |
| `ThinkNode` | 流式调一次模型；有工具调用就去 run_tools，没有就结束这一轮。网络错误用 PocketFlow 自带的重试 |
| `RunToolsNode` | 执行这一步的全部工具调用：全是只读的就并发，否则按顺序；被拒绝就停下等用户 |
| `CompactNode` | 让模型把历史总结成摘要，替换掉原来的消息 |
| `FinishNode` | 终点（PocketFlow 遇到没连线的 action 会告警，所以单独设一个） |

和上游 cookbook 里 coding agent 的区别：上游给每个工具建一个节点、action 名就是工具名，模型输出 YAML 再解析，
一步只能调一个工具。这里图只画循环的阶段，工具走原生 function calling，由 `RunToolsNode` 按注册表动态分发，
一步可以调多个工具；加工具、以后接 MCP 都不用改图。

## 分层

```
pocketflow/            上游核心代码原样复制（100 行），不改，见 VENDORED.md
pocketagent/
  agent.py             连线成图 + Agent.run_turn + create_agent 组装
  nodes.py             上面那 5 个节点；AgentNode 基类负责发节点进出事件
  context.py           AgentContext：节点间共享的状态（PocketFlow 的 shared store）
  config.py            配置文件 ~/.pocketagent/config.json（多个模型 + 默认值）和运行时 Settings
  llm/                 模型接口：openai_compat（流式 + 工具调用拼接）、scripted（测试用假模型）
  tools/               工具基类 + 注册表；files / search / shell 三组内置工具
  permissions.py       审批模式和审批接口
  events.py, trace.py  事件总线；trace 是写 JSONL 的订阅者
  session.py           会话持久化（续跑）
  compaction.py        上下文估算和压缩
  prompts.py           系统提示词，会拼进 ~/.pocketagent/ 和工作目录下的 AGENTS.md / CLAUDE.md
  cli.py               命令行界面：渲染事件 + 终端审批 + REPL
  web/
    server.py          aiohttp 服务：页面 + REST + 每个会话一条 WebSocket（协议写在文件开头）
    runtime.py         每个会话一个 Agent；把事件和审批请求广播给所有标签页
    history.py         把存下来的消息转成前端渲染用的条目
    static/            前端：index.html / style.css / app.js，不用构建、不依赖 CDN
tests/                 unittest；vendor_pocketflow/ 是上游测试
```

几条设计约定：

- **流式和 trace 走事件总线**。PocketFlow 的 `exec` 只能一次性返回，没有框架级流式，所以 provider 边收边发事件，
  界面和 trace 都只是订阅者。CLI 和 web 就是两套「订阅者 + 审批实现」（`ConsoleRenderer`/`ConsoleApprover` 和
  `WebSession`/`WebApprover`），agent 本身一行没改；以后做 TUI、IDE 插件照这个来。
- **消息一律用 OpenAI chat 格式**，new-api、vLLM、DeepSeek、Qwen 网关都直接吃。接别的协议时在各自的 provider 里转换。
- **中断不弄坏历史**。一轮被 Ctrl-C 中断或出错时，给没拿到结果的 tool_call 补上 tool 消息，不然下次请求会 400。
  按顺序执行时已经跑完的结果照实写进历史。
- **权限按工具类别**（只读 / 改文件 / 执行），四种审批模式对应 Codex 的 suggest / auto-edit / full-auto 和 Claude Code 的 plan 模式。

## 怎么扩展

- **加工具**：在 `pocketagent/tools/` 继承 `Tool`，填 `name / description / kind / parameters`，实现 `run`，
  在 `default_tool_registry()` 里注册。要在审批时显示 diff 就覆盖 `preview`。
- **换模型协议**：实现 `LLMProvider.complete(messages, tools, listener) -> LLMResponse`，传给 `create_agent(provider=...)`。
- **换界面**：订阅 `agent.context.events`，实现 `Approver.request_approval`。
- **改流程**：在 `nodes.py` 加节点、在 `agent.py` 的 `build_agent_flow` 里连线。子流程可以直接用 PocketFlow 的 Flow 嵌套。

## 路线图

对照 Codex / Claude Code / WorkBuddy / Qoder，还缺这些，大致按优先级排：

1. **MCP 客户端**：把 MCP server 的工具包装成 `Tool` 注册进来
2. **Skills**：扫描 `SKILL.md`，把名字和描述放进系统提示，按需加载正文
3. **子 agent**：一个工具起一个独立上下文的 `Agent`，只把结论交回来
4. **沙箱**：bash 现在只靠审批，可以参考 Codex 接 macOS `sandbox-exec` 或 Linux bubblewrap
5. **界面**：web 已有基础版，还缺切换工作目录、上传图片、在页面里看文件和 diff 历史、工具结果的错误状态持久化
   （刷新后历史里的工具卡片一律显示成功）；另外还有 TUI 和 IDE 插件
6. **Anthropic 等其他协议的 provider**（现在只有 OpenAI 兼容接口）
7. **todo / plan 工具、web_fetch、图片输入**
8. **hooks**（工具调用前后跑用户脚本）
9. **trace 回放和评测**：拿 JSONL trace 重放、对比不同模型或提示词
10. grep 换成 ripgrep；更准的 token 计数

## 测试

```bash
make test                                 # = python -m unittest discover -s tests -t .
make test PYTHON=~/env/s11/bin/python
```

测试不连网络，模型用 `ScriptedProvider` 按剧本返回。web 测试用 aiohttp 自带的测试客户端走真实的 HTTP 和 WebSocket，
没装 aiohttp 时自动跳过。
