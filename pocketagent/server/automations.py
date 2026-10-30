"""自动化：一条「什么时候跑 + 跑什么」的记录。

什么时候跑交给 `trigger`（四种源见 triggers/model.py），这个文件只管跑什么：
工作区、提示词、模型、权限，外加跑完之后的运行记录。

定义存在 ~/.pocketagent/automations.json。老的扁平写法（schedule_kind / daily_time /
weekdays / interval_minutes）读进来就转成 clock 源，存回去只写 trigger 一个字段，
所以老文件不用手动迁移，新文件也只有一种形状。
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pocketagent.config import ConfigError, new_model_id
from pocketagent.safety.permissions import ApprovalMode
from pocketagent.server.triggers.model import Trigger, trigger_from_dict

# 没人在场审批，能选的只有这两档
AUTOMATION_APPROVAL_MODES = [ApprovalMode.AI_AUTO.value, ApprovalMode.FULL_AUTO.value]


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Automation:
    id: str
    name: str
    workspace: str
    prompt: str
    model_id: str | None = None
    approval_mode: str = ApprovalMode.AI_AUTO.value
    enabled: bool = True
    trigger: Trigger = field(default_factory=Trigger)
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
        automation = cls(
            id=existing.id if existing else new_model_id(),
            name=name,
            workspace=str(Path(workspace).expanduser().resolve()),
            prompt=prompt,
            model_id=str(data.get("model_id") or "").strip() or None,
            approval_mode=approval_mode,
            enabled=bool(data.get("enabled", True)),
            trigger=_trigger_from_payload(data, existing.trigger if existing else None),
        )
        if existing is not None:
            automation.created_at = existing.created_at
            automation.last_run_at = existing.last_run_at
            automation.last_session_id = existing.last_session_id
            automation.last_status = existing.last_status
        return automation

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Automation":
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or ""),
            workspace=str(data.get("workspace") or ""),
            prompt=str(data.get("prompt") or ""),
            model_id=str(data["model_id"]) if data.get("model_id") else None,
            approval_mode=str(data.get("approval_mode") or ApprovalMode.AI_AUTO.value),
            enabled=bool(data.get("enabled", True)),
            trigger=_trigger_from_payload(data, None),
            created_at=str(data.get("created_at") or _now_text()),
            last_run_at=data.get("last_run_at"),
            last_session_id=data.get("last_session_id"),
            last_status=data.get("last_status"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "workspace": self.workspace,
            "prompt": self.prompt,
            "model_id": self.model_id,
            "approval_mode": self.approval_mode,
            "enabled": self.enabled,
            "trigger": self.trigger.to_dict(),
            "created_at": self.created_at,
            "last_run_at": self.last_run_at,
            "last_session_id": self.last_session_id,
            "last_status": self.last_status,
        }

    def reference_time(self) -> datetime:
        """算下一次计划时间的起点：跑过就用上次跑的时间，没跑过就用创建时间。"""
        return datetime.fromisoformat(self.last_run_at or self.created_at)

    def next_run_at(self) -> datetime | None:
        """下个计划时间。不是到点触发的自动化没有这个概念，返回 None。"""
        return self.trigger.next_run_at(self.reference_time())

    def describe_schedule(self, automation_names: dict[str, str] | None = None) -> str:
        return self.trigger.describe(automation_names)


def _trigger_from_payload(data: dict[str, Any], previous: Trigger | None) -> Trigger:
    """新写法把触发器放在 trigger 字段里，老写法平铺在自动化上，两种都认。"""
    trigger_data = data.get("trigger")
    if isinstance(trigger_data, dict):
        return trigger_from_dict(trigger_data, previous)
    return trigger_from_dict(data, previous)


class AutomationStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def list_all(self) -> list[Automation]:
        if not self.path.is_file():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [Automation.from_dict(item) for item in data]

    def get(self, automation_id: str) -> Automation:
        for automation in self.list_all():
            if automation.id == automation_id:
                return automation
        raise KeyError(automation_id)

    def name_map(self) -> dict[str, str]:
        """id → 名字。触发器描述里要显示「等「日报」跑完」，靠它。"""
        return {automation.id: automation.name for automation in self.list_all()}

    def save(self, automation: Automation) -> None:
        """新增或按 id 覆盖。"""
        automations = [item for item in self.list_all() if item.id != automation.id]
        automations.append(automation)
        # 同一秒建的几条按 id 定序：列表顺序不能每次读盘都抖一下
        automations.sort(key=lambda item: (item.created_at, item.id))
        self._save_all(automations)

    def delete(self, automation_id: str) -> None:
        self._save_all([item for item in self.list_all() if item.id != automation_id])

    def _save_all(self, automations: list[Automation]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps([automation.to_dict() for automation in automations], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
