"""AI 自动审批：改文件、执行命令之前，先让模型判断这次调用是否安全、是否在用户任务范围内。

判定安全就直接放行；不安全、拿不准、或者审查本身出错，都转给用户确认（没人在场时就是拒绝）。
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from pocketagent.interaction.attachments import message_display
from pocketagent.llm.base import LLMProvider, SilentListener

PREVIEW_LIMIT_CHARS = 4000
TASK_LIMIT_CHARS = 2000

REVIEW_PROMPT = """你是编程助手的安全审查员。助手正在用户的工作目录里替用户干活，现在想执行下面这个操作，请判断能不能直接放行。

可以放行（allow=true）：操作明显是为了完成用户的任务，影响只在工作目录内，而且风险低或者可以撤销。
例如修改、新建项目文件，运行测试、构建、格式化，查看 git 状态和日志。

读取工作目录以外的文件或目录（看文档、参考别的项目、查日志），只要和任务相关、不是密钥凭据，也可以放行。

不要放行（allow=false，会转给用户确认）：删除大量文件或目录、rm -rf、git push / reset --hard / 改写历史，
安装或卸载系统软件、sudo，读取或外传密钥，向外部服务发送数据，修改工作目录以外的东西，和任务无关的操作，或者你拿不准的。

用户最近的要求：
{task}

工作目录：{workspace}
工具：{tool_name}
参数：
{arguments}
{notes_section}{preview_section}
只输出一个 JSON 对象，不要输出别的：{{"allow": true 或 false, "reason": "一句话理由"}}"""


@dataclass
class ReviewResult:
    allow: bool
    reason: str


def recent_user_requests(messages: list[dict[str, Any]], count: int = 2) -> str:
    """取最近几条用户的原话，给审查员当任务背景。"""
    texts = []
    for message in reversed(messages):
        if message.get("role") == "user" and not message.get("_kind"):
            text = message_display(message)["text"].strip()
            if text:
                texts.append(text)
        if len(texts) >= count:
            break
    combined = "\n---\n".join(reversed(texts)) or "（没有找到用户的要求）"
    return combined[-TASK_LIMIT_CHARS:]


def parse_review(text: str) -> ReviewResult:
    """从模型回复里找出第一个 JSON 对象；解析不了就当不放行。"""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return ReviewResult(allow=False, reason="审查结果不是 JSON，转给用户确认")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return ReviewResult(allow=False, reason="审查结果解析失败，转给用户确认")
    return ReviewResult(allow=data.get("allow") is True, reason=str(data.get("reason", "")).strip() or "（没给理由）")


async def review_tool_call(
    provider: LLMProvider,
    messages: list[dict[str, Any]],
    workspace: str,
    tool_name: str,
    arguments: dict[str, Any],
    preview: str,
    notes: list[str] | None = None,
) -> ReviewResult:
    notes_section = "".join(f"注意：{note}\n" for note in notes or [])
    preview_section = ""
    if preview:
        preview_section = f"操作预览：\n{preview[:PREVIEW_LIMIT_CHARS]}\n"
    prompt = REVIEW_PROMPT.format(
        task=recent_user_requests(messages),
        workspace=workspace,
        tool_name=tool_name,
        arguments=json.dumps(arguments, ensure_ascii=False, indent=1)[:PREVIEW_LIMIT_CHARS],
        notes_section=notes_section,
        preview_section=preview_section,
    )
    try:
        response = await provider.complete([{"role": "user", "content": prompt}], [], SilentListener())
    except Exception as error:
        return ReviewResult(allow=False, reason=f"审查调用失败（{type(error).__name__}），转给用户确认")
    return parse_review(response.text or response.reasoning)
