"""检查点：用一个藏在 ~/.pocketagent 里的「影子 git 仓库」给工作区拍快照，变更列表、diff、撤销都基于它。

为什么不用之前「改文件前备份原文件」的做法：那只看得到 edit_file / write_file 的改动，
bash 改的、新建的、删的都看不见；撤销时还会拿「第一次见到时的样子」盖掉中间别的改动——撤销会骗人。
影子仓库每次把整个工作区拍一张，所有改动都看得到；撤销就是把文件恢复成某张快照里的样子。

- 仓库在 ~/.pocketagent/shadow/<工作区路径的哈希>/：GIT_DIR 指向它，GIT_WORK_TREE 指向工作区，不碰工作区自己的 .git
- 不读用户的 git 全局 / 系统配置（免得触发 lfs、fsmonitor 之类），关掉 hooks 和签名
- 工作区的 .gitignore 照常生效；node_modules、.venv 这类默认排除（写在影子仓库的 info/exclude）
- 每个任务用自己的 index 文件，同时拍快照不抢锁；同一个影子仓库的写操作用 asyncio.Lock 串起来
- 拍一次超过 30 秒（工作区太大）就把这个任务的检查点关掉，不卡对话
"""

import asyncio
import hashlib
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SNAPSHOT_TIMEOUT_SECONDS = 30
DEFAULT_EXCLUDES = [
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "*.pyc",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".DS_Store",
    ".next/",
    ".gradle/",
    "target/",
    "dist/",
    "build/",
    "*.egg-info/",
]
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_REPO_LOCKS: dict[Path, asyncio.Lock] = {}


class CheckpointError(RuntimeError):
    pass


@dataclass
class Checkpoint:
    commit: str
    label: str
    created_at: str
    # baseline 任务开始 / turn 每轮开始前 / safety 撤销、恢复之前自动拍的
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FileChange:
    path: str
    # added / modified / deleted
    status: str
    additions: int = 0
    deletions: int = 0
    binary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def git_available() -> bool:
    return shutil.which("git") is not None


class ShadowRepo:
    """一个工作区对应一个影子仓库；session_id 决定用哪个 index 文件、提交挂在哪个 ref 上。"""

    def __init__(self, shadow_root: Path, workspace: Path, session_id: str) -> None:
        self.workspace = workspace.resolve()
        digest = hashlib.sha1(str(self.workspace).encode("utf-8")).hexdigest()[:16]
        self.git_dir = shadow_root / digest
        self.session_id = session_id
        self.index_file = self.git_dir / f"index-{session_id}"
        self.ref = f"refs/pocketagent/{session_id}"

    def _lock(self) -> asyncio.Lock:
        return _REPO_LOCKS.setdefault(self.git_dir, asyncio.Lock())

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.update(
            {
                "GIT_DIR": str(self.git_dir),
                "GIT_WORK_TREE": str(self.workspace),
                "GIT_INDEX_FILE": str(self.index_file),
                # 不读用户的全局 / 系统配置
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_AUTHOR_NAME": "PocketAgent",
                "GIT_AUTHOR_EMAIL": "pocketagent@localhost",
                "GIT_COMMITTER_NAME": "PocketAgent",
                "GIT_COMMITTER_EMAIL": "pocketagent@localhost",
            }
        )
        return environment

    async def _git(self, *arguments: str, timeout: float = SNAPSHOT_TIMEOUT_SECONDS, check: bool = True) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "commit.gpgsign=false",
            "-c", "core.autocrlf=false",
            "-c", "core.quotepath=false",
            *arguments,
            cwd=self.workspace,
            env=self._environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise CheckpointError(f"git {arguments[0]} 超过 {timeout:.0f} 秒（工作区可能太大）")
        if check and process.returncode != 0:
            raise CheckpointError(f"git {' '.join(arguments[:2])} 失败：{stderr.decode('utf-8', 'replace').strip()[:300]}")
        return stdout.decode("utf-8", "replace")

    async def ensure_initialized(self) -> None:
        if (self.git_dir / "HEAD").is_file():
            return
        self.git_dir.mkdir(parents=True, exist_ok=True)
        await self._git("init", "--quiet")
        exclude = self.git_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join(DEFAULT_EXCLUDES) + "\n", encoding="utf-8")

    async def _current_tree(self) -> str:
        """把工作区现在的样子写进这个任务的 index，返回 tree 对象。调用方负责加锁。"""
        await self._git("add", "--all", "--", ".")
        return (await self._git("write-tree")).strip()

    async def snapshot(self, label: str) -> str:
        """拍一张快照，返回 commit；挂在这个任务的 ref 上，不会被 git 清理掉。"""
        await self.ensure_initialized()
        async with self._lock():
            tree = await self._current_tree()
            parent = (await self._git("rev-parse", "--verify", "--quiet", self.ref, check=False)).strip()
            arguments = ["commit-tree", tree, "-m", label]
            if parent:
                arguments += ["-p", parent]
            commit = (await self._git(*arguments)).strip()
            await self._git("update-ref", self.ref, commit)
            return commit

    async def changes_since(self, commit: str) -> list[FileChange]:
        """从某个快照到现在，工作区里所有变了的文件（不管是谁改的）。"""
        await self.ensure_initialized()
        async with self._lock():
            tree = await self._current_tree()
            numstat = await self._git("diff", "--no-renames", "--numstat", "-z", commit, tree)
            name_status = await self._git("diff", "--no-renames", "--name-status", "-z", commit, tree)
        statuses = _parse_name_status(name_status)
        changes = []
        for additions, deletions, path in _parse_numstat(numstat):
            binary = additions == "-"
            changes.append(
                FileChange(
                    path=path,
                    status=statuses.get(path, "modified"),
                    additions=0 if binary else int(additions),
                    deletions=0 if binary else int(deletions),
                    binary=binary,
                )
            )
        return changes

    async def diff(self, commit: str, path: str) -> str:
        await self.ensure_initialized()
        async with self._lock():
            tree = await self._current_tree()
            return await self._git("diff", "--no-renames", commit, tree, "--", path)

    async def _exists_in(self, commit: str, path: str) -> bool:
        return (await self._git("ls-tree", "--name-only", commit, "--", path)).strip() != ""

    async def restore_file(self, commit: str, path: str) -> None:
        """把一个文件恢复成快照里的样子；快照里没有这个文件就删掉。"""
        target = (self.workspace / path).resolve()
        if self.workspace not in target.parents:
            raise CheckpointError(f"路径不在工作区里：{path}")
        async with self._lock():
            if await self._exists_in(commit, path):
                await self._git("checkout", commit, "--", path)
            else:
                target.unlink(missing_ok=True)

    async def restore_all(self, commit: str) -> list[str]:
        """把整个工作区恢复成快照里的样子（只动有变化的文件），返回动过的路径。"""
        changes = await self.changes_since(commit)
        for change in changes:
            await self.restore_file(commit, change.path)
        return [change.path for change in changes]

    async def forget(self) -> None:
        """任务删掉时：去掉这个任务的 ref 和 index；共用的影子仓库留着给同一工作区的其他任务。"""
        if not (self.git_dir / "HEAD").is_file():
            return
        async with self._lock():
            await self._git("update-ref", "-d", self.ref, check=False)
        self.index_file.unlink(missing_ok=True)


