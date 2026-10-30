"""触发器：什么时候算到点、条件怎么判、引擎怎么去重和防环。"""

import unittest
from dataclasses import dataclass
from datetime import datetime

from pocketagent.config import ConfigError
from pocketagent.safety.processes import bash_executable
from pocketagent.server.automations import Automation
from pocketagent.server.triggers.engine import MAX_CHAIN_DEPTH, MAX_FIRES_PER_HOUR, TriggerEngine, sign_webhook
from pocketagent.server.triggers.model import (
    SOURCE_CLOCK,
    SOURCE_TASK_END,
    SOURCE_WEBHOOK,
    AlwaysCondition,
    ClockSource,
    ConditionResult,
    MatchCondition,
    Occurrence,
    PollSource,
    ShellCondition,
    StatusCondition,
    TaskEndSource,
    Trigger,
    WebhookSource,
)
from tests.helpers import TemporaryDirectories

HAS_BASH = bash_executable() is not None


@dataclass
class FakeSession:
    id: str


class FakeAutomations:
    """只实现引擎用得到的四个方法，省得每个测试都去写文件。"""

    def __init__(self, automations: list[Automation]) -> None:
        self.items = {automation.id: automation for automation in automations}

    def list_all(self) -> list[Automation]:
        return list(self.items.values())

    def get(self, automation_id: str) -> Automation:
        return self.items[automation_id]

    def name_map(self) -> dict[str, str]:
        return {automation.id: automation.name for automation in self.items.values()}

    def save(self, automation: Automation) -> None:
        self.items[automation.id] = automation


class RecordingRunner:
    """记下每一次被叫起来开任务，返回一个假任务。"""

    def __init__(self) -> None:
        self.started: list[tuple[Automation, Occurrence]] = []

    def __call__(self, automation: Automation, occurrence: Occurrence) -> FakeSession:
        self.started.append((automation, occurrence))
        return FakeSession(id=f"session-{len(self.started)}")


def build_automation(
    automation_id: str = "a1",
    name: str = "巡检",
    source: object | None = None,
    condition: object | None = None,
    created_at: str = "2026-10-26T08:00:00",
) -> Automation:
    return Automation(
        id=automation_id,
        name=name,
        workspace="/tmp",
        prompt="干活",
        trigger=Trigger(
            source=source if source is not None else ClockSource(),
            condition=condition if condition is not None else AlwaysCondition(),
        ),
        created_at=created_at,
    )


def build_engine(automations: list[Automation], runner: RecordingRunner, running: set[str] | None = None) -> TriggerEngine:
    always_running = running or set()
    return TriggerEngine(
        automations=FakeAutomations(automations),
        start_run=runner,
        is_running=lambda automation: automation.id in always_running,
        clock=lambda: datetime(2026, 10, 26, 9, 31),
    )


def task_end_occurrence(key: str = "t1", automation_id: str = "", depth: int = 0, status: str = "done", text: str = "") -> Occurrence:
    return Occurrence(
        kind=SOURCE_TASK_END,
        key=key,
        at=datetime(2026, 10, 26, 9, 31),
        automation_id=automation_id,
        automation_name="日报" if automation_id else "",
        session_id="s1",
        status=status,
        text=text,
        depth=depth,
    )


class ClockSourceTest(unittest.TestCase):
    def test_daily_fires_on_selected_weekdays(self) -> None:
        # 2026-10-21 是周三，选周一和周三
        source = ClockSource(daily_time="09:30", weekdays=[0, 2])
        reference = datetime(2026, 10, 21, 10, 0)

        self.assertEqual(source.next_run_at(reference), datetime(2026, 10, 26, 9, 30))
        self.assertEqual(source.describe(), "周一、周三 09:30")

    def test_interval_counts_from_reference(self) -> None:
        source = ClockSource(mode="interval", interval_minutes=120)

        self.assertEqual(source.next_run_at(datetime(2026, 10, 26, 8, 0)), datetime(2026, 10, 26, 10, 0))
        self.assertEqual(source.describe(), "每 2 小时")

    def test_describe_matches_old_wording(self) -> None:
        self.assertEqual(ClockSource(daily_time="09:00").describe(), "每天 09:00")
        self.assertEqual(ClockSource(daily_time="18:00", weekdays=[0, 1, 2, 3, 4]).describe(), "工作日 18:00")
        self.assertEqual(ClockSource(mode="interval", interval_minutes=30).describe(), "每 30 分钟")


