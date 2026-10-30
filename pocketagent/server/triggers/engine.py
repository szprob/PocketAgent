"""把「发生了一次」变成「跑一次任务」。

四种源最后都到这里，判断顺序是刻意的：

1. 匹配：这条自动化配的是不是这个源
2. 去重：这个 occurrence 是不是已经处理过
3. 频次和环路：一小时里跑太多次、或者已经套了三层触发，就别再往下传
4. 并发：上一次还在跑就跳过——同一个工作区同时开两个任务改同一批文件没有意义
5. 条件：正则 / 状态 / 脚本，最贵的一个，所以放最后

前四步都是纯内存判断，第五步才可能 fork 进程。
"""

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from pocketagent.config import ConfigError
from pocketagent.server.automations import Automation, AutomationStore
from pocketagent.server.triggers.model import (
    SOURCE_CLOCK,
    SOURCE_POLL,
    SOURCE_WEBHOOK,
    ClockSource,
    Occurrence,
    PollSource,
    WebhookSource,
)
from pocketagent.safety.scripts import run_script

# 同一个 occurrence 在一天里只算一次：够挡住 webhook 重试、连点、连着重启
DEFAULT_DEDUP_SECONDS = 24 * 60 * 60
MAX_FIRES_PER_HOUR = 12
MAX_CHAIN_DEPTH = 3
# 内存里的记账不会长到不可控：超过这个数就顺手清一遍过期的
DEDUP_PRUNE_THRESHOLD = 512


@dataclass
class FireOutcome:
    """一次判定的结论。没跑也要说清楚为什么，不然用户只能猜。"""

    automation_id: str
    automation_name: str
    fired: bool
    reason: str
    session_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "automation_id": self.automation_id,
            "automation_name": self.automation_name,
            "fired": self.fired,
            "reason": self.reason,
            "session_id": self.session_id,
        }