def _parse_numstat(output: str) -> list[tuple[str, str, str]]:
    """git diff --numstat -z：每项是「新增\\t删除\\t路径」，用 \\0 分隔。"""
    rows = []
    for item in output.split("\0"):
        if not item:
            continue
        parts = item.split("\t", 2)
        if len(parts) == 3:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def _parse_name_status(output: str) -> dict[str, str]:
    """git diff --name-status -z：状态和路径交替出现。"""
    labels = {"A": "added", "M": "modified", "D": "deleted", "T": "modified"}
    tokens = [token for token in output.split("\0") if token]
    statuses = {}
    for index in range(0, len(tokens) - 1, 2):
        statuses[tokens[index + 1]] = labels.get(tokens[index][:1], "modified")
    return statuses


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


class TaskCheckpoints:
    """一个任务的检查点：记在会话里（Session.checkpoints），快照存在影子仓库里。"""

    def __init__(self, shadow_root: Path, workspace: Path, session: Any) -> None:
        self.session = session
        self.repo = ShadowRepo(shadow_root, workspace, session.id)
        self.enabled = git_available()
        self.disabled_reason = "" if self.enabled else "没找到 git，检查点关掉了"

    def all_checkpoints(self) -> list[Checkpoint]:
        return [Checkpoint(**item) for item in self.session.checkpoints]

    def baseline(self) -> Checkpoint | None:
        checkpoints = self.all_checkpoints()
        return checkpoints[0] if checkpoints else None

    async def create(self, label: str, kind: str) -> Checkpoint | None:
        """拍快照并记进会话；失败（比如工作区太大）就关掉检查点，不影响对话。"""
        if not self.enabled:
            return None
        try:
            commit = await self.repo.snapshot(label)
        except CheckpointError as error:
            self.enabled = False
            self.disabled_reason = f"检查点关掉了：{error}"
            return None
        checkpoint = Checkpoint(commit=commit, label=label, created_at=now_text(), kind=kind)
        self.session.checkpoints.append(checkpoint.to_dict())
        return checkpoint

    async def changes(self) -> list[FileChange]:
        baseline = self.baseline()
        if baseline is None or not self.enabled:
            return []
        return await self.repo.changes_since(baseline.commit)

    async def diff(self, path: str) -> str:
        baseline = self.baseline()
        if baseline is None:
            return ""
        return await self.repo.diff(baseline.commit, path) or "（没有变化）"

    async def revert_file(self, path: str) -> None:
        """把一个文件恢复成任务开始时的样子。先拍一张，撤销本身也能撤销。"""
        baseline = self.baseline()
        if baseline is None:
            raise CheckpointError("这个任务还没有检查点")
        await self.create(f"撤销 {path} 之前", kind="safety")
        await self.repo.restore_file(baseline.commit, path)

    async def restore(self, commit: str) -> list[str]:
        """把整个工作区恢复到某个检查点。先拍一张「恢复之前」，恢复本身也能撤销。"""
        if commit not in {checkpoint.commit for checkpoint in self.all_checkpoints()}:
            raise CheckpointError("不是这个任务的检查点")
        await self.create("恢复之前的样子", kind="safety")
        return await self.repo.restore_all(commit)
