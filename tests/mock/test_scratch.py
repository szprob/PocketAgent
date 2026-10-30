"""临时任务目录：每次新建不重名，旧任务目录被清掉后能重建。"""

import tempfile
import unittest
from pathlib import Path

from pocketagent.core.scratch import create_scratch_workspace, ensure_scratch_workspace, scratch_root


class ScratchWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.home = Path(self._temporary.name) / "home"
        self.home.mkdir()

    def test_creates_a_fresh_directory_each_time(self) -> None:
        first = create_scratch_workspace(self.home)
        second = create_scratch_workspace(self.home)

        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, scratch_root(self.home))
        self.assertEqual(second.parent, scratch_root(self.home))

    def test_ensure_recreates_a_cleaned_directory(self) -> None:
        workspace = create_scratch_workspace(self.home)
        workspace.rmdir()

        restored = ensure_scratch_workspace(workspace)

        self.assertEqual(restored, workspace)
        self.assertTrue(workspace.is_dir())

    def test_ensure_keeps_existing_files(self) -> None:
        workspace = create_scratch_workspace(self.home)
        (workspace / "note.txt").write_text("keep me", encoding="utf-8")

        ensure_scratch_workspace(workspace)

        self.assertEqual((workspace / "note.txt").read_text(encoding="utf-8"), "keep me")
