"""云同步的设置接口：配置脱敏、保存、测试连接、立即同步。"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from aiohttp.test_utils import AioHTTPTestCase
except ImportError:
    raise unittest.SkipTest("没装 aiohttp，跳过 web 测试")

from pocketagent.config import AppConfig, Settings
from pocketagent.server.app import create_app
from pocketagent.sync.config import SyncConfig, sync_config_path

TOKEN = "test-token"
AUTH = {"token": TOKEN}


class SyncSettingsApiTest(AioHTTPTestCase):
    async def get_application(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        root = Path(self._temporary.name)
        self.home = root / "home"
        self.home.mkdir()
        self.remote = root / "remote"
        self.remote.mkdir()
        settings = Settings(workspace=root, home_dir=self.home)
        return create_app(settings, token=TOKEN, app_config=AppConfig())

    async def _get_sync(self):
        response = await self.client.get("/api/settings/sync", params=AUTH)
        return await response.json()

    async def _put_sync(self, payload):
        return await self.client.put("/api/settings/sync", params=AUTH, json=payload)

    def _webdav_form(self, **overrides):
        payload = {
            "kind": "webdav",
            "url": "https://dav.example/dav/pocketagent",
            "username": "me@example.com",
            "password": "app-password",
            "includes": ["sessions", "config"],
            "branch": "main",
        }
        payload.update(overrides)
        return payload

    async def test_unconfigured_returns_defaults(self):
        payload = await self._get_sync()

        self.assertFalse(payload["configured"])
        self.assertEqual(payload["includes"], ["sessions", "projects"])
        self.assertNotIn("password", payload["backend"])

    async def test_save_masks_password_and_writes_private_file(self):
        response = await self._put_sync(self._webdav_form())

        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body["ok"])
        sync = body["sync"]
        self.assertTrue(sync["configured"])
        self.assertTrue(sync["has_password"])
        self.assertEqual(sync["password_source"], "file")
        self.assertEqual(sync["includes"], ["sessions", "config"])
        self.assertEqual(sync["backend"]["branch"], "main")
        self.assertNotIn("password", sync["backend"])
        self.assertNotIn("app-password", json.dumps(sync))

        saved = SyncConfig.load(self.home)
        self.assertEqual(saved.backend.password, "app-password")
        config_data = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        entry = config_data["sync"]["devices"][saved.device_id]
        self.assertNotIn("password", entry["backend"])
        identity = sync_config_path(self.home)
        self.assertEqual(json.loads(identity.read_text(encoding="utf-8"))["password"], "app-password")
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(identity.stat().st_mode), 0o600)

    async def test_blank_password_keeps_saved_one(self):
        await self._put_sync(self._webdav_form())
        response = await self._put_sync(self._webdav_form(password="", includes=["sessions"]))

        body = await response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["sync"]["has_password"])
        self.assertEqual(SyncConfig.load(self.home).backend.password, "app-password")

    async def test_switching_backend_does_not_reuse_password(self):
        await self._put_sync(self._webdav_form())
        response = await self._put_sync({"kind": "github", "url": "me/repo", "password": "", "includes": ["sessions"]})

        body = await response.json()
        self.assertTrue(body["ok"], body)
        self.assertFalse(body["sync"]["has_password"])
        self.assertEqual(body["sync"]["backend"]["kind"], "github")

    async def test_empty_scope_is_rejected(self):
        response = await self._put_sync(self._webdav_form(includes=[]))

        self.assertEqual(response.status, 400)
        self.assertIn("至少要选一项", (await response.json())["error"])

    async def test_other_device_entries_are_kept(self):
        # 另一台机器已经同步下来的条目，不能被这台机器的保存冲掉
        config_data = {"sync": {"devices": {"other-machine": {"includes": ["sessions"]}}}}
        (self.home / "config.json").write_text(json.dumps(config_data), encoding="utf-8")

        response = await self._put_sync(self._webdav_form())

        self.assertEqual(response.status, 200)
        saved = json.loads((self.home / "config.json").read_text(encoding="utf-8"))
        self.assertIn("other-machine", saved["sync"]["devices"])
        self.assertEqual(len(saved["sync"]["devices"]), 2)

    async def test_local_backend_gets_default_directory(self):
        response = await self._put_sync({"kind": "local", "url": "", "includes": ["sessions"]})

        body = await response.json()
        self.assertTrue(body["ok"], body)
        self.assertTrue(body["sync"]["backend"]["url"])

    async def test_missing_url_is_rejected(self):
        response = await self._put_sync(self._webdav_form(url=""))

        self.assertEqual(response.status, 400)
        body = await response.json()
        self.assertFalse(body["ok"])
        self.assertIn("url", body["error"])

    async def test_test_endpoint_reports_missing_local_directory(self):
        missing = self.remote / "not-created"
        response = await self.client.post(
            "/api/settings/sync/test",
            params=AUTH,
            json={"kind": "local", "url": str(missing)},
        )
        body = await response.json()

        self.assertFalse(body["ok"])
        self.assertIn("不存在", body["error"])

    async def test_test_endpoint_accepts_existing_local_directory(self):
        response = await self.client.post(
            "/api/settings/sync/test",
            params=AUTH,
            json={"kind": "local", "url": str(self.remote)},
        )
        body = await response.json()

        self.assertTrue(body["ok"], body)

    async def test_run_uploads_and_reports_counts(self):
        sessions = self.home / "sessions"
        sessions.mkdir()
        (sessions / "s1.json").write_text(
            json.dumps({"id": "s1", "workspace": "/w", "ephemeral": False, "messages": [], "turns": 0}),
            encoding="utf-8",
        )
        await self._put_sync({"kind": "local", "url": str(self.remote), "includes": ["sessions"]})

        response = await self.client.post("/api/settings/sync/run", params=AUTH)
        body = await response.json()

        self.assertTrue(body["ok"], body)
        self.assertEqual(body["counts"]["upload"], 1)
        self.assertTrue((self.remote / "data" / "sessions" / "s1.json").is_file())
        payload = await self._get_sync()
        self.assertTrue(payload["last_synced_at"])

    async def test_run_without_config_is_rejected(self):
        response = await self.client.post("/api/settings/sync/run", params=AUTH)

        self.assertEqual(response.status, 400)
        self.assertIn("sync init", (await response.json())["error"])

    async def test_environment_password_is_reported_as_source(self):
        await self._put_sync(self._webdav_form())
        with mock.patch.dict(os.environ, {"POCKETAGENT_SYNC_PASSWORD": "from-env"}):
            payload = await self._get_sync()

        self.assertEqual(payload["password_source"], "env")


if __name__ == "__main__":
    unittest.main()
