"""新功能模块的单元测试：文件变更跟踪、附件、AI 审批结果解析、用量统计、自动化计划。"""

import base64
import unittest
from datetime import date, datetime
from pathlib import Path

from pocketagent.attachments import AttachmentStore, build_user_message, materialize_messages, message_display
from pocketagent.automations import Automation, AutomationStore
from pocketagent.changes import ChangeTracker
from pocketagent.config import ConfigError
from pocketagent.reviewer import parse_review, recent_user_requests
from pocketagent.usage import UsageLog, UsageMetadata
from tests.helpers import TemporaryDirectories

# 1×1 的透明 PNG
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class ChangeTrackerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.workspace = self.directories.workspace
        self.tracker = ChangeTracker(self.directories.home_dir / "checkpoints" / "s1", self.workspace)

    def tearDown(self) -> None:
        self.directories.cleanup()

    def write(self, name: str, content: str) -> Path:
        path = self.workspace / name
        self.tracker.remember_original(path)
        path.write_text(content, encoding="utf-8")
        return path

    def test_added_modified_and_unchanged_files(self) -> None:
        (self.workspace / "old.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        self.write("old.py", "a = 1\nb = 3\nc = 4\n")
        self.write("new.py", "print('hi')\n")
        (self.workspace / "same.py").write_text("x\n", encoding="utf-8")
        self.write("same.py", "x\n")

        changes = {change.display_path: change for change in self.tracker.list_changes()}

        self.assertEqual(set(changes), {"old.py", "new.py"})
        self.assertEqual(changes["old.py"].status, "modified")
        self.assertEqual((changes["old.py"].additions, changes["old.py"].deletions), (2, 1))
        self.assertEqual(changes["new.py"].status, "added")
        self.assertIn("+c = 4", self.tracker.diff(str(self.workspace / "old.py")))

    def test_only_first_original_is_kept_and_revert_restores_it(self) -> None:
        (self.workspace / "f.txt").write_text("original\n", encoding="utf-8")
        path = self.write("f.txt", "first edit\n")
        self.write("f.txt", "second edit\n")
        new_path = self.write("created.txt", "new\n")

        self.tracker.revert(str(path))
        self.tracker.revert(str(new_path))

        self.assertEqual(path.read_text(encoding="utf-8"), "original\n")
        self.assertFalse(new_path.exists())
        self.assertEqual(self.tracker.list_changes(), [])

    def test_index_survives_reload(self) -> None:
        self.write("a.txt", "x\n")

        reloaded = ChangeTracker(self.directories.home_dir / "checkpoints" / "s1", self.workspace)

        self.assertEqual([change.display_path for change in reloaded.list_changes()], ["a.txt"])


class AttachmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.root = self.directories.home_dir / "attachments"
        self.store = AttachmentStore(self.root, "s1")

    def tearDown(self) -> None:
        self.directories.cleanup()

    def test_kinds_and_safe_names(self) -> None:
        image = self.store.save("截图.png", TINY_PNG)
        text = self.store.save("../../etc/notes.md", "# 标题\n".encode("utf-8"))
        binary = self.store.save("data.bin", bytes(range(256)))

        self.assertEqual([image.kind, text.kind, binary.kind], ["image", "text", "binary"])
        self.assertEqual(text.name, "notes.md")
        self.assertTrue(Path(text.path).parent == self.root / "s1")
        with self.assertRaises(KeyError):
            self.store.get("../s2/x")

    def test_message_keeps_reference_and_materializes_for_model(self) -> None:
        image = self.store.save("a.png", TINY_PNG)
        text = self.store.save("readme.txt", b"hello file")
        message = build_user_message("看看这些", [image, text], self.store)

        for_vision = materialize_messages([message], self.root, supports_images=True)[0]
        for_text_only = materialize_messages([message], self.root, supports_images=False)[0]

        self.assertNotIn("_display", for_vision)
        self.assertEqual(for_vision["content"][0]["text"], "看看这些")
        self.assertTrue(for_vision["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertNotIn("_name", for_vision["content"][1])
        self.assertIn("你能直接看到", for_vision["content"][2]["text"])
        self.assertIn("hello file", for_vision["content"][3]["text"])
        self.assertEqual([part["type"] for part in for_text_only["content"]], ["text", "text", "text"])
        self.assertIn("不能看图", for_text_only["content"][1]["text"])
        self.assertEqual(message_display(message)["text"], "看看这些")
        self.assertEqual([item["name"] for item in message_display(message)["attachments"]], ["a.png", "readme.txt"])

    def test_text_only_attachments_give_plain_string_content(self) -> None:
        text = self.store.save("x.py", b"print(1)")

        message = build_user_message("", [text], self.store)

        self.assertIsInstance(message["content"], str)
        self.assertIn("print(1)", message["content"])


class ReviewerTest(unittest.TestCase):
    def test_parse_review(self) -> None:
        allowed = parse_review('好的。{"allow": true, "reason": "改项目文件"}')
        refused = parse_review('{"allow": false, "reason": "要删目录"}')
        garbage = parse_review("我觉得可以")

        self.assertTrue(allowed.allow)
        self.assertEqual(allowed.reason, "改项目文件")
        self.assertFalse(refused.allow)
        self.assertFalse(garbage.allow)

    def test_recent_user_requests_skips_tool_and_assistant(self) -> None:
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "好"},
            {"role": "user", "content": "第二句"},
            {"role": "tool", "tool_call_id": "c", "content": "结果"},
        ]

        self.assertEqual(recent_user_requests(messages), "第一句\n---\n第二句")


class UsageLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.log = UsageLog(self.directories.home_dir / "usage.jsonl")

    def tearDown(self) -> None:
        self.directories.cleanup()

    def test_summary_by_period_model_and_day(self) -> None:
        ds = UsageMetadata(model="deepseek-v4.1-flash", model_name="DS", session_id="s1")
        glm = UsageMetadata(model="glm-5.3-flash", model_name="GLM", session_id="s2")
        self.log.record(ds, 100, 20, estimated=False)
        self.log.record(ds, 50, 10, estimated=True)
        self.log.record(glm, 1000, 0, estimated=False)

        summary = self.log.summarize(today=date.today())
        today = summary["periods"][0]

        self.assertEqual(today["label"], "今天")
        self.assertEqual(today["total"], 1180)
        self.assertEqual(today["estimated_calls"], 1)
        self.assertEqual([item["model"] for item in summary["by_model"]], ["GLM", "DS"])
        self.assertEqual(len(summary["daily"]), 30)
        self.assertEqual(summary["daily"][-1]["total"], 1180)
        self.assertEqual(self.log.session_total("s1")["total"], 180)


class AutomationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()

    def tearDown(self) -> None:
        self.directories.cleanup()

    def form(self, **overrides) -> dict:
        data = {"name": "日报", "workspace": str(self.directories.workspace), "prompt": "总结今天的提交"}
        data.update(overrides)
        return data

    def test_daily_schedule_on_weekdays(self) -> None:
        automation = Automation.from_form(self.form(schedule_kind="daily", daily_time="09:30", weekdays=[0, 2]))
        # 2026-10-21 是周三
        automation.created_at = "2026-10-21T10:00:00"

        self.assertEqual(automation.describe_schedule(), "周一、周三 09:30")
        self.assertEqual(automation.next_run_at(), datetime(2026, 10, 26, 9, 30))
        self.assertTrue(automation.is_due(datetime(2026, 10, 26, 9, 31)))
        self.assertFalse(automation.is_due(datetime(2026, 10, 26, 9, 0)))

    def test_interval_schedule_counts_from_last_run(self) -> None:
        automation = Automation.from_form(self.form(schedule_kind="interval", interval_minutes=120))
        automation.last_run_at = "2026-10-26T08:00:00"

        self.assertEqual(automation.describe_schedule(), "每 2 小时")
        self.assertEqual(automation.next_run_at(), datetime(2026, 10, 26, 10, 0))

    def test_validation(self) -> None:
        invalid_forms = {
            "没提示词": self.form(prompt=""),
            "工作区不存在": self.form(workspace="/no/such/dir"),
            "要人审批的模式": self.form(approval_mode="ask"),
            "间隔太短": self.form(schedule_kind="interval", interval_minutes=1),
            "时间格式": self.form(daily_time="9点"),
        }
        for label, form in invalid_forms.items():
            with self.subTest(label), self.assertRaises(ConfigError):
                Automation.from_form(form)

    def test_store_round_trip_and_edit_keeps_history(self) -> None:
        store = AutomationStore(self.directories.home_dir / "automations.json")
        automation = Automation.from_form(self.form())
        automation.last_status = "done"
        store.save(automation)

        edited = Automation.from_form(self.form(name="周报"), existing=store.get(automation.id))
        store.save(edited)

        self.assertEqual([item.name for item in store.list_all()], ["周报"])
        self.assertEqual(store.get(automation.id).last_status, "done")


if __name__ == "__main__":
    unittest.main()