@dataclass
class TriggerEngine:
    """所有触发源的唯一入口。

    automations：AutomationStore，引擎只读它。
    start_run：真的去开一个任务，由 server 注入，引擎不关心任务是怎么开的。
    is_running：问一下这条自动化上一次是不是还在跑。
    """

    automations: AutomationStore
    start_run: Callable[[Automation, Occurrence], Any]
    is_running: Callable[[Automation], bool]
    clock: Callable[[], datetime] = datetime.now
    dedup_seconds: int = DEFAULT_DEDUP_SECONDS
    # 下面几个都是运行时的记账，重启就没了：计划错过的会补一次，事件类的补不回来
    seen: dict[str, float] = field(default_factory=dict)
    fires: dict[str, list[float]] = field(default_factory=dict)
    clock_reference: dict[str, datetime] = field(default_factory=dict)
    polled_at: dict[str, datetime] = field(default_factory=dict)
    problems: dict[str, str] = field(default_factory=dict)

    # ---- 三个入口 ----

    async def tick(self, now: datetime | None = None) -> list[FireOutcome]:
        """调度器每轮问一次：有没有到点的、有没有轮询该跑的。"""
        current = now or self.clock()
        outcomes: list[FireOutcome] = []
        for automation in self.automations.list_all():
            if not automation.enabled:
                continue
            source = automation.trigger.source
            if isinstance(source, ClockSource):
                occurrence = self._clock_occurrence(automation, source, current)
                if occurrence is not None:
                    outcomes.append(await self._guarded(automation, occurrence))
            elif isinstance(source, PollSource):
                occurrence = await self._poll_occurrence(automation, source, current)
                if occurrence is not None:
                    outcomes.append(await self._guarded(automation, occurrence))
        return outcomes

    async def fire(self, occurrence: Occurrence) -> list[FireOutcome]:
        """广播型事件（目前只有任务结束）：谁配了这个源、条件又过得去，谁跑。"""
        outcomes: list[FireOutcome] = []
        for automation in self.automations.list_all():
            if not automation.enabled:
                continue
            if isinstance(automation.trigger.source, WebhookSource):
                # 外部触发是点名进来的，不参与广播，见 fire_webhook
                continue
            if not automation.trigger.matches(occurrence):
                continue
            outcomes.append(await self._guarded(automation, occurrence))
        return outcomes

    async def fire_webhook(
        self,
        automation_id: str,
        payload: dict[str, Any],
        delivery_id: str = "",
    ) -> FireOutcome:
        """外面 POST 进来的那一下。签名已经在校验的调用方验过了，这里只管跑不跑。"""
        automation = self.automations.get(automation_id)
        source = automation.trigger.source
        if not isinstance(source, WebhookSource):
            raise ConfigError("这条自动化不是外部触发")
        occurrence = Occurrence(
            kind=SOURCE_WEBHOOK,
            key=self._webhook_key(automation_id, payload, delivery_id),
            at=self.clock(),
            automation_id=automation.id,
            automation_name=automation.name,
            payload=payload,
        )
        return await self._dispatch(automation, occurrence)

    # ---- 源 ----

    def _clock_occurrence(
        self,
        automation: Automation,
        source: ClockSource,
        now: datetime,
    ) -> Occurrence | None:
        reference = self.clock_reference.get(automation.id) or automation.reference_time()
        scheduled = source.next_run_at(reference)
        if scheduled > now:
            return None
        # 一旦看过这一次计划，就从现在重新算下一次：服务停了三天，回来只补一次，不补三次
        self.clock_reference[automation.id] = now
        return Occurrence(
            kind=SOURCE_CLOCK,
            key=f"clock:{automation.id}:{scheduled.isoformat(timespec='minutes')}",
            at=now,
            automation_id=automation.id,
            automation_name=automation.name,
        )

    async def _poll_occurrence(
        self,
        automation: Automation,
        source: PollSource,
        now: datetime,
    ) -> Occurrence | None:
        last = self.polled_at.get(automation.id)
        if last is not None and now - last < timedelta(minutes=source.interval_minutes):
            return None
        # 先记下这次时间：脚本再慢也不会叠加成两路
        self.polled_at[automation.id] = now
        result = await run_script(
            source.command,
            payload={
                "event": SOURCE_POLL,
                "automation_id": automation.id,
                "automation_name": automation.name,
                "workspace": automation.workspace,
                "at": now.isoformat(timespec="seconds"),
            },
            cwd=automation.workspace,
            timeout_seconds=source.timeout_seconds,
        )
        if result.ok:
            self.problems.pop(automation.id, None)
            return Occurrence(
                kind=SOURCE_POLL,
                key=f"poll:{automation.id}:{now.isoformat(timespec='minutes')}",
                at=now,
                automation_id=automation.id,
                automation_name=automation.name,
            )
        # 退出码 1 是「现在还没有」，不算毛病；其他情况和超时要把原因留在界面上
        if result.exit_code != 1:
            self.problems[automation.id] = result.describe_failure()
        return None

    def _webhook_key(self, automation_id: str, payload: dict[str, Any], delivery_id: str) -> str:
        if delivery_id.strip():
            return f"webhook:{automation_id}:{delivery_id.strip()}"
        # 没有 delivery id 就用正文的指纹：同一条重试发两次只跑一次
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        return f"webhook:{automation_id}:{digest.hexdigest()[:16]}"

    # ---- 判定 ----

    async def _guarded(self, automation: Automation, occurrence: Occurrence) -> FireOutcome:
        """一条自动化出事不能连累别的：记下原因，继续跑下一个。"""
        try:
            return await self._dispatch(automation, occurrence)
        except Exception as error:  # 存储坏了、工作区被删了，都只能记下来
            self.problems[automation.id] = f"{type(error).__name__}: {error}"
            return FireOutcome(
                automation_id=automation.id,
                automation_name=automation.name,
                fired=False,
                reason=f"出错：{error}",
            )

    async def _dispatch(self, automation: Automation, occurrence: Occurrence) -> FireOutcome:
        skipped = {
            "automation_id": automation.id,
            "automation_name": automation.name,
            "fired": False,
        }
        if not self._claim(automation, occurrence):
            return FireOutcome(**skipped, reason="这件事已经处理过")
        if not self._rate_allows(automation, occurrence.at):
            return FireOutcome(**skipped, reason=f"一小时内已经跑了 {MAX_FIRES_PER_HOUR} 次")
        if occurrence.depth >= MAX_CHAIN_DEPTH:
            return FireOutcome(**skipped, reason=f"触发链已经有 {occurrence.depth} 层")
        if self.is_running(automation):
            return FireOutcome(**skipped, reason="上一次还在跑")
        result = await automation.trigger.passes(occurrence, automation.workspace)
        if result.problem:
            self.problems[automation.id] = result.problem
        if not result.passed:
            return FireOutcome(**skipped, reason=result.detail or "条件不满足")
        try:
            session = self.start_run(automation, occurrence)
        except (ConfigError, KeyError) as error:
            self.problems[automation.id] = str(error)
            return FireOutcome(**skipped, reason=f"启动失败：{error}")
        self.fires.setdefault(automation.id, []).append(occurrence.at.timestamp())
        return FireOutcome(
            automation_id=automation.id,
            automation_name=automation.name,
            fired=True,
            reason="已启动",
            session_id=session.id,
        )

    def _claim(self, automation: Automation, occurrence: Occurrence) -> bool:
        """第一次见到这个 occurrence 才算数。先记账再干活：宁可少跑一次，也不少跑两次。"""
        now = occurrence.at.timestamp()
        if len(self.seen) > DEDUP_PRUNE_THRESHOLD:
            self._prune(now)
        key = f"{automation.id}|{occurrence.key}"
        if key in self.seen:
            return False
        self.seen[key] = now
        return True

    def _prune(self, now: float) -> None:
        expired = [key for key, seen_at in self.seen.items() if now - seen_at > self.dedup_seconds]
        for key in expired:
            del self.seen[key]

    def _rate_allows(self, automation: Automation, moment: datetime) -> bool:
        recent = [fired_at for fired_at in self.fires.get(automation.id, []) if moment.timestamp() - fired_at < 3600]
        self.fires[automation.id] = recent
        return len(recent) < MAX_FIRES_PER_HOUR

    def problem_for(self, automation_id: str) -> str:
        """最近一次轮询 / 条件脚本出的问题，界面用它提示「这条自动化坏了」。"""
        return self.problems.get(automation_id, "")


def sign_webhook(secret: str, body: bytes) -> str:
    """HMAC-SHA256 的十六进制串，放在 X-PocketAgent-Signature 里。"""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_webhook(secret: str, body: bytes, signature: str) -> bool:
    if not signature.strip():
        return False
    provided = signature.strip()
    if provided.startswith("sha256="):
        provided = provided[len("sha256=") :]
    return hmac.compare_digest(sign_webhook(secret, body), provided)
