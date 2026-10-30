"""AI 自动审批的模型和思考档位：默认用任务模型的最低档，也可以在设置里单独选一个模型。"""

import unittest

from pocketagent.config import (
    REASONING_INHERIT,
    REASONING_LOWEST,
    AppConfig,
    ConfigError,
    ModelProfile,
    resolve_reasoning,
)
from pocketagent.llm import LLMResponse
from pocketagent.llm.scripted import ScriptedProvider
from pocketagent.safety.permissions import ApprovalMode
from tests.helpers import TemporaryDirectories, build_harness, tool_call

WRITE_CALL = tool_call("c1", "write_file", '{"path": "out.txt", "content": "hi"}')
ALLOW = '{"allow": true, "reason": "新建项目文件"}'


class EffortRecordingProvider(ScriptedProvider):
    """记下审批时被换成了哪个思考档位；换档位后还是同一份剧本。"""

    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__(responses)
        self.requested_efforts: list[str | None] = []

    def with_reasoning_effort(self, reasoning_effort: str | None) -> "EffortRecordingProvider":
        self.requested_efforts.append(reasoning_effort)
        return self


class ResolveReasoningTest(unittest.TestCase):
    def test_lowest_inherit_and_explicit(self) -> None:
        choices = ["none", "low", "high", "max"]

        self.assertEqual(resolve_reasoning(REASONING_LOWEST, choices, "max"), "none")
        self.assertEqual(resolve_reasoning(REASONING_LOWEST, ["low", "high", "max"], "max"), "low")
        self.assertEqual(resolve_reasoning(REASONING_INHERIT, choices, "max"), "max")
        self.assertEqual(resolve_reasoning("high", choices, "max"), "high")
        # 不知道模型能选哪些档就不传
        self.assertIsNone(resolve_reasoning(REASONING_LOWEST, [], "max"))


class ReviewConfigTest(unittest.TestCase):
    def test_defaults_and_round_trip(self) -> None:
        models = [{"id": "ds", "model": "deepseek"}, {"id": "glm", "model": "glm-flash"}]
        default = AppConfig.from_dict({"models": models})
        chosen = AppConfig.from_dict({"models": models, "review_model": "glm", "review_reasoning": "inherit"})

        self.assertIsNone(default.review_model)
        self.assertEqual(default.review_reasoning, REASONING_LOWEST)
        reloaded = AppConfig.from_dict(chosen.to_dict())
        self.assertEqual(reloaded.review_model, "glm")
        self.assertEqual(reloaded.review_reasoning, "inherit")
        self.assertEqual(chosen.public_dict()["review_model"], "glm")

    def test_removed_review_model_falls_back_to_the_task_model(self) -> None:
        config = AppConfig.from_dict({"models": [{"id": "ds", "model": "deepseek"}], "review_model": "gone"})

        self.assertIsNone(config.review_model)

    def test_subagent_settings_default_and_round_trip(self) -> None:
        models = [{"id": "ds", "model": "deepseek"}, {"id": "glm", "model": "glm-flash"}]
        default = AppConfig.from_dict({"models": models})
        chosen = AppConfig.from_dict(
            {
                "models": models,
                "subagent_model": "glm",
                "subagent_reasoning": "lowest",
                "subagent_explore_minutes": 5,
                "subagent_general_minutes": 60,
            }
        )
        gone = AppConfig.from_dict({"models": models, "subagent_model": "removed"})

        self.assertIsNone(default.subagent_model)
        self.assertEqual(default.subagent_reasoning, REASONING_INHERIT)
        self.assertEqual((default.subagent_explore_minutes, default.subagent_general_minutes), (10, 40))
        reloaded = AppConfig.from_dict(chosen.to_dict())
        self.assertEqual(reloaded.subagent_model, "glm")
        self.assertEqual(reloaded.subagent_reasoning, REASONING_LOWEST)
        self.assertEqual((reloaded.subagent_explore_minutes, reloaded.subagent_general_minutes), (5, 60))
        self.assertIsNone(gone.subagent_model)
        with self.assertRaises(ConfigError):
            AppConfig.from_dict({"subagent_general_minutes": 0})

    def test_unknown_reasoning_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            AppConfig.from_dict({"review_reasoning": "turbo"})


class ReviewProviderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_review_uses_the_lowest_effort_of_the_task_model(self) -> None:
        provider = EffortRecordingProvider(
            [LLMResponse(tool_calls=[WRITE_CALL]), LLMResponse(text=ALLOW), LLMResponse(text="写好了")]
        )
        harness = build_harness(
            self.directories,
            [],
            mode=ApprovalMode.AI_AUTO,
            provider=provider,
            reasoning_effort="max",
            reasoning_choices=["none", "low", "high", "max"],
        )

        await harness.agent.run_turn("新建 out.txt")

        self.assertEqual(provider.requested_efforts, ["none"])
        self.assertEqual((self.directories.workspace / "out.txt").read_text(encoding="utf-8"), "hi")

    async def test_inherit_keeps_the_task_effort(self) -> None:
        provider = EffortRecordingProvider(
            [LLMResponse(tool_calls=[WRITE_CALL]), LLMResponse(text=ALLOW), LLMResponse(text="写好了")]
        )
        harness = build_harness(
            self.directories,
            [],
            mode=ApprovalMode.AI_AUTO,
            provider=provider,
            reasoning_effort="max",
            reasoning_choices=["none", "low", "high", "max"],
            review_reasoning=REASONING_INHERIT,
        )

        await harness.agent.run_turn("新建 out.txt")

        self.assertEqual(provider.requested_efforts, [])

    async def test_a_separate_review_model_gets_its_own_provider(self) -> None:
        task_provider = ScriptedProvider([LLMResponse(tool_calls=[WRITE_CALL]), LLMResponse(text="写好了")])
        review_provider = ScriptedProvider([LLMResponse(text=ALLOW)])
        built_for: list[str] = []

        def build_review_provider(settings):
            built_for.append(settings.model)
            return review_provider

        review_profile = ModelProfile(id="glm", name="GLM", model="glm-flash", base_url="https://open.bigmodel.cn/api/paas/v4")
        harness = build_harness(
            self.directories,
            [],
            mode=ApprovalMode.AI_AUTO,
            provider=task_provider,
            provider_builder=build_review_provider,
            review_profile=review_profile,
        )

        await harness.agent.run_turn("新建 out.txt")

        self.assertEqual(built_for, ["glm-flash"])
        self.assertEqual(len(review_provider.received_messages), 1)
        self.assertEqual(len(task_provider.received_messages), 2)
        self.assertEqual((self.directories.workspace / "out.txt").read_text(encoding="utf-8"), "hi")


if __name__ == "__main__":
    unittest.main()
