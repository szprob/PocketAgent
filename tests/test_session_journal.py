"""会话按 step 持久化：进行中日志、压缩 reset、全量保存后的去重。"""

import json
import unittest
from pathlib import Path

from pocketagent.agent import INTERRUPTED_TOOL_RESULT
from pocketagent.llm import LLMResponse
from pocketagent.session import Session, SessionJournal, SessionStore
from tests.helpers import TemporaryDirectories, build_harness, roles


def message(role: str, text: str) -> dict:
    return {"role": role, "content": text}


class SessionJournalTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.store = SessionStore(self.directories.home_dir / "sessions")
        self.session = Session(id="20260101-000000-abcdef", workspace=str(self.directories.workspace))

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_appended_messages_are_replayed(self) -> None:
        self.session.messages = [message("system", "s"), message("user", "q")]
        self.store.save(self.session)
        journal = SessionJournal(self.store, self.session)

        await journal.record([*self.session.messages, message("assistant", "a")], 0)
        loaded = self.store.load(self.session.id)

        self.assertEqual(roles(loaded.messages), ["system", "user", "assistant"])
        self.assertEqual(loaded.messages[-1]["content"], "a")

    async def test_reset_replaces_history(self) -> None:
        self.session.messages = [message("system", "s"), message("user", "q"), message("assistant", "a")]
        self.store.save(self.session)
        journal = SessionJournal(self.store, self.session)
        compacted = [message("system", "s"), message("user", "摘要")]

        await journal.record(compacted, 1)
        loaded = self.store.load(self.session.id)

        self.assertEqual(loaded.messages, compacted)

    async def test_full_save_before_replay_does_not_duplicate(self) -> None:
        self.session.messages = [message("system", "s"), message("user", "q")]
        self.store.save(self.session)
        journal = SessionJournal(self.store, self.session)
        with_assistant = [*self.session.messages, message("assistant", "a")]
        await journal.record(with_assistant, 0)

        # 模拟别处做了一次全量保存（比如恢复检查点），日志没清
        self.session.messages = with_assistant
        self.store.save(self.session)
        await journal.record([*with_assistant, message("tool", "t")], 0)

        loaded = self.store.load(self.session.id)

        self.assertEqual(roles(loaded.messages), ["system", "user", "assistant", "tool"])
        self.assertEqual([item["content"] for item in loaded.messages].count("a"), 1)

    async def test_torn_last_line_is_ignored(self) -> None:
        self.session.messages = [message("system", "s"), message("user", "q")]
        self.store.save(self.session)
        path = self.store.journal_path_for(self.session.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        valid = {"type": "append", "start": 2, "messages": [message("assistant", "a")]}
        path.write_text(json.dumps(valid) + "\n" + '{"type": "append", "start": 3, "mess', encoding="utf-8")

        loaded = self.store.load(self.session.id)

        self.assertEqual(roles(loaded.messages), ["system", "user", "assistant"])

    async def test_recover_false_skips_the_journal(self) -> None:
        self.session.messages = [message("system", "s"), message("user", "q")]
        self.store.save(self.session)
        journal = SessionJournal(self.store, self.session)
        await journal.record([*self.session.messages, message("assistant", "a")], 0)

        loaded = self.store.load(self.session.id, recover=False)

        self.assertEqual(roles(loaded.messages), ["system", "user"])

    async def test_delete_removes_the_journal(self) -> None:
        self.session.messages = [message("system", "s")]
        self.store.save(self.session)
        journal = SessionJournal(self.store, self.session)
        await journal.record([message("system", "s"), message("user", "q")], 0)

        self.store.delete(self.session.id)

        self.assertFalse(self.store.path_for(self.session.id).exists())
        self.assertFalse(self.store.journal_path_for(self.session.id).exists())

    async def test_full_save_removes_the_journal(self) -> None:
        self.session.messages = [message("system", "s"), message("user", "q")]
        self.store.save(self.session)
        journal = SessionJournal(self.store, self.session)
        updated = [*self.session.messages, message("assistant", "a")]
        await journal.record(updated, 0)
        self.assertTrue(self.store.journal_path_for(self.session.id).exists())

        self.session.messages = updated
        self.store.save(self.session)

        self.assertFalse(self.store.journal_path_for(self.session.id).exists())


class SessionJournalIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()

    def tearDown(self) -> None:
        self.directories.cleanup()

    async def test_successful_turn_clears_the_journal(self) -> None:
        harness = build_harness(self.directories, [LLMResponse(text="好了")])

        await harness.agent.run_turn("做点事")

        journal_path = harness.agent.session_store.journal_path_for(harness.agent.session.id)
        self.assertFalse(journal_path.exists())
        session_path = harness.agent.session_store.path_for(harness.agent.session.id)
        self.assertTrue(session_path.is_file())

    async def test_resume_repairs_a_crashed_tool_call(self) -> None:
        harness = build_harness(self.directories, [LLMResponse(text="继续了")])
        session = harness.agent.session
        session.messages = [
            message("system", "s"),
            message("user", "跑一下"),
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_crashed",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "echo hi"}'},
                    }
                ],
            },
        ]
        harness.agent.session_store.save(session)

        await harness.agent.run_turn("继续")

        tool_messages = [item for item in harness.agent.context.messages if item["role"] == "tool"]
        self.assertEqual(tool_messages[0]["tool_call_id"], "call_crashed")
        self.assertEqual(tool_messages[0]["content"], INTERRUPTED_TOOL_RESULT)
