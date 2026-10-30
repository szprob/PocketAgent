import subprocess
import unittest
from pathlib import Path

from pocketagent.config import AppConfig, Settings
from pocketagent.llm import ScriptedProvider
from pocketagent.safety.sandbox import SandboxPolicy, available_sandbox, wrap_command
from pocketagent.server.manager import SessionManager
from pocketagent.server.worktrees import create_worktree
from tests.helpers import TemporaryDirectories


def git(repository: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repository), *arguments], capture_output=True, text=True, check=True)


class WorktreeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.workspace = self.directories.workspace
        git(self.workspace, "init", "-q")
        (self.workspace / "tracked.txt").write_text("from-head\n", encoding="utf-8")
        (self.workspace / "sub").mkdir()
        (self.workspace / "sub" / "code.txt").write_text("code\n", encoding="utf-8")
        git(self.workspace, "add", ".")
        git(self.workspace, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "initial")
        self.manager = self._manager()

    def tearDown(self) -> None:
        self.directories.cleanup()

    def _manager(self, **config_overrides) -> SessionManager:
        settings = Settings(workspace=self.workspace, home_dir=self.directories.home_dir, sandbox_enabled=False)
        return SessionManager(
            settings, AppConfig(**config_overrides), provider_factory=lambda current: ScriptedProvider([])
        )

    def _isolated_manager(self) -> SessionManager:
        return self._manager(worktree_foreground=True)

    async def test_worktree_starts_from_head_and_isolates_parallel_changes(self) -> None:
        (self.workspace / "tracked.txt").write_text("dirty-original\n", encoding="utf-8")
        (self.workspace / "untracked.txt").write_text("not-copied\n", encoding="utf-8")
        manager = self._isolated_manager()

        first = manager.create(self.workspace)
        second = manager.create(self.workspace)
        first_path = Path(first.session.workspace)
        second_path = Path(second.session.workspace)

        self.assertNotEqual(first_path, self.workspace)
        self.assertNotEqual(first_path, second_path)
        self.assertEqual((first_path / "tracked.txt").read_text(encoding="utf-8"), "from-head\n")
        self.assertFalse((first_path / "untracked.txt").exists())
        (first_path / "tracked.txt").write_text("first-change\n", encoding="utf-8")
        (second_path / "tracked.txt").write_text("second-change\n", encoding="utf-8")
        self.assertEqual((self.workspace / "tracked.txt").read_text(encoding="utf-8"), "dirty-original\n")
        self.assertEqual((second_path / "tracked.txt").read_text(encoding="utf-8"), "second-change\n")
        self.assertTrue(first.session.worktree["branch"].startswith("pocketagent/"))

    async def test_foreground_edits_in_place_by_default(self) -> None:
        created = self._manager().create(self.workspace)

        self.assertIsNone(created.session.worktree)
        self.assertIsNone(created.session.worktree_fallback)
        self.assertEqual(Path(created.session.workspace), self.workspace)

    async def test_session_restore_and_delete_preserve_worktree(self) -> None:
        created = self._isolated_manager().create(self.workspace)
        session_id = created.id
        worktree_path = Path(created.session.workspace)
        branch = created.session.worktree["branch"]
        self.manager.store.save(created.session)
        replacement = self._manager()

        restored = replacement.get(session_id)
        await replacement.delete_task(session_id)

        self.assertEqual(restored.session.worktree["origin"], str(self.workspace))
        self.assertEqual(restored.settings.memory_dir, replacement.base_settings.memory_dir)
        self.assertTrue(worktree_path.is_dir())
        self.assertEqual(git(self.workspace, "show-ref", "--verify", f"refs/heads/{branch}").returncode, 0)

    async def test_subdirectory_workspace_keeps_same_relative_root(self) -> None:
        created = self._isolated_manager().create(self.workspace / "sub")

        self.assertEqual(Path(created.session.workspace).name, "sub")
        self.assertTrue((Path(created.session.workspace) / "code.txt").is_file())
        self.assertFalse((Path(created.session.workspace) / "tracked.txt").exists())

    async def test_file_browser_accepts_saved_worktree_without_registering_it(self) -> None:
        manager = self._isolated_manager()
        created = manager.create(self.workspace)
        manager.store.save(created.session)

        self.assertIn(created.session.workspace, manager.file_workspaces())
        self.assertNotIn(created.session.workspace, manager.app_config.workspaces)

    async def test_failed_isolation_falls_back_to_editing_in_place(self) -> None:
        non_git = self.workspace.parent / "not-git"
        non_git.mkdir()
        empty = self.workspace.parent / "empty"
        empty.mkdir()
        git(empty, "init", "-q")
        manager = self._isolated_manager()

        plain = manager.create(non_git)
        no_head = manager.create(empty)
        scratch = manager.create(ephemeral=True)

        self.assertIsNone(plain.session.worktree)
        self.assertIn("只支持普通 git 仓库", plain.session.worktree_fallback)
        self.assertEqual(Path(plain.session.workspace), non_git.resolve())
        self.assertIsNone(no_head.session.worktree)
        self.assertIn("还没有提交", no_head.session.worktree_fallback)
        # 临时任务本来就不隔离，也不算失败
        self.assertIsNone(scratch.session.worktree)
        self.assertIsNone(scratch.session.worktree_fallback)

    def test_git_metadata_points_to_real_worktree_and_common_repository(self) -> None:
        info = create_worktree(self.workspace, self.directories.home_dir, "test-session")

        self.assertTrue(Path(info.git_dirs[0], "index").is_file())
        self.assertTrue(Path(info.git_dirs[-1], "objects").is_dir())
        self.assertEqual(info.base_commit, git(self.workspace, "rev-parse", "HEAD").stdout.strip())


@unittest.skipUnless(available_sandbox() == "seatbelt", "只在有 sandbox-exec 的 macOS 上测")
class WorktreeSandboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directories = TemporaryDirectories()
        self.workspace = self.directories.workspace
        git(self.workspace, "init", "-q")
        (self.workspace / "a.txt").write_text("base\n", encoding="utf-8")
        git(self.workspace, "add", "a.txt")
        git(self.workspace, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "initial")
        self.info = create_worktree(self.workspace, self.directories.home_dir, "sandbox-session")

    def tearDown(self) -> None:
        self.directories.cleanup()

    def run_sandboxed(self, command: str) -> subprocess.CompletedProcess:
        policy = SandboxPolicy(
            workspace=Path(self.info.path),
            secret_dirs=[self.directories.home_dir],
            worktree_git_dirs=[Path(path) for path in self.info.git_dirs],
        )
        prepared = wrap_command(command, policy)
        return subprocess.run(prepared.command_line, cwd=self.info.path, capture_output=True, text=True, timeout=30)

    def test_git_commit_works_but_hooks_and_config_stay_protected(self) -> None:
        commit = self.run_sandboxed(
            "echo change > a.txt && git add a.txt && git -c user.name=t -c user.email=t@t commit -qm change"
        )
        common_dir = Path(self.info.git_dirs[-1])
        hook = self.run_sandboxed(f"echo bad > {common_dir / 'hooks' / 'pre-commit'}")
        config = self.run_sandboxed(f"echo bad >> {common_dir / 'config'}")

        self.assertEqual(commit.returncode, 0, commit.stderr)
        self.assertNotEqual(hook.returncode, 0)
        self.assertNotEqual(config.returncode, 0)
