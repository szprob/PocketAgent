"""todo_write：把多步任务的计划写成一份步骤清单，界面渲染成「任务计划」面板。

清单存在会话上（session.todos），跟着会话一起落盘；工具拿到的是同一个 list 引用，原地替换内容。
"""

from typing import Any

from pocketagent.tools.base import Tool, ToolContext, ToolKind, ToolResult

TODO_STATUSES = ("pending", "in_progress", "completed")
MAX_TODO_ITEMS = 50
MAX_TODO_CONTENT_CHARS = 200


def parse_todos(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """校验并规整一份清单；格式不对抛 ValueError，给模型看得懂的报错。"""
    raw_items = arguments.get("todos")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("todos 必须是非空的步骤数组")
    if len(raw_items) > MAX_TODO_ITEMS:
        raise ValueError(f"步骤太多（{len(raw_items)} 条），最多 {MAX_TODO_ITEMS} 条，拆成几个大步骤")
    todos = []
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            raise ValueError(f"第 {index} 步不是对象")
        content = str(raw_item.get("content") or "").strip()
        if not content:
            raise ValueError(f"第 {index} 步缺 content")
        if len(content) > MAX_TODO_CONTENT_CHARS:
            raise ValueError(f"第 {index} 步的 content 超过 {MAX_TODO_CONTENT_CHARS} 个字，一句话概括就行")
        status = str(raw_item.get("status") or "pending")
        if status not in TODO_STATUSES:
            raise ValueError(f"第 {index} 步的 status 只能是 {' / '.join(TODO_STATUSES)}，不能是 {status}")
        active_form = str(raw_item.get("active_form") or "").strip()
        item: dict[str, Any] = {"content": content, "status": status}
        if active_form:
            item["active_form"] = active_form[:MAX_TODO_CONTENT_CHARS]
        todos.append(item)
    return todos


def render_todos(todos: list[dict[str, Any]]) -> str:
    """给模型看的纯文本版本，也是模型调完工具收到的回执。"""
    marks = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
    return "\n".join(f"{marks.get(item['status'], '[ ]')} {item['content']}" for item in todos)


class TodoWriteTool(Tool):
    name = "todo_write"
    description = (
        "维护当前任务的步骤清单，界面渲染成「任务计划」面板。"
        "每次调用传完整的清单（不是增量），会整个替换上一次的内容。"
    )
    kind = ToolKind.READ
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_TODO_ITEMS,
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "这一步做什么，一句话、动词开头"},
                        "status": {
                            "type": "string",
                            "enum": list(TODO_STATUSES),
                            "description": "pending 还没做 / in_progress 正在做 / completed 做完了",
                        },
                        "active_form": {
                            "type": "string",
                            "description": "正在做时界面上的进行时说法，如「跑回归测试」，不填就用 content",
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    }

    def describe(self, arguments: dict[str, Any]) -> str:
        try:
            todos = parse_todos(arguments)
        except ValueError:
            return "更新任务计划"
        done = sum(1 for item in todos if item["status"] == "completed")
        return f"任务计划 {done}/{len(todos)}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            todos = parse_todos(arguments)
        except ValueError as error:
            return ToolResult(f"清单格式不对：{error}", is_error=True)
        # 换成会话落盘的那份 list，原地改，save 时自然带上
        context.todo_list[:] = todos
        if context.on_todos_changed is not None:
            await context.on_todos_changed(todos)
        return ToolResult(render_todos(todos))
