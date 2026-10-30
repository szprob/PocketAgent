"""agent 的工作模式：agent（默认）/ ask / plan / design。

- agent：正常干活，可以读文件、改文件、执行命令
- ask：只回答问题，只读；不改文件、不执行命令，也不提交方案
- plan：只读调研，写实施计划，用 submit_plan 提交；批准后切回 agent 模式执行
- design：只读调研，写设计方案（架构 / 接口 / 交互 / 取舍），用同一个 submit_plan 提交；批准后切回 agent 模式实现

plan / design 这两种会「提交方案等批准」的模式共用一个提案通道：Approver 决定怎么问用户
（web 推卡片，命令行用 input()），批准后由 Agent 自己切回 agent 模式。
"""
from dataclasses import dataclass
from enum import Enum

MAX_PLAN_CHARS = 20_000
MAX_REASON_CHARS = 500

NO_ONE_TO_APPROVE = (
    "现在没有人能批准这份方案。把方案作为最终回复交给用户，这一轮就到这里，不要动手改任何东西。"
)
PLAN_NOT_APPROVED = "用户没有批准这份方案，也没说怎么改。先把方案留给用户，这一轮不要动手改任何东西。"


class AgentMode(str, Enum):
    AGENT = "agent"
    ASK = "ask"
    PLAN = "plan"
    DESIGN = "design"

    @property
    def read_only(self) -> bool:
        """这个模式下只能读：ask / plan / design 都不许改文件、跑命令。"""
        return self is not AgentMode.AGENT

    @property
    def submits_proposal(self) -> bool:
        """调研完要提交方案等用户批准。"""
        return self in (AgentMode.PLAN, AgentMode.DESIGN)

    @property
    def label(self) -> str:
        return MODE_LABELS[self]


MODE_LABELS = {
    AgentMode.AGENT: "Agent",
    AgentMode.ASK: "Ask",
    AgentMode.PLAN: "计划",
    AgentMode.DESIGN: "设计",
}
MODE_VALUES = [mode.value for mode in AgentMode]


def parse_agent_mode(value: object, default: AgentMode = AgentMode.AGENT) -> AgentMode:
    """配置 / 会话 / 命令里的字符串或枚举转成模式；空值用默认，不认识的抛 ValueError。"""
    if isinstance(value, AgentMode):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    try:
        return AgentMode(text)
    except ValueError:
        choices = " / ".join(MODE_VALUES)
        raise ValueError(f"工作模式只能是 {choices}：{value}")


@dataclass
class PlanSwitchRequest:
    """普通模式下建议切到计划模式：reason 是为什么这个任务值得先规划。"""

    reason: str


@dataclass
class PlanApprovalRequest:
    """提交计划 / 设计方案，等用户批准执行或者要求继续完善。"""

    plan: str
    # "plan" 或 "design"，界面和用户答复的措辞跟着变
    mode: str = AgentMode.PLAN.value


@dataclass
class PlanDecision:
    approve: bool
    # 用户要求继续完善时写的意见；空表示只是没批准
    feedback: str = ""


def normalize_plan(raw_plan: str) -> str:
    plan = str(raw_plan or "").strip()
    if not plan:
        raise ValueError("plan 不能是空的")
    if len(plan) > MAX_PLAN_CHARS:
        raise ValueError(f"方案太长（{len(plan)} 字），最多 {MAX_PLAN_CHARS} 字，写重点")
    return plan


def normalize_reason(raw_reason: str) -> str:
    reason = str(raw_reason or "").strip()
    if not reason:
        raise ValueError("reason 不能是空的：用一两句话说明为什么这个任务适合先规划")
    return reason[:MAX_REASON_CHARS]


async def ask_plan_switch(approver: object, request: PlanSwitchRequest) -> bool:
    """问用户要不要切到计划模式；通道没实现（单次执行、测试）就当用户不想切。"""
    ask = getattr(approver, "request_plan_switch", None)
    if ask is None:
        return False
    return bool(await ask(request))


async def ask_plan_approval(approver: object, request: PlanApprovalRequest) -> PlanDecision:
    """问用户批不批准方案；没人能回答时不算批准，让 agent 把方案交出来停下。"""
    ask = getattr(approver, "request_plan_approval", None)
    if ask is None:
        return PlanDecision(approve=False)
    decision = await ask(request)
    if not isinstance(decision, PlanDecision):
        return PlanDecision(approve=False)
    return PlanDecision(approve=decision.approve, feedback=decision.feedback.strip())
