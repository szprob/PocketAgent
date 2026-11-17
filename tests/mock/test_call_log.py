"""模型调用原文（evaluation/call_log.py）：每次调用的请求体和回复原样留下，增量存、还原后一字不差。"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pocketagent.cli.evaluation import run_export_calls_command
from pocketagent.evaluation.call_log import (
    CallLog,
    CallMetadata,
    CallRecordingProvider,
    export_calls,
    read_calls,
    wire_request,
)
from pocketagent.llm import LLMResponse, ScriptedProvider, SilentListener, Usage
from pocketagent.llm.anthropic import AnthropicProvider
from pocketagent.llm.openai_compat import OpenAICompatibleProvider
from tests.helpers import TemporaryDirectories, build_harness, tool_call


class RecordedAgentRunTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_every_call_is_kept_verbatim_and_stored_as_a_delta(self) -> None:
        (self.directories.workspace / "a.txt").write_text("hi\n", encoding="utf-8")
        harness = build_harness(
            self.directories,
            [
                LLMResponse(reasoning="先读文件", tool_calls=[tool_call("c1", "read_file", '{"path": "a.txt"}')]),
                LLMResponse(text="文件里是 hi", usage=Usage(prompt_tokens=50, completion_tokens=5)),
            ],
        )

        await harness.agent.run_turn("a.txt 里写了什么")

        path = harness.settings.calls_dir / f"{harness.agent.session.id}.jsonl"
        raw = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        calls = list(read_calls(path))
        self.assertEqual(len(calls), 2)
        # 还原出来的请求就是模型当时收到的，一个字不差
        for index, call in enumerate(calls):
            self.assertEqual(call["request"]["messages"], harness.provider.received_messages[index])
            self.assertEqual(call["request"]["tools"], harness.provider.received_tools[index])
        # 第二次只存新增的消息（assistant 调用 + 工具结果），工具定义写「同第 0 次」
        self.assertEqual((raw[1]["messages"]["base"], raw[1]["messages"]["keep"]), (0, len(calls[0]["request"]["messages"])))
        self.assertEqual([message["role"] for message in raw[1]["messages"]["append"]], ["assistant", "tool"])
        self.assertEqual(raw[1]["tools"], {"same_as": 0})
        # 回复原样：思考、工具调用的原始参数、用量
        self.assertEqual(calls[0]["response"]["reasoning"], "先读文件")
        self.assertEqual(calls[0]["response"]["tool_calls"][0]["arguments"], '{"path": "a.txt"}')
        self.assertEqual(calls[1]["response"]["usage"]["prompt_tokens"], 50)
        self.assertEqual(calls[1]["meta"]["session_id"], harness.agent.session.id)

    async def test_turned_off_writes_nothing(self) -> None:
        harness = build_harness(self.directories, [LLMResponse(text="好")], record_model_calls=False)

        await harness.agent.run_turn("你好")

        self.assertFalse((harness.settings.calls_dir / f"{harness.agent.session.id}.jsonl").exists())


class WireRequestTest(unittest.TestCase):
    """记的是 provider 真正发给接口的请求体，不是 PocketAgent 交给它的消息。"""

    MESSAGES = [
        {"role": "system", "content": "系统"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "在", "reasoning_content": "想", "thinking_blocks": [{"position": 0, "block": {"type": "thinking", "thinking": "嗯", "signature": "s"}}]},
        {"role": "user", "content": "再说一句"},
    ]

    def test_openai_compatible_body_with_extra_body_flattened(self) -> None:
        provider = OpenAICompatibleProvider(
            model="x-ai/grok", base_url="https://openrouter.ai/api/v1", api_key="k",
            reasoning_effort="low", reasoning_style="openrouter", extra_body={"top_k": 20},
        )

        body = wire_request(provider, self.MESSAGES, [])

        self.assertEqual(body["model"], "x-ai/grok")
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertEqual(body["top_k"], 20)
        self.assertNotIn("extra_body", body)
        # OpenRouter 认 reasoning_content；Anthropic 的思考块不发给它
        self.assertEqual(body["messages"][2]["reasoning_content"], "想")
        self.assertNotIn("thinking_blocks", body["messages"][2])

    def test_anthropic_body_uses_its_own_format(self) -> None:
        provider = AnthropicProvider(model="claude-opus-4-6", base_url=None, api_key="k", reasoning_effort="high")

        body = wire_request(provider, self.MESSAGES, [])

        self.assertEqual(body["system"], "系统")
        self.assertEqual(body["thinking"]["type"], "adaptive")
        self.assertEqual(body["messages"][1]["content"][0], {"type": "thinking", "thinking": "嗯", "signature": "s"})


class CallLogFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls_dir = Path(self.tmp.name) / "calls"
        self.metadata = CallMetadata(session_id="s1", model="m")

    def record(self, log: CallLog, messages: list[dict]) -> None:
        log.record({"model": "m", "messages": messages, "tools": [{"name": "t"}]}, LLMResponse(text="好"), self.metadata)

    def test_interleaved_streams_and_restart_still_restore_exactly(self) -> None:
        path = self.calls_dir / "s1.jsonl"
        main = [{"role": "system", "content": "主"}, {"role": "user", "content": "问"}]
        child = [{"role": "system", "content": "子 agent"}, {"role": "user", "content": "查"}]
        log = CallLog(path)
        self.record(log, main)
        self.record(log, child)
        self.record(log, main + [{"role": "assistant", "content": "答"}, {"role": "user", "content": "再问"}])
        # 进程重启：新实例接着编号，第一条存完整的消息
        restarted = CallLog(path)
        self.record(restarted, main + [{"role": "assistant", "content": "答"}])

        raw = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        calls = list(read_calls(path))

        self.assertEqual([call["id"] for call in calls], [0, 1, 2, 3])
        # 主 agent 的第二次以第 0 次（不是紧挨着的子 agent 那次）为底子
        self.assertEqual((raw[2]["messages"]["base"], raw[2]["messages"]["keep"]), (0, 2))
        self.assertIsNone(raw[3]["messages"]["base"])
        self.assertEqual(calls[2]["request"]["messages"][-1], {"role": "user", "content": "再问"})
        self.assertEqual(calls[1]["request"]["messages"], child)
        self.assertEqual(calls[3]["request"]["tools"], [{"name": "t"}])

    def test_export_all_or_some_sessions_and_the_cli(self) -> None:
        for session_id in ("s1", "s2"):
            log = CallLog(self.calls_dir / f"{session_id}.jsonl")
            self.record(log, [{"role": "user", "content": session_id}])
        output = Path(self.tmp.name) / "out.jsonl"

        self.assertEqual(export_calls(self.calls_dir, output), 2)
        self.assertEqual(export_calls(self.calls_dir, output, ["s2"]), 1)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["request"]["messages"], [{"role": "user", "content": "s2"}])

        cli_output = Path(self.tmp.name) / "cli.jsonl"
        with mock.patch.dict(os.environ, {"POCKETAGENT_HOME": self.tmp.name}):
            self.assertEqual(run_export_calls_command(["-o", str(cli_output)]), 0)
        self.assertEqual(len(cli_output.read_text(encoding="utf-8").splitlines()), 2)

    def test_failed_calls_are_kept_too(self) -> None:
        """请求发出去了、重试完还是失败：原文里也要有这一条（轨迹上看得到卡在哪），错误照常往上抛。"""

        class Failing:
            async def complete(self, messages, tools, listener):
                raise RuntimeError("HTTP 500")

        path = Path(self.tmp.name) / "calls" / "s1.jsonl"
        provider = CallRecordingProvider(Failing(), CallLog(path), self.metadata, enabled=lambda: True)

        with self.assertRaises(RuntimeError):
            asyncio.run(provider.complete([{"role": "user", "content": "hi"}], [], SilentListener()))

        call = list(read_calls(path))[0]
        self.assertIsNone(call["response"])
        self.assertEqual(call["error"], "RuntimeError: HTTP 500")
        self.assertEqual(call["request"]["messages"], [{"role": "user", "content": "hi"}])

    def test_recording_failure_never_breaks_the_call(self) -> None:
        log = CallLog(Path(self.tmp.name) / "calls" / "s1.jsonl")
        provider = CallRecordingProvider(ScriptedProvider([LLMResponse(text="照常回复")]), log, self.metadata, enabled=lambda: True)

        with mock.patch.object(CallLog, "record", side_effect=OSError("磁盘满了")):
            response = asyncio.run(provider.complete([{"role": "user", "content": "hi"}], [], SilentListener()))

        self.assertEqual(response.text, "照常回复")
