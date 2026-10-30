"""触发器：什么时候跑。

一条自动化 = 什么时候跑（这个文件）+ 跑什么（`Automation` 自己：工作区、提示词、模型、权限）。

四种源都只负责产生「发生了一次」这个事实，条件只负责判断这次算不算数，谁都不碰执行。
执行永远走 `TriggerEngine`，那里还有去重、环路和并发三件事。

- clock：到点。原来的「每天几点 / 每隔 N 分钟」现在只是它的一种写法。
- task_end：某个任务的一轮跑完，看的是 stop_reason 和最后那段话。
- webhook：外面 POST 进来，每条触发器一个密钥，验签之后才算数。
- poll：每隔一段时间跑一条命令，退出码 0 就算「发生了」。
"""

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar

from pocketagent.config import ConfigError
from pocketagent.safety.scripts import MAX_SCRIPT_TIMEOUT_SECONDS, run_script

SOURCE_CLOCK = "clock"
SOURCE_TASK_END = "task_end"
SOURCE_WEBHOOK = "webhook"
SOURCE_POLL = "poll"
SOURCE_KINDS = (SOURCE_CLOCK, SOURCE_TASK_END, SOURCE_WEBHOOK, SOURCE_POLL)

CLOCK_DAILY = "daily"
CLOCK_INTERVAL = "interval"
CLOCK_MODES = (CLOCK_DAILY, CLOCK_INTERVAL)

CONDITION_ALWAYS = "always"
CONDITION_STATUS = "status"
CONDITION_MATCH = "match"
CONDITION_SHELL = "shell"
CONDITION_KINDS = (CONDITION_ALWAYS, CONDITION_STATUS, CONDITION_MATCH, CONDITION_SHELL)

DEFAULT_DAILY_TIME = "09:00"
DEFAULT_INTERVAL_MINUTES = 5
MIN_INTERVAL_MINUTES = 5
DEFAULT_CONDITION_TIMEOUT_SECONDS = 30
WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]
ALL_WEEKDAYS = [0, 1, 2, 3, 4, 5, 6]
MAX_PATTERN_CHARS = 200


