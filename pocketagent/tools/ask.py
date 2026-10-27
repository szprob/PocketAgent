"""ask_user：向用户提几个问题，等用户选完（或者自己写、或者跳过）再接着干。"""

from typing import Any

from pocketagent.interaction.questions import (
    HEADER_LIMIT_CHARS,
    MAX_OPTIONS,
    MAX_QUESTIONS,
    MIN_OPTIONS,
    NO_ONE_TO_ASK,
    QuestionError,
    format_answers,
    parse_questions,
)
from pocketagent.tools.base import Tool, ToolContext, ToolKind, ToolResult


class AskUserTool(Tool):
    name = "ask_user"
    description = (
        "向用户提问并等回答。需求有歧义、有几种合理做法要用户拍板、缺少只有用户知道的信息时用。"
        f"一次 1–{MAX_QUESTIONS} 个问题，每个给 {MIN_OPTIONS}–{MAX_OPTIONS} 个选项；"
        "界面会自动加「其他」让用户自己写，不要自己写这个选项。推荐的选项放第一个，label 后面加「（推荐）」。"
        "能自己查清楚的事别问；不要在回复正文里列问题等用户回，用这个工具。"
    )
    kind = ToolKind.READ
    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_QUESTIONS,
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "完整的问题，以问号结尾"},
                        "header": {"type": "string", "description": f"界面上的小标签，不超过 {HEADER_LIMIT_CHARS} 个字，如「数据库」"},
                        "options": {
                            "type": "array",
                            "minItems": MIN_OPTIONS,
                            "maxItems": MAX_OPTIONS,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string", "description": "选项本身，简短"},
                                    "description": {"type": "string", "description": "选这个意味着什么、有什么取舍"},
                                },
                                "required": ["label"],
                            },
                        },
                        "multi_select": {"type": "boolean", "description": "能不能多选，默认 false"},
                    },
                    "required": ["question", "options"],
                },
            }
        },
        "required": ["questions"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        questions = arguments.get("questions") or []
        names = [str(item.get("header") or item.get("question") or "") for item in questions if isinstance(item, dict)]
        return "、".join(name for name in names if name)[:120]

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            questions = parse_questions(arguments)
        except QuestionError as error:
            return ToolResult(f"问题格式不对：{error}", is_error=True)
        if context.ask_user is None:
            return ToolResult(NO_ONE_TO_ASK)
        answers = await context.ask_user(questions)
        return ToolResult(format_answers(questions, answers))
