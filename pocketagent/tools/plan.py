"""工作模式相关的工具：建议切到计划模式，以及在计划 / 设计模式下提交方案等用户批准。"""

from typing import Any

from pocketagent.interaction.modes import (
    NO_ONE_TO_APPROVE,
    PLAN_NOT_APPROVED,
    AgentMode,
    normalize_plan,
    normalize_reason,
)
from pocketagent.tools.base import Tool, ToolContext, ToolKind, ToolResult

PLAN_MODE_ACTIVE_NOTE = "已经在计划模式里了，不用再建议切换。继续调研，调研清楚后用 submit_plan 提交计划。"
MODE_LABELS = {
    AgentMode.AGENT: "Agent",
    AgentMode.ASK: "Ask",
    AgentMode.PLAN: "计划",
    AgentMode.DESIGN: "设计",
}


class SuggestPlanModeTool(Tool):
    name = "suggest_plan_mode"
    description = (
        "在 Agent 模式下，任务看起来需要先规划时，问用户要不要切到计划模式。"
        "适合：要改好几个文件、跨多个模块，或者需求边界还不清楚，贸然动手容易做偏。"
        "不适合：读一两个文件、改一两处、一次就能做完的小事。"
        "用户同意后会自动切到计划模式：你只能读和调研，调研完用 submit_plan 提交计划，用户批准后再动手。"
        "同一个任务里用户拒绝过一次，就不要再反复问。"
    )
    kind = ToolKind.READ
    parameters = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "为什么这个任务适合先规划，一两句话，给用户看的",
            }
        },
        "required": ["reason"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("reason") or "")[:120]

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        mode = context.agent_mode()
        if mode is AgentMode.PLAN:
            return ToolResult(PLAN_MODE_ACTIVE_NOTE)
        if mode is not AgentMode.AGENT:
            return ToolResult(
                f"现在是 {MODE_LABELS[mode]} 模式，不能建议切计划模式；按当前模式的要求继续。", is_error=True
            )
        try:
            reason = normalize_reason(str(arguments.get("reason") or ""))
        except ValueError as error:
            return ToolResult(str(error), is_error=True)
        if context.suggest_plan_mode is None:
            return ToolResult("现在没有人能回答。直接在 Agent 模式下继续做，不要等确认。")
        switched = await context.suggest_plan_mode(reason)
        if switched:
            return ToolResult(
                "用户同意用计划模式，已经切换过来了。现在只做调研（读文件、搜索、只读子 agent），"
                "不要改文件、不要执行命令；调研清楚后用 submit_plan 提交计划。"
            )
        return ToolResult("用户选择直接开始，不用计划模式。按 Agent 模式继续做，这个任务里不要再建议切换计划模式。")


class SubmitPlanTool(Tool):
    name = "submit_plan"
    description = (
        "计划 / 设计模式下调研完，把方案交给用户批准；批准后会自动切回 Agent 模式，你就可以动手执行或实现。"
        "计划模式写实施计划：目标、要改哪些文件、按什么顺序做、怎么验证。"
        "设计模式写设计方案：目标、约束、备选方案与取舍、架构 / 接口 / 数据结构 / 交互、风险和实施建议。"
        "用户可能要求继续完善；改好后再提交一次。Agent / Ask 模式下不要用这个工具。"
    )
    kind = ToolKind.READ
    parameters = {
        "type": "object",
        "properties": {
            "plan": {
                "type": "string",
                "description": "完整方案（Markdown）：计划模式写实施步骤，设计模式写设计方案",
            }
        },
        "required": ["plan"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        first_line = str(arguments.get("plan") or "").strip().splitlines()
        return first_line[0][:120] if first_line else "提交方案"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        mode = context.agent_mode()
        if not mode.submits_proposal:
            if mode is AgentMode.ASK:
                return ToolResult("现在是 Ask 模式，只回答问题，不要提交方案；需要动手时让用户切到 Agent 模式。", is_error=True)
            return ToolResult(
                "现在是 Agent 模式，不需要提交方案。要规划就用 suggest_plan_mode 问用户，否则直接做。", is_error=True
            )
        try:
            plan = normalize_plan(str(arguments.get("plan") or ""))
        except ValueError as error:
            return ToolResult(str(error), is_error=True)
        if context.submit_plan is None:
            return ToolResult(NO_ONE_TO_APPROVE)
        decision = await context.submit_plan(plan)
        kind = "设计方案" if mode is AgentMode.DESIGN else "计划"
        if decision.approve:
            if mode is AgentMode.DESIGN:
                return ToolResult(
                    "用户批准了设计方案，已经切回 Agent 模式。按设计开始实现；实现中偏离设计的地方告诉用户。"
                    "\n\n设计方案：\n" + plan
                )
            return ToolResult(
                "用户批准了计划，已经切回 Agent 模式。现在按计划开始执行；计划里没覆盖到但确实需要改的地方，"
                "做之前先跟用户说一声。\n\n计划：\n" + plan
            )
        if decision.feedback:
            return ToolResult(
                f"用户还没批准，要求继续完善：{decision.feedback}\n"
                f"继续在{MODE_LABELS[mode]}模式下调研，把{kind}改好后再用 submit_plan 提交。"
            )
        return ToolResult(PLAN_NOT_APPROVED)
