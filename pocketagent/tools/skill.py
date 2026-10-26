"""activate_skill：加载一个技能的完整说明（SKILL.md 正文 + 技能目录 + 附带文件清单）。"""

from typing import Any

from pocketagent.skills import Skill, loaded_skill_names, skill_content
from pocketagent.tools.base import Tool, ToolContext, ToolKind, ToolResult


class ActivateSkillTool(Tool):
    name = "activate_skill"
    description = "加载一个技能的完整说明。系统提示的 <available_skills> 里列了可用技能，任务对得上某个技能时先调它，再照说明去做。"
    kind = ToolKind.READ

    def __init__(self, skills: list[Skill]) -> None:
        self.skills = {skill.name: skill for skill in skills}
        # 名字限定在已有的技能里，模型不会编一个不存在的
        self.parameters = {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": list(self.skills), "description": "技能名"}},
            "required": ["name"],
        }

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("name", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        name = str(arguments["name"]).strip()
        skill = self.skills.get(name)
        if skill is None:
            available = "、".join(self.skills) or "（没有）"
            return ToolResult(f"没有叫 {name} 的技能，可用的有：{available}", is_error=True)
        if name in loaded_skill_names(context.history()):
            return ToolResult(f"技能 {name} 的完整说明已经在上面的对话里了，直接照着做，不用重复加载。")
        try:
            return ToolResult(skill_content(skill))
        except OSError as error:
            return ToolResult(f"读不了 {skill.path}：{error}", is_error=True)
