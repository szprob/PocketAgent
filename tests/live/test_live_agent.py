"""真实模型 + agent 主循环的联调：读文件、写文件、跑 bash、联网读网页，顺带检查会话 / trace / 用量落盘。

用 `make test-live` 跑；默认用配置里的免费模型。没有可用模型时整组跳过。
"""

import json
import unittest

from pocketagent.core.events import EventType
from tests.helpers import TemporaryDirectories
from tests.live.support import build_live_agent, skip_without_model


@skip_without_model
class LiveAgentLoopTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.harness = build_live_agent(self.directories)

    async def asyncTearDown(self) -> None:
        await self.harness.agent.close()
        self.directories.cleanup()

    def tool_names(self) -> list[str]:
        return [event.data["name"] for event in self.harness.recorder.of_type(EventType.TOOL_CALL)]

    async def test_read_file_and_persist_session_trace_usage(self) -> None:
        (self.directories.workspace / "notes.txt").write_text("随机编号：MAGIC-7281\n", encoding="utf-8")

        final_text = await self.harness.agent.run_turn(
            "请用 read_file 工具读取工作目录下的 notes.txt，然后原样告诉我文件里的随机编号，一个字符都不要改。"
        )

        self.assertIn("MAGIC-7281", final_text)
        self.assertIn("read_file", self.tool_names())
        self.assertTrue(self.harness.recorder.of_type(EventType.TEXT_DELTA), "没有收到流式文本事件")

        await self.harness.agent.close()
        session_id = self.harness.agent.session.id
        session_file = self.harness.settings.sessions_dir / f"{session_id}.json"
        self.assertTrue(session_file.is_file(), "会话没有落盘")
        saved_messages = json.loads(session_file.read_text(encoding="utf-8"))["messages"]
        self.assertTrue(any(message.get("role") == "tool" for message in saved_messages), "工具结果没进会话")

        trace_file = self.harness.settings.traces_dir / f"{session_id}.jsonl"
        self.assertTrue(trace_file.is_file(), "trace 没有落盘")
        trace_types = [json.loads(line)["type"] for line in trace_file.read_text(encoding="utf-8").splitlines()]
        self.assertIn(EventType.TURN_END.value, trace_types)
        self.assertIn(EventType.TOOL_RESULT.value, trace_types)

        self.assertTrue(self.harness.settings.usage_log_path.is_file(), "用量没有落盘")
        usage_entries = [
            json.loads(line)
            for line in self.harness.settings.usage_log_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(usage_entries)
        self.assertGreater(usage_entries[-1]["prompt_tokens"], 0)

    async def test_write_file_then_bash_reads_it_back(self) -> None:
        final_text = await self.harness.agent.run_turn(
            "请用 write_file 工具在工作目录创建 live.txt，内容正好是 live-ok；"
            "然后用 bash 执行 cat live.txt 把内容读回来。"
        )

        target = self.directories.workspace / "live.txt"
        self.assertTrue(target.is_file(), "模型没有把文件写出来")
        self.assertEqual(target.read_text(encoding="utf-8").strip(), "live-ok")
        names = self.tool_names()
        self.assertIn("write_file", names)
        self.assertIn("bash", names)
        self.assertIn("live-ok", final_text)

    async def test_web_fetch_inside_agent_loop(self) -> None:
        final_text = await self.harness.agent.run_turn(
            "请用 web_fetch 工具打开 https://example.com ，然后告诉我页面标题是什么。"
        )

        self.assertIn("web_fetch", self.tool_names())
        self.assertIn("Example Domain", final_text)
