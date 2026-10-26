"""系统提示词：基础行为约定 + 运行环境 + 项目说明（AGENTS.md / CLAUDE.md）。"""

import platform
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pocketagent.memory import memory_prompt_section
from pocketagent.sandbox import SandboxPolicy
from pocketagent.skills import Skill, skills_prompt_section
from pocketagent.tools.base import ToolRegistry

# 依次查找，找到的都拼进去：用户级说明在前，项目级在后
PROJECT_INSTRUCTION_FILE_NAMES = ["AGENTS.md", "CLAUDE.md"]

BASE_PROMPT = """你是 PocketAgent，一个在用户本地代码仓库里工作的编程助手。

工作方式：
- 先用工具弄清楚情况（列目录、搜索、读文件），再动手改；不要凭猜测描述文件内容。
- 改已有文件用 edit_file 做精确替换，只有新建文件或整体重写才用 write_file。
- 互不依赖的只读操作可以在一次回复里同时发出多个工具调用。
- 用户附带的图片如果直接出现在消息里，你就能看到，直接看图回答，不要再用工具去读图片文件。
- 用户消息里的「@路径」指工作目录里的文件或目录，需要时用 read_file / list_dir 去看。
- 工作目录以外的文件也能读写（用绝对路径），这类调用会先请用户确认；只在任务确实需要时才去碰。
- 需要最新信息、文档或者查报错时，可以用 web_search 搜索、web_fetch 打开网页（如果有这两个工具）。
- 执行命令前想清楚副作用。删除文件、git reset --hard、强制推送这类不可逆操作，除非用户明确要求，否则先问。
- 需求有歧义、有几种合理做法需要用户拍板时，用 ask_user 一次问清楚（给出选项），不要在回复里列问题等用户回；能自己查清楚的别问。
- 工具调用被用户拒绝时，不要原样重试，停下来说明你想做什么、为什么。
- 完成后用简短的话说明改了什么、怎么验证的；没验证就直说没验证。"""


def _environment_section(workspace: Path) -> str:
    is_git_repository = (workspace / ".git").exists()
    lines = [
        "运行环境：",
        f"- 工作目录：{workspace}",
        f"- 是否 git 仓库：{'是' if is_git_repository else '否'}",
        f"- 平台：{platform.system()} {platform.release()}",
        f"- 今天：{date.today().isoformat()}",
    ]
    return "\n".join(lines)


def _tools_section(tools: ToolRegistry) -> str:
    return "可用工具：" + "、".join(tools.names())


def _sandbox_section(sandbox: SandboxPolicy | None) -> str:
    if sandbox is None:
        return "bash 命令不在沙箱里，直接在用户机器上执行。"
    network = "可以联网" if sandbox.allow_network else "不能联网"
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
) -> str:
    sections = [BASE_PROMPT, _environment_section(workspace), _tools_section(tools)]
    if "bash" in tools.names():
        sections.append(_sandbox_section(sandbox))
    if skills and "activate_skill" in tools.names():
        sections.append(skills_prompt_section(skills))
    if memory_dir is not None:
        sections.append(memory_prompt_section(memory_dir))
    for instruction_file in load_instruction_files(workspace, home_dir):
        sections.append(f"以下是 {instruction_file.path} 里的说明，请遵守：\n\n{instruction_file.content.strip()}")
    return "\n\n".join(sections)
