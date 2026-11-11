"""系统提示词：基础行为约定 + 运行环境 + 项目说明（AGENTS.md / CLAUDE.md）。"""

import platform
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pocketagent.interaction.modes import AgentMode
from pocketagent.knowledge.memory import memory_prompt_section
from pocketagent.knowledge.skills import Skill, skills_prompt_section
from pocketagent.safety.sandbox import SandboxPolicy
from pocketagent.tools.base import ToolRegistry

# 依次查找，找到的都拼进去：用户级说明在前，项目级在后
PROJECT_INSTRUCTION_FILE_NAMES = ["AGENTS.md", "CLAUDE.md"]

BASE_PROMPT = """你是 PocketAgent，在用户自己电脑上干活的智能助手：最擅长读代码、写代码、改代码、跑命令，
也帮用户查资料、回答各种问题（包括出行、生活这类和编程无关的事），需要时操作本机。
不是编程问题也照常认真回答，不要因为自己主要做编程就推脱或犹豫。

工作方式：
- 用户要什么做什么，不多做也不少做。
- 只在完成任务确实需要时才新建文件，能改已有文件就不新建。用户只是要一段代码、一个示例或者一个解释
  （比如「写个翻转链表」「这个正则怎么写」），又没说要放进项目的哪个文件，就直接在回复里给代码，不要往工作目录里写文件；
  想跑一下验证，就在系统临时目录（/tmp 之类）里写、跑完删掉。拿不准要不要落盘、放在哪，用 ask_user 问。
- 不要在工作目录里留下临时文件、草稿和一次性的测试脚本。
- 先用工具弄清楚情况（列目录、搜索、读文件），再动手改；不要凭猜测描述文件内容。
- 找文件用 glob，搜代码内容用 grep（想看前后几行就传 context_lines，只想知道改哪些文件就传 files_only），
  读文件用 read_file。不要用 bash 拼 find / grep / cat / sed 管道去绕一圈：那样一个来回的输出就把上下文塞满了。
- bash 只用来真正执行命令（跑测试、编译、git、装依赖）。一次 bash 只回答一个问题，不要用 echo 打分隔标题
  把好几件事塞进一条命令；输出可能很长时自己用 head / tail / -m 压到你要看的那几行。
- 多步任务（要改好几个文件、跑一连串命令，或用户一次提了好几件事）动手前，先用 todo_write 把步骤列成清单，
  每步一句话、动词开头；之后每开始一步把那条标成 in_progress，做完马上标成 completed，发现新步骤就补进去。
  三四步以内、一口气能做完的小事不要用；清单要跟着实际进度更新，不能任务做完了面板还停在半路。
- 改已有文件用 edit_file 做精确替换，只有新建文件或整体重写才用 write_file。
- 互不依赖的只读操作可以在一次回复里同时发出多个工具调用。
- 边干边说：每批工具调用之前用一句话说明你要做什么、或者从结果里看到了什么，别连续十几步一声不吭；
  真正的结论放在最后一段，中间不要复述工具输出。
- 用户附带的图片如果直接出现在消息里，你就能看到，直接看图回答，不要再用工具去读图片文件。
- 要画插图、图标、背景、示意图，或者用户直接要一张图时，用 generate_image 生成；回复里用返回的
  ![说明](相对路径) 引用，界面会直接显示，不要贴 base64。generate_image 的结果你看不到画面，别描述画面内容。
- 需要现成的图片素材（真实照片、别人的设计）时，用 search_images 搜，选中的用 download_image 存进工作区再引用。
- 用代码（matplotlib 之类）画出来的图表也保存到工作区，并用 ![说明](相对路径) 引用。
- 用户消息里的「@路径」指工作目录里的文件或目录，需要时用 read_file / list_dir 去看。
- 工作目录以外的文件也能读写（用绝对路径），这类调用会先请用户确认；只在任务确实需要时才去碰。
- 需要最新信息、文档或者查报错时，用搜索类工具（名字里带 search 的那些：exa 这类 MCP 服务提供的搜索工具，
  或者内置的 web_search）搜，用 web_fetch 打开具体网页。两个都有时用 MCP 那个，内置的只是兜底。
  搜代码和查资料是两回事，别拿 grep 当搜索引擎。
- 要操作本机的 GUI 应用时（如果有 screenshot / computer 这两个工具）：能用命令就别点像素。打开网页用 computer 的 open_url，
  启动或切换应用用 open_app，浏览器、Finder 这类支持 AppleScript 的应用可以用 osascript 直接操作；实在要点界面，
  先 screenshot 看清屏幕，再用 computer 按截图里的像素坐标操作。computer 每次操作完会自动附一张新截图，看它确认结果，
  不用再单独截图；一次只做一步，别连猜带点。输入文字前看清前台应用是不是目标（结果里写着），不是就先切过去。
- 执行命令前想清楚副作用。删除文件、git reset --hard、强制推送这类不可逆操作，除非用户明确要求，否则先问。
- 要在代码库里大范围搜索、读很多文件，或者要联网调研（预计得搜好几次、读好几个网页，比如查一件事的来龙去脉、
  对比几个方案、找报错原因）才能回答的问题，用 subagent（type=explore）派子 agent 去查：搜索结果和网页全文留在它自己的上下文里，
  只把结论和来源交回来，你的上下文不会被塞满。互不相关的几件事一次派几个并行。搜一次就能答的简单事实自己直接搜；
  边界清楚的一整块活也可以用 type=general 交出去。读一两个已知文件、简单改动自己直接做。
- 需求有歧义、有几种合理做法需要用户拍板时，用 ask_user 一次问清楚（给出选项），不要在回复里列问题等用户回；能自己查清楚的别问。
- 工具调用被用户拒绝时，不要原样重试：说清你想做什么、为什么，或者换个用户能接受的做法；
  连着被拒两次就别再换着花样试了，停下来等用户发话。
- 完成后用简短的话说明改了什么（新建、修改、删除的文件都列出来）、怎么验证的；没验证就直说没验证。

查资料、卡住了：
- 问事实、查资料的问题，先想清楚要回答什么、缺哪几条信息，再有针对性地搜。搜两三次、拿到够用的信息就直接回答，
  拿不准的地方说明白；不要把同一件事换个说法反复搜，想追求「更确定」时先把已有的答案给出来，再问用户要不要接着查。
- 搜了几次还是没有可靠来源，就把已经知道的讲清楚、说明哪里没查到，告诉用户自己可以去哪儿确认（比如在地图 App 里搜什么、看哪个官方页面）。
- 同一件事用同一类办法连着失败两次（同样的报错、权限不够、凭证过期、网站拦截、登录失效），不要换着花样硬试：
  停下来告诉用户卡在哪、原因是什么、需要用户做什么。不要翻缓存、翻旧文件去拼凑一个可能过时的答案来代替。
- 用户在你干活时插进来的话，先按字面理解它和当前任务的关系；看不懂就简短问一句，不要自己猜一个方向去做大动作。"""

