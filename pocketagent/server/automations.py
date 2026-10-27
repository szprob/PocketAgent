"""自动化（定时任务）：按计划在某个工作区里新开一个任务，把一段提示词交给 agent 去做。

定义存在 ~/.pocketagent/automations.json。调度只在 web 服务开着时进行（server/manager.py 里的调度器每 20 秒看一次），
服务关着时错过的计划，下次启动后补跑一次。
没人在场审批，所以权限只能选 AI 自动审批或完全访问；AI 审批不放行的操作会等用户确认，超时就拒绝。
"""

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pocketagent.config import ConfigError, new_model_id
from pocketagent.safety.permissions import ApprovalMode

SCHEDULE_DAILY = "daily"
SCHEDULE_INTERVAL = "interval"
MIN_INTERVAL_MINUTES = 5
WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]
ALL_WEEKDAYS = list(range(7))
AUTOMATION_APPROVAL_MODES = [ApprovalMode.AI_AUTO.value, ApprovalMode.FULL_AUTO.value]


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_time_of_day(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
    if match is None:
        raise ConfigError(f"时间要写成 HH:MM：{text}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ConfigError(f"时间超出范围：{text}")
    return hour, minute


@dataclass
class Automation:
    id: str
    name: str
    workspace: str
    prompt: str
    model_id: str | None = None
    approval_mode: str = ApprovalMode.AI_AUTO.value
    schedule_kind: str = SCHEDULE_DAILY
    # 每天几点（schedule_kind=daily），只在 weekdays 里的那几天跑；0 是周一
    daily_time: str = "09:00"
    weekdays: list[int] = field(default_factory=lambda: list(ALL_WEEKDAYS))
    # 每隔多少分钟（schedule_kind=interval）
    interval_minutes: int = 60
    enabled: bool = True
    created_at: str = field(default_factory=_now_text)
    last_run_at: str | None = None
    last_session_id: str | None = None
    # running / done / error / denied / step_limit / interrupted
    last_status: str | None = None

    @classmethod
    def from_form(cls, data: dict[str, Any], existing: "Automation | None" = None) -> "Automation":
        """网页表单 → Automation，校验不过抛 ConfigError。编辑时保留运行记录。"""
        name = str(data.get("name", "")).strip()
        prompt = str(data.get("prompt", "")).strip()
        workspace = str(data.get("workspace", "")).strip()
        if not name:
            raise ConfigError("名称不能为空")
        if not prompt:
            raise ConfigError("提示词不能为空")
        if not workspace or not Path(workspace).expanduser().is_dir():
            raise ConfigError(f"工作区不存在：{workspace}")
        approval_mode = str(data.get("approval_mode") or ApprovalMode.AI_AUTO.value)
        if approval_mode not in AUTOMATION_APPROVAL_MODES:
            raise ConfigError("自动化没人在场审批，权限只能选 AI 自动审批或完全访问")
        schedule_kind = str(data.get("schedule_kind") or SCHEDULE_DAILY)
        if schedule_kind not in (SCHEDULE_DAILY, SCHEDULE_INTERVAL):
            raise ConfigError(f"不认识的计划类型：{schedule_kind}")
        daily_time = str(data.get("daily_time") or "09:00")
        _parse_time_of_day(daily_time)
        weekdays = sorted({int(day) for day in data.get("weekdays", ALL_WEEKDAYS) if 0 <= int(day) <= 6})
        if schedule_kind == SCHEDULE_DAILY and not weekdays:
            raise ConfigError("至少选一天")
        interval_minutes = int(data.get("interval_minutes") or 60)
        if schedule_kind == SCHEDULE_INTERVAL and interval_minutes < MIN_INTERVAL_MINUTES:
            raise ConfigError(f"间隔不能少于 {MIN_INTERVAL_MINUTES} 分钟")
        automation = cls(
            id=existing.id if existing else new_model_id(),
            name=name,
            workspace=str(Path(workspace).expanduser().resolve()),
            prompt=prompt,
            model_id=str(data.get("model_id") or "").strip() or None,
            approval_mode=approval_mode,
            schedule_kind=schedule_kind,
            daily_time=daily_time,
            weekdays=weekdays,
            interval_minutes=interval_minutes,
            enabled=bool(data.get("enabled", True)),
        )
        if existing is not None:
            automation.created_at = existing.created_at
            automation.last_run_at = existing.last_run_at
            automation.last_session_id = existing.last_session_id
            automation.last_status = existing.last_status
        return automation

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def describe_schedule(self) -> str:
        if self.schedule_kind == SCHEDULE_INTERVAL:
            if self.interval_minutes % 60 == 0:
                return f"每 {self.interval_minutes // 60} 小时"
            return f"每 {self.interval_minutes} 分钟"
        if sorted(self.weekdays) == ALL_WEEKDAYS:
            return f"每天 {self.daily_time}"
        if sorted(self.weekdays) == [0, 1, 2, 3, 4]:
            return f"工作日 {self.daily_time}"
        days = "、".join(f"周{WEEKDAY_NAMES[day]}" for day in sorted(self.weekdays))
        return f"{days} {self.daily_time}"

    def _reference_time(self) -> datetime:
        return datetime.fromisoformat(self.last_run_at or self.created_at)

    def next_run_at(self) -> datetime:
        """上次运行（没跑过就是创建时间）之后的下一个计划时间；早于现在说明该跑了。"""
        reference = self._reference_time()
        if self.schedule_kind == SCHEDULE_INTERVAL:
            return reference + timedelta(minutes=self.interval_minutes)
        hour, minute = _parse_time_of_day(self.daily_time)
        candidate = reference.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= reference:
            candidate += timedelta(days=1)
        while candidate.weekday() not in self.weekdays:
            candidate += timedelta(days=1)
        return candidate

    def is_due(self, now: datetime) -> bool:
        return self.enabled and self.next_run_at() <= now


class AutomationStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def list_all(self) -> list[Automation]:
        if not self.path.is_file():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [Automation(**item) for item in data]

    def get(self, automation_id: str) -> Automation:
        for automation in self.list_all():
            if automation.id == automation_id:
                return automation
        raise KeyError(automation_id)

    def _save_all(self, automations: list[Automation]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps([automation.to_dict() for automation in automations], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def save(self, automation: Automation) -> None:
        """新增或按 id 覆盖。"""
        automations = [item for item in self.list_all() if item.id != automation.id]
        automations.append(automation)
        automations.sort(key=lambda item: item.created_at)
        self._save_all(automations)

    def delete(self, automation_id: str) -> None:
        self._save_all([item for item in self.list_all() if item.id != automation_id])
