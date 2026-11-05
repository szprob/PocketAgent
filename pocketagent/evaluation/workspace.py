"""回放 / 评测用的临时工作区：每次运行都在系统临时目录里放一份全新的副本，跑完删掉，绝不在用户的真实工作区里跑。

副本从哪来：
- trace 的起始检查点：影子仓库（~/.pocketagent/shadow）里「任务开始时」那张快照，原样取出来。
  嵌套的 git 仓库在快照里只是一个指针，内容在它自己的影子仓库里，会话记录里有它的提交号时一起取出来
- 一个目录（--workspace，或者评测用例的样例目录）：整个复制过来，但不复制 .git——
  worktree 的 .git 是指回原仓库的文件，在副本里跑 git 命令会改到原仓库的 index 和分支
"""

import asyncio
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pocketagent.safety.checkpoints import export_snapshot, find_snapshot, git_available


class WorkspaceError(RuntimeError):
    """准备不出工作区副本：检查点找不到、目录不存在等。消息直接给用户看。"""


class WorkspaceSource(Protocol):
    def describe(self) -> str: ...

    async def materialize(self, destination: Path) -> None: ...


@dataclass
class NestedSnapshot:
    relative_path: str
    git_dir: Path
    commit: str


@dataclass
class SnapshotSource:
    """影子仓库里的一张快照，外加嵌套仓库各自的快照。"""

    git_dir: Path
    commit: str
    nested: list[NestedSnapshot] = field(default_factory=list)
    # 找不到的嵌套仓库：副本里只是空目录，回放前提醒一句
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return f"trace 的起始检查点 {self.commit[:12]}（从影子仓库取出）"

    async def materialize(self, destination: Path) -> None:
        await export_snapshot(self.git_dir, self.commit, destination)
        for nested in self.nested:
            await export_snapshot(nested.git_dir, nested.commit, destination / nested.relative_path)


@dataclass
class DirectorySource:
    directory: Path
    label: str = "复制目录"
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return f"{self.label} {self.directory}"

    async def materialize(self, destination: Path) -> None:
        await asyncio.to_thread(copy_directory, self.directory, destination)


@dataclass
class EmptySource:
    """评测用例没给样例目录：从空目录开始。"""

    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return "空目录"

    async def materialize(self, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)


def copy_directory(source: Path, destination: Path) -> None:
    """符号链接原样保留不跟进去：链接指向的真实目录不会被复制，也不会被当成副本的一部分去改。"""
    shutil.copytree(source, destination, symlinks=True, ignore=shutil.ignore_patterns(".git"), dirs_exist_ok=True)


def directory_source(directory: Path, label: str = "复制目录") -> DirectorySource:
    resolved = directory.expanduser().resolve()
    if not resolved.is_dir():
        raise WorkspaceError(f"目录不存在：{directory}")
    return DirectorySource(directory=resolved, label=label)


async def snapshot_source(
    shadow_root: Path,
    commit: str,
    workspace: str | None,
    nested_commits: dict[str, str] | None = None,
) -> SnapshotSource | None:
    """在影子仓库里找 trace 的起始检查点；找不到返回 None，由调用方决定是报错还是改用目录。"""
    if not git_available():
        return None
    hints = [Path(workspace)] if workspace else []
    git_dir = await find_snapshot(shadow_root, commit, hints)
    if git_dir is None:
        return None
    source = SnapshotSource(git_dir=git_dir, commit=commit)
    for relative_path, nested_commit in sorted((nested_commits or {}).items()):
        if not _safe_relative(relative_path):
            continue
        nested_hints = [Path(workspace) / relative_path] if workspace else []
        nested_dir = await find_snapshot(shadow_root, nested_commit, nested_hints)
        if nested_dir is None:
            source.warnings.append(f"嵌套仓库 {relative_path} 的快照找不到了，副本里它是空目录")
            continue
        source.nested.append(NestedSnapshot(relative_path=relative_path, git_dir=nested_dir, commit=nested_commit))
    return source


def _safe_relative(relative_path: str) -> bool:
    """会话文件里的相对路径拼到副本下面之前先确认跳不出去。"""
    path = Path(relative_path)
    return bool(relative_path) and not path.is_absolute() and ".." not in path.parts


class TemporaryWorkspace:
    """with 块里是一个空的临时目录；keep=True 时跑完留着给人看（路径记进报告）。"""

    def __init__(self, prefix: str, keep: bool = False) -> None:
        self.prefix = prefix
        self.keep = keep
        self.path: Path | None = None

    def __enter__(self) -> Path:
        # macOS 的临时目录在 /var 下、真实路径在 /private/var：沙箱规则和影子仓库都按真实路径算
        self.path = Path(tempfile.mkdtemp(prefix=self.prefix)).resolve()
        return self.path

    def __exit__(self, *exc_info: object) -> None:
        if self.path is not None and not self.keep:
            shutil.rmtree(self.path, ignore_errors=True)
