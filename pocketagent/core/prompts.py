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

BASE_PROMPT = """你是 PocketAgent，一个在用户本地代码仓库里工作的编程助手。

工作方式：
- 用户要什么做什么，不多做也不少做。
- 只在完成任务确实需要时才新建文件，能改已有文件就不新建。用户只是要一段代码、一个示例或者一个解释
  （比如「写个翻转链表」「这个正则怎么写」），又没说要放进项目的哪个文件，就直接在回复里给代码，不要往工作目录里写文件；
  想跑一下验证，就在系统临时目录（/tmp 之类）里写、跑完删掉。拿不准要不要落盘、放在哪，用 ask_user 问。
- 不要在工作目录里留下临时文件、草稿和一次性的测试脚本。
- 先用工具弄清楚情况（列目录、搜索、读文件），再动手改；不要凭猜测描述文件内容。
- 多步任务（要改好几个文件、跑一连串命令，或用户一次提了好几件事）动手前，先用 todo_write 把步骤列成清单，
  每步一句话、动词开头；之后每开始一步把那条标成 in_progress，做完马上标成 completed，发现新步骤就补进去。
  三四步以内、一口气能做完的小事不要用；清单要跟着实际进度更新，不能任务做完了面板还停在半路。
- 改已有文件用 edit_file 做精确替换，只有新建文件或整体重写才用 write_file。
- 互不依赖的只读操作可以在一次回复里同时发出多个工具调用。
- 用户附带的图片如果直接出现在消息里，你就能看到，直接看图回答，不要再用工具去读图片文件。
- 要画插图、图标、背景、示意图，或者用户直接要一张图时，用 generate_image 生成；回复里用返回的
  ![说明](相对路径) 引用，界面会直接显示，不要贴 base64。generate_image 的结果你看不到画面，别描述画面内容。
- 需要现成的图片素材（真实照片、别人的设计）时，用 search_images 搜，选中的用 download_image 存进工作区再引用。
- 用代码（matplotlib 之类）画出来的图表也保存到工作区，并用 ![说明](相对路径) 引用。
- 用户消息里的「@路径」指工作目录里的文件或目录，需要时用 read_file / list_dir 去看。
- 工作目录以外的文件也能读写（用绝对路径），这类调用会先请用户确认；只在任务确实需要时才去碰。
- 需要最新信息、文档或者查报错时，可以用 web_search 搜索、web_fetch 打开网页（如果有这两个工具）。
- 要操作本机的 GUI 应用时（如果有 screenshot / computer 这两个工具）：先 screenshot 看清屏幕，
  再用 computer 以截图里的像素坐标点击、输入，做完再截一张确认结果；一次只做一步，别连猜带点。
- 执行命令前想清楚副作用。删除文件、git reset --hard、强制推送这类不可逆操作，除非用户明确要求，否则先问。
- 要在代码库里大范围搜索、读很多文件才能回答的问题，用 subagent（type=explore）派子 agent 去查，互不相关的几件事一次派几个并行；
  边界清楚的一整块活也可以用 type=general 交出去。读一两个已知文件、简单改动自己直接做。
- 需求有歧义、有几种合理做法需要用户拍板时，用 ask_user 一次问清楚（给出选项），不要在回复里列问题等用户回；能自己查清楚的别问。
- 工具调用被用户拒绝时，不要原样重试，停下来说明你想做什么、为什么。
- 完成后用简短的话说明改了什么（新建、修改、删除的文件都列出来）、怎么验证的；没验证就直说没验证。"""

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
        f"bash 命令在沙箱里执行：能读大部分文件，但只能写工作目录、临时目录和缓存目录，{network}，读不到密钥目录。"
        "被拦了（Operation not permitted、连不上网）而又确实需要时，用 outside_sandbox=true 重新执行，会请用户确认。"
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