class SourceMatchingTest(unittest.TestCase):
    def test_task_end_can_watch_one_automation(self) -> None:
        source = TaskEndSource(automation_id="a1")

        self.assertTrue(source.matches(task_end_occurrence(automation_id="a1")))
        self.assertFalse(source.matches(task_end_occurrence(automation_id="a2")))

    def test_task_end_without_filter_takes_any_automation(self) -> None:
        source = TaskEndSource()

        self.assertTrue(source.matches(task_end_occurrence(automation_id="a2")))
        # 手动跑的任务也发任务结束，automation_id 是空的
        self.assertTrue(source.matches(task_end_occurrence(automation_id="")))

    def test_source_ignores_other_kinds(self) -> None:
        clock_occurrence = Occurrence(kind=SOURCE_CLOCK, key="clock:1", at=datetime(2026, 10, 26, 9, 0))
        self.assertFalse(TaskEndSource().matches(clock_occurrence))

    def test_describe_names_the_watched_automation(self) -> None:
        source = TaskEndSource(automation_id="a1")

        self.assertEqual(source.describe({"a1": "日报"}), "「日报」跑完后")
        self.assertEqual(source.describe({}), "「另一个自动化」跑完后")


class ConditionTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_condition_rejects_failed_run(self) -> None:
        condition = StatusCondition(status="done")

        passed = await condition.check(task_end_occurrence(status="done"), "/tmp")
        failed = await condition.check(task_end_occurrence(status="error"), "/tmp")

        self.assertTrue(passed.passed)
        self.assertFalse(failed.passed)
        self.assertIn("error", failed.detail)

    async def test_match_condition_looks_at_final_text(self) -> None:
        condition = MatchCondition(pattern="已完成|DONE")

        self.assertTrue((await condition.check(task_end_occurrence(text="都已完成"), "/tmp")).passed)
        self.assertFalse((await condition.check(task_end_occurrence(text="还在跑"), "/tmp")).passed)

    @unittest.skipUnless(HAS_BASH, "这台机器上没有 bash")
    async def test_shell_condition_uses_exit_code(self) -> None:
        self.assertTrue((await ShellCondition(command="cat > /dev/null; exit 0").check(task_end_occurrence(), "/tmp")).passed)
        self.assertFalse((await ShellCondition(command="exit 1").check(task_end_occurrence(), "/tmp")).passed)

    @unittest.skipUnless(HAS_BASH, "这台机器上没有 bash")
    async def test_shell_condition_reports_broken_script_as_problem(self) -> None:
        result = await ShellCondition(command="exit 3").check(task_end_occurrence(), "/tmp")

        self.assertFalse(result.passed)
        self.assertIn("退出码 3", result.problem)


