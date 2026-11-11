"""云同步：两份 ~/.pocketagent 通过一个共享目录对齐。

用 LocalDirectoryBackend（就是一个目录）代替真实网盘，不联网；WebDAV 的解析用 httpx 的
MockTransport 喂固定的 PROPFIND XML。
"""

import asyncio
import json
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import httpx

from pocketagent.sync.backend import GitHubBackend, LocalDirectoryBackend, SyncError, WebDavBackend
from pocketagent.sync.config import (
    BackendSettings,
    SyncConfig,
    SyncConfigError,
    default_local_directory,
    sync_config_path,
)
from pocketagent.sync.engine import SyncEngine
from pocketagent.sync.merge import merge_json_object
from pocketagent.sync.service import SyncScheduler, is_sync_configured


def make_session(
    session_id: str = "s1",
    contents: tuple[str, ...] = ("你好",),
    updated_at: str = "2026-11-11T10:00:00",
    ephemeral: bool = False,
) -> dict:
    return {
        "id": session_id,
        "workspace": "/Users/me/project",
        "ephemeral": ephemeral,
        "created_at": "2026-11-11T09:00:00",
        "updated_at": updated_at,
        "model_profile": "model-a",
        "approval_mode": "ask",
        "agent_mode": "agent",
        "title": "一个任务",
        "source": None,
        "worktree": None,
        "worktree_fallback": None,
        "checkpoints": [{"commit": "deadbeef", "label": "任务开始时"}],
        "turns": len(contents),
        "tool_calls": 1,
        "todos": [],
        "messages": [
            {"role": "user", "content": text, "_id": f"id-{text}"} for text in contents
        ],
    }


class SyncFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        self.home_a = self.root / "device-a"
        self.home_b = self.root / "device-b"
        self.home_a.mkdir()
        self.home_b.mkdir()

    def config(self, device_id: str, includes: tuple[str, ...] = ("sessions", "projects")) -> SyncConfig:
        return SyncConfig(
            device_id=device_id,
            backend=BackendSettings(kind="local", url=str(self.remote)),
            includes=list(includes),
        )

    def engine(self, home: Path, device_id: str, includes: tuple[str, ...] = ("sessions", "projects")) -> SyncEngine:
        return SyncEngine(home, self.config(device_id, includes))

    def write_session(self, home: Path, session: dict, journal: bool = False) -> Path:
        directory = home / "sessions"
        if not directory.is_dir():
            directory.mkdir()
        path = directory / f"{session['id']}.json"
        path.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
        if journal:
            (directory / f"{session['id']}.messages.jsonl").write_text('{"type": "append"}\n', encoding="utf-8")
        return path

    def read_session(self, home: Path, session_id: str = "s1") -> dict:
        return json.loads((home / "sessions" / f"{session_id}.json").read_text(encoding="utf-8"))

    def append_message(self, home: Path, text: str, updated_at: str, session_id: str = "s1") -> None:
        session = self.read_session(home, session_id)
        session["messages"].append({"role": "user", "content": text, "_id": f"id-{text}"})
        session["updated_at"] = updated_at
        session["turns"] = session.get("turns", 0) + 1
        (home / "sessions" / f"{session_id}.json").write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")


