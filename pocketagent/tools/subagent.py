"""subagent：派一个子 agent 去独立完成一件事，做完只把结论交回来。怎么跑见 pocketagent/subagents.py。"""

from dataclasses import dataclass
from typing import Any

from pocketagent.tools.base import Tool, ToolContext, ToolKind, ToolResult

SUBAGENT_KINDS = ["explore", "general"]
STOP_REASON_LABELS = {
    "done": "做完了",
    "step_limit": "到了步数上限，下面是它停下时的结论",
    "denied": "有操作被用户拒绝，先停下了",
    "truncated": "回复多次超长，先停下了",
    "error": "出错停下了",
}


@dataclass
class SubagentRequest:
    description: str
    prompt: str
    # explore 只读（可以几个并行）/ general 能改文件、跑命令
    kind: str = "explore"


@dataclass
class SubagentResult:
    text: str
    stop_reason: str
    tool_calls: int
    seconds: float


class SubagentTool(Tool):
    name = "subagent"
    description = (
        "派一个子 agent 去独立完成一件事。它从零开始，看不到你们之前的对话，有自己的上下文，做完只把结论交回来，"
        "你的上下文不会被它读过的大量内容塞满。"
        "适合：在代码库里大范围搜索、读很多文件找答案、做调研（type=explore，只读；互不相关的几件事可以在一次回复里同时派几个并行）；"
        "把一块边界清楚的工作整个交出去（type=general，能改文件、跑命令，照常要用户审批）。"
        "不适合：读一两个已知文件、简单的改动，这种自己直接做更快。"
        "prompt 要写清楚背景、要做什么、做到什么程度、交回什么（比如「列出相关文件路径和行号」）。"
    )
    kind = ToolKind.READ
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "3–8 个字的标题，显示在界面上，如「找出所有调用方」"},
            "prompt": {"type": "string", "description": "交给子 agent 的完整任务说明"},
            "type": {"type": "string", "enum": SUBAGENT_KINDS, "description": "explore 只读调研（默认）/ general 能改文件跑命令"},
        },
        "required": ["description", "prompt"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        kind = "可改动" if arguments.get("type") == "general" else "只读"
        return f"{arguments.get('description', '')}（{kind}）"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        kind = arguments.get("type") or "explore"
        if kind not in SUBAGENT_KINDS:
            return ToolResult(f"type 只能是 {' / '.join(SUBAGENT_KINDS)}", is_error=True)
        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt:
            return ToolResult("prompt 不能是空的", is_error=True)
        if context.spawn_subagent is None:
            return ToolResult("子 agent 不能再派子 agent，自己做完这件事", is_error=True)
        request = SubagentRequest(description=str(arguments.get("description") or "子任务").strip(), prompt=prompt, kind=kind)
        result: SubagentResult = await context.spawn_subagent(request)
        status = STOP_REASON_LABELS.get(result.stop_reason, result.stop_reason)
        header = f"子 agent「{request.description}」{status}（{result.tool_calls} 次工具调用，{result.seconds:.0f} 秒）："
        return ToolResult(f"{header}\n\n{result.text or '（它没有给出结论）'}", is_error=result.stop_reason == "error")
