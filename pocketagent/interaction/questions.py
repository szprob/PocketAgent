"""向用户提问：模型一次问 1–4 个问题，每个问题给 2–4 个选项，用户选一个（或多选），
也可以选「其他」自己写；都不想答可以跳过。界面会自动加「其他」，模型不用自己写这个选项。

问题怎么送到用户面前由 Approver 决定（web 推给浏览器、命令行用 input()），和审批走同一条「问人」的通道。
"""

from dataclasses import asdict, dataclass, field
from typing import Any

MAX_QUESTIONS = 4
MIN_OPTIONS = 2
MAX_OPTIONS = 4
HEADER_LIMIT_CHARS = 12
# 模型自己写了「其他」选项的话去掉，界面上本来就有
OTHER_LABELS = {"其他", "其它", "other", "others", "其他（请说明）"}


class QuestionError(ValueError):
    pass


@dataclass
class QuestionOption:
    label: str
    description: str = ""


@dataclass
class Question:
    question: str
    options: list[QuestionOption]
    # 界面上显示成小标签的短名，比如「数据库」「部署方式」
    header: str = ""
    multi_select: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QuestionAnswer:
    # 选中的选项（多选时可能有几个）
    selected: list[str] = field(default_factory=list)
    # 用户在「其他」里自己写的
    other: str = ""

    def is_empty(self) -> bool:
        return not self.selected and not self.other.strip()

    @classmethod
    def from_dict(cls, data: Any) -> "QuestionAnswer":
        if not isinstance(data, dict):
            return cls()
        selected = [str(item) for item in data.get("selected") or [] if str(item).strip()]
        return cls(selected=selected, other=str(data.get("other") or "").strip())


def parse_questions(arguments: dict[str, Any]) -> list[Question]:
    """把模型给的参数整理成问题列表；不合要求就抛 QuestionError，原因会回给模型让它改。"""
    raw_questions = arguments.get("questions")
    if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= MAX_QUESTIONS:
        raise QuestionError(f"questions 要是 1 到 {MAX_QUESTIONS} 个问题的列表")
    questions = [_parse_question(index, raw) for index, raw in enumerate(raw_questions, start=1)]
    texts = [question.question for question in questions]
    if len(set(texts)) != len(texts):
        raise QuestionError("有重复的问题")
    return questions


def _parse_question(index: int, raw: Any) -> Question:
    if not isinstance(raw, dict) or not str(raw.get("question") or "").strip():
        raise QuestionError(f"第 {index} 个问题缺少 question")
    options = []
    for item in raw.get("options") or []:
        option = item if isinstance(item, dict) else {"label": item}
        label = str(option.get("label") or "").strip()
        if label and label.lower() not in OTHER_LABELS:
            options.append(QuestionOption(label=label, description=str(option.get("description") or "").strip()))
    labels = [option.label for option in options]
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        raise QuestionError(
            f"第 {index} 个问题要给 {MIN_OPTIONS} 到 {MAX_OPTIONS} 个选项（不用写「其他」，界面会自动加）"
        )
    if len(set(labels)) != len(labels):
        raise QuestionError(f"第 {index} 个问题的选项有重名")
    return Question(
        question=str(raw["question"]).strip(),
        options=options,
        header=str(raw.get("header") or "").strip()[:HEADER_LIMIT_CHARS],
        multi_select=bool(raw.get("multi_select")),
    )


def format_answers(questions: list[Question], answers: list[QuestionAnswer] | None) -> str:
    """把用户的回答写成给模型看的工具结果。"""
    if answers is None or all(answer.is_empty() for answer in answers):
        return "用户跳过了这些问题，没有回答。按你认为最合理的方式继续，并在最后说明你做了哪些假设。"
    lines = ["用户的回答："]
    for index, question in enumerate(questions, start=1):
        answer = answers[index - 1] if index - 1 < len(answers) else QuestionAnswer()
        prefix = f"〔{question.header}〕" if question.header else ""
        lines.append(f"{index}. {prefix}{question.question}")
        lines.append(f"   → {_answer_text(answer)}")
    return "\n".join(lines)


def _answer_text(answer: QuestionAnswer) -> str:
    if answer.is_empty():
        return "（这个没回答，你自己判断）"
    parts = []
    if answer.selected:
        parts.append("、".join(answer.selected))
    if answer.other:
        parts.append(f"用户自己写的：{answer.other}")
    return "；".join(parts)


NO_ONE_TO_ASK = "现在没有人能回答问题（非交互运行，或者等了太久没人理）。按你认为最合理的方式继续，并在最后说明你做了哪些假设。"