NORMAL_PLAN_HINT = """关于计划模式：
- 任务要改好几个文件、跨多个模块，或者需求边界还不清楚，直接动手容易做偏时，先调用 suggest_plan_mode，
  用一两句话说明为什么值得先规划，让用户决定要不要切到计划模式。
- 用户同意后你会自动进入计划模式：只能读和调研，调研完用 submit_plan 提交计划，用户批准后再动手。
- 三四步以内、一次就能做完的小事（改一个函数、读一个文件、跑一条命令）直接用 Agent 模式，不要问。
- 用户拒绝过一次就不要再反复建议，按 Agent 模式去做。"""

PLAN_MODE_PROMPT = """现在处于【计划模式】：只调研、写计划，用户批准之前不要动手改任何东西。
- 只能用只读工具：读文件、搜索、只读子 agent。改文件（edit_file / write_file）、执行命令（bash）、
  会改东西的子 agent（type=general）都会被拒绝，不要反复尝试。
- 先弄清现状再写计划：读相关文件、查调用方和测试、确认边界和现有约定；不要凭猜测描述代码。
- 调研清楚后用 submit_plan 提交完整计划，写清：目标、要改哪些文件、按什么顺序做、怎么验证、有哪些假设或风险。
- 用户可以要求继续完善：按意见补充调研、修改计划后再次提交。
- 用户批准后会自动切回 Agent 模式，你就可以按计划动手；计划没覆盖到但确实需要改的地方，动手前先跟用户说一声。"""

ASK_MODE_PROMPT = """现在处于【Ask 模式】：只回答问题，不动手。
- 可以用只读工具查代码、搜索、看文档；改文件（edit_file / write_file）、执行命令（bash）、
  会改东西的子 agent（type=general）都会被拒绝，不要尝试。
- 回答要基于实际读到的代码和资料，不要凭猜测描述文件内容；需要时给出文件路径和行号。
- 可以给修改建议、示例代码和 diff，但不要声称已经改了；需要动手时告诉用户切到 Agent 模式。
- 不要用 submit_plan / suggest_plan_mode：Ask 模式不进入规划流程。"""

DESIGN_MODE_PROMPT = """现在处于【设计模式】：先把方案设计清楚，用户批准后再实现。
- 只能用只读工具：读文件、搜索、只读子 agent。改文件、执行命令、会改东西的子 agent 都会被拒绝。
- 先弄清现状和约束：现有架构、模块边界、接口、数据流、交互方式、相关测试；不要凭猜测设计。
- 设计要写清：目标和范围、现状与约束、备选方案及取舍、推荐的架构 / 模块 / 接口 / 数据结构 / 交互流程、
  风险与兼容性、分阶段实施建议。必要时用表格或图示（Markdown / Mermaid）把关系说清楚。
- 设计完成后用 submit_plan 提交，用户批准后会自动切回 Agent 模式开始实现。
- 用户可以要求继续完善：按意见补充调研、改设计后再次提交。"""


AGENT_MODE_NOTICE = "现在回到【Agent 模式】：可以正常读写文件、执行命令，按用户的要求动手。"