class TriggerEngineTest(unittest.IsolatedAsyncioTestCase):
    async def test_clock_fires_once_and_then_waits_for_tomorrow(self) -> None:
        automation = build_automation(source=ClockSource(daily_time="09:00"))
        runner = RecordingRunner()
        engine = build_engine([automation], runner)

        first = await engine.tick(datetime(2026, 10, 26, 9, 1))
        second = await engine.tick(datetime(2026, 10, 26, 9, 2))

        self.assertTrue(first[0].fired)
        self.assertEqual(second, [])
        self.assertEqual(len(runner.started), 1)
        self.assertEqual(runner.started[0][1].key, "clock:a1:2026-10-26T09:00")

    async def test_disabled_automation_never_ticks(self) -> None:
        automation = build_automation(source=ClockSource(daily_time="09:00"))
        automation.enabled = False
        runner = RecordingRunner()

        outcomes = await build_engine([automation], runner).tick(datetime(2026, 10, 26, 9, 1))

        self.assertEqual(outcomes, [])

    async def test_same_occurrence_is_not_run_twice(self) -> None:
        automation = build_automation(source=TaskEndSource())
        runner = RecordingRunner()
        engine = build_engine([automation], runner)
        occurrence = task_end_occurrence(key="t1")

        first = await engine.fire(occurrence)
        second = await engine.fire(occurrence)

        self.assertTrue(first[0].fired)
        self.assertFalse(second[0].fired)
        self.assertIn("已经处理过", second[0].reason)

    async def test_chain_depth_stops_runaway_loops(self) -> None:
        automation = build_automation(source=TaskEndSource())
        runner = RecordingRunner()
        engine = build_engine([automation], runner)

        outcome = (await engine.fire(task_end_occurrence(key="deep", depth=MAX_CHAIN_DEPTH)))[0]

        self.assertFalse(outcome.fired)
        self.assertIn("触发链", outcome.reason)
        self.assertEqual(runner.started, [])

    async def test_running_automation_is_skipped_not_queued(self) -> None:
        automation = build_automation(source=TaskEndSource())
        runner = RecordingRunner()
        engine = build_engine([automation], runner, running={"a1"})

        outcome = (await engine.fire(task_end_occurrence(key="t9")))[0]

        self.assertFalse(outcome.fired)
        self.assertIn("还在跑", outcome.reason)

    async def test_hourly_limit(self) -> None:
        automation = build_automation(source=TaskEndSource())
        runner = RecordingRunner()
        engine = build_engine([automation], runner)

        outcomes = [await engine.fire(task_end_occurrence(key=f"k{index}")) for index in range(MAX_FIRES_PER_HOUR + 1)]

        self.assertTrue(all(outcome[0].fired for outcome in outcomes[:MAX_FIRES_PER_HOUR]))
        self.assertFalse(outcomes[-1][0].fired)
        self.assertIn("一小时", outcomes[-1][0].reason)

    async def test_failed_condition_does_not_start_a_task(self) -> None:
        automation = build_automation(source=TaskEndSource(), condition=StatusCondition(status="done"))
        runner = RecordingRunner()
        engine = build_engine([automation], runner)

        outcome = (await engine.fire(task_end_occurrence(key="bad", status="error")))[0]

        self.assertFalse(outcome.fired)
        self.assertIn("stop_reason=error", outcome.reason)

    async def test_only_matching_source_fires(self) -> None:
        watched = build_automation("a1", "被等的", source=TaskEndSource(automation_id="upstream"))
        other = build_automation("a2", "别人的", source=TaskEndSource(automation_id="somewhere-else"))
        runner = RecordingRunner()
        engine = build_engine([watched, other], runner)

        await engine.fire(task_end_occurrence(key="t2", automation_id="upstream"))

        self.assertEqual([automation.id for automation, _ in runner.started], ["a1"])

    async def test_start_failure_is_recorded_not_raised(self) -> None:
        automation = build_automation(source=TaskEndSource())

        def explode(automation: Automation, occurrence: Occurrence) -> FakeSession:
            raise ConfigError("工作区不见了")

        engine = TriggerEngine(
            automations=FakeAutomations([automation]),
            start_run=explode,
            is_running=lambda candidate: False,
        )

        outcome = (await engine.fire(task_end_occurrence(key="boom")))[0]

        self.assertFalse(outcome.fired)
        self.assertIn("工作区不见了", engine.problem_for("a1"))


class PollTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directories = TemporaryDirectories()

    async def asyncTearDown(self) -> None:
        self.directories.cleanup()

    @unittest.skipUnless(HAS_BASH, "这台机器上没有 bash")
    async def test_exit_zero_fires_and_exit_one_does_not(self) -> None:
        hits = build_automation(
            "hit",
            "有货",
            source=PollSource(interval_minutes=5, command="exit 0"),
        )
        misses = build_automation(
            "miss",
            "没货",
            source=PollSource(interval_minutes=5, command="exit 1"),
        )
        for automation in (hits, misses):
            automation.workspace = str(self.directories.workspace)
        runner = RecordingRunner()
        engine = build_engine([hits, misses], runner)

        await engine.tick(datetime(2026, 10, 26, 9, 1))

        self.assertEqual([automation.id for automation, _ in runner.started], ["hit"])

    @unittest.skipUnless(HAS_BASH, "这台机器上没有 bash")
    async def test_interval_gates_repeated_polls(self) -> None:
        automation = build_automation(source=PollSource(interval_minutes=5, command="exit 0"))
        automation.workspace = str(self.directories.workspace)
        runner = RecordingRunner()
        engine = build_engine([automation], runner)

        await engine.tick(datetime(2026, 10, 26, 9, 1))
        await engine.tick(datetime(2026, 10, 26, 9, 3))
        await engine.tick(datetime(2026, 10, 26, 9, 7))

        self.assertEqual(len(runner.started), 2)

    @unittest.skipUnless(HAS_BASH, "这台机器上没有 bash")
    async def test_broken_poll_script_is_reported(self) -> None:
        automation = build_automation(source=PollSource(interval_minutes=5, command="exit 7"))
        automation.workspace = str(self.directories.workspace)
        engine = build_engine([automation], RecordingRunner())

        await engine.tick(datetime(2026, 10, 26, 9, 1))

        self.assertIn("退出码 7", engine.problem_for("a1"))


class WebhookTest(unittest.IsolatedAsyncioTestCase):
    def build(self) -> tuple[Automation, RecordingRunner, TriggerEngine]:
        automation = build_automation(source=WebhookSource(secret="s3cret"))
        runner = RecordingRunner()
        return automation, runner, build_engine([automation], runner)

    def test_signature_round_trip(self) -> None:
        from pocketagent.server.triggers.engine import verify_webhook

        body = b'{"hello": 1}'
        signature = sign_webhook("s3cret", body)

        self.assertTrue(verify_webhook("s3cret", body, signature))
        self.assertTrue(verify_webhook("s3cret", body, f"sha256={signature}"))
        self.assertFalse(verify_webhook("s3cret", body, "deadbeef"))
        self.assertFalse(verify_webhook("other", body, signature))
        self.assertFalse(verify_webhook("s3cret", body, ""))

    async def test_webhook_fires_once_per_delivery(self) -> None:
        automation, runner, engine = self.build()

        first = await engine.fire_webhook(automation.id, {"build": 42}, delivery_id="d-1")
        retry = await engine.fire_webhook(automation.id, {"build": 42}, delivery_id="d-1")

        self.assertTrue(first.fired)
        self.assertFalse(retry.fired)
        self.assertEqual(len(runner.started), 1)

    async def test_webhook_without_delivery_id_dedups_on_body(self) -> None:
        automation, runner, engine = self.build()

        await engine.fire_webhook(automation.id, {"build": 42})
        await engine.fire_webhook(automation.id, {"build": 42})
        await engine.fire_webhook(automation.id, {"build": 43})

        self.assertEqual(len(runner.started), 2)

    async def test_webhook_source_is_not_part_of_broadcast(self) -> None:
        automation, runner, engine = self.build()

        await engine.fire(task_end_occurrence(key="broadcast"))

        self.assertEqual(runner.started, [])

    async def test_non_webhook_automation_rejects_webhook_call(self) -> None:
        automation = build_automation(source=ClockSource())
        engine = build_engine([automation], RecordingRunner())

        with self.assertRaises(ConfigError):
            await engine.fire_webhook(automation.id, {})


class TriggerDescribeTest(unittest.TestCase):
    def test_task_end_trigger_sentence(self) -> None:
        trigger = Trigger(source=TaskEndSource(automation_id="a1"), condition=StatusCondition(status="done"))

        self.assertEqual(trigger.describe({"a1": "日报"}), "「日报」跑完后（仅 done）")

    def test_webhook_and_poll_sentences(self) -> None:
        self.assertEqual(Trigger(source=WebhookSource(secret="x")).describe(), "外部 POST")
        self.assertEqual(Trigger(source=PollSource(command="true")).describe(), "每 5 分钟检查一次")


if __name__ == "__main__":
    unittest.main()
