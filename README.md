# PocketAgent

基于 [PocketFlow](https://github.com/The-Pocket/PocketFlow) 的编程 agent，目标是做成 Codex / Claude Code / WorkBuddy / Qoder 这一类：
在本地仓库里读代码、改文件、跑命令，用户在旁边审批。

已经能用的：主循环、内置工具（文件、搜索、bash（含跨重启的持久后台命令）、联网搜索、读网页、生图 / 搜图、技能、向用户提问、子 agent）、多档权限（含 AI 自动审批）、
bash 沙箱、工作目录外的访问审批、影子 git 检查点（看变更、撤销、整体恢复）、技能（Agent Skills 标准）、项目记忆、
流式事件、trace、会话续跑、后台自动压缩上下文、多模型配置（OpenAI 兼容 / Anthropic 两种协议，思考档位 / 上下文长度随模型走）、
MCP、钩子（几个固定时机跑你自己的脚本，能拦住工具调用）、附件（图片 / 文件）、插话和排队、token 用量和缓存命中统计、
自动化（到点 / 上一个任务跑完 / 外部 POST / 轮询检查都能当触发），
以及命令行 REPL、全屏 TUI 和 web 三套界面。
新任务默认就地编辑；前台任务派出的、会改文件的子 agent 自动在独立 git worktree 里做，互不干扰。grep / glob 优先用 ripgrep（遵守 .gitignore），没装 rg 时回退纯 Python 实现。

设计原则（可学习性优先、小内核、权限是架构的一部分等）见 [docs/design.md](docs/design.md)。

## 快速开始

核心依赖 `openai`（用它的 SDK 调 OpenAI 兼容接口）和 `httpx`；web 界面另外要 `aiohttp`、`pillow`，全屏 TUI 另外要 `textual`。Python ≥ 3.11。

```bash
# 可编辑安装：改源码立即生效，不用重装（只有改了 pyproject 的依赖或入口才要重跑）
uv pip install -e '.[web]'          # 或 pip install -e '.[web]'
uv pip install -e '.[tui]'          # 要用全屏交互式终端时再装这个 extra

pocketagent --web                   # 起 web 界面并自动打开浏览器；第一次在左下角 ⚙ 设置里添加模型和 API Key
pocketagent --tui                   # 全屏交互式终端（Textual）
pocketagent                         # 默认仍是轻量命令行 REPL，在当前目录工作
pocketagent -C ~/projects/foo --mode ai_auto
pocketagent --scratch                # 不指定工作区：在 ~/.pocketagent/scratch 下建个临时目录跑
pocketagent -c                      # 继续这个目录最近的会话
pocketagent -m "DS 4.1 Flash" -p "跑一下测试，把失败的修好" --mode full_auto   # 指定模型、单次执行后退出
```

不安装也能用：在仓库根目录 `python -m pocketagent ...`。

### Windows

Windows 上 web、命令行和桌面端都能跑，装依赖和启动换成：

```powershell
py -3.12 -m pip install -e ".[web]"
py -3.12 -m pocketagent --web -C D:\SZ\git\foo
```

和 macOS / Linux 的差别：

- bash 工具用 Git for Windows 自带的 `bash.exe`：不在 PATH 时按默认安装位置找（`%ProgramFiles%\Git`、
  `%LOCALAPPDATA%\Programs\Git`），都没有就提示装 Git，不会静默失败
- 没有沙箱：命令直接执行，系统提示里写明「不在沙箱里」，审批模式是唯一的防线
- 命令超时或点停止时结束整棵进程树（POSIX 杀进程组，Windows 用 `taskkill /T`）
- `screenshot` / `computer` 只在 macOS 上注册
- 桌面端默认用 `py -3` 启动后端；Python 装在别处就设 `POCKETAGENT_PYTHON`
- 测试用 `py -m unittest discover -s tests/mock -t .` 和 `py -m unittest discover -s tests/vendor_pocketflow -t .`
  （Windows 上不一定有 make）；`tests/vendor_pocketflow/` 里上游的并行 batch 测试依赖字典插入顺序，
  在 Windows 上会挂，和本项目代码无关

## 文档

详细文档在 [docs/](docs/)：

| 文档 | 内容 |
|---|---|
| [设计哲学](docs/design.md) | 八条设计原则、对扩展的约束、设计边界 |
| [架构](docs/architecture.md) | 主循环、代码分层、几条设计约定 |
| [界面与交互](docs/interfaces.md) | 命令行 / TUI / web 三套界面、斜杠命令、插话排队、向你提问、附件、访问控制 |
| [自动化与触发器](docs/triggers.md) | 什么时候跑（四种事件源 + 条件）、跑什么、去重和环路保护 |
| [权限与安全](docs/safety.md) | 权限模式、AI 审批、免问目录、沙箱、独立 worktree、检查点 |
| [内置工具](docs/tools.md) | 后台命令、联网、图片、本机屏幕、ripgrep 搜索、MCP、子 agent |
| [技能与项目记忆](docs/knowledge.md) | Agent Skills 标准的技能、跨会话项目记忆 |
| [运行机制](docs/runtime.md) | 临时任务、上下文压缩、工具输出落盘、会话持久化 |
| [配置](docs/configuration.md) | config.json 全部字段、模型协议（OpenAI 兼容 / Anthropic） |

## 怎么扩展

- **加工具**：在 `pocketagent/tools/` 继承 `Tool`，填 `name / description / kind / parameters`，实现 `run`，
  在 `default_tool_registry()` 里注册。要在审批时显示 diff 就覆盖 `preview`；按路径干活的覆盖 `access`，
  工作目录外、密钥目录的审批就自动生效。改了文件不用做什么，检查点拍整个工作区。
- **加技能**：不用写代码，放一个带 `SKILL.md` 的目录进技能目录。
- **换模型协议**：实现 `LLMProvider.complete(messages, tools, listener) -> LLMResponse`，传给 `create_agent(provider=...)`。
- **换界面**：订阅 `agent.context.events`，实现 `Approver.request_approval` 和 `Approver.ask_questions`。
- **改流程**：在 `core/nodes.py` 加节点、在 `core/agent.py` 的 `build_agent_flow` 里连线。子流程可以直接用 PocketFlow 的 Flow 嵌套。

## 路线图

对照 Codex / Claude Code / WorkBuddy / Qoder，还缺这些，大致按优先级排：

1. **trace 回放和评测**：拿 JSONL trace 重放、对比不同模型或提示词
2. **界面**：IDE 插件；web 里搜索任务；一条自动化的触发时间线（什么时候被谁拉起来、条件卡在哪一步）
3. **更多协议**：Anthropic 已经接上，Gemini 等其他原生协议按 `LLMProvider` 再加

## 测试

```bash
make test                                 # mock：不联网，假模型按剧本返回，改完代码就跑
make test-live                            # live：调真实免费 API 校验流程，默认跳过
make test PYTHON=~/env/s11/bin/python
```

`tests/mock/` 不连网络，模型用 `ScriptedProvider` 按剧本返回；web 测试用 aiohttp 自带的测试客户端走真实的 HTTP 和 WebSocket，
没装 aiohttp 时自动跳过；TUI 测试用 Textual 的 `run_test()` 无头驱动真实控件，没装 textual 时自动跳过；
沙箱测试只在有 `sandbox-exec` 的 macOS 上跑。新功能必须补 mock 测试。

`tests/live/` 调真实免费接口（自动挑配置里带 `:free` 的模型、必应搜索、example.com、智谱免费生图），
没有 `POCKETAGENT_LIVE=1` 时全部跳过，所以不会误花钱。两套怎么分工、怎么加用例见 [tests/README.md](tests/README.md)。