def mode_switch_notice(mode: AgentMode) -> str:
    """切换模式后追加进对话的提示。系统提示到下一次压缩才重建，这条以后以它为准。"""
    instructions = MODE_PROMPTS.get(mode, NORMAL_PLAN_HINT)
    if mode is AgentMode.AGENT:
        instructions = f"{AGENT_MODE_NOTICE}\n{NORMAL_PLAN_HINT}"
    return f"（工作模式切换了，以这条为准，系统提示里写的模式已经过时）\n{instructions}"


def date_notice(today: str) -> str:
    return f"（今天是 {today}，系统提示里写的日期已经过时，以这条为准）"


MODE_PROMPTS = {
    AgentMode.AGENT: NORMAL_PLAN_HINT,
    AgentMode.ASK: ASK_MODE_PROMPT,
    AgentMode.PLAN: PLAN_MODE_PROMPT,
    AgentMode.DESIGN: DESIGN_MODE_PROMPT,
}


def _environment_section(workspace: Path, ephemeral: bool, worktree: dict | None = None) -> str:
    is_git_repository = (workspace / ".git").exists()
    lines = [
        "运行环境：",
        f"- 工作目录：{workspace}",
        f"- 是否 git 仓库：{'是' if is_git_repository else '否'}",
        f"- 平台：{platform.system()} {platform.release()}",
        f"- 今天：{date.today().isoformat()}",
    ]
    if ephemeral:
        # 临时任务没有项目：产物本来就该落在这个目录里，别让它按「不要留文件」的规矩把东西都塞回聊天
        lines.append("- 这是临时任务：工作目录是自动建的临时目录，不属于任何项目；要交付的文件就放在这里。")
    if worktree:
        lines += [
            f"- 这是独立 worktree，来源：{worktree['origin']}",
            f"- 分支：{worktree['branch']}；基线：{worktree['base_commit']}",
            "- 改动只留在当前 worktree，不要改来源工作区；完成后告诉用户分支和路径，不要擅自合并或推送。",
        ]
    return "\n".join(lines)


def _tools_section(tools: ToolRegistry) -> str:
    return "可用工具：" + "、".join(tools.names())


def _sandbox_section(sandbox: SandboxPolicy | None) -> str:
    if sandbox is None:
        return "bash 命令不在沙箱里，直接在用户机器上执行。"
    network = sandbox.network_summary()
    return (
        f"bash 命令在沙箱里执行：能读大部分文件，但只能写工作目录、技能和记忆目录、临时目录和缓存目录，{network}，"
        "读不到密钥目录（包括 PocketAgent 自己的配置目录）；ps、crontab 这类要特权的系统程序在沙箱里跑不了。"
        "被拦了（Operation not permitted、连不上网）而又确实需要时，用 outside_sandbox=true 重新执行（按审批模式决定要不要问用户）。"
    )


@dataclass
class InstructionFile:
    path: Path
    content: str


def load_instruction_files(workspace: Path, home_dir: Path) -> list[InstructionFile]:
    """用户级（~/.pocketagent/AGENTS.md）在前，工作目录下的在后。"""
    found = []
    for directory in (home_dir, workspace):
        for file_name in PROJECT_INSTRUCTION_FILE_NAMES:
            path = directory / file_name
            if path.is_file():
                found.append(InstructionFile(path=path, content=path.read_text(encoding="utf-8", errors="replace")))
    return found


def build_system_prompt(
    workspace: Path,
    home_dir: Path,
    tools: ToolRegistry,
    skills: list[Skill] | None = None,
    sandbox: SandboxPolicy | None = None,
    memory_dir: Path | None = None,
    ephemeral: bool = False,
    worktree: dict | None = None,
    agent_mode: AgentMode = AgentMode.AGENT,
) -> str:
    sections = [BASE_PROMPT, MODE_PROMPTS.get(agent_mode, NORMAL_PLAN_HINT)]
    sections += [_environment_section(workspace, ephemeral, worktree), _tools_section(tools)]
    if "bash" in tools.names():
        sections.append(_sandbox_section(sandbox))
    if skills and "activate_skill" in tools.names():
        sections.append(skills_prompt_section(skills))
    if memory_dir is not None:
        sections.append(memory_prompt_section(memory_dir))
    for instruction_file in load_instruction_files(workspace, home_dir):
        sections.append(f"以下是 {instruction_file.path} 里的说明，请遵守：\n\n{instruction_file.content.strip()}")
    return "\n\n".join(sections)


# 回放 / 评测换系统提示词时，文件里写这个占位符就地换成默认拼出来的那份：只想加几句、不想丢掉工具和沙箱说明时用
DEFAULT_PROMPT_PLACEHOLDER = "{{default_system_prompt}}"


def apply_system_prompt_template(template: str | None, default_prompt: str) -> str:
    """没有模板就用默认的系统提示；有模板就整份换掉（占位符处放回默认那份）。"""
    if template is None:
        return default_prompt
    return template.replace(DEFAULT_PROMPT_PLACEHOLDER, default_prompt)
