# PocketAgent

基于 [PocketFlow](https://github.com/The-Pocket/PocketFlow) 的编程 agent，目标是做成 Codex / Claude Code / WorkBuddy / Qoder 这一类：
在本地仓库里读代码、改文件、跑命令，用户在旁边审批。

已经能用的：主循环、内置工具（文件、搜索、bash、联网搜索、读网页、生图 / 搜图、技能、向用户提问、子 agent）、多档权限（含 AI 自动审批）、
bash 沙箱、工作目录外的访问审批、影子 git 检查点（看变更、撤销、整体恢复）、技能（Agent Skills 标准）、项目记忆、
流式事件、trace、会话续跑、后台自动压缩上下文、多模型配置（思考档位 / 上下文长度随模型走）、
附件（图片 / 文件）、插话和排队、token 用量和缓存命中统计、定时自动化，以及命令行和 web 两套界面。
grep / glob 优先用 ripgrep（遵守 .gitignore），没装 rg 时回退纯 Python 实现。

## 设计哲学

PocketAgent 有两个目标：在本地替用户完成编程任务，以及作为一个**可阅读、可拆解、可学习**的 agent 实现。
前者决定它需要什么能力，后者决定代码、文档和默认值应该怎么设计。下面这些原则用于指导取舍，不是功能清单。

### 1. 可学习性优先

这个项目的代码和运行过程本身就是说明书。

- 主循环只用五个阶段表达（`guard → think → run_tools → guard`，另有 `compact` / `finish`），
  连线集中在 `pocketagent/core/agent.py` 的 `build_agent_flow()`，阅读连线即可看到一轮对话从哪里开始、在哪里结束。
- 状态集中在 `AgentContext`，不在节点之间传递隐式参数；消息统一使用 OpenAI chat 格式，
  界面需要的字段以下划线开头，发给模型前去掉。
- 工具、provider、审批器、技能都是显式接口：增加能力是新增一个类并注册，不是修改框架内部。
- 代码遵守 `AGENTS.md` 的约定：拆小函数、变量用全名、注释解释「为什么」；
  不引入隐藏控制流、元编程，或必须先学一套 DSL 才能阅读的抽象。
- 运行时同样保持可解释：工具调用有摘要，文件改动有 diff，关键取舍在回复里说明，过程可以通过 trace 复盘，
  方便人观察和学习 agent 的工作方式。
- 文档、测试和注释是功能的一部分。一个机制如果没有测试、没有在 README 里说明清楚，视为没有完成。

衡量标准是：一个新接触这个项目的人，能否沿着「入口 → 主循环 → 工具 → 权限 / 沙箱」这条线，
在一次系统阅读中建立完整的心智模型。

### 2. 小内核，显式机制

PocketFlow 只保留上游约 100 行核心代码，不额外包装；图只描述循环阶段，不为每个工具建立节点；
工具由 `RunToolsNode` 按注册表动态分发（`pocketagent/core/nodes.py`）。新增工具或以后接入 MCP，都不需要修改主图。

框架的职责是让流程可见，而不是替开发者隐藏流程。

### 3. 内核与界面解耦

agent 不感知 CLI、web、TUI 或 IDE。所有过程以事件发出（`pocketagent/core/events.py`），界面只是事件的订阅者；
审批通过 `Approver` 接口注入。命令行和 web 是两套「订阅者 + 审批实现」，
增加新的界面或编辑器插件时不需要修改 agent 本身。

### 4. 权限是架构的一部分

权限不是系统提示词里的请求，而是工具通过 `access()` 上报路径、沙箱和密钥，由 `PermissionGate`
在调用前判定（`pocketagent/safety/permissions.py`）。默认值保守：ask 模式、沙箱默认断网、
密钥目录在任何模式下都要确认（除非用户自己把它配进免问目录）、跳出沙箱必须人工确认。AI 审批只作为第二意见，不放行就转给用户。

模型不被信任为自己授权。

### 5. 上下文是稀缺资源

采用渐进披露：技能清单只放名字和描述，使用时才加载正文；子 agent 有独立上下文，只回传结论；
历史超过阈值后在后台压缩；过长的工具输出截断，完整内容落盘并在结果里给出路径。

目标是让模型在正确的时机读取正确的信息，而不是把所有内容一次性塞进上下文窗口。

### 6. 本地优先，过程可审计，结果可回滚

运行产物全部在 `~/.pocketagent/`：会话、trace、用量、附件、影子 git 检查点、工具输出，没有云端依赖和遥测。
每轮动手前拍文件检查点，变更可以看 diff、撤销、整体恢复；会话在节点边界追加日志，
进程异常退出后可以恢复到当前进度。

信任来自可审计和可回滚，而不是来自对模型的假设。

### 7. 标准协议，能力可替换、可降级

消息使用 OpenAI chat 格式，模型接口只要求 OpenAI 兼容；技能遵循 Agent Skills 标准，
项目说明读取 `AGENTS.md` / `CLAUDE.md`，记忆使用 Markdown + frontmatter。
缺少某个组件时降级而不是崩溃：没有 ripgrep 时回退 Python，没有可用沙箱时明确提示后直接执行，
web 和联网工具都是可选的。

### 8. 工程克制

基础设施保持简单：JSON / JSONL 存储、静态前端无需构建、标准库 unittest、
核心依赖只有 `openai` 和 `httpx`。复杂度留给真正困难的问题——权限、上下文管理、状态恢复——
而不是构建系统、数据库或插件框架。

### 对扩展的约束

新增能力时按以下顺序判断：

1. 能否通过现有接口完成（Tool / LLMProvider / Approver / Skill）？
2. 能否只增加工具或事件，而不修改主循环？
3. 新机制能否在 README 和测试里讲清楚？
4. 默认值是否保守，能力是否显式开启？

如果必须修改主循环或消息格式，先说明为什么现有抽象不够；不要为了一个功能引入第二套状态或隐藏的控制流。

### 设计边界

PocketAgent 不追求功能数量最多、默认权限最宽、把 agent 做成云端黑盒、绑定单一模型或网关、
用提示词代替权限控制，或为了扩展性预先引入复杂框架。它优先保证代码可读、过程可见、权限可控、结果可回滚。

## 快速开始

核心依赖 `openai`（用它的 SDK 调 OpenAI 兼容接口）和 `httpx`；web 界面另外要 `aiohttp`、`pillow`。Python ≥ 3.11。

```bash
# 可编辑安装：改源码立即生效，不用重装（只有改了 pyproject 的依赖或入口才要重跑）
uv pip install -e '.[web]'          # 或 pip install -e '.[web]'

pocketagent --web                   # 起 web 界面并自动打开浏览器；第一次在左下角 ⚙ 设置里添加模型和 API Key
pocketagent                         # 命令行交互，在当前目录工作
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
- 测试用 `py -m unittest discover -s tests -t .`（Windows 上不一定有 make）；
  `tests/vendor_pocketflow/` 里上游的并行 batch 测试依赖字典插入顺序，在 Windows 上会挂，和本项目代码无关

## 斜杠命令

输入以 / 开头的行按命令处理（web 和命令行共用同一套，见 `pocketagent/interaction/commands.py`）；命中 `/技能名` 的照常交给 agent，
普通文字和 `/usr/bin` 这种路径不会被误认成命令。

| 命令 | 说明 |
|---|---|
| `/help [命令]` | 命令列表；带命令名只讲这一条 |
| `/new` | 开新会话（别名 `/clear`；web 上是同工作区开个新任务） |
| `/compact` | 手动压缩上下文 |
| `/mode [模式]` | 查看 / 切换审批模式：`read_only` / `ask` / `auto_edit` / `ai_auto` / `full_auto` |
| `/model [模型]` | 查看 / 切换模型（按 id、名称或模型名） |
| `/skills [技能名]` | 列出技能；带名字看它的说明和位置 |
| `/status` | 模型、工作区、会话、上下文占用 |
| `/usage` | 这个会话的 token 用量和今天的用量 |
| `/sessions` | 这个工作区的会话列表 |
| `/resume <id>` | 续跑某个会话 |
| `/memory [文件]` | 项目记忆索引；带文件名看正文 |
| `/undo [路径\|all]` | 列出任务里的文件改动；带路径撤销一个，`all` 全撤 |
| `/exit` | 退出命令行（别名 `/quit`；web 上关掉标签页就行） |
| `/技能名 [要求]` | 启用这个技能并把要求交给 agent |

web 的输入框里打 / 会浮出命令补全（内置命令 + 当前工作区的技能），命令行里 `/help` 查看全部。
命令行 REPL 里 Ctrl-C 只中断当前这一轮，会话保留，可以接着说。

## web 界面

```bash
pocketagent --web -C ~/projects/foo     # 这个目录会加进工作区列表并排第一；8765 被占用就往后找空闲端口
pocketagent --web --no-browser          # 不自动打开浏览器（比如在远端机器上起）
pocketagent --web --host 0.0.0.0        # 让别的机器访问（比如开发机上起、经网关转发）
```

不带 `-C` 时不动工作区列表（列表还是空的、第一次用时才把启动目录加进去），所以在界面里移除的工作区不会在下次启动时又冒出来。

外观和字号在 ⚙ 设置 → 通用里选（`appearance`：跟随系统 / 浅色 / 深色；`font_size`：小 / 中 / 大），存在 `config.json` 里，web 和桌面端共用。

布局参考 Qoder，三栏：

- **左侧栏**：新任务、自动化；工作区（点开是这个工作区的文件树，点文件预览，预览里可以「@ 引用」到输入框；
  任务改了文件会自动刷新）；最近任务（全部历史，运行中的有蓝点，等你回答 / 确认的换成琥珀色的 ? / !，
  悬停出现 × 可以删除）；两段都能点标题折叠；
  最下面是账户名和 ⚙ 设置。
- **中间**：新任务页选工作区、模型、权限后直接说话（工作区也可以选「临时任务」，见下）；任务页是对话。输入框底下一排：📎 附件（也可以粘贴截图、拖文件进来）、
  权限、模型（选模型的面板里直接改思考档位和上下文长度，存进配置；设置里可以让某个模型不出现在这里）、发送 / 停止；
  再下面是上下文占用和压缩阈值。
- **右侧栏**（右上角按钮开关）：任务概要（最新进展、token 用量、缓存命中、费用、上下文）、变更（从任务开始到现在
  工作区里所有变了的文件，不管是 edit_file 还是 bash 改的；点开看 diff，可以撤销单个文件）、产出（新建的文件）、
  检查点（每一轮开始前拍一张，可以把整个工作区恢复到那时）。

### 工具调用折叠

一轮里常常要调很多次工具。默认把它们收进一块可展开的折叠，折叠头上一行说清这一块干了什么：

- 跑完：`✓ 读取 3 个文件 · 新建 2 个文件 · 修改 1 个文件 · 运行 1 条命令`（同一个文件读两次算一个；出错的不算进去，另外标 `1 个出错`）
- 跑的时候：`● 读取 2 个文件 · bash · pytest -q · 12.4s`，思考时是 `● 思考中 3.2s · 最新想到的一句`
- 点折叠头展开，里面还是原来那样一张卡片一次调用，卡片还能各自展开看输出

AI 说的话永远在折叠外面：它一开口，前面那一块就收尾，这段话直接显示，后面再开新的一块。
审批卡、向你提问的卡片、任务计划面板也都在外面——这些是要你动手的，不能藏起来。

⚙ 设置 → 通用里的「把工具调用收进折叠」（配置文件里的 `fold_tool_calls`）可以关掉，关掉就回到
一次调用一张卡片平铺的老样子；切换的时候已经画出来的折叠会就地摊平，不会留下一半折一半没折。

### 临时任务

不指定工作区也能开任务：新任务页的工作区下拉里选「临时任务（不指定工作区）」，命令行用 `pocketagent --scratch`。
服务端在 `~/.pocketagent/scratch/<时间>-<随机>/` 建一个空目录给这个任务，文件、命令照常能用，产出就留在那儿，
不碰任何项目；任务列表里标着「临时」。命令行 REPL 里 `/new` 会再建一个干净目录。

- 临时任务不读也不写项目记忆（没有项目）；沙箱照常开着，只是把这个临时目录当成可读可写的工作区
- 删任务不会删这些文件；`~/.pocketagent/scratch` 整个可以随时清掉，重开旧任务时会重建一个空目录

### 权限

| 选项 | 只读工具 | 改文件 / 执行命令 |
|---|---|---|
| 每次询问 | 直接放行 | 每次弹审批卡片（改文件显示 diff） |
| AI 自动审批 | 直接放行 | 先让当前模型判断是否安全、是否在任务范围内；判定安全就放行，否则照常问你 |
| 完全访问 | 直接放行 | 全部放行 |

AI 审批用哪个模型、什么思考档位在 ⚙ 设置 → 权限与安全里选（配置文件里的 `review_model` / `review_reasoning`）：
模型默认跟任务的一样，思考档位默认「最低」（`lowest`：DeepSeek / new-api 是 `none` 关掉思考，智谱是 `low`），
也可以选「跟模型配置一样」（`inherit`）或具体档位。判断一次调用安不安全用不着深度思考，默认就不用每次等一轮 max 思考。

一步里有好几个要审的调用时，一起审完再按顺序执行：AI 审批并发做，要问你的合成一张卡片（每项可以单独允许、总是允许或拒绝；命令行里一项一项问）。后一个调用的预览依赖前一个的执行结果（同一个文件改两次，或者前面有 bash 这类改哪里说不准的）时，等前面执行完再审它；前面有被拒绝的，后面的不再执行。

模式之外，有三种情况要多问一句，审批卡片上会写明原因：

| 情况 | 每次询问 | AI 自动审批 | 完全访问 |
|---|---|---|---|
| 读工作目录以外的路径 | 问你 | 交给 AI 判断 | 放行 |
| 改工作目录以外的路径 | 问你 | 问你 | 放行 |
| bash 要在沙箱外执行 | 问你 | 问你（不交给 AI） | 放行 |
| 碰到密钥目录（`~/.ssh`、`~/.aws`、`~/.pocketagent` 等），配进免问目录的除外 | 问你 | 问你 | 也问你 |

「本任务总是允许」记的是具体的东西：工具名、工作目录外的那个目录、那条沙箱外的命令；碰到密钥目录时不给这个选项。
碰到工作目录外的目录时还可以选「访问 上一级目录 下的所有目录」，之后兄弟目录也不再问；上一级是根目录、
`/Users`、家目录这一层的不给这个选项（放行它们就等于放开整个家目录甚至整个磁盘）。
附件目录和技能目录不算工作目录外，读它们不用问。

免问目录（⚙ 设置 → 权限与安全，或者配置文件里的 `trusted_dirs`）用来放工作目录以外、你固定会用的目录，
比如存数据的 `~/data`、多个仓库共用的 `~/git/skills`：它们和工作目录一样对待——读文件不问，改文件按审批模式走
（每次询问模式下照样问），bash 沙箱里也能写。设置里的「读写工作目录以外的文件不再询问」（`allow_outside_workspace`）
相当于把除密钥目录以外的所有地方都当成免问目录。
落在免问目录里的密钥目录也不再问：配了家目录就等于 `~/.ssh`、`~/.pocketagent` 都免了，这是你明确选的；
bash 沙箱照旧读不到密钥，和密钥目录有父子关系的免问目录也不进沙箱的可写范围。
注意检查点只给工作目录拍快照，免问目录和工作目录以外的改动撤销不了。

### 沙箱

bash 默认关在沙箱里（macOS 用系统自带的 `sandbox-exec`，Linux 用 bubblewrap，Linux 这条还没在真机上测过）：

- 能读大部分文件，读不到 `~/.ssh`、`~/.aws`、`~/.gnupg`、`~/.netrc`、`~/.config/gh` 和 PocketAgent 自己的目录（有 API Key）
- 只能写工作目录、临时目录、`~/.cache`、`~/Library/Caches` 和设置里的免问目录；工作目录里的 `.git/hooks`、`.git/config` 也不能写
- 默认不能联网（⚙ 设置 → 权限与安全里可以打开）

被拦下时工具结果会提示模型；它确实需要时（装依赖、`git push`、写别处）会带 `outside_sandbox=true` 重新执行，
这一次要你确认，界面上的工具卡片标着「沙箱外」。机器上没有可用的沙箱时命令直接执行，设置里会说明。

bash 的输出边跑边显示：网页里工具卡片会自动展开并实时追加，命令行逐行打印；模型最后拿到的仍然是完整结果，
不是流式增量。

### 检查点

影子 git 仓库放在 `~/.pocketagent/shadow/<工作区路径的哈希>/`，`GIT_DIR` 指向它、`GIT_WORK_TREE` 指向工作区，
不碰工作区自己的 `.git`，不读你的全局 git 配置，`.gitignore` 照常生效，`node_modules`、`.venv` 之类默认排除。
任务开始时和每一轮开始前给整个工作区拍一张，所以 bash 改的、新建的、删的都看得到；
撤销和恢复之前都会先再拍一张，撤销本身也能撤销。工作区大到拍一张超过 30 秒，这个任务的检查点就关掉，不卡对话。

- 工作区是「一个目录下放好几个仓库」时，git 只会把嵌套的仓库记成一个指针、看不到里面的改动，
  所以每个嵌套仓库单独一个影子仓库，各自的 `.gitignore` 照常生效；变更列表里的路径还是相对工作区的
- 每一轮的快照和第一次调用模型同时进行；只读的工具不等它，第一个会写的批次（bash 也算写）之前一定拍完；新任务的 index 从同一工作区上一个任务复制，
  只重新读改过的文件

### 会话持久化

一轮正常结束会把整份会话原子地写进 `~/.pocketagent/sessions/<id>.json`。一轮进行中每到一个节点边界，
把新增消息追加到旁边的 `<id>.messages.jsonl`：进程被杀、断电或直接关窗口后，`--continue` / 重新打开旧任务
会从 JSON + 日志恢复到当前进度，压缩把历史换掉时日志里写一条 reset。续跑前如果有 assistant 的 tool_calls
没拿到结果（崩在工具执行中间），会补一条「用户中断」的 tool 消息，避免下一次请求 400。

### 技能

按 [Agent Skills](https://agentskills.io) 开放标准，和 Claude Code、Codex 等用的是同一种格式：一个目录一个技能，
目录里的 `SKILL.md` 开头写 `name` 和 `description`，正文写怎么做，旁边可以放 `scripts/`、`references/`、`assets/`。

- 系统提示里放 `<available_skills>` 清单（名字、描述、SKILL.md 的位置），模型用得上时调 `activate_skill`
  拿到正文（`<skill_content>` 包着，附带技能目录和文件清单），也可以直接 read_file；同一个技能不重复加载
- 消息开头写 `/技能名` 直接启用（命令行里 `/skills` 列出全部）
- 压缩上下文时，加载过的技能正文原样保留，不进摘要
- 不合规范（名字不是小写短横线、和目录名不一致、描述超过 2048 字）只提示、照样加载；没有描述的跳过

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

- `web_search`：默认抓必应的结果页，免费、不用密钥；设置里可以换成 DeepSeek 搜索（0.005 元/次）
  或智谱 Web Search（0.01 元/次起），密钥不单独配就用对应厂商模型的密钥
- `web_fetch`：打开网页转成正文，只认 http / https

### 图片

- `generate_image`：文生图，走 OpenAI 兼容的 `/images/generations`。默认智谱 CogView
  （`cogview-3-flash` 免费，密钥不单独配就用智谱模型的）；也可以在设置里换成其他 OpenAI 兼容接口
  （接口地址空着就用第一个 OpenAI 兼容模型的网关地址）。图片存到工作区 `images/` 下，回复里用相对路径
  `![说明](路径)` 引用就会直接显示，一轮结束的「本轮产出的图片」也会自动带上
- `search_images`：搜现成的图片，抓必应图片结果页，免费、不用密钥；只给标题、原图链接、缩略图和来源，不下载
- `download_image`：把搜到的图下载到工作区再引用。单独一个工具，因为下载是写文件（要按写权限审批），
  而默认沙箱里不联网，用 bash 去抓每次都得走「沙箱外」审批
- 工具结果是文本，模型看不到生成出来的画面；生成的图是写文件，按审批模式决定要不要确认

### 本机屏幕（仅 macOS）

能操作整台电脑、又不在沙箱里，所以默认关：⚙ 设置 → 工具 →「操作电脑」勾上（配置文件里的 `computer_use_enabled`）才会给模型这两个工具。

- `screenshot`：截主屏幕，截图直接进工具结果，模型看得到；走系统 `screencapture`，需要
  「屏幕录制」权限。同时记下「截图宽 / 屏幕逻辑宽」的缩放比，供 `computer` 换算坐标
- `computer`：键盘鼠标操作（单击 / 双击 / 输入文字 / 按键，坐标用截图像素坐标自动换算），
  走 AppleScript（System Events），零第三方依赖，需要「辅助功能」权限。没截图过就先 `screenshot`
  ——工具会拦住没有换算比的点击。右键和拖拽不支持；`screenshot` 是只读工具，
  `computer` 按执行类审批

### 搜索

`grep` / `glob` 优先调用 [ripgrep](https://github.com/BurntSushi/ripgrep)：

- 遵守 `.gitignore`，默认跳过 `.git`；大仓库上比纯 Python 遍历快很多
- rg 的 Rust 正则不支持 lookaround（`(?=...)`）这类写法，遇到时自动回退到 Python 正则，结果不变
- 机器上没有 rg 就用原来的 Python 实现。配置里的 `ripgrep_path` 或环境变量 `POCKETAGENT_RIPGREP`
  可以指定可执行文件位置（Homebrew 装在 `/opt/homebrew/bin/rg`，Qoder 插件缓存里也可能有一个）

### 上下文

- 记账：每次模型调用后以接口报的 `prompt_tokens` 为锚点，之后新加的消息按字符估算（中文每字 0.7、其他 0.27 token，
  拿 DeepSeek / 智谱 / Grok 的分词器量过）；图片按 1600 算
- 超过阈值（默认上下文长度的 80%）就在后台总结前面的历史，不打断对话，做完在下一次调用模型前换上；
  快满了（95%）才停下来等。接口报上下文超长时不管开没开自动压缩，都会压缩后重试
- 重试只针对 429 / 5xx / 连接错误，指数退避加抖动，有 `Retry-After` 就按它等；400 / 401 不重试
- 回复超出输出上限（`finish_reason=length`）时，截断的工具调用不执行，让模型拆小了重来；纯文字就让它接着写

### 工具输出

单个工具结果超过 30k 字符时，头尾照常给模型（中间标出省略了多少），完整内容写到
`~/.pocketagent/tool_outputs/<任务>/<call id>.log`，结果里告诉模型路径和怎么分段读。这个目录不在工作区，
检查点、变更列表、撤销都看不到；模型用 `read_file` / `grep` 读它不用审批，bash 沙箱也放行。
超过 7 天的自动清理，删任务时整个目录一起删。

### 向你提问

需求有歧义、有几种合理做法要你拍板时，agent 会用 `ask_user` 一次问 1–4 个问题，每个问题 2–4 个选项（推荐的放第一个），
可以单选或多选；每个问题最后都有「其他」，可以自己写。全部选好点提交，也可以跳过让它自己判断（它会在最后说明做了哪些假设）。
问题卡片刷新页面后还在；命令行里按编号回答，0 是自己写，直接回车跳过。没人能回答的时候（单次执行、自动化等太久），
它会自己判断着往下做。

### 子 agent

agent 可以用 `subagent` 工具派子 agent 去独立干一件事。子 agent 从零开始（看不到主对话），有自己的上下文，
做完只把结论交回来，主 agent 的上下文不会被它读过的大量文件塞满。

- `type=explore`：只读（读文件、搜索、查资料），互不相关的几件事可以在一次回复里同时派几个，并行跑
- `type=general`：能改文件、跑命令，每一步照常按权限审批，审批卡片上标着是哪个子 agent
- 子 agent 不能问用户，也不能再派子 agent；用量、沙箱、「本任务总是允许」和主 agent 共用
- 模型默认和任务的一样，⚙ 设置 → 智能体里可以单独选模型和思考档位（`subagent_model` / `subagent_reasoning`）
- 有时长上限：explore 默认 10 分钟、general 默认 40 分钟（`subagent_explore_minutes` / `subagent_general_minutes`），等你审批的时间不算。到点不会直接丢掉：停下正在跑的命令（连进程树一起杀）、补齐没结果的工具调用，再让它不带工具写一次总结交回来
- 一批里有 general 子 agent 时按顺序跑，不会和别的改动同时进行
- 子 agent 每调一个工具，它那张工具卡片下面就实时多一行；点停止时子 agent 一起停

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

页面还会把 token 记在浏览器本地（按端口和子路径分开），之后 REST、WebSocket、附件和预览图都自己带上——
cookie 在同一个主机上不分端口，桌面端或另一个 `--web` 实例用 `?token=` 打开页面时会把 cookie 冲掉，光靠 cookie
已经开着的页面就会一直 401。

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
  "fold_tool_calls": true,
  "auto_compact": true,
  "compact_threshold_percent": 80,
  "web_search_enabled": true,
  "web_search_provider": "bing",
  "web_search_engine": "search_std",
  "web_fetch_enabled": true,
  "image_gen_enabled": true,
  "image_gen_provider": "zhipu",
  "image_gen_model": null,
  "image_gen_base_url": null,
  "image_search_enabled": true,
  "computer_use_enabled": false,
  "sandbox_enabled": true,
  "sandbox_network": false,
  "allow_outside_workspace": false,
  "skills_enabled": true,
  "skill_dirs": [],
  "disabled_skills": [],
  "memory_enabled": true,
  "workspaces": ["/Users/me/projects/foo"],
  "trusted_dirs": ["/Users/me/data"],
  "ripgrep_path": null,
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
- 任务记录、trace、附件、影子仓库、用量、自动化、工具输出都在 `~/.pocketagent/`（`POCKETAGENT_HOME` 可以改到别处）

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
pocketflow/              上游核心代码原样复制（100 行），不改，见 VENDORED.md
pocketagent/
  cli.py                 命令行界面：渲染事件 + 终端审批 + REPL
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
  llm/                   模型接口：openai_compat（流式、工具调用拼接、重试）、scripted（测试用假模型）
  tools/                 工具基类 + 注册表；files / search / shell / web / image / computer / skill / ask / todo / subagent
    ripgrep.py           ripgrep 的调用和 --json 解析；rg 用不了时 search.py 回退 Python 实现
  safety/                权限和兜底
    permissions.py       权限模式、审批接口；按工具类别和这次调用碰到的地方（工作目录外、沙箱外、密钥）决定
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
    automations.py       自动化的定义、计划计算、存储
    files.py             文件树、文件预览、选目录（只能看登记过的工作区）
    history.py           把存下来的消息转成前端渲染用的条目
  web/                   浏览器前端（ES 模块，不用构建、不依赖 CDN），由 server 一起服务
    static/              index.html / style.css / js/
      js/                main.js 入口；core/ 不碰 DOM 的接口和状态，ui/ 通用界面件，
                         task/ 任务页，sidebar/ 左栏，settings/ 设置，automations/ 自动化
desktop/                 Electron 客户端：前端独立实现，只调 server 的 REST + WebSocket
tests/                   unittest；vendor_pocketflow/ 是上游测试
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
- **改流程**：在 `core/nodes.py` 加节点、在 `core/agent.py` 的 `build_agent_flow` 里连线。子流程可以直接用 PocketFlow 的 Flow 嵌套。

## 路线图

对照 Codex / Claude Code / WorkBuddy / Qoder，还缺这些，大致按优先级排：

1. **MCP 客户端**：把 MCP server 的工具包装成 `Tool` 注册进来
2. **Anthropic 等其他协议的 provider**（现在只有 OpenAI 兼容接口）
3. **todo / plan 工具**
4. **hooks**（工具调用前后跑用户脚本）
5. **沙箱联网白名单**：现在只有开 / 关，可以像 Claude Code 那样走代理按域名放行；Linux bubblewrap 要在真机上验证
6. **trace 回放和评测**：拿 JSONL trace 重放、对比不同模型或提示词
7. **界面**：TUI、IDE 插件；web 里搜索任务

## 测试

```bash
make test                                 # = python -m unittest discover -s tests -t .
make test PYTHON=~/env/s11/bin/python
```

测试不连网络，模型用 `ScriptedProvider` 按剧本返回。web 测试用 aiohttp 自带的测试客户端走真实的 HTTP 和 WebSocket，
没装 aiohttp 时自动跳过；沙箱测试只在有 `sandbox-exec` 的 macOS 上跑。