def parse_time_of_day(text: str) -> tuple[int, int]:
    """「09:30」→ (9, 30)。"""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if match is None:
        raise ConfigError(f"时间要写成 HH:MM：{text}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ConfigError(f"时间超出范围：{text}")
    return hour, minute


@dataclass
class Occurrence:
    """发生过的一件事。四种源产生的都是它，后面的判断只认这个结构。

    带 key 是为了去重：同一个 key 在有效期内只算一次，webhook 重试、
    scheduler 连着重启、同一轮结束发了两次，都落在这一个 key 上。
    """

    kind: str
    key: str
    at: datetime
    # 产生这件事的任务（task_end 才有），source 靠它判断「是不是我等的那个」
    automation_id: str = ""
    automation_name: str = ""
    session_id: str = ""
    # task_end 的 stop_reason：done / error / denied / step_limit / interrupted
    status: str = ""
    # 判断条件时要看的正文（目前是这一轮最后那段话）
    text: str = ""
    # 外部 POST 带进来的原始正文，条件脚本里当 body 用
    payload: dict[str, Any] = field(default_factory=dict)
    # 触发链深度：a 触发 b 记 1，b 再触发 c 记 2，到上限就不许再传了
    depth: int = 0

    def to_payload(self) -> dict[str, Any]:
        """给条件脚本的 stdin：字段和钩子的 payload 尽量长得像，写脚本的人不用记两套。"""
        return {
            "event": self.kind,
            "occurrence_key": self.key,
            "session_id": self.session_id,
            "automation_id": self.automation_id,
            "automation_name": self.automation_name,
            "status": self.status,
            "text": self.text,
            "body": self.payload,
            "depth": self.depth,
            "at": self.at.isoformat(timespec="seconds"),
        }


class Source:
    """事件源的共同部分：认得自己的事件、会给界面写一句说明。"""

    kind: ClassVar[str] = ""

    def matches(self, occurrence: Occurrence) -> bool:
        return occurrence.kind == self.kind

    def next_run_at(self, reference: datetime) -> datetime | None:
        """只有 clock 有时间表，别的源返回 None。"""
        return None

    def describe(self, automation_names: dict[str, str] | None = None) -> str:
        raise NotImplementedError


@dataclass
class ClockSource(Source):
    """到点。

    mode=daily 走「每天几点、周几跑」；mode=interval 走「每隔多少分钟」。
    reference 是「上一次算过的计划时间」，不是上一次跑的时间——条件没过也算算过，
    不然一个条件永远不满足的计划会被每 20 秒重试一次。
    """

    kind: ClassVar[str] = SOURCE_CLOCK
    mode: str = CLOCK_DAILY
    daily_time: str = DEFAULT_DAILY_TIME
    weekdays: list[int] = field(default_factory=lambda: list(ALL_WEEKDAYS))
    interval_minutes: int = 60

    def next_run_at(self, reference: datetime) -> datetime:
        if self.mode == CLOCK_INTERVAL:
            return reference + timedelta(minutes=self.interval_minutes)
        hour, minute = parse_time_of_day(self.daily_time)
        candidate = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= reference:
            candidate += timedelta(days=1)
        while candidate.weekday() not in self.weekdays:
            candidate += timedelta(days=1)
        return candidate

    def describe(self, automation_names: dict[str, str] | None = None) -> str:
        if self.mode == CLOCK_INTERVAL:
            if self.interval_minutes % 60 == 0:
                return f"每 {self.interval_minutes // 60} 小时"
            return f"每 {self.interval_minutes} 分钟"
        if sorted(self.weekdays) == ALL_WEEKDAYS:
            return f"每天 {self.daily_time}"
        if sorted(self.weekdays) == [0, 1, 2, 3, 4]:
            return f"工作日 {self.daily_time}"
        days = "、".join(f"周{WEEKDAY_NAMES[day]}" for day in sorted(self.weekdays))
        return f"{days} {self.daily_time}"


@dataclass
class TaskEndSource(Source):
    """某个任务跑完之后。automation_id 为空表示「任何自动化跑完都算」。"""

    kind: ClassVar[str] = SOURCE_TASK_END
    automation_id: str = ""

    def matches(self, occurrence: Occurrence) -> bool:
        if occurrence.kind != self.kind:
            return False
        if not self.automation_id:
            return True
        return occurrence.automation_id == self.automation_id

    def describe(self, automation_names: dict[str, str] | None = None) -> str:
        if not self.automation_id:
            return "任何自动化跑完后"
        name = (automation_names or {}).get(self.automation_id, "另一个自动化")
        return f"「{name}」跑完后"


@dataclass
class WebhookSource(Source):
    """外面 POST 进来。secret 只在这里出现，别的源没有这个概念。"""

    kind: ClassVar[str] = SOURCE_WEBHOOK
    secret: str = ""

    def matches(self, occurrence: Occurrence) -> bool:
        # 外部触发是点名进来的（见 TriggerEngine.fire_webhook），不参与广播
        return False

    def describe(self, automation_names: dict[str, str] | None = None) -> str:
        return "外部 POST"


@dataclass
class PollSource(Source):
    """每隔一段时间跑一条命令，退出码 0 就算「发生了」。"""

    kind: ClassVar[str] = SOURCE_POLL
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    command: str = ""
    timeout_seconds: int = DEFAULT_CONDITION_TIMEOUT_SECONDS

    def describe(self, automation_names: dict[str, str] | None = None) -> str:
        return f"每 {self.interval_minutes} 分钟检查一次"


@dataclass
class ConditionResult:
    """条件检查的结论。problem 只在脚本自己坏了时才有值，不是「不满足」。"""

    passed: bool
    detail: str = ""
    problem: str = ""


class Condition:
    """条件：这次算不算数。全都写成 async，调用方不用区分是哪种。"""

    kind: ClassVar[str] = ""

    async def check(self, occurrence: Occurrence, workspace: str) -> ConditionResult:
        raise NotImplementedError

    def describe(self) -> str:
        return ""


@dataclass
class AlwaysCondition(Condition):
    kind: ClassVar[str] = CONDITION_ALWAYS

    async def check(self, occurrence: Occurrence, workspace: str) -> ConditionResult:
        return ConditionResult(passed=True)


@dataclass
class StatusCondition(Condition):
    """只有这一轮正常跑完才算数，失败 / 被拒 / 超步数都不往下传。"""

    kind: ClassVar[str] = CONDITION_STATUS
    status: str = "done"

    async def check(self, occurrence: Occurrence, workspace: str) -> ConditionResult:
        passed = occurrence.status == self.status
        return ConditionResult(passed=passed, detail=f"stop_reason={occurrence.status or '（没有）'}")

    def describe(self) -> str:
        return f"（仅 {self.status}）"


@dataclass
class MatchCondition(Condition):
    """最后那段话里匹配得上才跑。用正则是为了「出现某个标记」和「出现某个数字」能用同一套。"""

    kind: ClassVar[str] = CONDITION_MATCH
    pattern: str = ""

    async def check(self, occurrence: Occurrence, workspace: str) -> ConditionResult:
        if not self.pattern:
            return ConditionResult(passed=True)
        matched = re.search(self.pattern, occurrence.text or "", re.MULTILINE) is not None
        return ConditionResult(passed=matched, detail=f"正文里{'有' if matched else '没有'}匹配 /{self.pattern}/")

    def describe(self) -> str:
        return f"（最后一段话匹配 /{self.pattern}/）"


@dataclass
class ShellCondition(Condition):
    """跑一条命令：退出码 0 算数，1 明确说不，其他值和超时都算脚本自己出了问题。"""

    kind: ClassVar[str] = CONDITION_SHELL
    command: str = ""
    timeout_seconds: int = DEFAULT_CONDITION_TIMEOUT_SECONDS

    async def check(self, occurrence: Occurrence, workspace: str) -> ConditionResult:
        result = await run_script(
            self.command,
            payload=occurrence.to_payload(),
            cwd=workspace,
            timeout_seconds=self.timeout_seconds,
        )
        if result.ok:
            return ConditionResult(passed=True)
        if result.exit_code == 1:
            return ConditionResult(passed=False, detail="检查脚本说这次不满足")
        return ConditionResult(passed=False, problem=result.describe_failure())

    def describe(self) -> str:
        return "（检查脚本通过才跑）"


@dataclass
class Trigger:
    """什么时候跑 = 一个源 + 一个条件。"""

    source: Source = field(default_factory=ClockSource)
    condition: Condition = field(default_factory=AlwaysCondition)

    def next_run_at(self, reference: datetime) -> datetime | None:
        return self.source.next_run_at(reference)

    def matches(self, occurrence: Occurrence) -> bool:
        return self.source.matches(occurrence)

    async def passes(self, occurrence: Occurrence, workspace: str) -> ConditionResult:
        return await self.condition.check(occurrence, workspace)

    def describe(self, automation_names: dict[str, str] | None = None) -> str:
        return self.source.describe(automation_names) + self.condition.describe()

    def to_dict(self) -> dict[str, Any]:
        return {"source": _dataclass_dict(self.source), "condition": _dataclass_dict(self.condition)}


def _dataclass_dict(value: Any) -> dict[str, Any]:
    """dataclass → 能 json.dumps 的字典，ClassVar 的 kind 不算字段，这里补回去。"""
    data = {key: value for key, value in vars(value).items()}
    data["kind"] = value.kind
    return data


def trigger_from_dict(data: Any, previous: Trigger | None = None) -> Trigger:
    """存储和表单都从这里进：认新结构，也认老 automations.json 里的扁平写法。"""
    if not isinstance(data, dict):
        raise ConfigError("触发器要是一个 JSON 对象")
    if "source" in data:
        return Trigger(
            source=source_from_dict(data["source"], previous.source if previous else None),
            condition=condition_from_dict(data.get("condition")),
        )
    # 老写法：schedule_kind / daily_time / weekdays / interval_minutes 平铺在自动化上
    condition = previous.condition if previous is not None else AlwaysCondition()
    return Trigger(source=_clock_source(data), condition=condition)


def source_from_dict(data: Any, previous: Source | None = None) -> Source:
    if not isinstance(data, dict):
        raise ConfigError("事件源要是一个 JSON 对象")
    kind = str(data.get("kind") or SOURCE_CLOCK)
    if kind == SOURCE_CLOCK:
        return _clock_source(data)
    if kind == SOURCE_TASK_END:
        return TaskEndSource(automation_id=str(data.get("automation_id") or "").strip())
    if kind == SOURCE_WEBHOOK:
        return _webhook_source(data, previous)
    if kind == SOURCE_POLL:
        return _poll_source(data)
    raise ConfigError(f"不认识的事件源：{kind}")


def _clock_source(data: dict[str, Any]) -> ClockSource:
    """同时认新字段 mode 和老字段 schedule_kind，两者写的是一件事。"""
    mode = str(data.get("mode") or data.get("schedule_kind") or CLOCK_DAILY).strip()
    if mode not in CLOCK_MODES:
        raise ConfigError(f"不认识的计划方式：{mode}")
    if mode == CLOCK_INTERVAL:
        return ClockSource(mode=CLOCK_INTERVAL, interval_minutes=_interval_minutes(data.get("interval_minutes"), 60))
    daily_time = str(data.get("daily_time") or DEFAULT_DAILY_TIME)
    parse_time_of_day(daily_time)
    weekdays = _weekdays(data.get("weekdays"))
    if not weekdays:
        raise ConfigError("至少选一天")
    return ClockSource(mode=CLOCK_DAILY, daily_time=daily_time, weekdays=weekdays)


def _webhook_source(data: dict[str, Any], previous: Source | None) -> WebhookSource:
    # 编辑别的字段时不能把密钥换掉，不然已经发出去的外部调用全 401
    previous_secret = previous.secret if isinstance(previous, WebhookSource) else ""
    secret = str(data.get("secret") or "").strip() or previous_secret or secrets.token_urlsafe(24)
    return WebhookSource(secret=secret)


def _poll_source(data: dict[str, Any]) -> PollSource:
    command = str(data.get("command") or "").strip()
    if not command:
        raise ConfigError("轮询要写一条检查命令")
    return PollSource(
        interval_minutes=_interval_minutes(data.get("interval_minutes"), DEFAULT_INTERVAL_MINUTES),
        command=command,
        timeout_seconds=_timeout_seconds(data.get("timeout_seconds")),
    )


def condition_from_dict(data: Any) -> Condition:
    if data is None:
        return AlwaysCondition()
    if not isinstance(data, dict):
        raise ConfigError("条件要是一个 JSON 对象")
    kind = str(data.get("kind") or CONDITION_ALWAYS)
    if kind == CONDITION_ALWAYS:
        return AlwaysCondition()
    if kind == CONDITION_STATUS:
        return StatusCondition(status=str(data.get("status") or "done").strip())
    if kind == CONDITION_MATCH:
        pattern = str(data.get("pattern") or "")
        if len(pattern) > MAX_PATTERN_CHARS:
            raise ConfigError(f"条件里的正则太长了（上限 {MAX_PATTERN_CHARS} 字）")
        if pattern:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ConfigError(f"条件里的正则写错了：{error}")
        return MatchCondition(pattern=pattern)
    if kind == CONDITION_SHELL:
        command = str(data.get("command") or "").strip()
        if not command:
            raise ConfigError("脚本条件要写一条命令")
        return ShellCondition(command=command, timeout_seconds=_timeout_seconds(data.get("timeout_seconds")))
    raise ConfigError(f"不认识的条件：{kind}")


def _weekdays(raw: Any) -> list[int]:
    if raw in (None, ""):
        return list(ALL_WEEKDAYS)
    try:
        days = sorted({int(day) for day in raw})
    except (TypeError, ValueError):
        raise ConfigError("weekdays 要是一串 0–6 的数字")
    if any(day < 0 or day > 6 for day in days):
        raise ConfigError("weekdays 要是一串 0–6 的数字")
    return days


def _positive_int(raw: Any, default: int) -> int:
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"要是一个整数：{raw}")
    if value <= 0:
        raise ConfigError(f"要大于 0：{raw}")
    return value


def _interval_minutes(raw: Any, default: int) -> int:
    value = _positive_int(raw, default)
    if value < MIN_INTERVAL_MINUTES:
        raise ConfigError(f"间隔不能少于 {MIN_INTERVAL_MINUTES} 分钟")
    return value


def _timeout_seconds(raw: Any) -> int:
    value = _positive_int(raw, DEFAULT_CONDITION_TIMEOUT_SECONDS)
    if value > MAX_SCRIPT_TIMEOUT_SECONDS:
        raise ConfigError(f"超时不能超过 {MAX_SCRIPT_TIMEOUT_SECONDS} 秒")
    return value