class FirstSyncTest(SyncFixture):
    def test_first_sync_uploads_then_second_device_downloads(self) -> None:
        self.write_session(self.home_a, make_session())

        report = self.engine(self.home_a, "device-a").run()

        self.assertEqual(report.count("upload"), 1)
        self.assertTrue((self.remote / "data" / "sessions" / "s1.json").is_file())
        self.assertTrue((self.remote / "devices" / "device-a.json").is_file())

        report_b = self.engine(self.home_b, "device-b").run()

        self.assertEqual(report_b.count("download"), 1)
        downloaded = self.read_session(self.home_b)
        self.assertEqual([message["content"] for message in downloaded["messages"]], ["你好"])
        # 检查点指向本机的影子 git，不能跟着会话跑过去
        self.assertIsNone(downloaded["checkpoints"])

    def test_ephemeral_and_journal_files_are_not_uploaded(self) -> None:
        self.write_session(self.home_a, make_session(ephemeral=True), journal=True)
        self.write_session(self.home_a, make_session(session_id="s2"), journal=True)

        self.engine(self.home_a, "device-a").run()

        names = sorted(path.name for path in (self.remote / "data" / "sessions").iterdir())
        self.assertEqual(names, ["s2.json"])

    def test_excluded_directories_stay_local(self) -> None:
        self.write_session(self.home_a, make_session())
        for name in ("shadow", "traces", "tool_outputs", "attachments"):
            directory = self.home_a / name
            directory.mkdir()
            (directory / "x.json").write_text("{}", encoding="utf-8")
        (self.home_a / "usage.jsonl").write_text("{}\n", encoding="utf-8")
        (self.home_a / "config.json").write_text("{}", encoding="utf-8")

        self.engine(self.home_a, "device-a").run()

        uploaded = sorted(
            path.relative_to(self.remote / "data").as_posix()
            for path in (self.remote / "data").rglob("*")
            if path.is_file()
        )
        self.assertEqual(uploaded, ["sessions/s1.json"])

    def test_config_needs_explicit_include_and_loses_web_token(self) -> None:
        (self.home_a / "config.json").write_text(
            json.dumps({"models": [{"id": "m", "api_key": "sk-secret"}], "web_token": "local-token"}),
            encoding="utf-8",
        )

        self.engine(self.home_a, "device-a").run()
        self.assertFalse((self.remote / "data" / "config.json").exists())

        self.engine(self.home_a, "device-a", ("sessions", "config")).run()
        uploaded = json.loads((self.remote / "data" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(uploaded["models"][0]["api_key"], "sk-secret")
        self.assertIsNone(uploaded["web_token"])

    @unittest.skipIf(sys.platform == "win32", "Windows 只认只读位，可执行位落不下去")
    def test_hook_script_is_executable_after_download(self) -> None:
        hooks = self.home_a / "hooks"
        hooks.mkdir()
        script = hooks / "notify.sh"
        script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        script.chmod(0o755)

        self.engine(self.home_a, "device-a", ("sessions", "hooks")).run()
        self.engine(self.home_b, "device-b", ("sessions", "hooks")).run()

        downloaded = self.home_b / "hooks" / "notify.sh"
        self.assertTrue(stat.S_IMODE(downloaded.stat().st_mode) & stat.S_IXUSR)


class MergeTest(SyncFixture):
    def test_diverged_session_keeps_messages_from_both_sides(self) -> None:
        self.write_session(self.home_a, make_session())
        self.engine(self.home_a, "device-a").run()
        self.engine(self.home_b, "device-b").run()

        self.append_message(self.home_a, "来自 A", "2026-11-11T11:00:00")
        self.append_message(self.home_b, "来自 B", "2026-11-11T11:05:00")

        self.engine(self.home_a, "device-a").run()
        report_b = self.engine(self.home_b, "device-b").run()

        self.assertEqual(report_b.count("merge"), 1)
        contents_b = [message["content"] for message in self.read_session(self.home_b)["messages"]]
        self.assertEqual(contents_b, ["你好", "来自 B", "来自 A"])

        self.engine(self.home_a, "device-a").run()
        contents_a = [message["content"] for message in self.read_session(self.home_a)["messages"]]
        self.assertEqual(contents_a, ["你好", "来自 B", "来自 A"])
        # 合并时统计取两边最大值，不会因为同步变小
        self.assertEqual(self.read_session(self.home_a)["turns"], 2)
        # 本机的检查点指针在合并后还在（远端那份没有，由本地补回来）
        self.assertIsNotNone(self.read_session(self.home_a)["checkpoints"])

    def test_memory_conflict_keeps_local_and_saves_remote_copy(self) -> None:
        memory = self.home_a / "projects" / "p" / "memory" / "MEMORY.md"
        memory.parent.mkdir(parents=True)
        memory.write_text("第一行\n", encoding="utf-8")
        self.engine(self.home_a, "device-a").run()
        self.engine(self.home_b, "device-b").run()

        memory.write_text("第一行\nA 的改动\n", encoding="utf-8")
        (self.home_b / "projects" / "p" / "memory" / "MEMORY.md").write_text("第一行\nB 的改动\n", encoding="utf-8")
        self.engine(self.home_a, "device-a").run()

        report = self.engine(self.home_b, "device-b").run()

        self.assertEqual(report.count("conflict"), 1)
        self.assertEqual(
            (self.home_b / "projects" / "p" / "memory" / "MEMORY.md").read_text(encoding="utf-8"),
            "第一行\nB 的改动\n",
        )
        conflicts = list((self.home_b / "sync" / "conflicts").iterdir())
        self.assertEqual(len(conflicts), 1)
        self.assertIn("A 的改动", conflicts[0].read_text(encoding="utf-8"))


class DeletionTest(SyncFixture):
    def test_local_delete_removes_remote_and_other_device(self) -> None:
        self.write_session(self.home_a, make_session())
        self.engine(self.home_a, "device-a").run()
        self.engine(self.home_b, "device-b").run()

        (self.home_a / "sessions" / "s1.json").unlink()
        report_a = self.engine(self.home_a, "device-a").run()
        report_b = self.engine(self.home_b, "device-b").run()

        self.assertEqual(report_a.count("delete-remote"), 1)
        self.assertEqual(report_b.count("delete-local"), 1)
        self.assertFalse((self.home_b / "sessions" / "s1.json").exists())

    def test_local_delete_resurrects_when_remote_changed(self) -> None:
        self.write_session(self.home_a, make_session())
        self.engine(self.home_a, "device-a").run()
        self.engine(self.home_b, "device-b").run()
        self.append_message(self.home_b, "B 还在改", "2026-11-11T12:00:00")
        self.engine(self.home_b, "device-b").run()

        (self.home_a / "sessions" / "s1.json").unlink()
        report = self.engine(self.home_a, "device-a").run()

        self.assertEqual(report.count("download"), 1)
        self.assertTrue((self.home_a / "sessions" / "s1.json").is_file())

    def test_remote_delete_resurrects_when_local_changed(self) -> None:
        self.write_session(self.home_a, make_session())
        self.engine(self.home_a, "device-a").run()
        self.engine(self.home_b, "device-b").run()
        self.append_message(self.home_a, "A 还在改", "2026-11-11T12:00:00")

        (self.remote / "data" / "sessions" / "s1.json").unlink()
        report = self.engine(self.home_a, "device-a").run()

        self.assertEqual(report.count("upload"), 1)
        self.assertTrue((self.remote / "data" / "sessions" / "s1.json").is_file())


class SafetyTest(SyncFixture):
    def test_status_does_not_touch_state_or_files(self) -> None:
        self.write_session(self.home_a, make_session())
        self.engine(self.home_a, "device-a").run()
        self.append_message(self.home_a, "新的", "2026-11-11T13:00:00")
        state_before = (self.home_a / "sync" / "state.json").read_text(encoding="utf-8")

        report = self.engine(self.home_a, "device-a").status()

        self.assertEqual(report.count("upload"), 1)
        self.assertEqual((self.home_a / "sync" / "state.json").read_text(encoding="utf-8"), state_before)
        remote_copy = json.loads((self.remote / "data" / "sessions" / "s1.json").read_text(encoding="utf-8"))
        self.assertEqual(len(remote_copy["messages"]), 1)

    def test_mass_deletion_in_remote_aborts_before_deleting_local(self) -> None:
        for index in range(5):
            self.write_session(self.home_a, make_session(session_id=f"s{index}", contents=(f"第 {index} 个",)))
        self.engine(self.home_a, "device-a").run()
        shutil.rmtree(self.remote / "data")

        report = self.engine(self.home_a, "device-a").run()

        self.assertTrue(report.errors)
        self.assertIn("先确认地址", report.errors[0])
        self.assertEqual(len(list((self.home_a / "sessions").glob("*.json"))), 5)

    def test_push_refuses_to_overwrite_remote_change(self) -> None:
        self.write_session(self.home_a, make_session())
        self.engine(self.home_a, "device-a").run()
        self.engine(self.home_b, "device-b").run()
        self.append_message(self.home_b, "B 的版本", "2026-11-11T14:00:00")
        self.engine(self.home_b, "device-b").run()
        self.append_message(self.home_a, "A 的版本", "2026-11-11T14:10:00")

        report = self.engine(self.home_a, "device-a").run(direction="push")

        self.assertEqual(report.count("blocked"), 1)
        remote_copy = json.loads((self.remote / "data" / "sessions" / "s1.json").read_text(encoding="utf-8"))
        self.assertEqual([message["content"] for message in remote_copy["messages"]], ["你好", "B 的版本"])


class SchedulerTest(unittest.IsolatedAsyncioTestCase):
    def _configured_home(self, directory: str) -> tuple[Path, Path]:
        home = Path(directory)
        remote = home / "remote"
        remote.mkdir()
        SyncConfig(
            device_id="mac-1",
            backend=BackendSettings(kind="local", url=str(remote)),
            includes=["sessions"],
        ).save(home)
        return home, remote

    async def test_repeated_triggers_coalesce_into_one_sync(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home, _remote = self._configured_home(directory)
            calls: list[Path] = []

            def fake_sync(path: Path) -> str:
                calls.append(path)
                return ""

            with mock.patch("pocketagent.sync.service.sync_once", side_effect=fake_sync):
                scheduler = SyncScheduler(home)
                scheduler.schedule("turn-end", delay=0.05)
                scheduler.schedule("turn-end", delay=0.05)
                await asyncio.sleep(0.25)
                await scheduler.close()

            self.assertEqual(len(calls), 1)

    async def test_no_config_means_no_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            scheduler = SyncScheduler(home)
            scheduler.schedule("turn-end", delay=0.01)
            self.assertIsNone(scheduler._task)
            self.assertFalse(is_sync_configured(home))

    def test_is_configured_after_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home, _remote = self._configured_home(directory)
            self.assertTrue(is_sync_configured(home))

    @unittest.skipIf(sys.platform == "win32", "Windows 上不加文件锁")
    def test_concurrent_run_is_skipped(self) -> None:
        import fcntl

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            remote = home / "remote"
            remote.mkdir()
            config = SyncConfig(
                device_id="mac-1",
                backend=BackendSettings(kind="local", url=str(remote)),
                includes=["sessions"],
            )
            engine = SyncEngine(home, config)
            engine.lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = engine.lock_path.open("a+")
            self.addCleanup(handle.close)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            with self.assertRaises(SyncError) as context:
                engine.run()

            self.assertIn("另一个同步", str(context.exception))


class SyncConfigTest(unittest.TestCase):
    def test_settings_go_to_config_per_device_and_password_stays_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = SyncConfig(
                device_id="mac-123456",
                backend=BackendSettings(kind="webdav", url="https://dav.example/dav/pa", username="me", password="pw"),
                includes=["sessions"],
            )
            config.save(home)

            loaded = SyncConfig.load(home)
            self.assertEqual(loaded.backend.url, "https://dav.example/dav/pa")
            self.assertEqual(loaded.backend.password, "pw")
            self.assertEqual(loaded.device_id, "mac-123456")

            config_data = json.loads((home / "config.json").read_text(encoding="utf-8"))
            entry = config_data["sync"]["devices"]["mac-123456"]
            self.assertEqual(entry["includes"], ["sessions"])
            self.assertNotIn("password", entry["backend"])

            identity_path = sync_config_path(home)
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            self.assertEqual(identity["password"], "pw")
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(identity_path.stat().st_mode), 0o600)

    def test_two_devices_keep_separate_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            common = {"includes": ["sessions"]}
            first = SyncConfig(
                device_id="mac-1",
                backend=BackendSettings(kind="webdav", url="https://a", username="a", password="a"),
                **common,
            )
            first.save(home)
            second = SyncConfig(
                device_id="mac-2",
                backend=BackendSettings(kind="local", url=str(home / "remote"), password=""),
                **common,
            )
            second.save(home)

            config_data = json.loads((home / "config.json").read_text(encoding="utf-8"))
            devices = config_data["sync"]["devices"]
            self.assertEqual(set(devices), {"mac-1", "mac-2"})
            self.assertEqual(devices["mac-2"]["backend"]["kind"], "local")

    def test_unknown_backend_is_rejected(self) -> None:
        with self.assertRaises(SyncConfigError):
            SyncConfig.from_dict({"backend": {"kind": "ftp", "url": "x"}})

    def test_local_backend_ping_requires_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = LocalDirectoryBackend(Path(directory) / "missing")
            with self.assertRaises(SyncError):
                backend.ping()

    def test_missing_file_says_to_run_init(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SyncConfigError) as context:
                SyncConfig.load(Path(directory))
            self.assertIn("sync init", str(context.exception))

    @unittest.skipUnless(sys.platform == "darwin", "默认目录按 macOS 的坚果云 / iCloud 推")
    def test_default_local_directory_prefers_nutstore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "Nutstore Files").mkdir()
            self.assertEqual(default_local_directory(home), home / "Nutstore Files" / "pocketagent")


class ConfigMergeTest(unittest.TestCase):
    def test_sync_devices_merge_per_device_without_conflict(self) -> None:
        def encode(value):
            return json.dumps(value, ensure_ascii=False).encode("utf-8")

        base = {"sync": {"devices": {"mac-1": {"includes": ["sessions"]}}}}
        local = {"sync": {"devices": {"mac-1": {"includes": ["sessions", "config"]}}}}
        remote = {"sync": {"devices": {"mac-2": {"includes": ["projects"]}}}}

        result = merge_json_object(encode(local), encode(remote), encode(base))

        merged = json.loads(result.data.decode("utf-8"))
        self.assertEqual(set(merged["sync"]["devices"]), {"mac-1", "mac-2"})
        self.assertEqual(merged["sync"]["devices"]["mac-1"]["includes"], ["sessions", "config"])
        self.assertEqual(merged["sync"]["devices"]["mac-2"]["includes"], ["projects"])
        self.assertFalse(result.conflict)


class GitHubBackendTest(unittest.TestCase):
    def _client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer t"})

    def test_lists_and_reads_files(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/contents/data"):
                return httpx.Response(
                    200,
                    json=[
                        {"type": "dir", "path": "data/sessions"},
                        {"type": "file", "path": "data/config.json", "sha": "cfg-sha", "size": 12},
                    ],
                )
            if request.url.path.endswith("/contents/data/sessions"):
                return httpx.Response(
                    200,
                    json=[{"type": "file", "path": "data/sessions/s1.json", "sha": "s1-sha", "size": 34}],
                )
            if request.url.path.endswith("/contents/data/config.json"):
                return httpx.Response(200, content=b'{"models": []}')
            return httpx.Response(404)

        backend = GitHubBackend("me/repo", "t", client=self._client(handler))
        entries = backend.list_files("data")

        self.assertEqual([entry.path for entry in entries], ["data/config.json", "data/sessions/s1.json"])
        self.assertEqual(entries[0].etag, "cfg-sha")
        self.assertEqual(backend.read("data/config.json"), b'{"models": []}')

    def test_write_creates_then_updates_with_sha_and_delete(self) -> None:
        puts: list[dict] = []
        deleted: list[dict] = []
        existing = {"sha": ""}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                if not existing["sha"]:
                    return httpx.Response(404)
                return httpx.Response(200, json={"type": "file", "path": "data/x.json", "sha": existing["sha"]})
            if request.method == "PUT":
                body = json.loads(request.content)
                puts.append(body)
                existing["sha"] = "new-sha"
                return httpx.Response(201, json={"content": {"sha": "new-sha"}})
            if request.method == "DELETE":
                deleted.append(json.loads(request.content))
                existing["sha"] = ""
                return httpx.Response(200, json={})
            return httpx.Response(500)

        backend = GitHubBackend("me/repo", "t", branch="dev", client=self._client(handler))

        self.assertEqual(backend.write("data/x.json", b"{}"), "new-sha")
        self.assertNotIn("sha", puts[0])
        self.assertEqual(puts[0]["branch"], "dev")

        self.assertEqual(backend.write("data/x.json", b"{}"), "new-sha")
        self.assertEqual(puts[1]["sha"], "new-sha")

        backend.delete("data/x.json")
        self.assertEqual(deleted[0]["sha"], "new-sha")

    def test_ping_reports_read_only_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"permissions": {"push": False}})

        backend = GitHubBackend("me/repo", "t", client=self._client(handler))
        with self.assertRaises(SyncError) as context:
            backend.ping()
        self.assertIn("只有读权限", str(context.exception))

    def test_bad_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        backend = GitHubBackend("me/repo", "t", client=self._client(handler))
        with self.assertRaises(SyncError) as context:
            backend.ping()
        self.assertIn("Token", str(context.exception))


