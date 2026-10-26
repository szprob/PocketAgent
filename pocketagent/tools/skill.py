"""load_skill：读一个技能的完整说明（SKILL.md 正文），顺带列出技能目录里的其他文件。"""

from typing import Any

from pocketagent.skills import Skill, split_frontmatter
from pocketagent.tools.base import Tool, ToolContext, ToolKind, ToolResult

LISTED_FILES_LIMIT = 50


class LoadSkillTool(Tool):
    name = "load_skill"
    description = "读取一个技能的完整说明。系统提示里列了可用技能，任务对得上某个技能时先调它，再照说明去做。"
    kind = ToolKind.READ
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "技能名，就是技能清单里冒号前面的部分"}},
        "required": ["name"],
    }

    def __init__(self, skills: list[Skill]) -> None:
        self.skills = {skill.name: skill for skill in skills}

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("name", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        skill = self.skills.get(str(arguments["name"]).strip())
        if skill is None:
            available = "、".join(self.skills) or "（没有）"
            return ToolResult(f"没有叫 {arguments['name']} 的技能，可用的有：{available}", is_error=True)
        try:
            text = skill.path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            return ToolResult(f"读不了 {skill.path}：{error}", is_error=True)
        _, body = split_frontmatter(text)
        header = (
            f"技能 {skill.name}，目录 {skill.directory}。"
            "说明里提到的相对路径都相对这个目录；里面的文件用 read_file 读，脚本用 bash 跑。"
        )
        return ToolResult(f"{header}\n{_other_files(skill)}\n\n{body.strip()}")


def _other_files(skill: Skill) -> str:
    files = []
    for path in sorted(skill.directory.rglob("*")):
        if len(files) >= LISTED_FILES_LIMIT:
            files.append("……")
            break
        relative = path.relative_to(skill.directory)
        if path.is_file() and path.name != "SKILL.md" and not any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
            files.append(str(relative))
    if not files:
        return "（目录里没有别的文件）"
    return "目录里的其他文件：" + "、".join(files)