class WebDavBackendTest(unittest.TestCase):
    ROOT_XML = (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
        "<d:response><d:href>/dav/pa/</d:href><d:propstat><d:prop><d:resourcetype>"
        "<d:collection/></d:resourcetype></d:prop></d:propstat></d:response>"
        "<d:response><d:href>/dav/pa/data/</d:href><d:propstat><d:prop><d:resourcetype>"
        "<d:collection/></d:resourcetype></d:prop></d:propstat></d:response>"
        "</d:multistatus>"
    )
    DATA_XML = (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
        "<d:response><d:href>/dav/pa/data/</d:href><d:propstat><d:prop><d:resourcetype>"
        "<d:collection/></d:resourcetype></d:prop></d:propstat></d:response>"
        "<d:response><d:href>/dav/pa/data/sessions/</d:href><d:propstat><d:prop><d:resourcetype>"
        "<d:collection/></d:resourcetype></d:prop></d:propstat></d:response>"
        '<d:response><d:href>/dav/pa/data/sessions/a.json</d:href><d:propstat><d:prop>'
        "<d:resourcetype/><d:getetag>\"abc\"</d:getetag><d:getcontentlength>12</d:getcontentlength>"
        "</d:prop></d:propstat></d:response></d:multistatus>"
    )

    def test_lists_nested_files_with_etag_and_size(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method != "PROPFIND":
                return httpx.Response(500)
            if request.url.path.rstrip("/").endswith("/data"):
                return httpx.Response(207, text=self.DATA_XML)
            return httpx.Response(207, text=self.ROOT_XML)

        client = httpx.Client(transport=httpx.MockTransport(handler), auth=("me", "pw"))
        backend = WebDavBackend("https://dav.example/dav/pa", "me", "pw", client=client)

        entries = backend.list_files()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].path, "data/sessions/a.json")
        self.assertEqual(entries[0].etag, "abc")
        self.assertEqual(entries[0].size, 12)

    def test_missing_directories_are_created_before_put(self) -> None:
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            if request.method == "MKCOL":
                return httpx.Response(405)
            if request.method == "PUT":
                return httpx.Response(201, headers={"ETag": '"v1"'})
            return httpx.Response(500)

        client = httpx.Client(transport=httpx.MockTransport(handler), auth=("me", "pw"))
        backend = WebDavBackend("https://dav.example/dav/pa", "me", "pw", client=client)

        etag = backend.write("data/sessions/a.json", b"{}")

        self.assertEqual(etag, "v1")
        self.assertEqual(methods, ["MKCOL", "MKCOL", "PUT"])

    def test_put_without_etag_asks_propfind_for_one(self) -> None:
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            if request.method == "MKCOL":
                return httpx.Response(405)
            if request.method == "PUT":
                # 坚果云就是这样：PUT 成功但不回 ETag
                return httpx.Response(201)
            if request.method == "PROPFIND":
                return httpx.Response(
                    207,
                    text=(
                        '<d:multistatus xmlns:d="DAV:"><d:response>'
                        "<d:href>/dav/pa/data/sessions/a.json</d:href><d:propstat><d:prop>"
                        "<d:getetag>opaque-etag</d:getetag></d:prop></d:propstat></d:response>"
                        "</d:multistatus>"
                    ),
                )
            return httpx.Response(500)

        client = httpx.Client(transport=httpx.MockTransport(handler), auth=("me", "pw"))
        backend = WebDavBackend("https://dav.example/dav/pa", "me", "pw", client=client)

        etag = backend.write("data/sessions/a.json", b"{}")

        self.assertEqual(etag, "opaque-etag")
        self.assertEqual(methods, ["MKCOL", "MKCOL", "PUT", "PROPFIND"])

    def test_ping_on_missing_folder_says_to_create_it(self) -> None:
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
        backend = WebDavBackend("https://dav.example/dav/pa", "me", "pw", client=client)

        with self.assertRaises(SyncError) as context:
            backend.ping()

        self.assertIn("先在网盘里建好这个目录", str(context.exception))

    def test_unauthorized_explains_app_password(self) -> None:
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401)))
        backend = WebDavBackend("https://dav.example/dav/pa", "me", "pw", client=client)

        with self.assertRaises(SyncError) as context:
            backend.list_files()

        self.assertIn("第三方应用密码", str(context.exception))


if __name__ == "__main__":
    unittest.main()
